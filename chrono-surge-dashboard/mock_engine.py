"""
mock_engine.py

Synthetic stand-in for the Phase 4 Chronological State Engine output.
Produces a deterministic frame-by-frame trace so the Phase 5 dashboard
can be built and gated entirely before the real VSS pipeline is ready.
"""

import math
import random

ITEMS = ["Pencil-A", "Pencil-B", "Gauze-Pack", "ID-Card"]
ZONES = ["TRAY", "HANDS", "FIELD", "ABSENT"]

random.seed(7)


def _base_item_state():
    return {name: {"zone": "TRAY", "committed": True, "bbox": None} for name in ITEMS}


def _fake_bbox(zone, frame_index, seed):
    if zone == "ABSENT":
        return None
    cx = 60 + (seed * 140) % 700
    cy = {"TRAY": 380, "HANDS": 220, "FIELD": 120}.get(zone, 250)
    wobble = int(6 * math.sin(frame_index / 3 + seed))
    return (cx + wobble, cy + wobble, 70, 70)


def generate_trace(scenario: str, n_frames: int = 60):
    state = _base_item_state()
    trace = []

    for f in range(n_frames):
        endpoint_ok = True
        alert = None

        if scenario == "round_trip":
            if f == 10:
                state["Pencil-A"]["zone"] = "HANDS"
            if f == 20:
                state["Pencil-A"]["zone"] = "FIELD"
            if f == 35:
                state["Pencil-A"]["zone"] = "HANDS"
            if f == 45:
                state["Pencil-A"]["zone"] = "TRAY"

        elif scenario == "occlusion":
            if f == 10:
                state["Gauze-Pack"]["zone"] = "FIELD"
            if 25 <= f < 28:
                state["Gauze-Pack"]["committed"] = False
            if f == 28:
                state["Gauze-Pack"]["committed"] = True
            if f == 40:
                state["Gauze-Pack"]["zone"] = "TRAY"

        elif scenario == "retention":
            if f == 8:
                state["ID-Card"]["zone"] = "FIELD"
            if f == 23:
                alert = {"item": "ID-Card", "type": "HAZARD"}
            if f > 23:
                state["ID-Card"]["zone"] = "ABSENT"

        elif scenario == "endpoint_drop":
            if 15 <= f < 30:
                endpoint_ok = False

        items_out = {}
        for name, s in state.items():
            items_out[name] = {
                "zone": s["zone"],
                "committed": s["committed"],
                "bbox": _fake_bbox(s["zone"], f, hash(name) % 5),
            }

        latency = 42 + 6 * math.sin(f / 4) + random.uniform(-1.5, 1.5)
        mem = 3120 + 40 * math.sin(f / 9) + random.uniform(-8, 8)

        trace.append(
            {
                "frame_index": f,
                "endpoint_ok": endpoint_ok,
                "items": items_out,
                "alert": alert,
                "latency_ms": round(latency, 1),
                "mem_footprint_mb": round(mem, 1),
            }
        )

    return trace


SCENARIOS = {
    "Round trip (no alert)": "round_trip",
    "Occlusion (no alert — negative case)": "occlusion",
    "Retention (HAZARD alert)": "retention",
    "Endpoint drop (degraded)": "endpoint_drop",
}
