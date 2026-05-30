# Bus Charging Scheduler

A Streamlit application that schedules electric bus charging along a fixed route using an event-driven simulation with a weighted priority queue.

---

## Route

```
Bengaluru → A → B → C → D → Kochi
            100  120  100  120  100 km  (total 540 km)
```

- Each bus starts with **240 km** of charge (full)
- Every bus **must charge at least twice** (540 km > 240 km)
- Each intermediate station has **1 charger** (configurable in JSON)
- Charging always refills to full and takes **25 minutes**

---

## How to Run Locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

Open `http://localhost:8501` in your browser.

---

## Project Structure

```
bus-charging-scheduler/
├── app.py              # Streamlit UI (display only, no business logic)
├── scheduler.py        # Event-driven simulation engine
├── models.py           # Dataclasses for all domain objects
├── scenarios/
│   ├── scenario_1.json  # Even Spacing
│   ├── scenario_2.json  # Bunched Start
│   ├── scenario_3.json  # Asymmetric Load
│   ├── scenario_4.json  # Operator Heavy (fairness test)
│   └── scenario_5.json  # Worst Case Convergence
├── requirements.txt
├── README.md
└── ARCHITECTURE.md
```

---

## The 5 Scenarios

| # | Name | Key Behaviour |
|---|------|---------------|
| 1 | Even Spacing | Buses every 15 min — baseline, minimal contention |
| 2 | Bunched Start | Tight 8-min clusters — queue formation test |
| 3 | Asymmetric Load | 10 BK buses vs 4 KB buses — unequal load |
| 4 | Operator Heavy | 8 KPN buses dominate; `operator` weight = 2.0 |
| 5 | Worst Case Convergence | All 20 buses in 8-min clusters — max stress |

---

## How to Change a Weight

Open any scenario JSON and edit the `weights` block:

```json
"weights": {
  "individual": 1.0,
  "operator": 2.0,
  "overall": 1.0
}
```

- **`individual`** — Penalises buses that have accumulated a lot of queue wait. Raise this to reduce starvation.
- **`operator`** — Penalises operators whose fleet average wait is high. Raise this for inter-operator fairness.
- **`overall`** — Rewards buses that arrived at the station earlier. Raise this to minimise total network time.

No code changes required.

---

## How to Add a New Scenario

1. Copy any existing JSON file: `cp scenarios/scenario_1.json scenarios/scenario_6.json`
2. Edit `meta.scenario_id`, `meta.name`, `meta.description`
3. Change the `buses` array (id, operator, direction, departure_time)
4. Optionally change `weights` or `world`
5. Restart the app — it auto-discovers all `scenario_*.json` files

---

## How to Add a New Scheduling Rule

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full walkthrough.

Short version:
1. Add a new key to `weights` in your scenario JSON
2. Add a matching term to `compute_priority()` in `scheduler.py`
3. Update the `Weights` dataclass in `models.py`

---

## Deploy to Streamlit Community Cloud

1. Push this repo to GitHub
2. Go to [share.streamlit.io](https://share.streamlit.io)
3. Connect repo → set main file to `app.py` → Deploy

No environment variables needed.
