# Chronological State Engine (CSE) Spec

Governs `cse.py` (`ChronoStateEngine`) and its caller `vss_adapter.py` — the
standalone, per-frame zone-tracking pipeline. **Not currently wired into
`app.py`** (see [[system-design]] for why); this spec documents the rules it
already enforces so it's ready to integrate later without re-deriving them.

## Zones
Every tracked item is in exactly one of: `TRAY` (resting, safe), `HANDS`
(in transit), `FIELD` (inside the sterile/patient area — the hazard zone),
`ABSENT` (derived by the engine, never claimed directly by the VLM — see
Rule 3).

## Rule 1 — Debounce before trusting a zone
`ItemState.majority()` / `commit_if_ready()`.

- Keeps the last `DEBOUNCE_M=5` raw observations per item in a sliding
  window.
- A candidate zone is only **committed** (trusted enough to act on) once it
  has at least `DEBOUNCE_N=3` of those 5 votes. A single noisy frame can't
  flip an item's official state.

## Rule 2 — Occlusion tolerance before assuming absence
`ItemState.observe()`.

- If a frame's detection doesn't mention an item at all, that's treated as
  "unobserved," not "gone" — `unobserved_streak` increments but nothing is
  pushed into the debounce window yet.
- Only after `OCCLUSION_TOLERANCE=5` consecutive unobserved frames does the
  engine start feeding `"ABSENT"` into the debounce window (which then
  itself needs 3-of-5 to actually commit as absent). This is what lets a
  brief hand-occlusion pass without falsely triggering anything — verified
  against `mock_engine.py`'s `occlusion` gate-check scenario, which must
  NOT trigger an alert.

## Rule 3 — ABSENT is derived, never asserted by the VLM
Per the module docstring: the VLM prompt only asks for TRAY/HANDS/FIELD —
it is never allowed to claim "ABSENT" itself, since a model asserting
absence is indistinguishable from a model that simply failed to detect
something. Absence is a conclusion the *engine* reaches (via Rules 1+2),
not an input it accepts.

## Rule 4 — Retention alert fires exactly once
`ItemState.check_retention()`.

- Fires **only** on the transition `FIELD -> ABSENT` (an item that was
  inside the sterile field and then vanished) — an item going missing from
  `TRAY` is not the hazard this system exists to catch.
- Requires `RETENTION_WINDOW=15` consecutive committed-ABSENT frames after
  the FIELD state, so a single dropped frame right after the transition
  doesn't fire prematurely.
- `self.alerted` latches so the same retention event can only ever fire
  once — verified against `mock_engine.py`'s `retention` gate-check scenario
  (must fire **exactly one** HAZARD alert, not one per frame it stays gone).

## Rule 5 — Name identity must survive open-set naming
`ChronoStateEngine._canonicalize()` — same problem and same fix as
[[inventory-guardrail-spec]] Rule 1 (first-seen name wins on a shared-word
subset match), reimplemented locally in `cse.py` since this module doesn't
import `tray_config.py` (kept dependency-free by design, so it can run
standalone via `vss_adapter.py` without pulling in the dashboard's prompt
config).

## Known unverified assumption
`vss_adapter.py` sends one JPEG frame at a time to Cosmos3 via `image_url`
(as opposed to the dashboard's `video_url` whole/windowed-clip calls) — this
was confirmed working via a live single-frame test during development, but
per-frame throughput was judged too slow for a live demo (~30-90s per VLM
call regardless of content), which is why this pipeline stayed CLI-only
rather than becoming a fourth dashboard mode.
