"""
app.py — Streamlit UI for the Bus Charging Scheduler.

All business logic lives in scheduler.py and models.py.
This file is responsible only for layout, formatting, and display.
"""

import json
import os
from pathlib import Path

import streamlit as st

from models import (
    Bus, Route, Scenario, ScenarioMeta, Segment,
    Station, Weights, World,
)
from scheduler import run_scheduler

# ── Constants ─────────────────────────────────────────────────────────────────

SCENARIOS_DIR = Path(__file__).parent / "scenarios"

OPERATOR_COLORS = {
    "kpn":      ("🔵", "#1d4ed8", "#dbeafe"),   # blue
    "freshbus": ("🟢", "#15803d", "#dcfce7"),   # green
    "flixbus":  ("🟠", "#c2410c", "#ffedd5"),   # orange
}

DIRECTION_COLORS = {
    "BK": ("#4338ca", "#e0e7ff"),   # indigo
    "KB": ("#7c3aed", "#ede9fe"),   # violet
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def minutes_to_hhmm(minutes: float) -> str:
    """Convert fractional minutes-from-midnight to HH:MM string."""
    total = int(round(minutes))
    h = (total // 60) % 24
    m = total % 60
    return f"{h:02d}:{m:02d}"


def load_scenario(path: Path) -> Scenario:
    """Parse a scenario JSON file into a Scenario dataclass."""
    with open(path) as f:
        d = json.load(f)

    world = World(**d["world"])
    segments = [Segment(from_stop=s["from"], to_stop=s["to"],
                        distance_km=s["distance_km"]) for s in d["route"]["segments"]]
    route = Route(stops=d["route"]["stops"], segments=segments)
    stations = [Station(**s) for s in d["stations"]]
    weights = Weights(**d["weights"])

    buses = []
    for b in d["buses"]:
        h, m = b["departure_time"].split(":")
        buses.append(Bus(
            id=b["id"],
            operator=b["operator"],
            direction=b["direction"],
            departure_time_min=int(h) * 60 + int(m),
        ))

    meta = ScenarioMeta(**d["meta"])
    return Scenario(meta=meta, world=world, route=route,
                    stations=stations, weights=weights, buses=buses)


def operator_badge(operator: str) -> str:
    icon, color, bg = OPERATOR_COLORS.get(operator, ("⚪", "#374151", "#f3f4f6"))
    return (
        f'<span style="background:{bg};color:{color};padding:2px 8px;'
        f'border-radius:9999px;font-size:0.78rem;font-weight:600;">'
        f'{icon} {operator}</span>'
    )


def direction_badge(direction: str) -> str:
    color, bg = DIRECTION_COLORS.get(direction, ("#374151", "#f3f4f6"))
    label = "BK →" if direction == "BK" else "← KB"
    return (
        f'<span style="background:{bg};color:{color};padding:2px 8px;'
        f'border-radius:9999px;font-size:0.78rem;font-weight:600;">'
        f'{label}</span>'
    )


# ── Page config ────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Bus Charging Scheduler",
    page_icon="⚡",
    layout="wide",
)

st.title("⚡ Bus Charging Scheduler")
st.caption("Bengaluru → A → B → C → D → Kochi  |  540 km  |  240 km max range  |  25 min charge")

# ── Scenario selector ──────────────────────────────────────────────────────────

scenario_files = sorted(SCENARIOS_DIR.glob("scenario_*.json"))
scenario_map: dict = {}
for sf in scenario_files:
    sc = load_scenario(sf)
    scenario_map[f"Scenario {sc.meta.scenario_id} — {sc.meta.name}"] = sf

selected_label = st.selectbox(
    "Select Scenario",
    list(scenario_map.keys()),
    index=0,
)

scenario = load_scenario(scenario_map[selected_label])
result = run_scheduler(scenario)

st.markdown(f"> *{scenario.meta.description}*")
st.divider()

# ── Tabs ───────────────────────────────────────────────────────────────────────

tab1, tab2, tab3 = st.tabs(["📋 Scenario Input", "🚌 Per-Bus Timetable", "🏗️ Per-Station View"])


# ════════════════════════════════════════════════════════════════════════════════
# Tab 1 — Scenario Input
# ════════════════════════════════════════════════════════════════════════════════

with tab1:
    col1, col2, col3 = st.columns(3)

    with col1:
        st.subheader("🌍 World Config")
        st.metric("Speed", f"{scenario.world.speed_kmh} km/h")
        st.metric("Battery Range", f"{scenario.world.battery_range_km} km")
        st.metric("Charge Time", f"{scenario.world.charge_time_min} min")

    with col2:
        st.subheader("⚖️ Scheduler Weights")
        st.metric("Individual (wait fairness)", scenario.weights.individual)
        st.metric("Operator (fleet fairness)", scenario.weights.operator)
        st.metric("Overall (network speed)", scenario.weights.overall)

    with col3:
        st.subheader("🛣️ Route")
        route_str = " → ".join(scenario.route.stops)
        st.markdown(f"**{route_str}**")
        for seg in scenario.route.segments:
            st.markdown(f"- {seg.from_stop} → {seg.to_stop}: **{seg.distance_km} km**")

    st.subheader("🚌 Buses in this Scenario")
    rows = []
    for bus in scenario.buses:
        rows.append({
            "Bus ID": bus.id,
            "Operator": bus.operator,
            "Direction": bus.direction,
            "Departure": minutes_to_hhmm(bus.departure_time_min),
        })

    # Render table with HTML badges
    header_cols = st.columns([2, 2, 1.5, 1.5])
    header_cols[0].markdown("**Bus ID**")
    header_cols[1].markdown("**Operator**")
    header_cols[2].markdown("**Direction**")
    header_cols[3].markdown("**Departure**")

    for row in rows:
        cols = st.columns([2, 2, 1.5, 1.5])
        cols[0].markdown(f"`{row['Bus ID']}`")
        cols[1].markdown(operator_badge(row["Operator"]), unsafe_allow_html=True)
        cols[2].markdown(direction_badge(row["Direction"]), unsafe_allow_html=True)
        cols[3].markdown(f"**{row['Departure']}**")


# ════════════════════════════════════════════════════════════════════════════════
# Tab 2 — Per-Bus Timetable
# ════════════════════════════════════════════════════════════════════════════════

with tab2:
    st.subheader("Per-Bus Journey Summary")

    # Summary metrics
    m1, m2, m3, m4 = st.columns(4)
    all_waits = [b.total_wait_minutes for b in result.buses]
    all_durations = [b.trip_duration_minutes for b in result.buses]
    m1.metric("Total Buses", len(result.buses))
    m2.metric("Avg Wait", f"{sum(all_waits)/len(all_waits):.1f} min")
    m3.metric("Max Wait", f"{max(all_waits):.1f} min")
    m4.metric("Avg Trip Duration", f"{sum(all_durations)/len(all_durations):.0f} min")

    st.divider()

    # Sort by direction then departure
    sorted_buses = sorted(result.buses, key=lambda b: (b.direction, b.departure_time_min))

    for br in sorted_buses:
        stations_used = ", ".join(s.station for s in br.charging_stops)
        header = (
            f"{operator_badge(br.operator)}&nbsp;&nbsp;"
            f"{direction_badge(br.direction)}&nbsp;&nbsp;"
            f"**`{br.bus_id}`** &nbsp;|&nbsp; "
            f"Departs **{minutes_to_hhmm(br.departure_time_min)}** &nbsp;|&nbsp; "
            f"Arrives **{minutes_to_hhmm(br.arrival_time_min)}** &nbsp;|&nbsp; "
            f"Wait **{br.total_wait_minutes:.0f} min** &nbsp;|&nbsp; "
            f"Charges at: **{stations_used}**"
        )
        with st.expander(f"{br.bus_id}  —  {minutes_to_hhmm(br.departure_time_min)} → {minutes_to_hhmm(br.arrival_time_min)}  |  wait {br.total_wait_minutes:.0f} min"):
            st.markdown(header, unsafe_allow_html=True)
            st.markdown(f"**Trip duration:** {br.trip_duration_minutes:.0f} min")

            if br.charging_stops:
                st.markdown("**Charging Stops:**")
                stop_cols = st.columns([1, 1.5, 1.5, 1.5, 1.5, 2])
                stop_cols[0].markdown("**Station**")
                stop_cols[1].markdown("**Arrives**")
                stop_cols[2].markdown("**Waits (min)**")
                stop_cols[3].markdown("**Charge Start**")
                stop_cols[4].markdown("**Charge End**")
                stop_cols[5].markdown("**Range on Arrival (km)**")

                for stop in br.charging_stops:
                    sc2 = st.columns([1, 1.5, 1.5, 1.5, 1.5, 2])
                    sc2[0].markdown(f"**{stop.station}**")
                    sc2[1].markdown(minutes_to_hhmm(stop.arrival_time_min))
                    wait_color = "🔴" if stop.wait_minutes > 25 else ("🟡" if stop.wait_minutes > 0 else "🟢")
                    sc2[2].markdown(f"{wait_color} {stop.wait_minutes:.0f}")
                    sc2[3].markdown(minutes_to_hhmm(stop.charge_start_min))
                    sc2[4].markdown(minutes_to_hhmm(stop.charge_end_min))
                    sc2[5].markdown(f"{stop.range_remaining_on_arrival_km:.0f}")
            else:
                st.info("No charging stops (bus completed trip on single charge).")


# ════════════════════════════════════════════════════════════════════════════════
# Tab 3 — Per-Station View
# ════════════════════════════════════════════════════════════════════════════════

with tab3:
    st.subheader("Per-Station Charging Queue")

    # Station summary row
    stat_cols = st.columns(len(result.stations))
    for i, sr in enumerate(result.stations):
        total_sessions = len(sr.charging_order)
        total_wait = sum(s.wait_minutes for s in sr.charging_order)
        max_queue = max(
            (sum(1 for other in sr.charging_order if other.start_min <= s.arrival_time_min < other.end_min)
             for s in sr.charging_order),
            default=0,
        )
        stat_cols[i].metric(
            f"Station {sr.station_id}",
            f"{total_sessions} sessions",
            f"total wait: {total_wait:.0f} min",
        )

    st.divider()

    for sr in result.stations:
        with st.expander(f"🏗️ Station {sr.station_id}  —  {len(sr.charging_order)} charging sessions", expanded=True):
            if not sr.charging_order:
                st.info("No buses charged here in this scenario.")
                continue

            hdr = st.columns([0.5, 2, 2, 1.5, 1.5, 1.5, 1.5])
            hdr[0].markdown("**#**")
            hdr[1].markdown("**Bus ID**")
            hdr[2].markdown("**Operator**")
            hdr[3].markdown("**Arrives**")
            hdr[4].markdown("**Waits (min)**")
            hdr[5].markdown("**Charge Start**")
            hdr[6].markdown("**Charge End**")

            for idx, slot in enumerate(sr.charging_order, 1):
                row = st.columns([0.5, 2, 2, 1.5, 1.5, 1.5, 1.5])
                bg = "#f9fafb" if idx % 2 == 0 else "#ffffff"
                row[0].markdown(f"{idx}")
                row[1].markdown(f"`{slot.bus_id}`")
                row[2].markdown(operator_badge(slot.operator), unsafe_allow_html=True)
                row[3].markdown(minutes_to_hhmm(slot.arrival_time_min))
                wait_icon = "🔴" if slot.wait_minutes > 25 else ("🟡" if slot.wait_minutes > 0 else "🟢")
                row[4].markdown(f"{wait_icon} {slot.wait_minutes:.0f}")
                row[5].markdown(f"**{minutes_to_hhmm(slot.start_min)}**")
                row[6].markdown(minutes_to_hhmm(slot.end_min))
