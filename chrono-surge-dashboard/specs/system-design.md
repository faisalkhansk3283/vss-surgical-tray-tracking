# Chrono-Surge — System Design

## What this is
A retained-surgical-item detector: monitors a fixed-camera view of a surgical
tray area and flags when an item that entered the sterile field (`FIELD`
zone) never comes back out — the actual clinical hazard ("never events" like
retained sponges/instruments).

## Runtime dependency
All VLM calls go to `nvidia/cosmos3-nano-reasoner`, served locally by the
VSS ("video-search-and-summarization") blueprint's `base` profile
(`~/video-search-and-summarization`), reachable at
`http://localhost:30082/v1/chat/completions` (OpenAI-compatible). No LLM is
used in this pipeline — `nemotron-nano-9b-v2` (also part of the `base`
profile) is text-only and unused here.

## Three dashboard modes (`app.py`)
| Mode | Input pattern | Guardrails applied |
|---|---|---|
| **Mock Scenario** | Synthetic trace, `mock_engine.py` | N/A — deterministic, used only to gate-check the dashboard's rendering logic before any real VLM was wired in. |
| **Upload Video (Live VSS)** | Whole clip in one VLM call, `vss_client.analyze_video()` | [[inventory-guardrail-spec]] |
| **Live Analysis (10s windows)** | Clip split into disjoint windows, one VLM call each, `vss_client.analyze_clip()` per window | [[inventory-guardrail-spec]] and [[live-inventory-decay-spec]], applied per-window with a persistent `NameCanonicalizer` across the whole run |

A fourth, standalone pipeline — `vss_adapter.py` + `cse.py` — exists but is
**not wired into `app.py`**. It does per-*frame* (not per-window) zone
classification (TRAY/HANDS/FIELD) with a proper debounced state machine and
retention-alert logic. See [[chronological-state-engine-spec]]. It was kept
separate rather than merged into the windowed mode per an explicit scope
decision to keep the demo path simple (present/count only, no zone
granularity) ahead of the submission deadline.

## Data flow (Live Analysis mode, the primary demo path)
```
uploaded.mp4
  -> vss_client.trim_and_encode()      (ffmpeg: cut window, re-encode, escalate compression if >7MB)
  -> vss_client.analyze_clip()          (Cosmos3 VLM call, structured JSON prompt)
  -> tray_config.merge_objects_by_canonical_name()   (same-window duplicate/reword collapse)
  -> tray_config.reject_fragment_names()             (drop hallucinated word-fragments)
  -> app.py ledger/baseline continuity check          (CRITICAL / DEGRADED / NOMINAL)
  -> session_store.append_turn() / write_summary()    (disk-persisted transcript + summary)
```

## Persistence
`session_store.py` writes one folder per run under `analysis_runs/`
(video copy + `transcript.json` + `summary.json`). `compute_overall_stats()`
aggregates token/latency totals across every past run's `summary.json` so
the sidebar's "All-time stats" panel survives mode switches and app
restarts — it deliberately re-derives from disk rather than caching in
`st.session_state`, so it can't be wiped by a Streamlit rerun.

## Why this matters
This doc is the map for anything that touches more than one file — start
here before editing `vss_client.py`, `tray_config.py`, or `app.py`'s VLM
branches, since those three are tightly coupled.
