"""
tray_config.py

Fixed configuration for the tray-monitoring VLM prompts: expected surgical
items, the shadow/reflection exclusion rule, and the structured-output
contract the dashboard parses.
"""

EXPECTED_ITEMS = ["Pencil-A", "Pencil-B", "Gauze-Pack", "ID-Card"]

SYSTEM_PROMPT = (
    "You are monitoring a fixed camera view of a surgical tray area where "
    "surgical equipment is placed before a procedure and must be returned "
    "after use. Track distinct physical objects placed on or removed from "
    "the tray area throughout the video. "
    "Ignore shadows, reflections, glare, hand,and lighting changes — these are "
    "not physical objects and must never be reported as objects. "
    "Only report solid physical items you can see placed in, held above, "
    "or moved into/out of the tray area."
)

USER_PROMPT = (
    "List every distinct physical object relevant to the tray area in this "
    "video. For each object report whether it was present at the very "
    "start of the video and whether it is present at the very end of the "
    "video, plus how many instances were visible at each point. "
    "Respond as JSON with EXACTLY this schema and nothing else "
    "(no markdown fences, no commentary outside the JSON):\n"
    '{"objects": [{"name": str, "present_at_start": bool, '
    '"present_at_end": bool, "count_start": int, "count_end": int}], '
    '"notes": str}'
)

WINDOW_SECONDS = 5

WINDOW_USER_PROMPT = (
    "This is a short {duration:.0f}-second clip, from a longer surgical tray "
    "monitoring video, covering {start:.0f}s to {end:.0f}s. "
    "List every distinct physical object relevant to the tray area visible "
    "in THIS CLIP. For each object report whether it was present at the "
    "very start of this clip and whether it is still present at the very end of "
    "this clip, plus how many instances were visible at each point. "
    "Respond as JSON with EXACTLY this schema and nothing else "
    "(no markdown fences, no commentary outside the JSON):\n"
    '{{"objects": [{{"name": str, "present_at_start": bool, '
    '"present_at_end": bool, "count_start": int, "count_end": int}}], '
    '"notes": str}}'
)

MODEL_NAME = "nvidia/cosmos3-nano-reasoner"
VLM_ENDPOINT = "http://localhost:30082/v1/chat/completions"

NAME_MATCH_THRESHOLD = 0.3


class NameCanonicalizer:
    """Maps a VLM object name onto an already-tracked item's name if they
    share a significant word (e.g. "rectangular packet" -> "packet"),
    so a later window's rewording of the same physical object updates the
    existing inventory row instead of adding a new one. The first window
    seeds the canonical names, matching how the video's first frame is the
    reference point for the live inventory.

    Rules enforced here are governed by specs/inventory-guardrail-spec.md
    (Rule 1) — read that spec before changing the matching/merge logic.
    """

    def __init__(self):
        self.canonical_names = []
        self.baseline_indices = None  # set of positions in canonical_names, once frozen

    def mark_baseline(self):
        """Call once, right after the first window/frame has been fully
        canonicalized. Freezes the *positions* (not the name strings) of
        the current registry as the baseline — the true reference set every
        later window's names must align back to (specs/live-inventory-decay-spec.md,
        Rule 2). Tracking by index, not by name, matters: a baseline entry's
        display name can still get relabeled by the generic-collapse case
        below (e.g. two colored siblings collapsing to one generic word)
        without falling out of "is this a baseline item" — indexing by the
        name string caused exactly that bug during development (a renamed
        baseline entry silently lost its baseline protection and collided
        with a second, unrelated baseline item).
        """
        self.baseline_indices = set(range(len(self.canonical_names)))

    def _best_match(self, raw_tokens, index_filter=None):
        best_idx, best_name, best_score = None, None, 0.0
        for idx, cname in enumerate(self.canonical_names):
            if index_filter is not None and idx not in index_filter:
                continue
            c_tokens = set(cname.lower().split())
            overlap = raw_tokens & c_tokens
            if not overlap:
                continue
            # Divide by the SMALLER token set so a bare one-word fragment
            # ("white") still scores a full match against a longer name it's
            # entirely contained in ("white label with text") — dividing by
            # the larger set would dilute that match below threshold.
            score = len(overlap) / min(len(raw_tokens), len(c_tokens))
            if score > best_score:
                best_idx, best_name, best_score = idx, cname, score
        return best_idx, best_name, best_score

    def canonicalize(self, raw_name):
        raw_tokens = set(raw_name.lower().split())

        if self.baseline_indices:
            # Baseline items are immutable anchors: if a raw name resembles
            # ANY baseline item at all, it resolves to that baseline item's
            # name EXACTLY, unchanged — never renamed/collapsed. This is
            # what keeps two genuinely different baseline objects (e.g. "thin
            # metal rod" and "metallic needle") from ever merging into each
            # other just because a later window's rewording of one happens
            # to share a word with the other.
            idx, name, score = self._best_match(raw_tokens, self.baseline_indices)
            if name and score >= NAME_MATCH_THRESHOLD:
                return name

        # No baseline match (or no baseline yet, e.g. still inside window 0) —
        # fall back to matching/merging among ALL registered names, where
        # renaming/collapsing is allowed (this is how window 0 itself
        # resolves "yellow pencil" + "blue pencil" -> "pencil", and how a
        # genuinely new post-baseline item's own reworded mentions merge).
        best_idx, best_name, best_score = self._best_match(raw_tokens)

        if best_name and best_score >= NAME_MATCH_THRESHOLD:
            best_tokens = set(best_name.lower().split())
            if raw_tokens <= best_tokens or best_tokens <= raw_tokens:
                # One name's tokens are a subset of the other's (in either
                # direction) — the already-registered name always wins,
                # never a later arrival, so a full name never degrades into
                # a bare fragment (e.g. "white label with text" must not be
                # overwritten by a later "white").
                return best_name
            # Neither is a plain superset of the other (e.g. "yellow pencil" vs
            # "blue pencil") — fall back to their shared generic word(s).
            generic = " ".join(sorted(raw_tokens & best_tokens))
            self.canonical_names[best_idx] = generic
            return generic

        self.canonical_names.append(raw_name)
        return raw_name


def merge_objects_by_canonical_name(objects, canonicalizer):
    """Collapse same-window duplicates that the VLM named differently
    (e.g. "yellow pencil" + "blue pencil" -> one "pencil" entry with
    combined count) rather than treating them as distinct items.

    Governed by specs/inventory-guardrail-spec.md Rule 2.

    Two passes: the first lets the canonicalizer's registry settle for this
    batch (a later item can cause an earlier item's canonical name to be
    renamed, e.g. once "blue pencil" arrives, "yellow pencil" also becomes
    "pencil"); the second re-resolves every item against that now-stable
    registry so all duplicates land under the same final key.
    """
    for o in objects:
        canonicalizer.canonicalize(o.get("name", ""))

    merged = {}
    for o in objects:
        cname = canonicalizer.canonicalize(o.get("name", ""))
        if cname not in merged:
            m = dict(o)
            m["name"] = cname
            merged[cname] = m
        else:
            m = merged[cname]
            m["count_start"] = m.get("count_start", 0) + o.get("count_start", 0)
            m["count_end"] = m.get("count_end", 0) + o.get("count_end", 0)
            m["present_at_start"] = m.get("present_at_start", False) or o.get("present_at_start", False)
            m["present_at_end"] = m.get("present_at_end", False) or o.get("present_at_end", False)
    return list(merged.values())


# Bare color/material/size adjectives the VLM sometimes emits as their own
# "object" when it's actually describing a fragment of an already-reported
# item (e.g. "white" alongside "white label with text"). These are never
# legitimate standalone tray items on their own.
FRAGMENT_STOPLIST = {
    "white", "black", "red", "blue", "yellow", "green", "gray", "grey",
    "metallic", "plastic", "thin", "small", "large", "long", "short", "round",
}


def reject_fragment_names(objects):
    """Guardrail pass, run once per window/video after merge_objects_by_canonical_name.

    Governed by specs/inventory-guardrail-spec.md Rule 3 — every dropped
    item must be surfaced to the caller and logged (see the spec's
    Auditability contract), never silently discarded.

    Drops two kinds of hallucinated fragment entries rather than letting them
    become tracked inventory items:
      - stoplist: the name is only a bare adjective (color/material/size)
        with no noun, e.g. "white", "thin", "metallic".
      - subset duplicate: the name's tokens are a strict subset of another
        object already accepted from the same batch (e.g. "text white with"
        when "white label with text" is also present) — belt-and-suspenders
        for fragments the canonicalizer didn't already catch.

    Returns (clean_objects, dropped) so the caller can audit-log what was
    filtered instead of silently discarding it.
    """
    clean, dropped = [], []
    accepted_token_sets = []

    for o in objects:
        name = (o.get("name") or "").strip()
        tokens = set(name.lower().split())

        if tokens and tokens <= FRAGMENT_STOPLIST:
            dropped.append({"name": name, "reason": "stoplist"})
            continue

        is_subset_of_accepted = any(
            tokens < other_tokens for other_tokens in accepted_token_sets
        )
        if is_subset_of_accepted:
            dropped.append({"name": name, "reason": "subset_duplicate"})
            continue

        clean.append(o)
        accepted_token_sets.append(tokens)

    return clean, dropped


def update_ledger(ledger, objects, baseline):
    """Advances the running per-item ledger by exactly one window.

    Governed by specs/live-inventory-decay-spec.md — read that spec for the
    full state machine and worked examples before changing this function.

    Every tracked item is in one of two states each window:
      - seen this window: ledger takes the VLM's reported end-state directly
        (present/absent, count_end), unseen_streak resets to 0.
      - NOT mentioned this window at all: state flips to present=False and
        the last-known count decays by 1 (floored at 0) rather than being
        left stale — this is what keeps the Live Inventory panel and the
        Safety Monitor's alert in agreement (they used to disagree, since
        the old ledger only updated names the VLM mentioned that window and
        left absent ones frozen at whatever "present" state they last had).

    A count-invariant guardrail also runs here: a baseline-tracked item's
    reported count_end may never exceed its baseline count_start (items can
    only be removed or returned, never spontaneously multiply) — an
    over-count is clipped to the baseline value and logged as an anomaly
    rather than trusted.

    Returns (new_ledger, still_missing, count_anomalies):
      - still_missing: names whose decayed count has reached 0 while absent
        — this is the CRITICAL condition, not mere absence.
      - count_anomalies: list of {"name", "reported", "capped_to"} for any
        count-invariant violation this window.
    """
    seen_this_window = {}
    for o in objects:
        name = o.get("name", "")
        seen_this_window[name] = {
            "count": o.get("count_end", 0),
            "present": o.get("present_at_end", False),
        }

    count_anomalies = []
    for name, state in seen_this_window.items():
        base = baseline.get(name) if baseline else None
        if base is not None and state["count"] > base["count"]:
            count_anomalies.append({
                "name": name,
                "reported": state["count"],
                "capped_to": base["count"],
            })
            state["count"] = base["count"]

    new_ledger = {}
    for name, state in seen_this_window.items():
        new_ledger[name] = {**state, "unseen_streak": 0}

    for name, prev in ledger.items():
        if name in seen_this_window:
            continue
        new_ledger[name] = {
            "count": max(0, prev["count"] - 1),
            "present": False,
            "unseen_streak": prev.get("unseen_streak", 0) + 1,
        }

    still_missing = {
        name for name, state in new_ledger.items()
        if not state["present"] and state["count"] == 0
    }

    return new_ledger, still_missing, count_anomalies
