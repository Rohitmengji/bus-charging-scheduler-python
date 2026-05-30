# Architecture: Bus Charging Scheduler

---

## 1. Scheduling Approach

### Why Event-Driven Simulation with a Priority Queue?

The problem is fundamentally a **discrete-event simulation (DES)**: buses arrive at stations at known future times, and each arrival potentially triggers future events (charge complete → depart → arrive at next station). A DES naturally handles this without polling or fixed time-steps.

**The algorithm in four steps:**

1. **Greedy charging plan per bus** — for each bus, compute the minimum set of stations to charge at using a greedy range check: travel as far as possible before charging. This guarantees physical feasibility (no segment exceeds battery range) while minimising total stops.

2. **Seed the event heap** — push one `ARRIVE` event per bus for its first charging stop.

3. **Process events in chronological order** using a min-heap (`heapq`). Two event types:
   - `ARRIVE(bus, station, time)` — bus physically reaches the station. If the charger is free, start charging. Otherwise, enqueue the bus.
   - `CHARGE_COMPLETE(bus, station, time)` — charging finishes. Advance bus to next stop or destination. Then call `dispatch_from_queue` to serve the next waiting bus.

4. **Priority scoring at dispatch time** — when multiple buses wait at the same station, re-score all of them at the moment a charger becomes free (not at arrival). This ensures stale scores don't penalise buses that have been waiting longer.

**Why not FCFS?** First-come-first-served ignores operator fairness and accumulated wait. The weighted score lets operators tune the trade-off explicitly.

**Why not a global optimiser (LP/ILP)?** The problem is online — in practice you don't know future arrivals. A simulation-based greedy approach is explainable, fast (O(N log N)), and matches how a real dispatcher would operate.

---

## 2. Data Structure Design

### Why JSON scenario files?

Each scenario is a **self-contained, declarative specification** of the entire simulation world. The scheduler has no hardcoded knowledge of distances, speeds, operators, or charger counts. This enables:

- **Zero code changes** to change physical parameters
- **Zero code changes** to add a new operator or station
- **Versioned scenarios** that can be diffed in git
- **Human-readable** — operators can inspect and edit scenarios without engineering

### Key design decisions

| Field | Why it exists |
|-------|---------------|
| `world.speed_kmh` | Not hardcoded — change once in JSON, affects all travel times |
| `world.battery_range_km` | Per-scenario; future buses may have different battery sizes |
| `stations[].chargers` | Integer ≥1; adding a second charger requires only a data change |
| `route.segments[]` | Explicit list — adding a new stop is an array append, not a code change |
| `bus.direction` | String, not boolean — supports multi-route future extensions |
| `bus.operator` | Plain string — no enum; add "megabus" with zero code change |
| `weights` | Object with named floats — add a new key = new rule with no migration |

---

## 3. Anticipated Future Changes

All of the following require **zero code changes** to the scheduler unless noted.

### Adding more chargers at a station
Change `"chargers": 1` to `"chargers": 2` in the station object. The scheduler already queries `station_chargers[id]` as a list of N slots.

### Adding a new intermediate station
Add the station to `route.stops` and two entries to `route.segments` (split the existing segment). Add `{"id": "E", "chargers": 1}` to `stations`. No code change.

### Changing segment distances
Edit `distance_km` in the relevant segment. All travel times and range calculations derive from this value at runtime.

### Adding a new operator
Add buses with `"operator": "megabus"` to the buses array. The scheduler treats operator as an opaque string — the fairness term averages wait times across all buses sharing the same string.

### Adding time-of-day electricity cost rule
1. Add `"time_of_day": 0.5` to the `weights` object in the scenario JSON
2. Add `time_of_day: float = 0.0` to the `Weights` dataclass
3. Add one term to `compute_priority()`:
   ```python
   + weights.time_of_day * peak_hour_penalty(state.current_time)
   ```
   where `peak_hour_penalty` is a small pure function. That's the entire change.

### Priority buses (VIP / emergency)
Add `"priority": true` to a bus object. Add `bus.priority: bool = False` to the `Bus` dataclass. Add a large negative term in `compute_priority()`:
```python
- (10000.0 if state.bus.priority else 0.0)
```
This effectively skips the queue.

### Multiple routes sharing stations
`Route` is already a first-class object on `Scenario`. Load two scenarios, run the scheduler once per route, and merge `station_slots` by station ID before building `StationResult`. The per-station output already aggregates all buses regardless of route.

### Driver shift constraints
Add `"shift_start": "18:00", "shift_end": "04:00"` to the bus object. In `compute_priority()`, add a penalty term if a charge would push the bus past `shift_end`. The scheduler already tracks `state.current_time` in minutes.

### More than 20 buses
The simulation is O(N log N) in the number of events. 200 buses would produce roughly 10× the events and complete in well under a second.

### Different battery sizes per bus
Add an optional `"battery_range_km": 320` override on individual bus objects. In `build_charging_plan()`, check `bus.battery_range_km` before falling back to `world.battery_range_km`. One additional field in the `Bus` dataclass; no scheduler logic changes.

---

## 4. How to Change a Weight

Open the scenario JSON and edit the `weights` block. Example — prioritise network throughput over individual fairness:

```json
"weights": {
  "individual": 0.5,
  "operator": 0.5,
  "overall": 2.0
}
```

Restart the app. The scheduler recalculates with the new coefficients on every run.

---

## 5. How to Add a New Rule

**Example: penalise buses that have been travelling for more than 4 hours (fatigue proxy)**

**Step 1** — Add weight to scenario JSON:
```json
"weights": {
  "individual": 1.0,
  "operator": 1.0,
  "overall": 1.0,
  "fatigue": 1.5
}
```

**Step 2** — Add field to `Weights` dataclass in `models.py`:
```python
@dataclass
class Weights:
    individual: float = 1.0
    operator:   float = 1.0
    overall:    float = 1.0
    fatigue:    float = 0.0   # new field with safe default
```

**Step 3** — Write the rule function in `scheduler.py`:
```python
def fatigue_score(state: BusState) -> float:
    """Return minutes over the 4-hour mark, or 0."""
    travel_time = state.current_time - state.bus.departure_time_min
    return max(0.0, travel_time - 240.0)
```

**Step 4** — Add the term to `compute_priority()`:
```python
def compute_priority(state, weights, operator_avg_wait):
    ...
    return (
        - weights.individual * state.total_wait
        - weights.operator   * op_wait
        + weights.overall    * state.current_time
        - weights.fatigue    * fatigue_score(state)   # ← new term
    )
```

That's the entire change. Scenarios that don't include `"fatigue"` in weights will use the default `0.0` and behave identically to before.

---

## 6. Assumptions

1. **All buses start with a full charge** (battery_range_km) at their departure terminal.
2. **Charging always refills to 100%** — partial charging is not modelled.
3. **Buses travel at constant speed** (`world.speed_kmh`) and do not stop except to charge.
4. **No backtracking** — a bus visits stations in strict route order. A BK bus will never go back to a previously passed station.
5. **Charger slots are interchangeable** — if a station has `chargers: 2`, any waiting bus can use whichever slot becomes free first.
6. **Time is simulated from midnight (minute 0)** — 19:00 departure = minute 1140.
7. **Priority scores are re-computed at dispatch time**, not at arrival. This prevents stale scores and ensures buses that waited longer are correctly rewarded.
8. **The route is identical for all buses in a scenario** — there is no express service skipping stations.
9. **There is no minimum dwell time** at a station other than the charging time itself.
10. **Departures are on the same calendar day** — no midnight wrap-around is handled beyond modular hour display.
