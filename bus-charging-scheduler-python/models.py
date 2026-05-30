"""
models.py — Domain data models for the Bus Charging Scheduler.

WHAT:   Plain dataclasses representing every entity in the simulation —
        physical world, route topology, buses, weights, and output records.

WHY:    Separating data definitions from logic (scheduler.py) and UI (app.py)
        means we can extend the domain model without touching business logic.
        Dataclasses give us free __init__, __repr__, and equality comparison
        which makes debugging and testing trivial.

HOW:    Each scenario JSON maps 1-to-1 to these classes via load_scenario()
        in app.py.  The scheduler consumes these as input and produces the
        Output types (below) as results.

WHEN:   These models are instantiated once per scenario load and are immutable
        during the simulation (scheduler.py uses its own mutable BusState).
"""

from dataclasses import dataclass, field
from typing import List, Optional


# ═══════════════════════════════════════════════════════════════════════════════
# INPUT TYPES — read from the scenario JSON file
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class World:
    """
    Physical constants governing the simulation universe.

    WHY here:  Centralising these in a data object (and in scenario JSON) means
    the scheduler never hardcodes 240 km or 25 min — every constant comes from
    the scenario file.  Tomorrow's buses with 320 km range?  Change JSON only.

    Fields:
        speed_kmh         — Uniform travel speed for all buses (km/h).
                            Determines all travel-time calculations.
        battery_range_km  — Max distance on a full charge (km).
                            The hard constraint: no inter-charge gap can exceed this.
        charge_time_min   — Time to charge from any level to full (minutes).
                            Currently fixed at 25 min for all stations.
    """
    speed_kmh: float
    battery_range_km: float
    charge_time_min: float


@dataclass
class Segment:
    """
    One road segment between two consecutive stops on the route.

    WHY explicit:  Rather than storing distances in a matrix, an ordered list
    of segments makes the route editable by non-engineers (add a row = add a
    station).  The scheduler iterates over these in travel-order.

    Fields:
        from_stop    — Name of the origin stop for this segment.
        to_stop      — Name of the destination stop for this segment.
        distance_km  — Road distance in km.  Drives both range consumption
                       and travel time (distance / speed).
    """
    from_stop: str
    to_stop: str
    distance_km: float


@dataclass
class Route:
    """
    Full route topology: ordered stop names + the segments connecting them.

    WHY both stops AND segments:  `stops` gives O(1) lookup for "what's the
    origin/destination?" and iteration order.  `segments` gives the distances.
    Together they support both BK and KB directions by simple reversal.

    Fields:
        stops    — Ordered list: ["Bengaluru", "A", "B", "C", "D", "Kochi"]
        segments — Ordered list of Segment objects (always stored BK-order;
                   scheduler reverses for KB direction at runtime).
    """
    stops: List[str]
    segments: List[Segment]


@dataclass
class Station:
    """
    A mid-route charging station (only A, B, C, D — not the terminals).

    WHY chargers field:  Even though today each station has 1 charger, the
    scheduler already initialises `station_chargers[id]` as a list of length
    `chargers`.  Changing this to 2 in JSON instantly enables parallel charging.

    Fields:
        id       — Station identifier (matches a stop name in Route.stops).
        chargers — Number of independent charger slots at this station.
                   Default 1.  Increase via JSON for capacity expansion.
    """
    id: str
    chargers: int = 1


@dataclass
class Bus:
    """
    A single bus entering the simulation with a known departure time.

    WHY direction as string:  "BK"/"KB" rather than a bool allows future
    multi-route systems (e.g. "BK-express", "coastal") without schema migration.

    Fields:
        id                 — Unique identifier (e.g. "bus-BK-01").
        operator           — Opaque string for the operating company.
                             Used only for priority scoring — no enum needed.
        direction          — "BK" = Bengaluru→Kochi, "KB" = Kochi→Bengaluru.
        departure_time_min — Scheduled departure in minutes from midnight.
                             e.g. 19:00 = 1140 minutes.
    """
    id: str
    operator: str
    direction: str
    departure_time_min: int


@dataclass
class Weights:
    """
    Tunable coefficients for the priority scoring function.

    WHAT:  Each weight scales one term in compute_priority() (scheduler.py).
    WHY:   Operators can tune the scheduling trade-off without code changes.
    HOW:   Set in the scenario JSON under "weights": { ... }.
    WHEN:  Read once at simulation start; applied at every dispatch decision.

    To add a new scheduling rule:
      1. Add a field here with a safe default (0.0).
      2. Add a matching term in scheduler.compute_priority().
      3. Set the weight in any scenario JSON that should use the rule.
      Scenarios that omit the key use the default → backwards compatible.

    Fields:
        individual — Controls starvation prevention.  Higher value = buses
                     that have already waited a lot get served sooner.
        operator   — Controls inter-operator fairness.  Higher value = operators
                     whose fleet average wait is high get a priority boost.
        overall    — Controls network throughput.  Higher value = earlier-arriving
                     buses get priority (closer to FCFS behaviour).
    """
    individual: float = 1.0
    operator: float = 1.0
    overall: float = 1.0


@dataclass
class ScenarioMeta:
    """
    Human-readable metadata for the scenario (displayed in the UI header).

    Fields:
        scenario_id  — Integer ID for ordering in the dropdown (1-based).
        name         — Short label (e.g. "Even Spacing").
        description  — One-sentence explanation shown below the dropdown.
    """
    scenario_id: int
    name: str
    description: str


@dataclass
class Scenario:
    """
    Top-level container: everything the scheduler needs to run one simulation.

    WHY one object:  Passing a single Scenario to run_scheduler() keeps the API
    clean and makes it trivial to add fields (just extend this dataclass and
    the JSON loader — the function signature never changes).
    """
    meta: ScenarioMeta
    world: World
    route: Route
    stations: List[Station]
    weights: Weights
    buses: List[Bus]


# ═══════════════════════════════════════════════════════════════════════════════
# OUTPUT TYPES — produced by run_scheduler(), consumed by the UI
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class ChargingStop:
    """
    Record of one bus's visit to one charging station.

    Created during simulation when a bus finishes charging (or immediately
    upon arrival if charger is free).  Stored in BusState.charging_stops
    and later copied into BusResult for the UI to display.

    Fields:
        station                       — Station ID where this charge occurred.
        arrival_time_min              — When the bus physically arrived (min from midnight).
        wait_minutes                  — How long it queued before charging started.
                                        0.0 if charger was free on arrival.
        charge_start_min              — When charging began (= arrival + wait).
        charge_end_min                — When charging completed (= start + charge_time).
        range_remaining_on_arrival_km — Battery level when arriving (diagnostic;
                                        lets the UI show how "close to empty" a bus was).
    """
    station: str
    arrival_time_min: float
    wait_minutes: float
    charge_start_min: float
    charge_end_min: float
    range_remaining_on_arrival_km: float


@dataclass
class BusResult:
    """
    Complete journey result for a single bus (scheduler output → UI input).

    Created once per bus at the end of its trip via _finalise_bus().
    Contains everything needed to render the per-bus timetable tab.

    Fields:
        bus_id               — Matches Bus.id for cross-referencing.
        operator             — Copied for display convenience (avoids lookups).
        direction            — "BK" or "KB".
        departure_time_min   — When the bus left its origin terminal.
        charging_stops       — Ordered list of every station visit (see ChargingStop).
        total_wait_minutes   — Sum of all wait_minutes across stops.
        arrival_time_min     — When the bus reached its destination terminal.
        trip_duration_minutes — arrival - departure (includes travel + charge + wait).
    """
    bus_id: str
    operator: str
    direction: str
    departure_time_min: float
    charging_stops: List[ChargingStop]
    total_wait_minutes: float
    arrival_time_min: float
    trip_duration_minutes: float


@dataclass
class StationSlot:
    """
    One charging session from a station's perspective (charger allocation log).

    WHY separate from ChargingStop:  ChargingStop is bus-centric; StationSlot is
    station-centric.  The UI's "per-station view" iterates StationSlots to show
    the queue order at each station.

    Fields:
        bus_id           — Which bus used this slot.
        operator         — For display/grouping in the station view.
        start_min        — When charging began at this slot.
        end_min          — When charging ended (start + charge_time_min).
        wait_minutes     — How long the bus waited before getting this slot.
        arrival_time_min — When the bus physically arrived (for queue context).
    """
    bus_id: str
    operator: str
    start_min: float
    end_min: float
    wait_minutes: float
    arrival_time_min: float


@dataclass
class StationResult:
    """
    All charging sessions at one station, sorted chronologically by start_min.
    Used directly by the per-station tab in the UI.
    """
    station_id: str
    charging_order: List[StationSlot]


@dataclass
class ScheduleResult:
    """
    Final output of run_scheduler() — contains all information the UI needs.

    Fields:
        buses    — One BusResult per bus in the scenario.
        stations — One StationResult per charging station (A, B, C, D).
    """
    buses: List[BusResult]
    stations: List[StationResult]
