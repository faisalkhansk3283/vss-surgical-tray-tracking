"""
cse.py — Chronological State Engine (FR-4)

Turns a stream of noisy per-frame VLM observations into a stable,
auditable per-item zone timeline. Mirrors PRD Appendix B pseudocode.

Usage:
    engine = ChronoStateEngine()
    for frame_index, observation in per_frame_observations:
        record = engine.step(frame_index, observation, endpoint_ok=True,
                              latency_ms=..., mem_footprint_mb=...)
        trace.append(record)

`observation` is a dict: {item_name: {"zone": "TRAY"/"HANDS"/"FIELD", "bbox": [...] or None}}
Only zones a VLM can actually claim to see go in an observation — never "ABSENT".
ABSENT is derived here, per PRD §8.

Rules enforced by this module are governed by
specs/chronological-state-engine-spec.md — read that spec before changing
debounce/occlusion/retention thresholds or the name-canonicalization logic.
"""

from collections import deque

DEBOUNCE_N = 3
DEBOUNCE_M = 5
OCCLUSION_TOLERANCE = 5
RETENTION_WINDOW = 15
ZONES = ("TRAY", "HANDS", "FIELD", "ABSENT")


class ItemState:
    def __init__(self, name):
        self.name = name
        self.window = deque(maxlen=DEBOUNCE_M)
        self.committed_zone = "TRAY"
        self.prior_committed_zone = "TRAY"
        self.unobserved_streak = 0
        self.frames_since_absent = 0
        self.last_bbox = None
        self.alerted = False

    def majority(self):
        if not self.window:
            return self.committed_zone
        counts = {}
        for z in self.window:
            counts[z] = counts.get(z, 0) + 1
        zone, n = max(counts.items(), key=lambda kv: kv[1])
        return zone if n >= DEBOUNCE_N else self.committed_zone

    def observe(self, zone, bbox=None):
        if zone is None:
            self.unobserved_streak += 1
            if self.unobserved_streak >= OCCLUSION_TOLERANCE:
                self.window.append("ABSENT")
            # else: hold — don't push anything, last committed zone stands
        else:
            self.unobserved_streak = 0
            self.last_bbox = bbox
            self.window.append(zone)

    def commit_if_ready(self):
        candidate = self.majority()
        if candidate != self.committed_zone:
            self.prior_committed_zone = self.committed_zone
            self.committed_zone = candidate

    def check_retention(self):
        """Return True exactly once, the frame retention fires."""
        if self.committed_zone == "ABSENT" and self.prior_committed_zone == "FIELD":
            self.frames_since_absent += 1
            if self.frames_since_absent >= RETENTION_WINDOW and not self.alerted:
                self.alerted = True
                return True
        else:
            self.frames_since_absent = 0
        return False


NAME_MATCH_THRESHOLD = 0.3


class ChronoStateEngine:
    def __init__(self):
        self.items = {}
        self.canonical_names = []  # first-seen name for each tracked item

    def _get(self, name):
        if name not in self.items:
            self.items[name] = ItemState(name)
        return self.items[name]

    def _canonicalize(self, raw_name):
        """Map a new detection name onto an already-tracked item's name if
        they share a significant word (e.g. "red pencil" -> "pencil"),
        instead of predefining a fixed item list. First name seen for an
        object wins and stays the label used everywhere else (dashboard,
        trace.json, alerts).
        """
        raw_tokens = set(raw_name.lower().split())
        best_name, best_score = None, 0.0
        for cname in self.canonical_names:
            c_tokens = set(cname.lower().split())
            overlap = raw_tokens & c_tokens
            if not overlap:
                continue
            score = len(overlap) / max(len(raw_tokens), len(c_tokens))
            if score > best_score:
                best_name, best_score = cname, score

        if best_name and best_score >= NAME_MATCH_THRESHOLD:
            return best_name

        self.canonical_names.append(raw_name)
        return raw_name

    def step(self, frame_index, observation, endpoint_ok=True,
              latency_ms=0.0, mem_footprint_mb=0.0):
        """
        observation: {name: {"zone": "TRAY"|"HANDS"|"FIELD", "bbox": [...]}}
        Items previously seen but absent from this observation are
        treated as unobserved this frame (candidate=None).
        Raw detection names are canonicalized first, so a name change like
        "pencil" -> "red pencil" updates the existing tracked item instead
        of creating a new one.
        """
        canonical_observation = {}
        for raw_name, obs in observation.items():
            canonical_observation[self._canonicalize(raw_name)] = obs
        observation = canonical_observation

        seen = set(observation.keys())
        for name in seen:
            self._get(name)  # ensure tracked

        alert = None
        items_out = {}
        for name, item in self.items.items():
            if name in observation:
                obs = observation[name]
                item.observe(obs.get("zone"), obs.get("bbox"))
            else:
                item.observe(None)

            item.commit_if_ready()
            fired = item.check_retention()
            if fired and alert is None:
                alert = {"item": name, "type": "HAZARD"}

            items_out[name] = {
                "zone": item.committed_zone,
                "committed": item.unobserved_streak == 0,
                "bbox": item.last_bbox if item.committed_zone != "ABSENT" else None,
            }

        return {
            "frame_index": frame_index,
            "endpoint_ok": endpoint_ok,
            "items": items_out,
            "alert": alert,
            "latency_ms": round(latency_ms, 1),
            "mem_footprint_mb": round(mem_footprint_mb, 1),
        }
