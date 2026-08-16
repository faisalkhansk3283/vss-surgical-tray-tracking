# Live Inventory Decay & Baseline-Alignment Spec

Governs `tray_config.update_ledger()` and the baseline-priority matching in
`tray_config.NameCanonicalizer` (both used by `app.py`'s "Live Analysis (10s
windows)" mode). Written after a real run surfaced a live bug: the Safety
Monitor panel said an item ("metallic needle") was still absent, while the
Live Inventory panel — in the same render — showed it as "present, count 1".
This spec is the fix, and the reference for anyone touching this logic again.

## Root cause of the bug this spec fixes

The old `ledger` dict only updated a name's entry when the VLM's structured
response mentioned that name in the current window. If a window didn't
mention an item **at all** (not absent — simply not talked about), its
ledger entry was left completely untouched, frozen at whatever `present`
state it last had. Meanwhile, the CRITICAL alert was computed from a
separate, freshly-calculated `still_missing` set each window. The two pieces
of state could disagree, which is exactly what got reported.

The fix: replace the two disconnected pieces of state with **one state
machine per item**, `update_ledger()`, that is the single source of truth
both panels read from.

## Rule 1 — Presence decay, not a frozen stale reading

Every tracked item, every window, is in exactly one of two situations:

- **Seen this window** (the VLM's structured object list mentions it): the
  ledger takes the VLM's reported end-state directly — `present_at_end`,
  `count_end` — and its `unseen_streak` resets to 0.
- **Not mentioned at all**: `present` flips to `False` and the last-known
  `count` **decays by 1** (floored at 0) — it is never left stale.

The decayed count doubles as a confidence countdown, not a literal physical
count. This gives multi-instance items (count starts higher) more benefit of
the doubt across consecutive misses than single-instance items — matching
the intuition that seeing 2 pencils go quiet once is less alarming than a
single sponge going quiet once.

### Worked example — the exact scenario the user specified
```
Window 0: pencil — present, count 2        (baseline)
Window 1: pencil not mentioned by the VLM  -> pencil — absent, count 1  (DEGRADED: decaying)
Window 2: pencil not mentioned by the VLM  -> pencil — absent, count 0  (CRITICAL: confirmed missing)
Window 3: pencil not mentioned by the VLM  -> pencil — absent, count 0  (CRITICAL persists)
```
`still_missing` (the CRITICAL condition) is `count == 0 and not present` —
**not** merely `not present`. An item with count > 0 that's currently unseen
is DEGRADED ("still decaying, not yet confirmed missing"), which gives the
dashboard a visibly different state for "quiet for one check" vs. "confirmed
gone" instead of collapsing both into one alarm.

### Recovery
If an item reappears (the VLM mentions it again) before its count reaches 0,
it snaps back to `present=True` with the freshly reported count — the decay
is cancelled, not averaged or carried forward.

## Rule 2 — Baseline names are immutable anchors

`NameCanonicalizer.mark_baseline()` freezes the **positions** (not the name
strings — see the note below on why) of every name registered by the end of
window 0. From then on, `canonicalize()` first tries to match a new raw name
against baseline names *only*. If it resembles any baseline item at all, it
resolves to that baseline item's name **exactly, unchanged** — a baseline
name is never renamed or collapsed once frozen.

This directly implements the requested rule: *"we will be comparing
similarity from the start of the object and its names detected with what it
aligns more."* Window 0 is the reference point; everything after tries to
snap back onto it first, before ever being treated as a new item.

### Why indices, not name strings
An earlier draft of this used `set(canonical_names)` (a snapshot of the
*strings*) as the baseline set. That broke immediately: the existing
sibling-collapse rule (e.g. "yellow pencil" + "blue pencil" -> "pencil")
can rename a registered entry — and once a baseline entry got renamed, its
*old* string fell out of the frozen baseline-string set, so it silently lost
baseline protection and could then collide with a second, unrelated baseline
item. Concretely, with baseline items `"thin metal rod"` and `"metallic
needle"`, a later window's `"metallic rod"` collapsed the *rod* entry down
to the single generic word `"metallic"` — which then absorbed the unrelated
needle too, destroying both real items into one bogus entry that got dropped
entirely by the fragment stoplist. Tracking baseline membership by **index**
instead means a baseline entry can still be relabeled by history, but its
frozen status travels with its position, not its (possibly stale) name.

### Worked example
```
Baseline (window 0): "thin metal rod", "metallic needle"   <- two distinct real objects
Window 5: VLM reports "metallic rod" (picked up, in hand)
          -> aligns to baseline "thin metal rod" (shares "rod"), NOT "metallic needle"
          -> ledger entry "thin metal rod" updated, "metallic needle" untouched
Window 8: VLM reports "metallic rod" again, now back on the tray
          -> still aligns to "thin metal rod" -> recovers to present
```

## Rule 3 — End count can only be the same or less than baseline

A baseline-tracked item's `count_end` may never exceed its baseline
`count_start` — items can only be removed or returned over the course of the
video, never spontaneously multiply. If a window reports a higher count than
baseline, it's a VLM over-count hallucination, not a real event: the value is
clipped to the baseline count and logged as a `count_anomalies` entry
(`{"name", "reported", "capped_to"}`) in the same audit trail as dropped
fragments — never silently trusted or silently discarded.

### Worked example
```
Baseline: pencil — count 2
Window 4: VLM reports pencil count_end = 5   (hallucinated recount)
          -> ledger shows pencil count = 2 (clipped)
          -> count_anomalies = [{"name": "pencil", "reported": 5, "capped_to": 2}]
          -> window_state = DEGRADED (flagged, not silently accepted)
```

## Example fixtures (for testing without a live VLM call)

These are the exact kind of per-window `objects` payloads `update_ledger()`
expects — useful as test fixtures or to explain the edge cases to a reviewer
without needing to run the full video pipeline.

**Decay to CRITICAL:**
```json
[{"name": "pencil", "present_at_start": true, "present_at_end": true, "count_start": 2, "count_end": 2}]
```
followed by two windows reporting `"objects": []` (empty — the VLM saw
nothing worth mentioning) drives `pencil` from count 2 -> 1 -> 0, flipping
CRITICAL on the second empty window.

**Baseline realignment:**
```json
[{"name": "metallic rod", "present_at_start": true, "present_at_end": false, "count_start": 1, "count_end": 0}]
```
sent in any window after baseline is marked, where baseline contains
`"thin metal rod"`, resolves to `"thin metal rod"` in the ledger — never
creates a new `"metallic rod"` row.

**Count-invariant violation:**
```json
[{"name": "pencil", "present_at_start": true, "present_at_end": true, "count_start": 2, "count_end": 5}]
```
against a baseline of `pencil: count 2` produces a `count_anomalies` entry
and clips the displayed count to 2.

## Auditability contract
Both `still_missing` and `count_anomalies` are logged into
`session_store.append_turn()`'s per-window record (`transcript.json`) —
`decaying` (items mid-countdown) is logged alongside them so the full
history of every item's confidence trajectory is reconstructable from disk,
not just its final CRITICAL/NOMINAL state.
