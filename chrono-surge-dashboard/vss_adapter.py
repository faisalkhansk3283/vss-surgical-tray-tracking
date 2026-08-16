"""
vss_adapter.py — the missing middle layer between VSS and the CSE.

VSS's local API is conversational (prompt in, text out) — it does not
emit a native per-frame structured stream. This script:

  1. Extracts frames from a video at a fixed sample rate (FR-2).
  2. Sends each frame as an image to VSS's /v1/chat, asking for
     generic object identification in a fixed JSON shape (FR-3).
  3. Parses the model's JSON out of its text response.
  4. Feeds each parsed observation through the Chronological State
     Engine (FR-4) to get committed zones, debouncing, occlusion
     tolerance, and the retention alert.
  5. Writes trace.json in the exact shape the dashboard already reads.

Run:
    python3 vss_adapter.py <video_path> [--fps 1] [--out trace.json]

Before running on the full clip, test on ONE frame first (see
`--single-frame-test` below) to confirm the prompt actually gets
VSS to return parseable JSON on this deployment — that's unverified
until you run it.
"""

import argparse
import base64
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import requests

from cse import ChronoStateEngine

VSS_BASE = "http://localhost:30082"
VLM_MODEL = "nvidia/cosmos3-nano-reasoner"

PROMPT = """You are looking at a single still frame from a video of objects on a table.
Identify every distinct object visible in this frame. For each object, report:
- name: a short generic name for the object (e.g. "pencil", "paper napkin", "id card")
- zone: one of "TRAY" (resting on the table/tray), "HANDS" (being held / in transit), or "FIELD" (inside the marked/central zone)
- bbox: approximate bounding box as [x, y, width, height] in pixels, or null if unsure

Respond with ONLY a JSON object in this exact shape, no other text:
{"items": [{"name": "...", "zone": "TRAY", "bbox": [x, y, w, h]}]}
"""


def extract_frames(video_path, out_dir, fps=1):
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vf", f"fps={fps}",
        str(out_dir / "frame_%04d.jpg"),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return sorted(out_dir.glob("frame_*.jpg"))


def call_vss_on_frame(image_path):
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")

    payload = {
        "model": VLM_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                    },
                ],
            }
        ],
        "max_tokens": 500,
    }

    start = time.time()
    r = requests.post(f"{VSS_BASE}/v1/chat/completions", json=payload, timeout=60)
    latency_ms = (time.time() - start) * 1000
    r.raise_for_status()
    return r.json(), latency_ms


def parse_items(raw_response):
    """Pull the JSON block out of whatever text VSS returned."""
    text = None
    if isinstance(raw_response, dict):
        # try common shapes: {"value": "..."} or chat-completion style
        if "value" in raw_response:
            text = raw_response["value"]
        elif "choices" in raw_response:
            text = raw_response["choices"][0]["message"]["content"]
        else:
            text = json.dumps(raw_response)
    else:
        text = str(raw_response)

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return {}
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}

    observation = {}
    for item in parsed.get("items", []):
        name = item.get("name")
        zone = item.get("zone")
        bbox = item.get("bbox")
        if name and zone in ("TRAY", "HANDS", "FIELD"):
            observation[name] = {"zone": zone, "bbox": bbox}
    return observation


def get_mem_footprint_mb():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, check=True,
        )
        return float(out.stdout.strip().splitlines()[0])
    except Exception:
        return 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video_path")
    ap.add_argument("--fps", type=float, default=1.0)
    ap.add_argument("--out", default="trace.json")
    ap.add_argument("--single-frame-test", action="store_true",
                     help="Only process frame 1 and print raw + parsed output, then exit.")
    args = ap.parse_args()

    video_path = Path(args.video_path)
    frames_dir = Path("/tmp/vss_adapter_frames")
    print(f"Extracting frames at {args.fps} fps...")
    frames = extract_frames(video_path, frames_dir, fps=args.fps)
    print(f"Extracted {len(frames)} frames.")

    if args.single_frame_test:
        raw, latency = call_vss_on_frame(frames[0])
        print("=== raw VSS response ===")
        print(json.dumps(raw, indent=2)[:3000])
        print(f"=== latency: {latency:.1f} ms ===")
        obs = parse_items(raw)
        print("=== parsed observation ===")
        print(json.dumps(obs, indent=2))
        return

    engine = ChronoStateEngine()
    trace = []
    for i, frame_path in enumerate(frames):
        try:
            raw, latency = call_vss_on_frame(frame_path)
            obs = parse_items(raw)
            endpoint_ok = True
        except Exception as e:
            print(f"frame {i}: VSS call failed: {e}", file=sys.stderr)
            obs = {}
            latency = 0.0
            endpoint_ok = False

        mem = get_mem_footprint_mb()
        record = engine.step(i, obs, endpoint_ok=endpoint_ok,
                              latency_ms=latency, mem_footprint_mb=mem)
        trace.append(record)
        print(f"frame {i}: {len(obs)} items, "
              f"alert={record['alert']}, "
              f"endpoint_ok={endpoint_ok}, "
              f"latency={record['latency_ms']}ms")

    with open(args.out, "w") as f:
        json.dump(trace, f, indent=2)
    print(f"\nWrote {len(trace)} frame records to {args.out}")


if __name__ == "__main__":
    main()
