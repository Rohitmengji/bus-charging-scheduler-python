"""
scheduler.py — Event-driven simulation engine for the Bus Charging Scheduler.

WHAT
────
A discrete-event simulation (DES) that takes a Scenario (route, buses, weights)
and produces a complete ScheduleResult (per-bus timelines + per-station queues).

WHY THIS APPROACH
─────────────────
• The problem is inherently event-based: buses ARRIVE at stations and COMPLETE
  charging at discrete points in time.  A DES processes only those moments,
  making it O(N log N) rather than polling every minute.
• It mirrors how a real-time dispatcher would work — you can't see the future,
  you react to events as they happen.
• It's trivially explainable: you can trace exactly why bus X got priority over
  bus Y at any specific moment.
• An LP/ILP alternative would require reformulating constraints for every new
  soft rule — here, adding a rule is just one new term in compute_priority().

HOW IT WORKS (high level)
─────────────────────────
1. PLAN: For each bus, greedily choose the minimum set of charging stations
   that keep the bus within its 240 km range at all times.
2. SEED: Push an ARRIVE event for each bus at its first planned station.
3. SIMULATE: Pop the earliest event from a min-heap.  Two event types:
   • ARRIVE  → If charger is free, start charging.  Otherwise, enqueue the bus.
   • CHARGE_COMPLETE → Advance bus to next stop (or destination).  Then serve
     the highest-priority waiting bus from the queue.
4. COLLECT: Once all events are processed, bundle results into ScheduleResult.

WHEN BUSES CONTEND FOR THE SAME CHARGER
────────────────────────────────────────
Priority is determined by compute_priority() which returns a SCORE (lower = higher
priority).  Crucially, scores are RE-COMPUTED at dispatch time (when a charger
becomes free) — not at arrival.  This ensures a bus that has waited 50 minutes
is correctly valued over a bus that waited 5 minutes.

EXTENDING THIS ENGINE
─────────────────────
• New soft rule  → add a weight field to Weights (models.py) + one term in
  compute_priority() below.
• New hard rule  → add logic in the simulation loop (e.g. minimum gap between
  charges).
• More chargers  → change "chargers": N in JSON; scheduler already handles lists.
• More stations  → add to route JSON; greedy planner adapts automatically.
"""

import heapq
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from models import (
    Bus, BusResult, ChargingStop, Route, Scenario,
    ScheduleResult, StationResult, StationSlot, Weights, World,
)


# ═══════════════════════════════════════════════════════════════════════════════
# EVENT TYPES
# ═══════════════════════════════════════════════════════════════════════════════
# Only two events drive the entire simulation.  Adding a third (e.g. BREAKDOWN)
# would require only a new elif branch in the main loop.

ARRIVE = "ARRIVE"              # Bus physically reaches a charging station
CHARGE_COMPLETE = "CHARGE_COMPLETE"  # Bus finishes charging → ready to depart


@dataclass(order=True)
class Event:
    """
    A single simulation event, comparable by (time, seq) for the min-heap.

    WHY seq:  Two buses may arrive at the exact same minute.  Without a
    tie-breaker, Python's dataclass comparison would try to compare `kind`
    (a string), which is non-deterministic.  `seq` guarantees FIFO within
    the same timestamp.

    Fields marked compare=False are excluded from __lt__ / __eq__ so that
    heapq only uses (time, seq) for ordering.
    """
    time: float                # WHEN this event fires (minutes from midnight)
    seq: int                   # Tie-breaker: monotonically increasing counter
    kind: str = field(compare=False)        # ARRIVE or CHARGE_COMPLETE
    bus_id: str = field(compare=False)      # Which bus this event concerns
    station_id: str = field(compare=False)  # Which station this event occurs at


# ═══════════════════════════════════════════════════════════════════════════════
# MUTABLE PER-BUS STATE (internal to the simulation)
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class BusState:
    """
    Tracks a bus's evolving state as the simulation advances.

    WHY mutable:  Unlike Bus (immutable input), this accumulates wait times,
    records charging stops, and tracks remaining battery as time passes.
    One BusState exists per bus and is mutated in-place by the event loop.

    Fields:
        bus             — Reference to the immutable Bus input data.
        range_remaining — km of charge left at the current simulation time.
                          Decremented after each travel segment, reset to
                          battery_range_km after each charge.
        current_time    — The bus's local clock (minutes from midnight).
                          Advances on travel and charging.
        total_wait      — Accumulated minutes spent waiting in queues.
                          Used by compute_priority() for fairness scoring.
        charging_stops  — Grows as the bus completes each charging session.
                          Eventually copied into BusResult.
        next_stop_idx   — (Reserved) Index into the planned station list;
                          currently derived from len(charging_stops) instead.
    """
    bus: Bus
    range_remaining: float
    current_time: float
    total_wait: float = 0.0
    charging_stops: List[ChargingStop] = field(default_factory=list)
    next_stop_idx: int = 0


# ═══════════════════════════════════════════════════════════════════════════════
# PURE HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════


def travel_time(distance_km: float, speed_kmh: float) -> float:
    """
    Convert distance to travel time in minutes.

    Formula: time_min = (distance_km / speed_kmh) * 60
    Example: 100 km at 60 km/h = 100 minutes.
    """
    return (distance_km / speed_kmh) * 60.0


def parse_time(hhmm: str) -> int:
    """Convert 'HH:MM' string → minutes from midnight (e.g. '19:00' → 1140)."""
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def stations_for_direction(route: Route, direction: str) -> List[str]:
    """
    Return the list of intermediate charging stations in travel order.

    WHY: BK buses see stations as [A, B, C, D]; KB buses see [D, C, B, A].
    The scheduler uses this to build charging plans in the correct order.
    Terminal stops (Bengaluru, Kochi) are excluded — they are NOT scheduling
    stations (they have slow overnight chargers handled outside this system).
    """
    intermediate = route.stops[1:-1]  # Strip first (origin) and last (dest)
    return intermediate if direction == "BK" else list(reversed(intermediate))


def ordered_segments(route: Route, direction: str) -> List[Tuple[str, str, float]]:
    """
    Return (from, to, distance_km) tuples in the bus's actual travel order.

    WHY: Route.segments are always stored in BK order.  For KB buses, we
    reverse the list AND swap from/to in each tuple so the scheduler can
    iterate "forward" regardless of direction.
    """
    segs = [(s.from_stop, s.to_stop, s.distance_km) for s in route.segments]
    if direction == "KB":
        # Reverse segment order AND flip from/to within each segment
        segs = [(t, f, d) for f, t, d in reversed(segs)]
    return segs


# ═══════════════════════════════════════════════════════════════════════════════
# CHARGING PLAN BUILDER (Phase 1 of the algorithm)
# ═══════════════════════════════════════════════════════════════════════════════


def build_charging_plan(bus: Bus, world: World, route: Route) -> List[str]:
    """
    Determine WHICH stations a bus must charge at (but not WHEN — that's
    determined by the simulation).

    ALGORITHM: Greedy "go as far as possible before charging."
    ─────────────────────────────────────────────────────────
    Walk segments in travel order.  At each intermediate station, check:
    "Can I reach the NEXT stop without charging?"  If no → charge here.

    WHY GREEDY:
    • Minimises total number of stops (proven optimal for uniform-cost stops).
    • Simple, O(segments) per bus, and deterministic.
    • The simulation handles contention (queuing) dynamically — we only need
      to guarantee physical feasibility here.

    GUARANTEE: The returned plan always satisfies:
      distance(origin → first_station) ≤ battery_range_km
      distance(station_i → station_i+1) ≤ battery_range_km
      distance(last_station → destination) ≤ battery_range_km

    EDGE CASE: If the greedy pass produces fewer than 2 stops (shouldn't happen
    for 540 km / 240 km range, but defensive), we force-add the earliest
    feasible stations to ensure route completion.
    """
    segs = ordered_segments(route, bus.direction)
    station_set = {s for s in route.stops[1:-1]}  # Only intermediate stations

    plan: List[str] = []
    range_left = world.battery_range_km

    for i, (frm, to, dist) in enumerate(segs):
        # Sanity check: a single segment longer than battery range is physically
        # impossible — no charging plan can save this bus.
        if dist > world.battery_range_km:
            raise ValueError(
                f"Segment {frm}→{to} ({dist} km) exceeds max range "
                f"({world.battery_range_km} km) — physically impossible."
            )

        range_left -= dist

        # Decision point: we've arrived at `to`.  If `to` is a station,
        # check whether we NEED to charge here.
        if to in station_set:
            # Look ahead: what's the distance to the NEXT stop after this one?
            if i + 1 < len(segs):
                next_dist = segs[i + 1][2]
            else:
                next_dist = 0  # `to` is the last station before destination

            # Charge here if:
            # (a) we can't reach the next stop, OR
            # (b) we've already gone negative (shouldn't happen, but defensive)
            if range_left < next_dist or range_left < 0:
                plan.append(to)
                range_left = world.battery_range_km  # Battery refilled to 100%

    # Safety net: with 540 km total and 240 km range, minimum 2 stops are
    # required.  If greedy somehow found fewer, force-fill.
    if len(plan) < 2:
        stations_in_order = stations_for_direction(route, bus.direction)
        if stations_in_order[0] not in plan:
            plan.insert(0, stations_in_order[0])
        if len(plan) < 2 and len(stations_in_order) > 1:
            for s in stations_in_order[1:]:
                if s not in plan:
                    plan.append(s)
                    break

    return plan


# ═══════════════════════════════════════════════════════════════════════════════
# DISTANCE CALCULATOR
# ═══════════════════════════════════════════════════════════════════════════════


def segment_distance(route: Route, direction: str, from_stop: str, to_stop: str) -> float:
    """
    Compute total road distance between any two stops (may span multiple segments).

    HOW: Walk segments in travel order.  Start summing when we hit `from_stop`,
    stop when we reach `to_stop`.

    EXAMPLE: segment_distance(route, "BK", "A", "C") = 120 + 100 = 220 km
    """
    segs = ordered_segments(route, direction)
    total = 0.0
    counting = False
    for frm, to, dist in segs:
        if frm == from_stop:
            counting = True
        if counting:
            total += dist
        if to == to_stop and counting:
            break
    return total


# ═══════════════════════════════════════════════════════════════════════════════
# PRIORITY SCORING FUNCTION (the "brain" of the scheduler)
# ═══════════════════════════════════════════════════════════════════════════════


def compute_priority(
    state: BusState,
    weights: Weights,
    operator_avg_wait: Dict[str, float],
) -> float:
    """
    Compute a priority SCORE for a bus waiting in a station queue.

    LOWER score = HIGHER priority (served first from the queue).

    TERMS (each controlled by a weight from the scenario JSON):
    ─────────────────────────────────────────────────────────────
    1. INDIVIDUAL (- weights.individual × bus_total_wait)
       → Buses that have already waited a lot get a lower (better) score.
       → Prevents starvation: no bus waits forever.

    2. OPERATOR   (- weights.operator × operator_fleet_avg_wait)
       → Operators whose fleet has a high average wait get a boost.
       → Ensures inter-operator fairness (minority operators aren't starved).

    3. OVERALL    (+ weights.overall × current_time)
       → Buses that arrived earlier get a lower score (smaller current_time).
       → Approximates FCFS: first-come, first-served as a baseline.

    HOW TO ADD A NEW RULE:
    ──────────────────────
    1. Add a weight field to the Weights dataclass (models.py).
    2. Add one term here.  That's it.

    Example — penalise long-travelling buses (fatigue proxy):
        - weights.fatigue * max(0, state.current_time - state.bus.departure_time_min - 240)

    WHEN CALLED:
    ─────────────
    Scores are computed (or re-computed) at DISPATCH TIME — i.e., when a charger
    becomes free and we pick the next bus from the queue.  This is critical:
    scoring at arrival time would freeze a bus's priority before it has waited,
    making the individual-fairness term useless.
    """
    op_wait = operator_avg_wait.get(state.bus.operator, 0.0)
    score = (
        - weights.individual * state.total_wait    # More wait → lower score → higher priority
        - weights.operator   * op_wait             # Fleet behind → lower score → boost
        + weights.overall    * state.current_time  # Earlier arrival → lower score → priority
    )
    return score


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN SIMULATION ENGINE
# ═══════════════════════════════════════════════════════════════════════════════


def run_scheduler(scenario: Scenario) -> ScheduleResult:
    """
    Execute the full event-driven simulation for one scenario.

    INPUT:  A Scenario object (world + route + stations + weights + buses).
    OUTPUT: A ScheduleResult (per-bus timelines + per-station queue logs).

    LIFECYCLE:
    1. Build charging plans for all buses (deterministic, no contention).
    2. Initialise station charger slots and per-bus mutable state.
    3. Seed the event heap with one ARRIVE event per bus.
    4. Process events until the heap is empty.
    5. Finalise any remaining buses and return results.
    """
    world = scenario.world
    route = scenario.route
    weights = scenario.weights

    # ── Phase 1: Build charging plans ─────────────────────────────────────────
    # Determines WHICH stations each bus will visit (but not timing/order).
    charging_plans: Dict[str, List[str]] = {}
    for bus in scenario.buses:
        charging_plans[bus.id] = build_charging_plan(bus, world, route)

    # ── Phase 2: Initialise simulation state ──────────────────────────────────

    # Station charger availability: station_chargers[station_id] = list of
    # floats, each representing when that charger slot becomes free.
    # Length = number of chargers at that station.
    station_chargers: Dict[str, List[float]] = {}
    for st in scenario.stations:
        station_chargers[st.id] = [0.0] * st.chargers

    # Waiting queue per station.  Each entry is (score, seq, BusState).
    # We re-score at dispatch time, so the initial score is just for ordering.
    waiting: Dict[str, List[Tuple[float, int, BusState]]] = defaultdict(list)

    # Per-bus mutable state (tracks position, battery, wait accumulation)
    states: Dict[str, BusState] = {}
    for bus in scenario.buses:
        states[bus.id] = BusState(
            bus=bus,
            range_remaining=world.battery_range_km,  # Full charge at departure
            current_time=float(bus.departure_time_min),
        )

    # Output accumulators — populated as buses complete their journeys
    bus_results: Dict[str, BusResult] = {}
    station_slots: Dict[str, List[StationSlot]] = defaultdict(list)

    # ── Phase 3: Event heap (min-heap ordered by time, then seq) ──────────────
    heap: List[Event] = []
    seq_counter = 0

    def push_event(time: float, kind: str, bus_id: str, station_id: str):
        """Push an event onto the min-heap with a unique sequence number."""
        nonlocal seq_counter
        heapq.heappush(heap, Event(time=time, seq=seq_counter, kind=kind,
                                   bus_id=bus_id, station_id=station_id))
        seq_counter += 1

    # Seed: each bus departs its origin and travels to its first charging station.
    for bus in scenario.buses:
        state = states[bus.id]
        plan = charging_plans[bus.id]
        if plan:
            first_station = plan[0]
            # Calculate distance from origin terminal to first charging station
            origin = route.stops[0] if bus.direction == "BK" else route.stops[-1]
            dist = segment_distance(route, bus.direction, origin, first_station)
            arrive_time = state.current_time + travel_time(dist, world.speed_kmh)
            state.range_remaining -= dist  # Battery consumed during travel
            push_event(arrive_time, ARRIVE, bus.id, first_station)
        else:
            # Edge case: bus can complete trip without any charging (impossible
            # for 540km/240km, but handled defensively for other route configs).
            destination = route.stops[-1] if bus.direction == "BK" else route.stops[0]
            total_dist = sum(s.distance_km for s in route.segments)
            arrive_time = state.current_time + travel_time(total_dist, world.speed_kmh)
            _finalise_bus(bus, state, arrive_time, bus_results)

    # ── Inner helper: operator fairness snapshot ──────────────────────────────

    def get_operator_avg_wait() -> Dict[str, float]:
        """
        Compute current average wait per operator from all completed charging
        stops so far.  Used by compute_priority() for the operator-fairness term.

        WHY snapshot:  This is called at dispatch time so it reflects the latest
        state.  An operator whose first 3 buses waited 20 min each will have
        avg_wait=20, giving their 4th bus a priority boost.
        """
        op_waits: Dict[str, List[float]] = defaultdict(list)
        for bstate in states.values():
            for stop in bstate.charging_stops:
                op_waits[bstate.bus.operator].append(stop.wait_minutes)
        return {op: (sum(ws) / len(ws)) for op, ws in op_waits.items() if ws}

    # ── Inner helper: find the earliest-available charger at a station ────────

    def next_free_charger(station_id: str) -> Tuple[int, float]:
        """
        Return (slot_index, free_at_time) for the charger that becomes free
        soonest.  For single-charger stations, always returns (0, free_at).
        For multi-charger stations, picks the one with the lowest free_at time.
        """
        slots = station_chargers[station_id]
        idx = min(range(len(slots)), key=lambda i: slots[i])
        return idx, slots[idx]

    # ── Inner helper: serve the next bus from a station's queue ────────────────

    def dispatch_from_queue(station_id: str, now: float):
        """
        Called when a charger becomes free (after CHARGE_COMPLETE).
        If buses are waiting, re-score ALL of them with current data,
        pick the best (lowest score), start charging it, and schedule
        its CHARGE_COMPLETE event.

        WHY re-score:  A bus that arrived 30 min ago now has 30 min of
        accumulated wait that wasn't reflected in its original score.
        Re-scoring ensures temporal fairness.

        WHY pick-best (not FIFO):  The weighted priority function lets
        operators tune fairness vs throughput vs starvation prevention.
        """
        if not waiting[station_id]:
            return
        slot_idx, free_at = next_free_charger(station_id)
        if free_at > now:
            return  # No charger free yet — will re-trigger on next CHARGE_COMPLETE

        # Re-score all waiting buses with fresh operator averages
        op_avg = get_operator_avg_wait()
        rescored = [
            (compute_priority(bstate, weights, op_avg), sq, bstate)
            for _, sq, bstate in waiting[station_id]
        ]
        # Sort: lowest score = highest priority.  seq breaks ties (FIFO).
        rescored.sort(key=lambda x: (x[0], x[1]))
        best_score, best_seq, best_state = rescored[0]
        waiting[station_id] = [(s, q, bs) for s, q, bs in rescored[1:]]

        # Start charging the selected bus
        start_charge = free_at  # Charger is already free at this time
        end_charge = start_charge + world.charge_time_min
        wait = start_charge - best_state.current_time  # Time spent in queue
        best_state.total_wait += wait

        # Record this charging session (bus perspective)
        stop = ChargingStop(
            station=station_id,
            arrival_time_min=best_state.current_time,
            wait_minutes=wait,
            charge_start_min=start_charge,
            charge_end_min=end_charge,
            range_remaining_on_arrival_km=best_state.range_remaining,
        )
        best_state.charging_stops.append(stop)

        # Record this charging session (station perspective)
        station_slots[station_id].append(StationSlot(
            bus_id=best_state.bus.id,
            operator=best_state.bus.operator,
            start_min=start_charge,
            end_min=end_charge,
            wait_minutes=wait,
            arrival_time_min=best_state.current_time,
        ))

        # Update charger slot availability and bus state
        station_chargers[station_id][slot_idx] = end_charge
        best_state.range_remaining = world.battery_range_km  # Battery full
        best_state.current_time = end_charge

        # Schedule the completion event for this charging session
        push_event(end_charge, CHARGE_COMPLETE, best_state.bus.id, station_id)

    # ── Phase 4: Main simulation loop ─────────────────────────────────────────
    # Process events in chronological order until none remain.
    while heap:
        event = heapq.heappop(heap)
        bus_id = event.bus_id
        station_id = event.station_id
        now = event.time
        state = states[bus_id]
        bus = state.bus

        if event.kind == ARRIVE:
            # ── Bus arrives at a charging station ─────────────────────────────
            state.current_time = now
            slot_idx, free_at = next_free_charger(station_id)

            if free_at <= now:
                # HAPPY PATH: Charger is available — start charging immediately
                start_charge = now
                end_charge = start_charge + world.charge_time_min

                # Record: no wait, immediate charge
                stop = ChargingStop(
                    station=station_id,
                    arrival_time_min=now,
                    wait_minutes=0.0,
                    charge_start_min=start_charge,
                    charge_end_min=end_charge,
                    range_remaining_on_arrival_km=state.range_remaining,
                )
                state.charging_stops.append(stop)
                station_slots[station_id].append(StationSlot(
                    bus_id=bus_id,
                    operator=bus.operator,
                    start_min=start_charge,
                    end_min=end_charge,
                    wait_minutes=0.0,
                    arrival_time_min=now,
                ))

                # Update state: battery full, time advanced to charge end
                station_chargers[station_id][slot_idx] = end_charge
                state.range_remaining = world.battery_range_km
                state.current_time = end_charge

                push_event(end_charge, CHARGE_COMPLETE, bus_id, station_id)
            else:
                # CONTENTION: Charger is busy — enter the waiting queue.
                # Score now (will be re-scored at dispatch for accuracy).
                op_avg = get_operator_avg_wait()
                score = compute_priority(state, weights, op_avg)
                waiting[station_id].append((score, seq_counter, state))
                seq_counter += 1
                # The currently-charging bus's CHARGE_COMPLETE event will
                # trigger dispatch_from_queue(), which will serve this bus.

        elif event.kind == CHARGE_COMPLETE:
            # ── Bus finishes charging — what's next? ──────────────────────────
            plan = charging_plans[bus_id]
            current_stop_idx = len(state.charging_stops) - 1

            if current_stop_idx + 1 < len(plan):
                # MORE STATIONS TO VISIT: Travel to the next planned station
                next_station = plan[current_stop_idx + 1]
                prev_station = plan[current_stop_idx]
                dist = segment_distance(route, bus.direction, prev_station, next_station)
                state.range_remaining -= dist  # Consume battery
                arrive_time = now + travel_time(dist, world.speed_kmh)
                state.current_time = arrive_time
                push_event(arrive_time, ARRIVE, bus_id, next_station)
            else:
                # LAST STATION DONE: Travel to final destination
                last_station = plan[-1]
                destination = route.stops[-1] if bus.direction == "BK" else route.stops[0]
                dist = segment_distance(route, bus.direction, last_station, destination)
                arrival_time = now + travel_time(dist, world.speed_kmh)
                _finalise_bus(bus, state, arrival_time, bus_results)

            # After this bus is done charging, serve the next waiting bus (if any)
            dispatch_from_queue(station_id, now)

    # ── Phase 5: Finalise stragglers ──────────────────────────────────────────
    # Edge case: buses with no charging plan that were finalised inline during
    # seeding.  Also catches any bus that somehow wasn't processed (defensive).
    for bus in scenario.buses:
        if bus.id not in bus_results:
            state = states[bus.id]
            total_dist = sum(s.distance_km for s in route.segments)
            arrival = float(bus.departure_time_min) + travel_time(total_dist, world.speed_kmh)
            _finalise_bus(bus, state, arrival, bus_results)

    # ── Phase 6: Build station results (sorted by charge start time) ──────────
    station_results = []
    for st in scenario.stations:
        slots = sorted(station_slots.get(st.id, []), key=lambda s: s.start_min)
        station_results.append(StationResult(station_id=st.id, charging_order=slots))

    return ScheduleResult(
        buses=list(bus_results.values()),
        stations=station_results,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# HELPER: Finalise a bus's journey into a BusResult
# ═══════════════════════════════════════════════════════════════════════════════


def _finalise_bus(bus: Bus, state: BusState, arrival_time: float,
                  bus_results: Dict[str, BusResult]):
    """
    Package a completed bus journey into an immutable BusResult.

    Called either when the bus reaches its destination after the last
    CHARGE_COMPLETE event, or during seeding if no charging is needed.
    """
    bus_results[bus.id] = BusResult(
        bus_id=bus.id,
        operator=bus.operator,
        direction=bus.direction,
        departure_time_min=float(bus.departure_time_min),
        charging_stops=state.charging_stops,
        total_wait_minutes=state.total_wait,
        arrival_time_min=arrival_time,
        trip_duration_minutes=arrival_time - bus.departure_time_min,
    )
