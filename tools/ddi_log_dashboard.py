#!/usr/bin/env python3
"""DDI Gateway Sensor Report Dashboard.

Requires:
    pip install streamlit pandas plotly

Run:
    streamlit run tools/ddi_log_dashboard.py

Then select summary CSV and (optionally) detail/gap CSV from the sidebar.
"""

from __future__ import annotations

import io
import math
from datetime import datetime
from typing import Optional

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="DDI Gateway Sensor Report Dashboard",
    page_icon="📡",
    layout="wide",
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
REQUIRED_SUMMARY_COLS = [
    "uuid", "delivery_rate_percent", "missing_event_count", "total_messages",
    "ok_count", "warning_count", "delay_count", "max_gap_sec",
]

REQUIRED_DETAIL_COLS = ["timestamp", "gap_sec"]
DETAIL_IDENTITY_COLS = ["uuid", "name"]
TEMPERATURE_PAIR_NAMES = [
    ("omron_1", "japan_sensor_1"),
    ("omron_2", "japan_sensor_2"),
]

STATUS_COLORS = {
    "OK":           "#2ecc71",
    "WARNING":      "#f39c12",
    "CRITICAL":     "#e74c3c",
    "UNKNOWN_TEMP": "#9b59b6",
}

STATUS_BG = {
    "OK":           "background-color: #d5f5e3; color: #1a5631",
    "WARNING":      "background-color: #fef9e7; color: #7d6608",
    "CRITICAL":     "background-color: #fadbd8; color: #922b21",
    "UNKNOWN_TEMP": "background-color: #f0e6ff; color: #6c3483",
}


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------
def format_seconds(sec) -> str:
    """Convert seconds to human-readable h m s."""
    try:
        sec = float(sec)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(sec) or sec < 0:
        return "—"
    if sec < 60:
        return f"{sec:.0f}s"
    if sec < 3600:
        m, s = divmod(int(sec), 60)
        return f"{m}m {s}s"
    h, rem = divmod(int(sec), 3600)
    m = rem // 60
    return f"{h}h {m}m"


def load_csv(path: str) -> pd.DataFrame:
    """Load CSV with best-effort encoding detection."""
    for enc in ("utf-8-sig", "utf-8", "cp932", "latin1"):
        try:
            return pd.read_csv(path, encoding=enc)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"Cannot decode file: {path}")


def parse_timestamps(values: pd.Series) -> pd.Series:
    """Parse ISO timestamps that may mix second and microsecond precision."""
    return pd.to_datetime(values, errors="coerce", format="mixed")


def validate_summary(df: pd.DataFrame) -> list[str]:
    """Return list of missing critical columns."""
    return [c for c in REQUIRED_SUMMARY_COLS if c not in df.columns]


def validate_detail(df: pd.DataFrame) -> list[str]:
    """Return detail/gap CSV schema issues that would prevent rendering."""
    missing = [c for c in REQUIRED_DETAIL_COLS if c not in df.columns]
    has_identity = any(
        column in df.columns
        and df[column].fillna("").astype(str).str.strip().ne("").any()
        for column in DETAIL_IDENTITY_COLS
    )
    if not has_identity:
        missing.append("one of: uuid, name")
    return missing


def _np_select(df, delivery_critical, gap_critical):
    """Vectorised status using numpy."""
    import numpy as np

    dr = pd.to_numeric(df.get("delivery_rate_percent", pd.Series(dtype=float)), errors="coerce").fillna(0)
    delay = pd.to_numeric(df.get("delay_count", pd.Series(dtype=float)), errors="coerce").fillna(0)
    gap = pd.to_numeric(df.get("max_gap_sec", pd.Series(dtype=float)), errors="coerce").fillna(0)
    warn_cnt = pd.to_numeric(df.get("warning_count", pd.Series(dtype=float)), errors="coerce").fillna(0)
    unk = pd.to_numeric(df.get("unknown_temp_count", pd.Series(dtype=float)), errors="coerce").fillna(0)

    is_critical = (dr < delivery_critical) | (delay > 5) | (gap > gap_critical)
    is_warning  = (~is_critical) & ((dr < 95) | (warn_cnt > 0) | (unk > 0))

    return np.select([is_critical, is_warning], ["CRITICAL", "WARNING"], default="OK")


def add_device_status(
    df: pd.DataFrame,
    delivery_critical: float = 90.0,
    gap_critical: float = 5400.0,
) -> pd.DataFrame:
    df = df.copy()
    df["device_status"] = _np_select(df, delivery_critical, gap_critical)
    return df


def _label(name_col, uuid_col) -> pd.Series:
    """Use name if available, else uuid."""
    if name_col is not None and name_col.notna().any():
        return name_col.fillna(uuid_col)
    return uuid_col


def add_device_options(df: pd.DataFrame) -> pd.DataFrame:
    """Add unique, human-friendly labels for sidebar and device selectors."""
    df = df.copy()
    uuids = df["uuid"].fillna("").astype(str)
    if "name" in df.columns:
        names = df["name"].fillna("").astype(str).str.strip()
        labels = names.where(names.ne(""), uuids)
    else:
        labels = uuids

    duplicate_labels = labels.duplicated(keep=False) & labels.ne("")
    df["_device_option"] = labels.where(
        ~duplicate_labels,
        labels + " (" + uuids + ")",
    )
    return df


def filter_detail_to_devices(
    detail_df: pd.DataFrame,
    selected_summary: pd.DataFrame,
) -> pd.DataFrame:
    """Keep detail rows belonging to the devices still visible in the summary."""
    if detail_df.empty or selected_summary.empty:
        return detail_df.iloc[0:0].copy()

    mask = pd.Series(False, index=detail_df.index)
    matched_identity = False
    for column in DETAIL_IDENTITY_COLS:
        if column in detail_df.columns and column in selected_summary.columns:
            selected_values = selected_summary[column].dropna().unique()
            if len(selected_values):
                mask |= detail_df[column].isin(selected_values)
                matched_identity = True

    return detail_df[mask].copy() if matched_identity else detail_df.iloc[0:0].copy()


def supports_temperature_comparison(detail_df: Optional[pd.DataFrame]) -> bool:
    """Whether a detail file contains the fields needed for paired temperatures."""
    return detail_df is not None and {"timestamp", "temperature", "name"}.issubset(detail_df.columns)


def match_temperature_readings(
    observations: pd.DataFrame,
    omron_name: str,
    japan_name: str,
    tolerance_minutes: int,
) -> pd.DataFrame:
    """Match each Omron reading with the nearest Japan reading in time."""
    omron = observations.loc[
        observations["name"].eq(omron_name), ["timestamp", "temperature"]
    ].rename(columns={
        "timestamp": "omron_timestamp",
        "temperature": "omron_temperature",
    })
    japan = observations.loc[
        observations["name"].eq(japan_name), ["timestamp", "temperature"]
    ].rename(columns={
        "timestamp": "japan_timestamp",
        "temperature": "japan_temperature",
    })
    if omron.empty or japan.empty:
        return pd.DataFrame()

    matched = pd.merge_asof(
        omron.sort_values("omron_timestamp"),
        japan.sort_values("japan_timestamp"),
        left_on="omron_timestamp",
        right_on="japan_timestamp",
        direction="nearest",
        tolerance=pd.Timedelta(minutes=tolerance_minutes),
    )
    return matched.dropna(subset=["japan_temperature"]).copy()


# ---------------------------------------------------------------------------
# KPI cards
# ---------------------------------------------------------------------------
def render_kpi_cards(df: pd.DataFrame):
    total = len(df)
    ok_cnt    = (df["device_status"] == "OK").sum()
    warn_cnt  = (df["device_status"] == "WARNING").sum()
    crit_cnt  = (df["device_status"] == "CRITICAL").sum()
    avg_dr = (
        pd.to_numeric(df.get("delivery_rate_percent"), errors="coerce").mean()
        if "delivery_rate_percent" in df.columns else None
    )
    total_miss  = int(pd.to_numeric(df.get("missing_event_count", pd.Series(0)), errors="coerce").sum())
    total_delay = int(pd.to_numeric(df.get("delay_count", pd.Series(0)), errors="coerce").sum())
    worst_gap   = pd.to_numeric(df.get("max_gap_sec", pd.Series(0)), errors="coerce").max()

    cols = st.columns(8)
    metrics = [
        ("📡 Total Devices",    str(total),                         None),
        ("✅ Devices OK",       str(ok_cnt),                        None),
        ("⚠️ Devices Warning",  str(warn_cnt),                      None),
        ("🔴 Devices Critical", str(crit_cnt),                      None),
        ("📊 Avg Delivery",     f"{avg_dr:.1f}%" if avg_dr is not None else "—", None),
        ("📭 Missing Events",   str(total_miss),                    None),
        ("⏱ Total Delays",      str(total_delay),                   None),
        ("🕳 Worst Gap",        format_seconds(worst_gap),          None),
    ]
    for col, (label, value, _) in zip(cols, metrics):
        col.metric(label, value)


# ---------------------------------------------------------------------------
# Summary table
# ---------------------------------------------------------------------------
def render_summary_table(df: pd.DataFrame):
    st.subheader("📋 Summary Table")

    display_cols = [
        "device_status", "name", "type", "uuid",
        "delivery_rate_percent", "missing_event_count", "total_messages",
        "ok_count", "warning_count", "delay_count", "unknown_temp_count",
        "max_gap_sec", "avg_gap_sec",
        "last_seen", "last_temperature", "current_expected_interval_sec",
        "avg_daily_voltage_drop",
    ]
    show_cols = [c for c in display_cols if c in df.columns]
    disp = df[show_cols].copy()

    # Format numeric cols
    if "delivery_rate_percent" in disp.columns:
        disp["delivery_rate_percent"] = pd.to_numeric(disp["delivery_rate_percent"], errors="coerce").round(2)

    if "max_gap_sec" in disp.columns:
        disp["max_gap_sec"] = disp["max_gap_sec"].apply(
            lambda x: f"{x} ({format_seconds(x)})" if pd.notna(x) else "—"
        )

    def _color_row(row):
        status = row.get("device_status", "OK")
        style = STATUS_BG.get(status, "")
        return [style] * len(row)

    styled = disp.style.apply(_color_row, axis=1)
    st.dataframe(styled, width="stretch", height=380)


# ---------------------------------------------------------------------------
# Summary charts
# ---------------------------------------------------------------------------
def render_summary_charts(
    df: pd.DataFrame,
    delivery_critical: float,
    gap_critical: float,
):
    label = _label(df.get("name"), df["uuid"])
    df = df.copy()
    df["_label"] = label

    color_map = {s: STATUS_COLORS[s] for s in STATUS_COLORS}

    # --- Chart 1: Delivery rate ---
    st.subheader("📊 Chart 1 — Delivery Rate (%)")
    if "delivery_rate_percent" in df.columns:
        dr = pd.to_numeric(df["delivery_rate_percent"], errors="coerce")
        fig1 = px.bar(
            df, x="_label", y="delivery_rate_percent",
            color="device_status", color_discrete_map=color_map,
            labels={"_label": "Sensor", "delivery_rate_percent": "Delivery Rate (%)"},
        )
        fig1.add_hline(
            y=delivery_critical,
            line_dash="dash",
            line_color="red",
            annotation_text=f"{delivery_critical}% critical",
            annotation_position="top right",
        )
        if delivery_critical < 95:
            fig1.add_hline(y=95, line_dash="dot", line_color="orange",
                           annotation_text="95% warning", annotation_position="top right")
        fig1.update_layout(yaxis_range=[0, 105], showlegend=True)
        st.plotly_chart(fig1, width="stretch")
    else:
        st.info("Column delivery_rate_percent not found.")

    # --- Chart 2: Stacked bar missing/warning/delay ---
    st.subheader("📊 Chart 2 — Event Count Breakdown")
    stack_cols = {
        "missing_event_count": "#e74c3c",
        "warning_count":       "#f39c12",
        "delay_count":         "#3498db",
        "unknown_temp_count":  "#9b59b6",
    }
    avail = {c: v for c, v in stack_cols.items() if c in df.columns}
    if avail:
        fig2 = go.Figure()
        for col, color in avail.items():
            fig2.add_trace(go.Bar(
                x=df["_label"],
                y=pd.to_numeric(df[col], errors="coerce").fillna(0),
                name=col.replace("_", " ").title(),
                marker_color=color,
            ))
        fig2.update_layout(barmode="stack",
                           xaxis_title="Sensor", yaxis_title="Count")
        st.plotly_chart(fig2, width="stretch")
    else:
        st.info("No event count columns found.")

    # --- Chart 3: Gap chart ---
    st.subheader("📊 Chart 3 — Gap Analysis (seconds)")
    if "max_gap_sec" in df.columns:
        gap_df = df[["_label", "max_gap_sec", "avg_gap_sec"]].copy() if "avg_gap_sec" in df.columns \
            else df[["_label", "max_gap_sec"]].copy()
        for c in ["max_gap_sec", "avg_gap_sec"]:
            if c in gap_df.columns:
                gap_df[c] = pd.to_numeric(gap_df[c], errors="coerce")

        fig3 = go.Figure()
        fig3.add_trace(go.Bar(
            x=gap_df["_label"],
            y=gap_df["max_gap_sec"],
            name="Max Gap",
            marker_color="#e74c3c",
            customdata=gap_df["max_gap_sec"].apply(format_seconds),
            hovertemplate="%{x}<br>Max Gap: %{y}s (%{customdata})<extra></extra>",
        ))
        if "avg_gap_sec" in gap_df.columns:
            fig3.add_trace(go.Bar(
                x=gap_df["_label"],
                y=gap_df["avg_gap_sec"],
                name="Avg Gap",
                marker_color="#3498db",
                customdata=gap_df["avg_gap_sec"].apply(format_seconds),
                hovertemplate="%{x}<br>Avg Gap: %{y}s (%{customdata})<extra></extra>",
            ))
        if gap_critical != 1800:
            fig3.add_hline(y=1800, line_dash="dot", line_color="orange",
                           annotation_text="30 min reference", annotation_position="top right")
        fig3.add_hline(y=gap_critical, line_dash="dash", line_color="red",
                       annotation_text=f"{format_seconds(gap_critical)} critical",
                       annotation_position="top right")
        fig3.update_layout(barmode="group", xaxis_title="Sensor", yaxis_title="Seconds")
        st.plotly_chart(fig3, width="stretch")
    else:
        st.info("Column max_gap_sec not found.")

    # --- Chart 4: Last temperature vs threshold ---
    st.subheader("📊 Chart 4 — Last Temperature vs Threshold")
    if "last_temperature" in df.columns:
        tmp_df = df[["_label", "last_temperature", "type"]].copy() if "type" in df.columns \
            else df[["_label", "last_temperature"]].copy()
        tmp_df["last_temperature"] = pd.to_numeric(tmp_df["last_temperature"], errors="coerce")

        fig4 = px.bar(
            tmp_df, x="_label", y="last_temperature",
            color="type" if "type" in tmp_df.columns else None,
            labels={"_label": "Sensor", "last_temperature": "Temperature (°C)"},
        )
        # Threshold lines
        if "type" in tmp_df.columns:
            for sensor_type, threshold, color, label in [
                ("japan_sensor", 120, "#e74c3c", "Japan 120°C"),
                ("omron",        180, "#3498db", "Omron 180°C"),
            ]:
                if (tmp_df["type"] == sensor_type).any():
                    fig4.add_hline(y=threshold, line_dash="dash", line_color=color,
                                   annotation_text=label, annotation_position="top right")
        elif "threshold_temp" in df.columns:
            for _, row in df.iterrows():
                thresh = pd.to_numeric(row.get("threshold_temp"), errors="coerce")
                if pd.notna(thresh):
                    fig4.add_hline(y=thresh, line_dash="dash", line_color="#aaa",
                                   annotation_text=f"threshold {thresh}°C")
        st.plotly_chart(fig4, width="stretch")
    else:
        st.info("Column last_temperature not found.")

    # --- Chart 5: Voltage drop ---
    if "avg_daily_voltage_drop" in df.columns:
        vd = pd.to_numeric(df["avg_daily_voltage_drop"], errors="coerce")
        if vd.notna().any():
            st.subheader("📊 Chart 5 — Avg Daily Voltage Drop")
            fig5 = px.bar(
                df, x="_label", y="avg_daily_voltage_drop",
                labels={"_label": "Sensor", "avg_daily_voltage_drop": "Voltage Drop / Day"},
                color_discrete_sequence=["#f39c12"],
            )
            st.plotly_chart(fig5, width="stretch")


# ---------------------------------------------------------------------------
# Detail / Gap tab
# ---------------------------------------------------------------------------
def render_detail_charts(detail_df: pd.DataFrame, selected_summary: pd.DataFrame):
    st.subheader("🔍 Timeline / Gap Detail")

    if detail_df is None or detail_df.empty:
        st.info("No detail data loaded.")
        return

    # Normalise timestamp
    if "timestamp" in detail_df.columns:
        detail_df["timestamp"] = parse_timestamps(detail_df["timestamp"])

    # Keep this tab in sync with every summary filter, not only device selection.
    detail_df = filter_detail_to_devices(detail_df, selected_summary)

    if detail_df.empty:
        st.info("No detail rows match the selected filters.")
        return

    # Scatter timeline
    if "timestamp" in detail_df.columns and "gap_sec" in detail_df.columns:
        has_name = (
            "name" in detail_df.columns
            and detail_df["name"].fillna("").astype(str).str.strip().ne("").any()
        )
        label_col = "name" if has_name else "uuid"
        hover_cols = [c for c in [
            "uuid", "name", "timestamp", "previous_timestamp",
            "temperature", "previous_temperature",
            "gap_sec", "expected_interval_sec", "status", "reason",
        ] if c in detail_df.columns]

        fig = px.scatter(
            detail_df,
            x="timestamp", y="gap_sec",
            color="status" if "status" in detail_df.columns else None,
            facet_col=label_col if detail_df[label_col].nunique() > 1 else None,
            facet_col_wrap=2,
            hover_data=hover_cols,
            color_discrete_map={
                "OK": STATUS_COLORS["OK"],
                "WARNING": STATUS_COLORS["WARNING"],
                "DELAY": STATUS_COLORS["CRITICAL"],
                "UNKNOWN_TEMP": STATUS_COLORS["UNKNOWN_TEMP"],
            },
            labels={"gap_sec": "Gap (s)", "timestamp": "Time"},
        )
        if "expected_interval_sec" in detail_df.columns:
            expected_intervals = pd.to_numeric(
                detail_df["expected_interval_sec"], errors="coerce"
            ).dropna().unique()
            for val in expected_intervals:
                fig.add_hline(y=float(val), line_dash="dot", line_color="#aaa",
                              annotation_text=f"expected {format_seconds(val)}")
        st.plotly_chart(fig, width="stretch")

    # Anomaly detail table
    st.subheader("🚨 Anomaly Rows (WARNING / DELAY / UNKNOWN_TEMP)")
    if "status" in detail_df.columns:
        anomaly_mask = detail_df["status"].isin(["WARNING", "DELAY", "UNKNOWN_TEMP"])
        anomaly_df = detail_df[anomaly_mask]
    else:
        anomaly_df = detail_df.copy()

    # UUID/status filter
    c1, c2 = st.columns(2)
    with c1:
        uuid_filter = []
        if "uuid" in anomaly_df.columns:
            uuids = anomaly_df["uuid"].dropna().unique().tolist()
            uuid_filter = st.multiselect("Filter by UUID", uuids, key="detail_uuid_filter")
    with c2:
        status_filter = []
        if "status" in anomaly_df.columns:
            statuses = anomaly_df["status"].dropna().unique().tolist()
            status_filter = st.multiselect("Filter by Status", statuses, key="detail_status_filter")

    if uuid_filter:
        anomaly_df = anomaly_df[anomaly_df["uuid"].isin(uuid_filter)]
    if status_filter:
        anomaly_df = anomaly_df[anomaly_df["status"].isin(status_filter)]

    st.dataframe(anomaly_df, width="stretch", height=300)

    csv_buf = io.StringIO()
    anomaly_df.to_csv(csv_buf, index=False)
    st.download_button(
        "⬇️ Download filtered CSV",
        data=csv_buf.getvalue().encode("utf-8-sig"),
        file_name="ddi_anomaly_filtered.csv",
        mime="text/csv",
    )


# ---------------------------------------------------------------------------
# Paired temperature correlation tab
# ---------------------------------------------------------------------------
def render_temperature_correlation(
    detail_df: pd.DataFrame,
    selected_summary: pd.DataFrame,
):
    st.subheader("🌡️ Paired Temperature Correlation")
    st.caption(
        "Each Omron reading is matched to the nearest Japan-sensor reading "
        "within the selected time window. Pearson r describes linear correlation."
    )

    detail_df = filter_detail_to_devices(detail_df, selected_summary)
    observations = detail_df[["name", "timestamp", "temperature"]].copy()
    observations["timestamp"] = parse_timestamps(observations["timestamp"])
    observations["temperature"] = pd.to_numeric(observations["temperature"], errors="coerce")
    observations = observations.dropna(subset=["name", "timestamp", "temperature"])

    if observations.empty:
        st.info("No temperature readings match the selected filters.")
        return

    tolerance_minutes = st.slider(
        "Maximum time difference for pairing (minutes)",
        min_value=1,
        max_value=60,
        value=10,
        key="temperature_pair_tolerance_minutes",
    )

    for omron_name, japan_name in TEMPERATURE_PAIR_NAMES:
        pair_names = [omron_name, japan_name]
        pair_df = observations[observations["name"].isin(pair_names)].copy()
        present_names = set(pair_df["name"])

        st.markdown(f"### {omron_name} ↔ {japan_name}")
        if set(pair_names) - present_names:
            missing_names = ", ".join(sorted(set(pair_names) - present_names))
            st.info(f"No visible temperature data for: {missing_names}.")
            continue

        timeline_col, scatter_col = st.columns(2)
        with timeline_col:
            timeline = px.line(
                pair_df.sort_values("timestamp"),
                x="timestamp",
                y="temperature",
                color="name",
                markers=True,
                color_discrete_map={
                    omron_name: "#3498db",
                    japan_name: "#e74c3c",
                },
                labels={"name": "Sensor", "timestamp": "Time", "temperature": "Temperature (°C)"},
                title="Temperature over time",
            )
            st.plotly_chart(timeline, width="stretch")

        matched = match_temperature_readings(
            observations,
            omron_name,
            japan_name,
            tolerance_minutes,
        )
        with scatter_col:
            if matched.empty:
                st.info(
                    "No readings could be paired in the selected time window. "
                    "Increase the pairing window if the devices report at different times."
                )
                continue

            correlation = matched["omron_temperature"].corr(matched["japan_temperature"])
            offset_seconds = (
                matched["omron_timestamp"] - matched["japan_timestamp"]
            ).abs().dt.total_seconds()
            metric_left, metric_right = st.columns(2)
            metric_left.metric(
                "Pearson r",
                f"{correlation:.3f}" if pd.notna(correlation) else "—",
            )
            metric_right.metric("Matched readings", str(len(matched)))
            st.caption(f"Median time offset: {format_seconds(offset_seconds.median())}")

            scatter = px.scatter(
                matched,
                x="omron_temperature",
                y="japan_temperature",
                hover_data={
                    "omron_timestamp": True,
                    "japan_timestamp": True,
                    "omron_temperature": ":.2f",
                    "japan_temperature": ":.2f",
                },
                labels={
                    "omron_temperature": f"{omron_name} temperature (°C)",
                    "japan_temperature": f"{japan_name} temperature (°C)",
                },
                title="Matched readings",
            )
            min_temp = min(matched["omron_temperature"].min(), matched["japan_temperature"].min())
            max_temp = max(matched["omron_temperature"].max(), matched["japan_temperature"].max())
            scatter.add_trace(go.Scatter(
                x=[min_temp, max_temp],
                y=[min_temp, max_temp],
                mode="lines",
                name="Equal temperature",
                line={"dash": "dot", "color": "#7f8c8d"},
            ))
            st.plotly_chart(scatter, width="stretch")


# ---------------------------------------------------------------------------
# Device detail tab
# ---------------------------------------------------------------------------
def render_device_detail(df: pd.DataFrame, detail_df: Optional[pd.DataFrame]):
    st.subheader("🔎 Device Detail")

    options = df["_device_option"].dropna().tolist()
    if not options:
        st.info("No selectable sensors are available.")
        return
    selected = st.selectbox("Select sensor", options, key="device_detail_select")

    row = df[df["_device_option"] == selected]
    if row.empty:
        st.warning("Sensor not found.")
        return
    row = row.iloc[0]

    threshold_temp = pd.to_numeric(row.get("threshold_temp", None), errors="coerce")
    sensor_type = str(row.get("type", "")).lower()
    current_interval = pd.to_numeric(row.get("current_expected_interval_sec", None), errors="coerce")

    # Rule description
    if sensor_type == "japan_sensor":
        rule_desc = "Japan sensor: >120°C → 5 min interval | ≤120°C → 30 min interval"
    elif sensor_type == "omron":
        rule_desc = "Omron: >180°C → 5 min interval | ≤180°C → 30 min interval"
    else:
        rule_desc = "Unknown type — using default rule"

    info_cols = st.columns(3)
    fields = [
        ("UUID",            row.get("uuid", "—")),
        ("Name",            row.get("name", "—")),
        ("Type",            row.get("type", "—")),
        ("Threshold Temp",  f"{threshold_temp}°C" if pd.notna(threshold_temp) else "—"),
        ("Last Seen",       row.get("last_seen", "—")),
        ("Last Temp",       f"{row.get('last_temperature', '—')}°C"),
        ("Current Interval",format_seconds(current_interval) if pd.notna(current_interval) else "—"),
        ("Delivery Rate",   f"{pd.to_numeric(row.get('delivery_rate_percent', None), errors='coerce'):.2f}%"
                            if pd.notna(row.get("delivery_rate_percent")) else "—"),
        ("Missing Events",  str(row.get("missing_event_count", "—"))),
        ("Max Gap",         format_seconds(row.get("max_gap_sec"))),
        ("Device Status",   row.get("device_status", "—")),
        ("Rule",            rule_desc),
    ]
    for i, (label, value) in enumerate(fields):
        info_cols[i % 3].metric(label, value)

    # Timeline for this device
    if detail_df is not None and not detail_df.empty:
        st.markdown("---")
        st.subheader(f"📈 Timeline — {selected}")
        dev_uuid = row.get("uuid")
        dev_name = row.get("name")
        mask = pd.Series([False] * len(detail_df), index=detail_df.index)
        if "uuid" in detail_df.columns and dev_uuid:
            mask |= (detail_df["uuid"] == dev_uuid)
        if "name" in detail_df.columns and dev_name:
            mask |= (detail_df["name"] == dev_name)
        dev_detail = detail_df[mask].copy()

        if not dev_detail.empty and "timestamp" in dev_detail.columns and "gap_sec" in dev_detail.columns:
            dev_detail["timestamp"] = parse_timestamps(dev_detail["timestamp"])
            fig = px.line(
                dev_detail.sort_values("timestamp"),
                x="timestamp", y="gap_sec",
                markers=True,
                color="status" if "status" in dev_detail.columns else None,
                color_discrete_map={
                    "OK": STATUS_COLORS["OK"],
                    "WARNING": STATUS_COLORS["WARNING"],
                    "DELAY": STATUS_COLORS["CRITICAL"],
                    "UNKNOWN_TEMP": STATUS_COLORS["UNKNOWN_TEMP"],
                },
            )
            if "expected_interval_sec" in dev_detail.columns:
                expected_intervals = pd.to_numeric(
                    dev_detail["expected_interval_sec"], errors="coerce"
                ).dropna().unique()
                for val in expected_intervals:
                    fig.add_hline(y=float(val), line_dash="dot", line_color="grey",
                                  annotation_text=f"expected {format_seconds(val)}")
            st.plotly_chart(fig, width="stretch")
        else:
            st.info("No timeline data available for this sensor.")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    st.title("📡 DDI Gateway Sensor Report Dashboard")

    # -----------------------------------------------------------------------
    # Sidebar
    # -----------------------------------------------------------------------
    with st.sidebar:
        st.header("⚙️ Settings")

        summary_file = st.file_uploader("📂 Summary CSV", type=["csv"], key="summary_upload")
        detail_file  = st.file_uploader("📂 Detail / Gap CSV (optional)", type=["csv"], key="detail_upload")

        st.markdown("---")
        delivery_critical = st.slider("🔴 Delivery rate critical threshold (%)", 50, 95, 90)
        gap_critical      = st.slider("🔴 Max gap critical threshold (s)", 600, 18000, 5400, step=300,
                                      format="%d s")

    # -----------------------------------------------------------------------
    # Load summary
    # -----------------------------------------------------------------------
    if summary_file is None:
        st.info("👈 Upload a **Summary CSV** in the sidebar to get started.")
        st.stop()

    try:
        df = load_csv(summary_file)
    except Exception as exc:
        st.error(f"Cannot load summary CSV: {exc}")
        st.stop()

    missing_cols = validate_summary(df)
    if missing_cols:
        st.error(f"Summary CSV is missing required columns: {', '.join(missing_cols)}")
        st.stop()

    # Add status
    df = add_device_status(
        df,
        delivery_critical=delivery_critical,
        gap_critical=gap_critical,
    )
    df = add_device_options(df)

    # Header info
    col_h1, col_h2, col_h3 = st.columns([3, 1, 1])
    col_h1.markdown(f"**File:** `{summary_file.name}`")
    col_h2.markdown(f"**Loaded:** {datetime.now().strftime('%H:%M:%S')}")
    col_h3.markdown(f"**Sensors:** {len(df)}")

    # -----------------------------------------------------------------------
    # Sidebar: UUID/name select (after loading)
    # -----------------------------------------------------------------------
    with st.sidebar:
        st.markdown("---")
        type_options = (
            sorted(df["type"].dropna().astype(str).unique().tolist())
            if "type" in df.columns else []
        )
        type_filter = st.multiselect("Filter by type", type_options, default=type_options)
        status_options = ["OK", "WARNING", "CRITICAL"]
        status_filter = st.multiselect("Filter by status", status_options, default=status_options)

        all_devices = df["_device_option"].dropna().unique().tolist()
        selected_devices = st.multiselect("🔍 Filter devices", all_devices, default=all_devices)

    # Apply filters
    filt = df.copy()
    if selected_devices:
        filt = filt[filt["_device_option"].isin(selected_devices)]
    if type_filter and "type" in filt.columns:
        filt = filt[filt["type"].isin(type_filter)]
    if status_filter:
        filt = filt[filt["device_status"].isin(status_filter)]

    if filt.empty:
        st.warning("No sensors match the current filters.")
        st.stop()

    # -----------------------------------------------------------------------
    # Load detail CSV
    # -----------------------------------------------------------------------
    detail_df = None
    if detail_file is not None:
        try:
            detail_df = load_csv(detail_file)
            detail_issues = validate_detail(detail_df)
            if detail_issues:
                st.warning(
                    "Detail / Gap CSV was ignored because it is missing: "
                    + ", ".join(detail_issues)
                )
                detail_df = None
        except Exception as exc:
            st.warning(f"Could not load detail CSV: {exc}")

    # -----------------------------------------------------------------------
    # KPIs
    # -----------------------------------------------------------------------
    st.markdown("---")
    render_kpi_cards(filt)
    st.markdown("---")

    # -----------------------------------------------------------------------
    # Tabs
    # -----------------------------------------------------------------------
    tab_labels = ["📋 Summary", "📈 Charts"]
    has_temperature_comparison = supports_temperature_comparison(detail_df)
    if detail_df is not None:
        tab_labels.append("🕵️ Timeline / Gap Detail")
    if has_temperature_comparison:
        tab_labels.append("🌡️ Temperature Correlation")
    tab_labels.append("🔎 Device Detail")

    tabs = st.tabs(tab_labels)

    with tabs[0]:
        render_summary_table(filt)

    with tabs[1]:
        render_summary_charts(filt, delivery_critical, gap_critical)

    tab_idx = 2
    if detail_df is not None:
        with tabs[tab_idx]:
            render_detail_charts(detail_df, filt)
        tab_idx += 1

    if has_temperature_comparison:
        with tabs[tab_idx]:
            render_temperature_correlation(detail_df, filt)
        tab_idx += 1

    with tabs[tab_idx]:
        render_device_detail(filt, detail_df)


if __name__ == "__main__":
    main()
