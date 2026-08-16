"""
Chrono-Surge — Dashboard (Phase 5 + Live VSS)

Streamlit dashboard. Three modes:
  - Mock Scenario: synthetic trace from mock_engine.py (gate-check demo).
  - Upload Video (Live VSS): real video sent to the Cosmos3 VLM in one call
    via vss_client.py, tray-area object tracking with start/end reconciliation.
  - Live Analysis (10s windows): video split into disjoint 10s windows,
    each analyzed separately with window-to-window continuity checks, a
    final start-vs-end reconciliation, cumulative token metrics, and a
    persisted transcript/summary under analysis_runs/.

Run:
    pip install -r requirements.txt
    streamlit run app.py
"""

import math
import time
from datetime import datetime

import streamlit as st
from mock_engine import generate_trace, SCENARIOS, ITEMS
from tray_config import (
    EXPECTED_ITEMS,
    WINDOW_SECONDS,
    NameCanonicalizer,
    merge_objects_by_canonical_name,
    reject_fragment_names,
    update_ledger,
)
import session_store
import vss_client

st.set_page_config(page_title="Chrono-Surge Dashboard", layout="wide")

# ---------- Dark theme ----------
st.markdown(
    """
    <style>
    .stApp { background-color: #0b0e14; color: #e6e6e6; }
    .panel {
        background-color: #12161f;
        border: 1px solid #232838;
        border-radius: 10px;
        padding: 16px 18px;
        margin-bottom: 14px;
    }
    .panel h4 {
        margin-top: 0;
        color: #9aa4b2;
        font-size: 13px;
        letter-spacing: 1.5px;
        text-transform: uppercase;
    }
    .status-pill {
        display: inline-block;
        padding: 8px 16px;
        border-radius: 6px;
        font-weight: 700;
        letter-spacing: 0.5px;
        font-size: 15px;
    }
    .zone-tag {
        display: inline-block;
        padding: 2px 8px;
        border-radius: 4px;
        font-size: 12px;
        font-weight: 600;
    }
    .viewport-box {
        background: #05070a;
        border: 1px dashed #2a3244;
        border-radius: 8px;
        height: 340px;
        position: relative;
        overflow: hidden;
    }
    .bbox {
        position: absolute;
        border: 2px solid #3ddc97;
        border-radius: 3px;
        font-size: 10px;
        color: #3ddc97;
        padding: 1px 3px;
    }
    .stat-tile {
        background-color: #12161f;
        border: 1px solid #232838;
        border-radius: 10px;
        padding: 12px 14px;
        height: 100%;
    }
    .video-frame {
        max-width: 420px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# ---------- Status/design tokens (dataviz skill: reserved status scale,
# never reused for series identity; dark-surface validated steps) ----------
STATUS_TOKENS = {"good": "#0ca30c", "warning": "#fab219", "critical": "#d03b3b"}
STATUS_TRACK = {  # same-hue, lighter (alpha-lightened) unfilled meter track
    "good": "rgba(12,163,12,0.22)",
    "warning": "rgba(250,178,25,0.22)",
    "critical": "rgba(208,59,59,0.22)",
}
STATUS_ICON = {"good": "✓", "warning": "⚠", "critical": "✕"}

ZONE_COLORS = {
    "TRAY": "#2b6cb0",
    "HANDS": "#d69e2e",
    "FIELD": "#9f7aea",
    "ABSENT": "#4a5568",
}

STATE_STYLE = {
    "AWAITING": ("AWAITING FEED", "rgba(137,135,129,0.15)", "#c3c2b7"),
    "NOMINAL": ("NOMINAL", "rgba(12,163,12,0.15)", "#0ca30c"),
    "DEGRADED": ("MONITORING DEGRADED", "rgba(250,178,25,0.15)", "#fab219"),
    "CRITICAL": ("CRITICAL — RETAINED ITEM", "rgba(208,59,59,0.15)", "#d03b3b"),
}

# Safety-state -> (status token, symbolic ring fill %) — the ring's fill is a
# confidence read of the safety state, not a literal ratio.
SAFETY_METER = {
    "AWAITING": ("good", 0),
    "NOMINAL": ("good", 96),
    "DEGRADED": ("warning", 55),
    "CRITICAL": ("critical", 15),
}


def _latency_state(latency, budget):
    ratio = (latency / budget) if budget else 0
    if ratio >= 0.9:
        return "critical", ratio
    if ratio >= 0.6:
        return "warning", ratio
    return "good", ratio


def render_meter_ring(label, value_text, pct, state="good", size=108):
    """DGX-style radial meter: same-ramp track (lighter step of the same hue
    as the fill), never a needle/speedometer. Status is never color-alone —
    an icon + text label always accompanies the ring (dataviz skill, Rule:
    status colors are reserved and always paired with icon+label)."""
    pct = max(0, min(100, pct))
    deg = pct * 3.6
    color = STATUS_TOKENS[state]
    track = STATUS_TRACK[state]
    icon = STATUS_ICON[state]
    inner = size - 22
    return f"""
    <div style="display:flex; flex-direction:column; align-items:center; gap:6px;">
      <div style="width:{size}px; height:{size}px; border-radius:50%;
                  background: conic-gradient({color} 0deg {deg}deg, {track} {deg}deg 360deg);
                  display:flex; align-items:center; justify-content:center;">
        <div style="width:{inner}px; height:{inner}px; border-radius:50%; background:#1a1a19;
                    display:flex; align-items:center; justify-content:center;">
          <div style="font-size:16px; font-weight:700; color:#ffffff; text-align:center;">{value_text}</div>
        </div>
      </div>
      <div style="font-size:12px; color:{color}; font-weight:700; letter-spacing:0.3px;">{icon} {label}</div>
    </div>
    """


def render_stat_tile(label, value, sub=None):
    sub_html = (
        f'<div style="font-size:11px; color:#898781; margin-top:2px;">{sub}</div>'
        if sub else ""
    )
    return f"""
    <div class="stat-tile">
      <div style="font-size:12px; color:#c3c2b7;">{label}</div>
      <div style="font-size:26px; font-weight:700; color:#ffffff; margin-top:2px;">{value}</div>
      {sub_html}
    </div>
    """

st.title("Chrono-Surge")
st.caption("Retained Surgical Item detection — offline visual second-check")

# ---------- Sidebar controls ----------
st.sidebar.title("Chrono-Surge")

overall_stats = session_store.compute_overall_stats()
st.sidebar.markdown("**All-time stats**")
st.sidebar.caption(
    f"{overall_stats['n_runs']} runs · "
    f"{overall_stats['tokens']['total_tokens']} tokens · "
    f"{overall_stats['latency_s']:.1f}s"
)
st.sidebar.markdown("---")

mode = st.sidebar.radio(
    "Mode",
    ["Mock Scenario", "Upload Video (Live VSS)", "Live Analysis (10s windows)"],
)

# =====================================================================
# MODE 1: Mock Scenario (Phase 5 gate-check demo)
# =====================================================================
if mode == "Mock Scenario":
    st.sidebar.caption("Phase 5 — Dashboard (mock-driven)")
    scenario_label = st.sidebar.selectbox("Mock scenario", list(SCENARIOS.keys()))
    scenario_key = SCENARIOS[scenario_label]

    if "frame_idx" not in st.session_state or st.session_state.get("scenario") != scenario_key:
        st.session_state.trace = generate_trace(scenario_key, n_frames=60)
        st.session_state.frame_idx = 0
        st.session_state.scenario = scenario_key
        st.session_state.alerts_log = []
        st.session_state.playing = False

    col_a, col_b, col_c = st.sidebar.columns(3)
    if col_a.button("⏮ Reset"):
        st.session_state.frame_idx = 0
        st.session_state.alerts_log = []
    if col_b.button("▶ Play" if not st.session_state.playing else "⏸ Pause"):
        st.session_state.playing = not st.session_state.playing
    if col_c.button("⏭ Step"):
        st.session_state.frame_idx = min(
            st.session_state.frame_idx + 1, len(st.session_state.trace) - 1
        )

    frame_idx = st.sidebar.slider(
        "Frame", 0, len(st.session_state.trace) - 1, st.session_state.frame_idx
    )
    st.session_state.frame_idx = frame_idx

    record = st.session_state.trace[frame_idx]

    if record["alert"] and record["alert"] not in st.session_state.alerts_log:
        st.session_state.alerts_log.append(record["alert"])

    def safety_state(rec, alerts_log):
        if not rec["endpoint_ok"]:
            return "DEGRADED"
        if alerts_log:
            return "CRITICAL"
        return "NOMINAL"

    state = safety_state(record, st.session_state.alerts_log)
    label, bg, fg = STATE_STYLE[state]

    top_left, top_right = st.columns([1.3, 1])

    with top_left:
        st.markdown('<div class="panel"><h4>Active Viewport</h4>', unsafe_allow_html=True)
        st.markdown('<div class="viewport-box">', unsafe_allow_html=True)
        for name, item in record["items"].items():
            if item["bbox"]:
                x, y, w, h = item["bbox"]
                color = ZONE_COLORS[item["zone"]]
                st.markdown(
                    f'<div class="bbox" style="left:{x}px; top:{y-260}px; width:{w}px; height:{h}px; border-color:{color}; color:{color};">{name}</div>',
                    unsafe_allow_html=True,
                )
        st.markdown("</div>", unsafe_allow_html=True)
        st.caption(f"Frame index: {record['frame_index']}  |  Feed: {'OK' if record['endpoint_ok'] else 'DROPPED'}")
        st.markdown("</div>", unsafe_allow_html=True)

    with top_right:
        st.markdown('<div class="panel"><h4>Safety Monitor</h4>', unsafe_allow_html=True)
        st.markdown(
            f'<div class="status-pill" style="background:{bg}; color:{fg};">{label}</div>',
            unsafe_allow_html=True,
        )
        if state == "CRITICAL":
            last_alert = st.session_state.alerts_log[-1]
            st.error(f"Retained item detected: **{last_alert['item']}**")
        elif state == "DEGRADED":
            st.warning("Feed endpoint unstable — inventory tracking held at last known state.")
        else:
            st.success("No hazard conditions active.")
        st.markdown("</div>", unsafe_allow_html=True)

        st.markdown('<div class="panel"><h4>Live Inventory</h4>', unsafe_allow_html=True)
        for name, item in record["items"].items():
            color = ZONE_COLORS[item["zone"]]
            conf = "committed" if item["committed"] else "holding (occluded)"
            st.markdown(
                f'{name}: <span class="zone-tag" style="background:{color}22; color:{color}; border:1px solid {color};">{item["zone"]}</span> '
                f'<span style="color:#5a6472; font-size:11px;">— {conf}</span>',
                unsafe_allow_html=True,
            )
        st.markdown("</div>", unsafe_allow_html=True)

    bottom = st.container()
    with bottom:
        st.markdown('<div class="panel"><h4>Telemetry</h4>', unsafe_allow_html=True)
        window = st.session_state.trace[: frame_idx + 1]
        latencies = [r["latency_ms"] for r in window]
        mems = [r["mem_footprint_mb"] for r in window]
        p95 = sorted(latencies)[int(0.95 * (len(latencies) - 1))] if latencies else 0

        mean_latency = sum(latencies) / len(latencies)
        safety_meter_state, safety_pct = SAFETY_METER[state]
        latency_meter_state, _ = _latency_state(mean_latency, budget=80)
        latency_pct = min(100, (mean_latency / 80) * 100)

        t1, t2, t3, t4 = st.columns(4)
        with t1:
            st.markdown(render_meter_ring("SAFETY", state, safety_pct, safety_meter_state), unsafe_allow_html=True)
        with t2:
            st.markdown(render_meter_ring("LATENCY", f"{mean_latency:.0f}ms", latency_pct, latency_meter_state), unsafe_allow_html=True)
        with t3:
            st.markdown(render_stat_tile("Unified mem footprint", f"{mems[-1]:.0f} MB", sub=f"p95 latency {p95:.1f} ms"), unsafe_allow_html=True)
        with t4:
            st.markdown(render_stat_tile("Alerts this run", len(st.session_state.alerts_log)), unsafe_allow_html=True)
        st.line_chart({"latency_ms": latencies}, height=140)
        st.markdown("</div>", unsafe_allow_html=True)

    st.sidebar.markdown("---")
    st.sidebar.caption(
        "Gate check: NOMINAL for round_trip/occlusion (occlusion must NOT trip "
        "CRITICAL), exactly one CRITICAL alert for retention, DEGRADED during "
        "endpoint_drop window."
    )

    if st.session_state.playing and frame_idx < len(st.session_state.trace) - 1:
        time.sleep(0.15)
        st.session_state.frame_idx += 1
        st.rerun()

# =====================================================================
# MODE 2: Upload Video — real VSS / Cosmos3 VLM pipeline
# =====================================================================
elif mode == "Upload Video (Live VSS)":
    st.sidebar.caption("Live — tray-area object tracking via Cosmos3 VLM")
    st.sidebar.markdown("**Expected tray items**")
    st.sidebar.write(", ".join(EXPECTED_ITEMS))

    vlm_up = vss_client.check_vlm_ready()
    st.sidebar.markdown(
        f"VLM endpoint: {'🟢 reachable' if vlm_up else '🔴 unreachable'}"
    )

    uploaded = st.file_uploader("Upload surgical tray video (mp4)", type=["mp4", "mov", "avi"])

    if uploaded is not None:
        vid_col, _ = st.columns([1, 1.3])
        with vid_col:
            st.video(uploaded)
        if st.button("▶ Analyze"):
            with st.spinner("Sending video to Cosmos3 VLM — this can take ~30-90s..."):
                try:
                    result = vss_client.analyze_video(uploaded.getvalue(), uploaded.name)
                    result["objects"], dropped_fragments = reject_fragment_names(result["objects"])
                    st.session_state.live_result = result
                    st.session_state.live_error = None

                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    run_dir = session_store.create_run(uploaded.getvalue(), uploaded.name, timestamp)
                    session_store.write_summary(run_dir, {
                        "video": uploaded.name,
                        "mode": "single_call",
                        "objects": result["objects"],
                        "dropped_fragments": dropped_fragments,
                        "notes": result["notes"],
                        "cumulative_usage": result["usage"],
                        "cumulative_latency_s": result["latency_s"],
                        "run_dir": str(run_dir),
                    })
                except vss_client.VSSClientError as e:
                    st.session_state.live_result = None
                    st.session_state.live_error = str(e)

    if st.session_state.get("live_error"):
        st.error(st.session_state.live_error)

    result = st.session_state.get("live_result")

    if result is None:
        st.info("Upload a video and click Analyze to run the real detection pipeline.")
    else:
        objects = result["objects"]

        # ---------- Reconciliation ----------
        discrepancies = []
        for obj in objects:
            mismatch = (
                obj.get("present_at_start") != obj.get("present_at_end")
                or obj.get("count_start") != obj.get("count_end")
            )
            if mismatch:
                discrepancies.append(obj)

        missing_at_end = [
            o for o in discrepancies
            if o.get("present_at_start") and not o.get("present_at_end")
        ]

        if missing_at_end:
            state = "CRITICAL"
        elif discrepancies:
            state = "DEGRADED"
        else:
            state = "NOMINAL"
        label, bg, fg = STATE_STYLE[state]

        top_left, top_right = st.columns([1.3, 1])

        with top_left:
            st.markdown('<div class="panel"><h4>Analysis Notes</h4>', unsafe_allow_html=True)
            if result.get("compressed_note"):
                st.caption(f"⚙️ {result['compressed_note']}")
            st.write(result["notes"] or "_No notes returned._")
            with st.expander("Raw VLM response"):
                st.code(result["raw_content"], language="json")
            st.markdown("</div>", unsafe_allow_html=True)

        with top_right:
            st.markdown('<div class="panel"><h4>Safety Monitor</h4>', unsafe_allow_html=True)
            st.markdown(
                f'<div class="status-pill" style="background:{bg}; color:{fg};">{label}</div>',
                unsafe_allow_html=True,
            )
            if state == "CRITICAL":
                names = ", ".join(o["name"] for o in missing_at_end)
                st.error(f"Retained item risk — missing at end: **{names}**")
            elif state == "DEGRADED":
                st.warning("Object count mismatch between start and end — review required.")
            else:
                st.success("All tray objects accounted for — start and end match.")
            st.markdown("</div>", unsafe_allow_html=True)

            st.markdown('<div class="panel"><h4>Live Inventory (start → end)</h4>', unsafe_allow_html=True)
            if not objects:
                st.caption("No objects reported.")
            for obj in objects:
                name = obj.get("name", "unknown")
                is_mismatch = obj in discrepancies
                color = STATUS_TOKENS["critical"] if is_mismatch else STATUS_TOKENS["good"]
                st.markdown(
                    f'{name}: <span class="zone-tag" style="background:{color}22; color:{color}; border:1px solid {color};">'
                    f'start {obj.get("count_start", "?")} → end {obj.get("count_end", "?")}</span>',
                    unsafe_allow_html=True,
                )
            st.markdown("</div>", unsafe_allow_html=True)

        bottom = st.container()
        with bottom:
            st.markdown('<div class="panel"><h4>Telemetry / VSS Metrics</h4>', unsafe_allow_html=True)
            usage = result["usage"]
            safety_meter_state, safety_pct = SAFETY_METER[state]
            latency_meter_state, _ = _latency_state(result["latency_s"], budget=90)
            latency_pct = min(100, (result["latency_s"] / 90) * 100)

            t1, t2, t3, t4 = st.columns(4)
            with t1:
                st.markdown(render_meter_ring("SAFETY", state, safety_pct, safety_meter_state), unsafe_allow_html=True)
            with t2:
                st.markdown(render_meter_ring("LATENCY", f"{result['latency_s']}s", latency_pct, latency_meter_state), unsafe_allow_html=True)
            with t3:
                st.markdown(render_stat_tile("Total tokens", usage.get("total_tokens", "—"),
                                              sub=f"prompt {usage.get('prompt_tokens', '—')} · completion {usage.get('completion_tokens', '—')}"), unsafe_allow_html=True)
            with t4:
                st.markdown(render_stat_tile("Objects tracked", len(objects)), unsafe_allow_html=True)
            st.markdown("</div>", unsafe_allow_html=True)

    st.sidebar.markdown("---")
    st.sidebar.caption(
        "Rule: shadows/reflections are excluded by the system prompt. "
        "CRITICAL fires when any object present at start is missing at end."
    )

# =====================================================================
# MODE 3: Live Analysis — disjoint 10s windows, window-to-window continuity
# =====================================================================
else:
    st.sidebar.caption(f"Live Analysis — {WINDOW_SECONDS}s windows via Cosmos3 VLM")
    st.sidebar.markdown("**Expected tray items**")
    st.sidebar.write(", ".join(EXPECTED_ITEMS))

    vlm_up = vss_client.check_vlm_ready()
    st.sidebar.markdown(f"VLM endpoint: {'🟢 reachable' if vlm_up else '🔴 unreachable'}")

    uploaded = st.file_uploader(
        "Upload surgical tray video (mp4)", type=["mp4", "mov", "avi"], key="live_uploader"
    )

    if uploaded is not None:
        vid_col, _ = st.columns([1, 1.3])
        with vid_col:
            st.video(uploaded)
        start_clicked = st.button("▶ Start Live Analysis")
    else:
        start_clicked = False

    if start_clicked:
        video_bytes = uploaded.getvalue()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_dir = session_store.create_run(video_bytes, uploaded.name, timestamp)

        try:
            duration = vss_client.probe_duration_seconds(video_bytes, uploaded.name)
        except vss_client.VSSClientError as e:
            st.error(str(e))
            duration = None

        if duration:
            n_windows = math.ceil(duration / WINDOW_SECONDS)
            progress = st.progress(0.0)
            step_caption = st.empty()
            safety_ph = st.empty()
            inventory_ph = st.empty()
            telemetry_ph = st.empty()
            log_ph = st.container()

            ledger = {}       # name -> {"count": int, "present": bool}  (state at end of last window)
            baseline = None   # name -> {"count": int, "present": bool}  (true video start, from window 0)
            canonicalizer = NameCanonicalizer()  # keeps re-worded objects mapped to one inventory row
            cumulative = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            cumulative_latency = 0.0
            window_error = None

            for i in range(n_windows):
                start_s = i * WINDOW_SECONDS
                end_s = min(start_s + WINDOW_SECONDS, duration)
                step_caption.caption(f"Analyzing window {i+1}/{n_windows}  ({start_s:.0f}s–{end_s:.0f}s)...")

                try:
                    clip = vss_client.trim_and_encode(video_bytes, uploaded.name, start_s, end_s - start_s)
                    result = vss_client.analyze_clip(clip, uploaded.name, start_s, end_s)
                except vss_client.VSSClientError as e:
                    window_error = f"Window {i+1} failed: {e}"
                    break

                objects = merge_objects_by_canonical_name(result["objects"], canonicalizer)
                objects, dropped_fragments = reject_fragment_names(objects)

                if i == 0:
                    # Freeze window 0's names as the baseline reference — every
                    # later window's renamed/reworded detections align back to
                    # these first (specs/live-inventory-decay-spec.md Rule 2).
                    canonicalizer.mark_baseline()
                    baseline = {
                        o["name"]: {"count": o.get("count_start", 0), "present": o.get("present_at_start", False)}
                        for o in objects
                    }

                # Advance the ledger by exactly one window: items the VLM
                # didn't mention at all decay (count -1, present=False)
                # instead of being left stale at their last "present" reading
                # — see specs/live-inventory-decay-spec.md for the full state
                # machine and worked examples.
                ledger, still_missing, count_anomalies = update_ledger(ledger, objects, baseline)

                # decaying (present=False, count still >0) is a soft warning;
                # count==0-while-absent (still_missing) is the hard CRITICAL.
                decaying = {name for name, st in ledger.items() if not st["present"] and st["count"] > 0}

                count_mismatch = [
                    o["name"] for o in objects
                    if o.get("present_at_start") and o.get("present_at_end")
                    and o.get("count_start") != o.get("count_end")
                ]

                if still_missing:
                    window_state = "CRITICAL"
                elif count_mismatch or decaying or count_anomalies:
                    window_state = "DEGRADED"
                else:
                    window_state = "NOMINAL"

                cumulative["prompt_tokens"] += result["usage"].get("prompt_tokens", 0) or 0
                cumulative["completion_tokens"] += result["usage"].get("completion_tokens", 0) or 0
                cumulative["total_tokens"] += result["usage"].get("total_tokens", 0) or 0
                cumulative_latency += result["latency_s"]

                session_store.append_turn(run_dir, {
                    "step": i,
                    "start_s": start_s,
                    "end_s": end_s,
                    "system_prompt": result["system_prompt"],
                    "user_prompt": result["user_prompt"],
                    "raw_response": result["raw_content"],
                    "parsed_objects": objects,
                    "dropped_fragments": dropped_fragments,
                    "usage": result["usage"],
                    "latency_s": result["latency_s"],
                    "window_state": window_state,
                    "still_missing": sorted(still_missing),
                    "decaying": sorted(decaying),
                    "count_anomalies": count_anomalies,
                    "timestamp": datetime.now().isoformat(),
                })

                label, bg, fg = STATE_STYLE[window_state]
                with safety_ph.container():
                    st.markdown('<div class="panel"><h4>Safety Monitor (live)</h4>', unsafe_allow_html=True)
                    st.markdown(
                        f'<div class="status-pill" style="background:{bg}; color:{fg};">{label}</div>',
                        unsafe_allow_html=True,
                    )
                    if window_state == "CRITICAL":
                        st.error(f"Confirmed missing (decayed to 0 while absent): **{', '.join(sorted(still_missing))}**")
                    elif window_state == "DEGRADED":
                        msgs = []
                        if decaying:
                            msgs.append(f"still decaying, not yet confirmed missing: **{', '.join(sorted(decaying))}**")
                        if count_anomalies:
                            names = ', '.join(a['name'] for a in count_anomalies)
                            msgs.append(f"reported count exceeded baseline, capped: **{names}**")
                        if count_mismatch:
                            msgs.append(f"count changed within a window: **{', '.join(count_mismatch)}**")
                        st.warning(" · ".join(msgs) if msgs else "Degraded — review required.")
                    else:
                        st.success("Consistent with the previous check.")
                    st.markdown("</div>", unsafe_allow_html=True)

                with inventory_ph.container():
                    st.markdown('<div class="panel"><h4>Live Inventory (running)</h4>', unsafe_allow_html=True)
                    for name, st_now in ledger.items():
                        if name in still_missing:
                            color = STATUS_TOKENS["critical"]
                        elif not st_now["present"]:
                            color = STATUS_TOKENS["warning"]
                        else:
                            color = STATUS_TOKENS["good"]
                        presence = "present" if st_now["present"] else "absent"
                        st.markdown(
                            f'{name}: <span class="zone-tag" style="background:{color}22; color:{color}; border:1px solid {color};">'
                            f'{presence}, count {st_now["count"]}</span>',
                            unsafe_allow_html=True,
                        )
                    st.markdown("</div>", unsafe_allow_html=True)

                with telemetry_ph.container():
                    st.markdown('<div class="panel"><h4>Telemetry (this window / cumulative)</h4>', unsafe_allow_html=True)
                    u = result["usage"]
                    window_meter_state, window_safety_pct = SAFETY_METER[window_state]
                    latency_meter_state, _ = _latency_state(result["latency_s"], budget=90)
                    latency_pct = min(100, (result["latency_s"] / 90) * 100)

                    t1, t2, t3, t4 = st.columns(4)
                    with t1:
                        st.markdown(render_meter_ring("SAFETY", window_state, window_safety_pct, window_meter_state), unsafe_allow_html=True)
                    with t2:
                        st.markdown(render_meter_ring("LATENCY", f"{result['latency_s']}s", latency_pct, latency_meter_state), unsafe_allow_html=True)
                    with t3:
                        st.markdown(render_stat_tile("Window tokens", u.get("total_tokens", "—"),
                                                      sub=f"cumulative {cumulative['total_tokens']}"), unsafe_allow_html=True)
                    with t4:
                        st.markdown(render_stat_tile("Cumulative latency", f"{cumulative_latency:.1f}s"), unsafe_allow_html=True)
                    st.markdown("</div>", unsafe_allow_html=True)

                with log_ph:
                    st.caption(f"Window {i+1}: {result['notes']}")

                progress.progress((i + 1) / n_windows)

            step_caption.empty()

            if window_error:
                st.error(window_error)
            elif baseline is not None:
                final_mismatches = []
                for name, base in baseline.items():
                    final = ledger.get(name, {"count": 0, "present": False})
                    if base["present"] and (not final["present"] or base["count"] != final["count"]):
                        final_mismatches.append({"name": name, "start": base, "end": final})

                final_state = "CRITICAL" if any(not m["end"]["present"] for m in final_mismatches) else (
                    "DEGRADED" if final_mismatches else "NOMINAL"
                )
                label, bg, fg = STATE_STYLE[final_state]

                st.markdown('<div class="panel"><h4>Final Reconciliation (video start vs. video end)</h4>', unsafe_allow_html=True)
                st.markdown(
                    f'<div class="status-pill" style="background:{bg}; color:{fg};">{label}</div>',
                    unsafe_allow_html=True,
                )
                if not final_mismatches:
                    st.success("All tray objects present at the start are accounted for at the end.")
                else:
                    for m in final_mismatches:
                        st.error(
                            f"**{m['name']}**: start count {m['start']['count']} → "
                            f"end {'present' if m['end']['present'] else 'MISSING'}, count {m['end']['count']}"
                        )
                st.markdown("</div>", unsafe_allow_html=True)

                session_store.write_summary(run_dir, {
                    "video": uploaded.name,
                    "duration_s": duration,
                    "n_windows": n_windows,
                    "baseline": baseline,
                    "final_ledger": ledger,
                    "final_mismatches": final_mismatches,
                    "final_state": final_state,
                    "cumulative_usage": cumulative,
                    "cumulative_latency_s": round(cumulative_latency, 1),
                    "run_dir": str(run_dir),
                })
                st.caption(f"Saved to `{run_dir}`")

    st.sidebar.markdown("---")
    st.sidebar.caption(
        f"Each {WINDOW_SECONDS}s window is compared to the previous window's end-state. "
        "The Final Reconciliation panel is the authoritative start-vs-end check. "
        "Video + full transcript are saved under analysis_runs/."
    )
