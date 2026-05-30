"""
app.py — Streamlit UI for the Bus Charging Scheduler.

WHAT:   The presentation layer.  Renders scenario data and scheduler results
        into an interactive web application with three tabs.

WHY SEPARATE:  All business logic lives in scheduler.py and models.py.
        This file is responsible ONLY for layout, formatting, and display.
        If you need to change how scheduling works, you never touch this file.
        If you need to change how results are displayed, you never touch scheduler.py.

HOW IT WORKS:
        1. Auto-discovers all scenario_*.json files in the scenarios/ directory.
        2. User picks one from the dropdown.
        3. Calls load_scenario() to parse JSON → Scenario dataclass.
        4. Calls run_scheduler(scenario) → ScheduleResult.
        5. Renders three tabs from the result.

WHEN TO MODIFY THIS FILE:
        - Adding a new UI tab or display format.
        - Changing visual styling (badges, colors, column layouts).
        - NOT for changing scheduling logic or data models.
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


# ═══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════════

# Directory where scenario JSON files are stored.
# Auto-discovery: any file matching scenario_*.json is loaded into the dropdown.
SCENARIOS_DIR = Path(__file__).parent / "scenarios"

# Visual styling: operator → (emoji_icon, text_color, background_color)
# WHY: Makes it instantly clear which operator a bus belongs to in tables.
# HOW TO ADD: Just add a new key here for any new operator name.
OPERATOR_COLORS = {
    "kpn":      ("🔵", "#1d4ed8", "#dbeafe"),   # blue
    "freshbus": ("🟢", "#15803d", "#dcfce7"),   # green
    "flixbus":  ("🟠", "#c2410c", "#ffedd5"),   # orange
}

# Visual styling: direction → (text_color, background_color)
DIRECTION_COLORS = {
    "BK": ("#4338ca", "#e0e7ff"),   # indigo  — Bengaluru → Kochi
    "KB": ("#7c3aed", "#ede9fe"),   # violet  — Kochi → Bengaluru
}


# ═══════════════════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════


def minutes_to_hhmm(minutes: float) -> str:
    """
    Convert minutes-from-midnight to a human-readable HH:MM string.

    WHY: The scheduler works internally in minutes (e.g. 1140 = 19:00) because
    arithmetic is simpler.  The UI needs clock format for readability.

    Example: 1265.0 → "21:05"
    """
    total = int(round(minutes))
    h = (total // 60) % 24  # Modulo 24 handles midnight wraparound display
    m = total % 60
    return f"{h:02d}:{m:02d}"


def load_scenario(path: Path) -> Scenario:
    """
    Parse a scenario JSON file into a fully typed Scenario dataclass.

    WHAT:  The bridge between the file system and the domain model.
    WHY:   Single point of deserialization — if the JSON schema evolves,
           only this function needs updating.
    HOW:   Maps JSON keys directly to dataclass fields.  Departure times
           are converted from "HH:MM" strings to integer minutes here.
    WHEN:  Called once per scenario selection (and once during dropdown build
           to extract meta labels).
    """
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
        # Convert "19:00" → 1140 (minutes from midnight) for scheduler arithmetic
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
    """
    Generate an HTML pill badge for an operator name (for inline rendering).

    WHY HTML: Streamlit's markdown supports unsafe_allow_html for rich styling.
    Falls back to neutral grey for unknown operators (future-proof).
    """
    icon, color, bg = OPERATOR_COLORS.get(operator, ("⚪", "#374151", "#f3f4f6"))
    return (
        f'<span style="background:{bg};color:{color};padding:2px 8px;'
        f'border-radius:9999px;font-size:0.78rem;font-weight:600;">'
        f'{icon} {operator}</span>'
    )


def direction_badge(direction: str) -> str:
    """Generate an HTML pill badge for travel direction (BK or KB)."""
    color, bg = DIRECTION_COLORS.get(direction, ("#374151", "#f3f4f6"))
    label = "BK →" if direction == "BK" else "← KB"
    return (
        f'<span style="background:{bg};color:{color};padding:2px 8px;'
        f'border-radius:9999px;font-size:0.78rem;font-weight:600;">'
        f'{label}</span>'
    )


# ═══════════════════════════════════════════════════════════════════════════════
# PAGE CONFIGURATION & HEADER
# ═══════════════════════════════════════════════════════════════════════════════

st.set_page_config(
    page_title="Bus Charging Scheduler",
    page_icon="⚡",
    layout="wide",
)

st.title("⚡ Bus Charging Scheduler")
st.caption("Bengaluru → A → B → C → D → Kochi  |  540 km  |  240 km max range  |  25 min charge")


# ═══════════════════════════════════════════════════════════════════════════════
# SCENARIO SELECTOR
# Dynamically discovers all scenario_*.json files in the scenarios/ directory.
# Adding a new scenario file = it appears in the dropdown on next page load.
# ═══════════════════════════════════════════════════════════════════════════════

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

# Load the selected scenario and run the scheduler
scenario = load_scenario(scenario_map[selected_label])
result = run_scheduler(scenario)

st.markdown(f"> *{scenario.meta.description}*")
st.divider()


# ═══════════════════════════════════════════════════════════════════════════════
# TABS — The three required views from the assignment spec
# ═══════════════════════════════════════════════════════════════════════════════

tab1, tab2, tab3 = st.tabs(["📋 Scenario Input", "🚌 Per-Bus Timetable", "🏗️ Per-Station View"])


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 1 — SCENARIO INPUT
# Shows the raw input data so reviewers can verify what was fed to the scheduler.
# Three columns: World Config | Weights | Route, then the full bus table below.
# ═══════════════════════════════════════════════════════════════════════════════

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

    # Bus table — shows every bus in the scenario with operator/direction badges
    st.subheader("🚌 Buses in this Scenario")
    rows = []
    for bus in scenario.buses:
        rows.append({
            "Bus ID": bus.id,
            "Operator": bus.operator,
            "Direction": bus.direction,
            "Departure": minutes_to_hhmm(bus.departure_time_min),
        })

    # Table header
    header_cols = st.columns([2, 2, 1.5, 1.5])
    header_cols[0].markdown("**Bus ID**")
    header_cols[1].markdown("**Operator**")
    header_cols[2].markdown("**Direction**")
    header_cols[3].markdown("**Departure**")

    # Table rows — rendered with HTML badges for visual clarity
    for row in rows:
        cols = st.columns([2, 2, 1.5, 1.5])
        cols[0].markdown(f"`{row['Bus ID']}`")
        cols[1].markdown(operator_badge(row["Operator"]), unsafe_allow_html=True)
        cols[2].markdown(direction_badge(row["Direction"]), unsafe_allow_html=True)
        cols[3].markdown(f"**{row['Departure']}**")


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 2 — PER-BUS TIMETABLE
# For each bus: departure, arrival, total wait, and detailed charging stop list.
# Expandable rows so reviewers can drill into any bus without visual overload.
# ═══════════════════════════════════════════════════════════════════════════════

with tab2:
    st.subheader("Per-Bus Journey Summary")

    # Aggregate metrics across all buses — quick health check
    m1, m2, m3, m4 = st.columns(4)
    all_waits = [b.total_wait_minutes for b in result.buses]
    all_durations = [b.trip_duration_minutes for b in result.buses]
    m1.metric("Total Buses", len(result.buses))
    m2.metric("Avg Wait", f"{sum(all_waits)/len(all_waits):.1f} min")
    m3.metric("Max Wait", f"{max(all_waits):.1f} min")
    m4.metric("Avg Trip Duration", f"{sum(all_durations)/len(all_durations):.0f} min")

    st.divider()

    # Sort buses by direction then departure for logical grouping
    sorted_buses = sorted(result.buses, key=lambda b: (b.direction, b.departure_time_min))

    for br in sorted_buses:
        # Expandable row: one per bus, showing summary in the header
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
                # Detailed per-stop breakdown inside the expander
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
                    # Traffic light indicator: green=no wait, yellow=short, red=long
                    wait_color = "🔴" if stop.wait_minutes > 25 else ("🟡" if stop.wait_minutes > 0 else "🟢")
                    sc2[2].markdown(f"{wait_color} {stop.wait_minutes:.0f}")
                    sc2[3].markdown(minutes_to_hhmm(stop.charge_start_min))
                    sc2[4].markdown(minutes_to_hhmm(stop.charge_end_min))
                    sc2[5].markdown(f"{stop.range_remaining_on_arrival_km:.0f}")
            else:
                st.info("No charging stops (bus completed trip on single charge).")


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 3 — PER-STATION VIEW
# For each station (A, B, C, D): shows the chronological order of all buses
# that charged there, with wait times.  Validates that the scheduler's queue
# ordering makes sense given the configured weights.
# ═══════════════════════════════════════════════════════════════════════════════

with tab3:
    st.subheader("Per-Station Charging Queue")

    # Summary metrics row — one card per station for quick comparison
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

    # Detailed per-station tables (expanded by default for easy review)
    for sr in result.stations:
        with st.expander(f"🏗️ Station {sr.station_id}  —  {len(sr.charging_order)} charging sessions", expanded=True):
            if not sr.charging_order:
                st.info("No buses charged here in this scenario.")
                continue

            # Column headers for the station queue table
            hdr = st.columns([0.5, 2, 2, 1.5, 1.5, 1.5, 1.5])
            hdr[0].markdown("**#**")
            hdr[1].markdown("**Bus ID**")
            hdr[2].markdown("**Operator**")
            hdr[3].markdown("**Arrives**")
            hdr[4].markdown("**Waits (min)**")
            hdr[5].markdown("**Charge Start**")
            hdr[6].markdown("**Charge End**")

            # Each row = one charging session in chronological order
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
