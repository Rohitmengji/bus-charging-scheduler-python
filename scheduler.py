"""
scheduler.py — Event-driven simulation engine for the Bus Charging Scheduler.

Algorithm overview
──────────────────
1. For each bus, compute the minimum ordered set of charging stations
   using a greedy range-check (go as far as possible before charging).
2. Run a discrete-event simulation driven by a min-heap of (time, event).
3. When multiple buses contend for the same charger, rank them with a
   weighted priority score (lower = served first).
4. Collect per-bus and per-station results and return a ScheduleResult.

Extending the priority function
────────────────────────────────
Add a new weight key to the scenario JSON, then add one term to
`compute_priority`.  Nothing else needs to change.
"""

import heapq
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from models import (
    Bus, BusResult, ChargingStop, Route, Scenario,
    ScheduleResult, StationResult, StationSlot, Weights, World,
)

# ── Event types ──────────────────────────────────────────────────────────────

ARRIVE = "ARRIVE"
CHARGE_COMPLETE = "CHARGE_COMPLETE"


@dataclass(order=True)
class Event:
    time: float
    seq: int           # tie-breaker so dataclass comparison never touches payload
    kind: str = field(compare=False)
    bus_id: str = field(compare=False)
    station_id: str = field(compare=False)


# ── Mutable per-bus simulation state ─────────────────────────────────────────

@dataclass
class BusState:
    bus: Bus
    range_remaining: float      # km of charge left right now
    current_time: float         # minutes from midnight
    total_wait: float = 0.0     # accumulated queue-wait so far
    charging_stops: List[ChargingStop] = field(default_factory=list)
    # index into the bus's planned charging-station list
    next_stop_idx: int = 0


# ── Helpers ───────────────────────────────────────────────────────────────────

def travel_time(distance_km: float, speed_kmh: float) -> float:
    """Return travel time in minutes."""
    return (distance_km / speed_kmh) * 60.0


def parse_time(hhmm: str) -> int:
    """Convert 'HH:MM' → minutes from midnight."""
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def stations_for_direction(route: Route, direction: str) -> List[str]:
    """
    Return the ordered list of intermediate stations a bus visits.
    BK buses go Bengaluru→Kochi; KB buses go Kochi→Bengaluru.
    """
    # Intermediate stops only (exclude origin/destination terminals)
    intermediate = route.stops[1:-1]
    return intermediate if direction == "BK" else list(reversed(intermediate))


def ordered_segments(route: Route, direction: str) -> List[Tuple[str, str, float]]:
    """
    Return (from, to, distance_km) tuples in travel order for a given direction.
    """
    segs = [(s.from_stop, s.to_stop, s.distance_km) for s in route.segments]
    if direction == "KB":
        segs = [(t, f, d) for f, t, d in reversed(segs)]
    return segs


def build_charging_plan(bus: Bus, world: World, route: Route) -> List[str]:
    """
    Greedy algorithm: travel as far as possible; charge only when the next
    leg would exceed remaining range.  Returns the ordered list of station
    IDs where this bus must charge.

    Guarantees: every inter-charge gap ≤ battery_range_km.
    """
    segs = ordered_segments(route, bus.direction)
    station_set = {s for s in route.stops[1:-1]}  # only intermediate stations

    plan: List[str] = []
    range_left = world.battery_range_km

    for i, (frm, to, dist) in enumerate(segs):
        if dist > world.battery_range_km:
            raise ValueError(
                f"Segment {frm}→{to} ({dist} km) exceeds max range "
                f"({world.battery_range_km} km) — physically impossible."
            )
        range_left -= dist
        # Look ahead: if we're at a station and can't reach the NEXT station
        # (or destination) without running out, charge here.
        if to in station_set:
            # find the distance to the stop after `to`
            if i + 1 < len(segs):
                next_dist = segs[i + 1][2]
            else:
                next_dist = 0  # destination — no further travel needed

            if range_left < next_dist or range_left < 0:
                plan.append(to)
                range_left = world.battery_range_km  # fully charged

    # Validate: must charge ≥ 2 times (540 km > 240 km range)
    if len(plan) < 2:
        # Force a minimum-2-stop plan by inserting the earliest feasible station
        stations_in_order = stations_for_direction(route, bus.direction)
        # Add first station if not already there
        if stations_in_order[0] not in plan:
            plan.insert(0, stations_in_order[0])
        # Add second station if needed
        if len(plan) < 2 and len(stations_in_order) > 1:
            for s in stations_in_order[1:]:
                if s not in plan:
                    plan.append(s)
                    break

    return plan


def segment_distance(route: Route, direction: str, from_stop: str, to_stop: str) -> float:
    """Return total km between two stops (possibly spanning multiple segments)."""
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


def compute_priority(
    state: BusState,
    weights: Weights,
    operator_avg_wait: Dict[str, float],
) -> float:
    """
    Lower score → higher priority in the charging queue.

    Terms:
      individual  — reward buses that have already waited a long time
                    (negative sign → more wait = lower score = higher priority)
      operator    — reward buses whose operator has a high average fleet wait
      overall     — penalise buses that arrived later (earlier arrival = priority)

    To add a new rule: add a weight key to Weights and a term here.
    """
    op_wait = operator_avg_wait.get(state.bus.operator, 0.0)
    score = (
        - weights.individual * state.total_wait          # more wait → lower score
        - weights.operator   * op_wait                   # higher op avg → lower score
        + weights.overall    * state.current_time        # earlier arrival → lower score
    )
    return score


# ── Main scheduler ────────────────────────────────────────────────────────────

def run_scheduler(scenario: Scenario) -> ScheduleResult:
    """
    Run the full event-driven simulation and return per-bus and per-station results.
    """
    world = scenario.world
    route = scenario.route
    weights = scenario.weights

    # ── Build charging plans ──────────────────────────────────────────────────
    charging_plans: Dict[str, List[str]] = {}
    for bus in scenario.buses:
        charging_plans[bus.id] = build_charging_plan(bus, world, route)

    # ── Station state: charger availability + waiting queues ─────────────────
    # charger_free_at[station] = earliest minute a charger is free
    charger_free_at: Dict[str, float] = defaultdict(float)
    # Each station has `chargers` charger slots; track per-slot availability
    station_chargers: Dict[str, List[float]] = {}
    for st in scenario.stations:
        station_chargers[st.id] = [0.0] * st.chargers

    # Waiting queue per station: list of (priority_score, seq, BusState)
    # We re-score at dispatch time for accuracy.
    waiting: Dict[str, List[Tuple[float, int, BusState]]] = defaultdict(list)

    # ── Per-bus simulation state ──────────────────────────────────────────────
    states: Dict[str, BusState] = {}
    for bus in scenario.buses:
        states[bus.id] = BusState(
            bus=bus,
            range_remaining=world.battery_range_km,
            current_time=float(bus.departure_time_min),
        )

    # ── Output accumulators ───────────────────────────────────────────────────
    bus_results: Dict[str, BusResult] = {}
    station_slots: Dict[str, List[StationSlot]] = defaultdict(list)

    # ── Event heap ────────────────────────────────────────────────────────────
    heap: List[Event] = []
    seq_counter = 0

    def push_event(time: float, kind: str, bus_id: str, station_id: str):
        nonlocal seq_counter
        heapq.heappush(heap, Event(time=time, seq=seq_counter, kind=kind,
                                   bus_id=bus_id, station_id=station_id))
        seq_counter += 1

    # Seed: each bus departs and heads for its first charging stop
    for bus in scenario.buses:
        state = states[bus.id]
        plan = charging_plans[bus.id]
        if plan:
            first_station = plan[0]
            # distance from origin to first charging station
            origin = route.stops[0] if bus.direction == "BK" else route.stops[-1]
            dist = segment_distance(route, bus.direction, origin, first_station)
            arrive_time = state.current_time + travel_time(dist, world.speed_kmh)
            # range consumed travelling to first station
            state.range_remaining -= dist
            push_event(arrive_time, ARRIVE, bus.id, first_station)
        else:
            # No charge needed — bus completes trip directly (edge case)
            destination = route.stops[-1] if bus.direction == "BK" else route.stops[0]
            origin = route.stops[0] if bus.direction == "BK" else route.stops[-1]
            total_dist = sum(s.distance_km for s in route.segments)
            arrive_time = state.current_time + travel_time(total_dist, world.speed_kmh)
            _finalise_bus(bus, state, arrive_time, bus_results)

    def get_operator_avg_wait() -> Dict[str, float]:
        """Snapshot of average wait per operator from completed stops so far."""
        op_waits: Dict[str, List[float]] = defaultdict(list)
        for bstate in states.values():
            for stop in bstate.charging_stops:
                op_waits[bstate.bus.operator].append(stop.wait_minutes)
        return {op: (sum(ws) / len(ws)) for op, ws in op_waits.items() if ws}

    def next_free_charger(station_id: str) -> Tuple[int, float]:
        """Return (slot_index, free_at_time) for the earliest-free charger slot."""
        slots = station_chargers[station_id]
        idx = min(range(len(slots)), key=lambda i: slots[i])
        return idx, slots[idx]

    def dispatch_from_queue(station_id: str, now: float):
        """
        If a waiting bus is queued and a charger is free, start charging.
        Re-score all waiting buses at dispatch time for accuracy.
        """
        if not waiting[station_id]:
            return
        slot_idx, free_at = next_free_charger(station_id)
        if free_at > now:
            return  # no charger free yet; the CHARGE_COMPLETE event will re-trigger

        op_avg = get_operator_avg_wait()
        # Re-score all waiting buses
        rescored = [
            (compute_priority(bstate, weights, op_avg), sq, bstate)
            for _, sq, bstate in waiting[station_id]
        ]
        rescored.sort(key=lambda x: (x[0], x[1]))
        best_score, best_seq, best_state = rescored[0]
        waiting[station_id] = [(s, q, bs) for s, q, bs in rescored[1:]]

        start_charge = free_at  # charger was already free
        end_charge = start_charge + world.charge_time_min
        wait = start_charge - best_state.current_time
        best_state.total_wait += wait

        # Record the charging stop
        stop = ChargingStop(
            station=station_id,
            arrival_time_min=best_state.current_time,
            wait_minutes=wait,
            charge_start_min=start_charge,
            charge_end_min=end_charge,
            range_remaining_on_arrival_km=best_state.range_remaining,
        )
        best_state.charging_stops.append(stop)
        station_slots[station_id].append(StationSlot(
            bus_id=best_state.bus.id,
            operator=best_state.bus.operator,
            start_min=start_charge,
            end_min=end_charge,
            wait_minutes=wait,
            arrival_time_min=best_state.current_time,
        ))
        station_chargers[station_id][slot_idx] = end_charge
        best_state.range_remaining = world.battery_range_km
        best_state.current_time = end_charge

        push_event(end_charge, CHARGE_COMPLETE, best_state.bus.id, station_id)

    # ── Simulation loop ───────────────────────────────────────────────────────
    while heap:
        event = heapq.heappop(heap)
        bus_id = event.bus_id
        station_id = event.station_id
        now = event.time
        state = states[bus_id]
        bus = state.bus

        if event.kind == ARRIVE:
            state.current_time = now
            slot_idx, free_at = next_free_charger(station_id)

            if free_at <= now:
                # Charger is free — start charging immediately
                start_charge = now
                end_charge = start_charge + world.charge_time_min
                wait = 0.0

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
                station_chargers[station_id][slot_idx] = end_charge
                state.range_remaining = world.battery_range_km
                state.current_time = end_charge

                push_event(end_charge, CHARGE_COMPLETE, bus_id, station_id)
            else:
                # Charger busy — join the queue
                op_avg = get_operator_avg_wait()
                score = compute_priority(state, weights, op_avg)
                waiting[station_id].append((score, seq_counter, state))
                seq_counter += 1
                # The CHARGE_COMPLETE event for whoever is currently charging
                # will call dispatch_from_queue.

        elif event.kind == CHARGE_COMPLETE:
            # Check if there is a next charging stop for this bus
            plan = charging_plans[bus_id]
            current_stop_idx = len(state.charging_stops) - 1
            if current_stop_idx + 1 < len(plan):
                # Travel to next charging station
                next_station = plan[current_stop_idx + 1]
                prev_station = plan[current_stop_idx]
                dist = segment_distance(route, bus.direction, prev_station, next_station)
                state.range_remaining -= dist
                arrive_time = now + travel_time(dist, world.speed_kmh)
                state.current_time = arrive_time
                push_event(arrive_time, ARRIVE, bus_id, next_station)
            else:
                # No more charging stops — travel to destination
                last_station = plan[-1]
                destination = route.stops[-1] if bus.direction == "BK" else route.stops[0]
                dist = segment_distance(route, bus.direction, last_station, destination)
                arrival_time = now + travel_time(dist, world.speed_kmh)
                _finalise_bus(bus, state, arrival_time, bus_results)

            # Serve next bus waiting at this station
            dispatch_from_queue(station_id, now)

    # ── Finalise any buses whose journey was completed inline ────────────────
    # (handles buses already finalised above; skip duplicates)
    for bus in scenario.buses:
        if bus.id not in bus_results:
            state = states[bus.id]
            # Bus had no charging stops — compute direct arrival
            total_dist = sum(s.distance_km for s in route.segments)
            arrival = float(bus.departure_time_min) + travel_time(total_dist, world.speed_kmh)
            _finalise_bus(bus, state, arrival, bus_results)

    # ── Build station results ─────────────────────────────────────────────────
    station_results = []
    for st in scenario.stations:
        slots = sorted(station_slots.get(st.id, []), key=lambda s: s.start_min)
        station_results.append(StationResult(station_id=st.id, charging_order=slots))

    return ScheduleResult(
        buses=list(bus_results.values()),
        stations=station_results,
    )


def _finalise_bus(bus, state: BusState, arrival_time: float,
                  bus_results: Dict[str, BusResult]):
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
