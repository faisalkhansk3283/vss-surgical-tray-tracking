"""
session_store.py

Disk-backed persistence for Live Analysis runs: one folder per run holding
the uploaded video plus an ordered transcript of every VLM exchange and a
final summary — mirroring (on disk) how VSS's own agent keeps a video's
chat turns together in memory per conversation thread.
"""

import json
from pathlib import Path

RUNS_DIR = Path(__file__).parent / "analysis_runs"


def create_run(video_bytes: bytes, filename: str, timestamp: str) -> Path:
    """Creates analysis_runs/<timestamp>_<filename>/ and saves a copy of the video."""
    safe_name = "".join(c for c in filename if c.isalnum() or c in "._-") or "video"
    run_dir = RUNS_DIR / f"{timestamp}_{safe_name}"
    run_dir.mkdir(parents=True, exist_ok=True)

    video_path = run_dir / ("video" + (Path(filename).suffix or ".mp4"))
    video_path.write_bytes(video_bytes)

    (run_dir / "transcript.json").write_text("[]")
    return run_dir


def append_turn(run_dir: Path, turn: dict) -> None:
    transcript_path = run_dir / "transcript.json"
    turns = json.loads(transcript_path.read_text())
    turns.append(turn)
    transcript_path.write_text(json.dumps(turns, indent=2))


def write_summary(run_dir: Path, summary: dict) -> None:
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2))


def compute_overall_stats() -> dict:
    """Sums cumulative_usage / cumulative_latency_s across every run's
    summary.json under analysis_runs/. Skips run folders with no summary
    (failed/incomplete runs never reach write_summary).
    """
    tokens = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    latency_s = 0.0
    n_runs = 0

    if not RUNS_DIR.exists():
        return {"tokens": tokens, "latency_s": latency_s, "n_runs": n_runs}

    for run_dir in sorted(RUNS_DIR.glob("*")):
        summary_path = run_dir / "summary.json"
        if not summary_path.exists():
            continue
        try:
            summary = json.loads(summary_path.read_text())
        except (json.JSONDecodeError, OSError):
            continue

        usage = summary.get("cumulative_usage") or {}
        for key in tokens:
            tokens[key] += usage.get(key, 0) or 0
        latency_s += summary.get("cumulative_latency_s", 0) or 0
        n_runs += 1

    return {"tokens": tokens, "latency_s": latency_s, "n_runs": n_runs}
