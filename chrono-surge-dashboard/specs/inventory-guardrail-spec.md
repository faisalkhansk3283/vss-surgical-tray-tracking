# Inventory Guardrail Spec

Applies to both live VLM modes in `app.py` ("Upload Video" and "Live
Analysis"). Governs how a VLM's free-text object detections become stable
inventory rows, and what gets rejected as hallucination before it ever
reaches the dashboard or the audit log.

## Why this exists
Cosmos3 is prompted with an **open-ended** object list (no fixed item
vocabulary — see [[system-design]] for why: a hardcoded item list doesn't
generalize to videos with different tray contents). Open-ended naming means
the same physical object can be described differently frame-to-frame or
window-to-window ("pencil" vs "yellow pencil"), and the model can also
hallucinate word-fragments of an object's own name as if they were separate
objects ("white label with text" alongside a bogus "white"). Both failure
modes were observed in production output during development. This spec is
the enforced fix.

## Rule 1 — First-seen name wins (identity stability)
Implemented in `tray_config.NameCanonicalizer.canonicalize()`.

- The **first** window/frame a name is seen in is the reference point for
  that item's identity — matches the product requirement that "the first
  frame's object count is the reference."
- A later name is matched to an existing tracked name if they share at
  least `NAME_MATCH_THRESHOLD` (0.3) fraction of words, **scored against the
  smaller of the two token sets** (not the larger) — this is deliberate: a
  bare one-word name like `"white"` must still score a full match against a
  four-word name like `"white label with text"` that fully contains it;
  dividing by the larger set would dilute that match below threshold and
  let the fragment slip through unmerged.
- **Subset relationship, either direction** (`"packet"` vs
  `"rectangular packet"`, or `"white"` vs `"white label with text"`): the
  **already-registered** name always wins. A later arrival — however it's
  phrased — is only ever merged into it, **never** overwrites it. (An
  earlier version of this rule let the shorter name win, which caused
  established full names to silently degrade into a bare adjective over
  successive windows — the exact bug this spec now forbids by construction.)
- **Neither is a subset of the other** but they share a word (e.g. `"yellow
  pencil"` vs `"blue pencil"`): collapse to the shared word(s) as a new
  generic canonical name (`"pencil"`) — this is the one case where the
  canonical name is allowed to change, because it's resolving two sibling
  variants of the same object type, not a fragment.

## Rule 2 — Same-window duplicates merge, don't distinguish by color
Implemented in `tray_config.merge_objects_by_canonical_name()`.

- If two raw names in the *same* window's response map to the same
  canonical name (e.g. two pencils reported as `"yellow pencil"` and `"blue
  pencil"`), they collapse into **one** inventory row with **counts
  summed** — the dashboard must never show two colored variants as
  distinct items just because the VLM described them differently.
- Runs in two passes: the registry must fully settle for the batch before
  final name resolution, since a later item in the same batch can rename an
  earlier one's canonical label (see Rule 1's generic-collapse case).

## Rule 3 — Reject fragment hallucinations with no merge target
Implemented in `tray_config.reject_fragment_names()`. Last line of defense
for fragments that have no prior registered name to merge into yet (e.g. the
very first window already contains a stray `"thin"` alongside `"thin metal
rod"`, in either order).

- **Stoplist**: a name made up *entirely* of bare color/material/size
  adjectives (`FRAGMENT_STOPLIST`: white, black, red, blue, yellow, green,
  gray, grey, metallic, plastic, thin, small, large, long, short, round) is
  dropped outright — these are never legitimate standalone tray items.
- **Subset duplicate**: if a name's tokens are a strict subset of another
  name *already accepted from the same batch*, it's dropped as a
  duplicate fragment.
- Every dropped item is returned alongside the clean list — **never
  silently discarded**. Callers must log it.

## Auditability contract
Both `app.py` call sites must persist what was dropped:
- Live Analysis mode: `dropped_fragments` field in each
  `session_store.append_turn()` record (`transcript.json`).
- Upload Video mode: `dropped_fragments` field in
  `session_store.write_summary()` (`summary.json`).

This mirrors the audit-trail principle from MedTriage's `auditor.py` design
— a hallucination guardrail that silently discards data without a record is
not trustworthy for a safety-critical use case.

## Known limitation
Rule 2's count-summing can inflate a merged item's count if a genuine
fragment (not fully caught by Rule 3) still merges into a real item in the
same window — e.g. a fragment sharing ≥30% of words with a real item adds
its (usually `1`) count on top. This is judged lower-risk than the
identity-fragmentation bug it replaces, but is not fully closed. Next step
if this resurfaces: only sum counts when the merged-away name was itself
accepted as a distinct detection with a bounding box or other independent
evidence, not just a name match.
