# Interview Preparation Guide

> This is your personal prep doc. Do NOT push this to GitHub.

---

## 1. Walk-Through Script (2-3 min demo)

### Opening (30 sec)
"I built an event-driven discrete simulation with a weighted priority queue. The scheduler reads a JSON scenario file that is fully self-describing — route, stations, charger count, weights, and buses — and produces per-bus timelines and per-station charging order."

### Demo Flow
1. Open app → point out the dropdown (auto-discovers scenario files)
2. Pick Scenario 1 (Even Spacing) → show all 3 tabs
3. Switch to Scenario 4 (Operator Heavy) → point out operator weight = 2.0
4. Show Per-Bus tab: "notice freshbus and flixbus get slightly less wait despite being minority operators — that's the operator fairness weight doing its job"
5. Show Per-Station tab: "you can see the exact queuing order and who waited"

---

## 2. Key Decisions to Defend

### Q: "Why event-driven simulation and not an LP/ILP/constraint solver?"

**Your answer:**
- The problem is **online** in practice — in the real world buses arrive dynamically, you don't have perfect future knowledge
- A simulation matches how a real-time dispatcher would work — process events as they happen
- It's O(N log N), runs in milliseconds for 20 buses, scales linearly to 200+
- It's **explainable** — you can trace exactly why bus X got priority over bus Y at any moment
- LP/ILP would be brittle: every new soft rule = new constraint formulation, hard to extend
- The priority function gives you the **tuning knobs** without re-solving the entire optimization

**If they push back:** "An LP could theoretically find a global optimum, but the problem has online characteristics, and the soft rules make it multi-objective. A weighted priority heuristic gives 'good enough' solutions that are easy to reason about and extend. If we later needed provable optimality, we could layer a local-search improvement pass on top of the simulation output."

---

### Q: "Why JSON files and not a database?"

**Your answer:**
- The spec says "no DB, no auth, in-memory state is fine"
- JSON is human-readable, version-controllable (git diff), and trivially editable by non-engineers
- Self-contained: one file = one complete scenario, including world parameters
- Can be replaced with a DB read if needed later — the `load_scenario()` function is the only entry point

---

### Q: "Why greedy charging plan instead of optimizing which stations to use?"

**Your answer:**
- Greedy "go as far as possible" minimizes total charging stops (2 stops for most trips)
- With uniform speeds and full-charge policy, there's very little benefit to choosing "early" stops vs "late" stops — the total charging time is the same (25 min × N stops)
- What DOES matter is **when** you arrive at a station relative to other buses — that's the queuing problem, which the priority function handles
- If we wanted station-choice optimization, we could add a second pass that considers current queue depth — but the greedy plan is a correct baseline

---

### Q: "How does your priority scoring work? Walk me through it."

**Your answer:**
```
score = - individual_weight × bus_accumulated_wait
         - operator_weight × operator_fleet_avg_wait
         + overall_weight × arrival_time_at_station
```

- **Lower score = higher priority** (served first)
- `individual` term: bus that has waited a lot gets negative score → served first (starvation prevention)
- `operator` term: if an operator's fleet is behind on average, their buses get a boost (fairness across operators)
- `overall` term: buses that arrived earlier get lower score (FCFS baseline)
- Scores are **re-computed at dispatch time** (when charger becomes free), not at arrival — so a bus that has been waiting 50 minutes gets correctly scored higher than when it first arrived

**Key insight:** The three terms pull in slightly different directions — weights let you tune the trade-off. Scenario 4 shows this: operator=2.0 means KPN buses (majority) yield priority to freshbus/flixbus more often.

---

### Q: "What's the time complexity?"

**Your answer:**
- Each bus generates O(charging_stops) events ≈ 2-3 per bus
- Total events = O(N) where N = number of buses
- Each event: heap push/pop = O(log E) where E = total events
- Queue dispatch re-scores K waiting buses = O(K) per dispatch
- Overall: O(N log N) for simulation, O(N × K) worst case for re-scoring
- For 20 buses: ~50 events, completes in <10ms
- For 200 buses: ~600 events, still sub-second

---

## 3. "Extend the Design on the Spot" Scenarios

### They give you a fresh departure schedule
1. Copy any existing JSON: `cp scenarios/scenario_1.json scenarios/scenario_6.json`
2. Edit `meta` (id, name, description)
3. Replace the `buses` array with new data
4. Save → app auto-discovers it on reload

**Practice this live — takes 60 seconds.**

---

### They ask: "Add a new station E between C and D"

Edit JSON:
```json
"stops": ["Bengaluru", "A", "B", "C", "E", "D", "Kochi"],
"segments": [
  {"from": "Bengaluru", "to": "A", "distance_km": 100},
  {"from": "A", "to": "B", "distance_km": 120},
  {"from": "B", "to": "C", "distance_km": 100},
  {"from": "C", "to": "E", "distance_km": 60},
  {"from": "E", "to": "D", "distance_km": 60},
  {"from": "D", "to": "Kochi", "distance_km": 100}
]
```
Add to stations: `{"id": "E", "chargers": 1}`

**Zero code changes.** The scheduler reads stops/segments dynamically.

---

### They ask: "Double the chargers at station B"

Edit JSON:
```json
{"id": "B", "chargers": 2}
```
**Zero code changes.** The scheduler already initializes `station_chargers[id]` as a list of length `chargers`.

---

### They ask: "Add a new operator 'megabus'"

Just use `"operator": "megabus"` in the bus objects. The scheduler treats operator as an opaque string. The fairness scoring groups by string automatically.

**Zero code changes.** (The UI won't have a color badge — that's cosmetic only.)

---

### They ask: "Change segment distance"

Edit `distance_km` in the relevant segment object. All travel times derive from this at runtime.

---

## 4. "Add a New Rule Live" Scenarios

### Example: "Add a priority/VIP bus rule"

**Step 1** — Add to scenario JSON:
```json
{"id": "bus-BK-01", "operator": "kpn", "direction": "BK", 
 "departure_time": "19:00", "priority": true}
```

**Step 2** — Add to `Bus` dataclass in `models.py`:
```python
priority: bool = False
```

**Step 3** — Update `load_scenario()` in `app.py`:
```python
buses.append(Bus(
    ...,
    priority=b.get("priority", False),
))
```

**Step 4** — Add one line to `compute_priority()` in `scheduler.py`:
```python
score = (
    - weights.individual * state.total_wait
    - weights.operator   * op_wait
    + weights.overall    * state.current_time
    - (10000.0 if state.bus.priority else 0.0)  # VIP skips queue
)
```

**Total: ~5 lines of code. Engine unchanged.**

---

### Example: "Add time-of-day electricity cost penalty"

**Step 1** — Add weight: `"electricity_cost": 0.8`
**Step 2** — Add to Weights: `electricity_cost: float = 0.0`
**Step 3** — Define helper:
```python
def peak_penalty(time_min: float) -> float:
    hour = (time_min / 60) % 24
    return 1.0 if 18 <= hour <= 22 else 0.0  # peak hours
```
**Step 4** — Add term:
```python
+ weights.electricity_cost * peak_penalty(state.current_time)
```

Higher score → lower priority during peak → buses prefer to wait for off-peak.

---

### Example: "Add a hard rule — minimum gap between charges at same station"

This one requires actual logic change (it's a hard constraint, not a soft rule):
```python
# In dispatch_from_queue(), after picking the best bus:
if station_chargers[station_id][slot_idx] + MIN_GAP > now:
    return  # enforce cooldown gap
```

**Explain:** "Hard rules go into the simulation logic. Soft rules go into compute_priority(). That's the separation of concerns."

---

## 5. Common Technical Questions

### Q: "What happens if two buses have the exact same priority score?"
A: The `seq` counter in the Event dataclass and in the waiting queue acts as a tie-breaker — FIFO within identical scores.

### Q: "Is the greedy charging plan always optimal?"
A: It's optimal for minimizing number of stops (proof: going as far as possible before charging means you skip the maximum number of intermediate stations). It's not optimal for minimizing wait time (a bus might choose a less-congested station). That's a deliberate trade-off — the queuing system handles contention dynamically.

### Q: "What if a bus can't physically reach any station?"
A: The `build_charging_plan()` function raises a `ValueError` if any single segment exceeds battery range — this is a data validation check that catches impossible scenarios.

### Q: "How do you handle the Kochi→Bengaluru direction?"
A: The route is reversed using `ordered_segments(route, "KB")` which flips the segment list. Station order is also reversed for KB buses. Same scheduler logic, just reversed input.

### Q: "Why re-score at dispatch time instead of at arrival?"
A: If Bus A arrives and waits 50 min, and Bus B arrives 45 min later, scoring at arrival would permanently rank B above A (fresher data). Re-scoring at dispatch means A's accumulated wait is now properly reflected, making the system fair. This is a key design decision.

### Q: "What are the limitations of your approach?"
A: 
- **No global optimality** — a simulation-based approach finds a locally good schedule, not the theoretical best
- **Greedy station choice** — doesn't consider current queue depth when choosing where to charge (could be improved with a look-ahead)
- **No re-planning** — once a bus is assigned stations, it doesn't switch even if queues build up ahead (could add dynamic re-routing)
- **Single route** — while the data model supports multiple routes, the current scheduler processes one route at a time

---

## 6. Assumptions to Defend

1. **Speed = 60 km/h** — consistent with the spec's example. Makes math clean (100 km = 100 min travel).

2. **Greedy "go as far as possible" for station selection** — minimizes total stops while guaranteeing feasibility.

3. **Priority re-scoring at dispatch** — fairer than arrival-time scoring.

4. **No partial charging** — spec says "always to full, takes 25 minutes" — interpreted as binary.

5. **No minimum dwell time** — bus can theoretically arrive and start charging instantly if charger is free.

6. **Same-day simulation** — no midnight wraparound needed given departure times are 19:00-21:15 and trips take ~9-10 hours max.

---

## 7. Buzzwords to Drop Naturally

- "Event-driven simulation" / "Discrete-event simulation"
- "Priority queue with weighted scoring"
- "Data-driven configuration" / "Declarative scenario specification"
- "Separation of concerns" (hard constraints in sim logic, soft rules in priority function)
- "O(N log N) event processing"
- "Re-scoring at dispatch for temporal fairness"
- "Greedy feasibility guarantee"

---

## 8. Red Flags to Avoid

- DON'T say "I just followed the spec" — show initiative and foresight
- DON'T say "AI wrote this" without understanding — be ready to trace any line of code
- DON'T get defensive about limitations — acknowledge them and explain the trade-off
- DON'T over-complicate explanations — they want clarity and confidence

---

## 9. Questions to Ask THEM (shows engagement)

- "In the real system, would buses report their actual position/battery in real-time? That would enable dynamic re-routing."
- "Are there plans for heterogeneous fleets (different battery sizes, charging speeds)?"
- "How do you currently handle the case where a bus misses its scheduled departure?"
- "Would the scheduling system eventually need to handle reservations (pre-book a charging slot)?"
