"""
vss_client.py

Real backend client: sends an uploaded video straight to the Cosmos3 VLM
NIM (nvidia/cosmos3-nano-reasoner), running locally as part of the VSS
"base" profile deployment, and parses its structured tray-inventory
response.
"""

import base64
import json
import mimetypes
import shutil
import subprocess
import tempfile
import time

import requests

from tray_config import (
    MODEL_NAME,
    SYSTEM_PROMPT,
    USER_PROMPT,
    VLM_ENDPOINT,
    WINDOW_USER_PROMPT,
)

MAX_VIDEO_BYTES = 7_000_000  # ~7MB raw -> stays under the NIM's base64 url length cap
COMPRESSION_PASSES = [
    # (scale_width, fps, crf) — tried in order until output fits MAX_VIDEO_BYTES
    (960, 15, 30),
    (640, 12, 32),
    (480, 10, 34),
    (360, 8, 36),
]


class VSSClientError(Exception):
    pass


def _reencode(src_path: str, scale_w: int, fps: int, crf: int, extra_args=None) -> bytes:
    """Runs one ffmpeg re-encode pass on src_path, returning the output bytes."""
    with tempfile.NamedTemporaryFile(suffix=".mp4") as dst:
        cmd = ["ffmpeg", "-y"] + (extra_args or []) + [
            "-i", src_path,
            "-vf", f"scale={scale_w}:-2",
            "-r", str(fps),
            "-c:v", "libx264", "-crf", str(crf), "-preset", "fast",
            "-an",
            dst.name,
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=120)
        if proc.returncode != 0:
            return b""
        return dst.read()


def _compress_video(file_bytes: bytes, filename: str) -> bytes:
    """Re-encodes an oversized video with ffmpeg until it fits MAX_VIDEO_BYTES.

    Tries progressively more aggressive resolution/fps/CRF settings. Raises
    VSSClientError if ffmpeg is missing or the video still doesn't fit after
    the most aggressive pass.
    """
    if shutil.which("ffmpeg") is None:
        raise VSSClientError(
            "Video is too large to send inline and ffmpeg is not installed "
            "to auto-compress it. Install ffmpeg or trim the clip manually."
        )

    suffix = "." + filename.rsplit(".", 1)[-1] if "." in filename else ".mp4"
    with tempfile.NamedTemporaryFile(suffix=suffix) as src:
        src.write(file_bytes)
        src.flush()

        for scale_w, fps, crf in COMPRESSION_PASSES:
            compressed = _reencode(src.name, scale_w, fps, crf)
            if compressed and len(compressed) <= MAX_VIDEO_BYTES:
                return compressed

    raise VSSClientError(
        "Video is still too large to send after maximum compression. "
        "Trim the clip to a shorter duration and retry."
    )


def probe_duration_seconds(file_bytes: bytes, filename: str) -> float:
    """Returns the video's duration in seconds via ffprobe."""
    if shutil.which("ffprobe") is None:
        raise VSSClientError("ffprobe is not installed; cannot determine video duration.")

    suffix = "." + filename.rsplit(".", 1)[-1] if "." in filename else ".mp4"
    with tempfile.NamedTemporaryFile(suffix=suffix) as src:
        src.write(file_bytes)
        src.flush()
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            src.name,
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=30)
        if proc.returncode != 0:
            raise VSSClientError(f"ffprobe failed: {proc.stderr.decode(errors='ignore')[:300]}")
        try:
            return float(proc.stdout.decode().strip())
        except ValueError:
            raise VSSClientError(f"Could not parse duration from ffprobe output: {proc.stdout!r}")


def trim_and_encode(file_bytes: bytes, filename: str, start_s: float, duration_s: float) -> bytes:
    """Extracts [start_s, start_s+duration_s) from the video and re-encodes it,
    escalating through COMPRESSION_PASSES only if the short clip is still too big.
    """
    if shutil.which("ffmpeg") is None:
        raise VSSClientError("ffmpeg is not installed; cannot trim video windows.")

    suffix = "." + filename.rsplit(".", 1)[-1] if "." in filename else ".mp4"
    with tempfile.NamedTemporaryFile(suffix=suffix) as src:
        src.write(file_bytes)
        src.flush()

        seek_args = ["-ss", str(start_s), "-t", str(duration_s)]
        for scale_w, fps, crf in COMPRESSION_PASSES:
            clip = _reencode(src.name, scale_w, fps, crf, extra_args=seek_args)
            if clip and len(clip) <= MAX_VIDEO_BYTES:
                return clip

    raise VSSClientError(
        f"Could not encode window [{start_s:.0f}s, {start_s + duration_s:.0f}s) "
        "under the size cap even at maximum compression."
    )


def check_vlm_ready(timeout=3):
    try:
        r = requests.get(VLM_ENDPOINT.replace("/chat/completions", "/models"), timeout=timeout)
        return r.status_code == 200
    except requests.RequestException:
        return False


def _strip_json_fence(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    return text.strip()


def _call_vlm(file_bytes: bytes, filename: str, user_prompt: str):
    """Shared VLM-call body: base64-inlines the clip and posts to the NIM.

    Returns {"objects", "notes", "usage", "latency_s", "raw_content"}.
    Raises VSSClientError on failure (bad response, HTTP error).
    Caller is responsible for size-checking/compressing file_bytes first.
    """
    mime_type, _ = mimetypes.guess_type(filename)
    mime_type = mime_type or "video/mp4"
    encoded = base64.b64encode(file_bytes).decode()
    data_url = f"data:{mime_type};base64,{encoded}"

    body = {
        "model": MODEL_NAME,
        "temperature": 0.0,
        "max_tokens": 1024,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_prompt},
                    {"type": "video_url", "video_url": {"url": data_url}},
                ],
            },
        ],
    }

    t0 = time.time()
    try:
        r = requests.post(VLM_ENDPOINT, json=body, timeout=180)
    except requests.RequestException as e:
        raise VSSClientError(f"Could not reach VLM at {VLM_ENDPOINT}: {e}")
    latency_s = time.time() - t0

    if r.status_code != 200:
        raise VSSClientError(f"VLM returned HTTP {r.status_code}: {r.text[:500]}")

    payload = r.json()
    try:
        content = payload["choices"][0]["message"]["content"]
        usage = payload.get("usage", {})
    except (KeyError, IndexError):
        raise VSSClientError(f"Unexpected VLM response shape: {json.dumps(payload)[:500]}")

    cleaned = _strip_json_fence(content)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        raise VSSClientError(f"VLM did not return valid JSON:\n{content[:800]}")

    return {
        "objects": parsed.get("objects", []),
        "notes": parsed.get("notes", ""),
        "usage": usage,
        "latency_s": round(latency_s, 1),
        "raw_content": content,
        "system_prompt": SYSTEM_PROMPT,
        "user_prompt": user_prompt,
    }


def analyze_video(file_bytes: bytes, filename: str):
    """
    Sends the whole video to the Cosmos3 VLM in one call and returns the
    parsed result plus a "compressed_note" if auto-compression kicked in.
    Raises VSSClientError on failure (oversized file, bad response, HTTP error).
    """
    compressed_note = None
    if len(file_bytes) > MAX_VIDEO_BYTES:
        original_mb = len(file_bytes) / 1e6
        file_bytes = _compress_video(file_bytes, filename)
        filename = filename.rsplit(".", 1)[0] + ".mp4"
        compressed_note = (
            f"Video auto-compressed from {original_mb:.1f}MB to "
            f"{len(file_bytes)/1e6:.1f}MB before analysis."
        )

    result = _call_vlm(file_bytes, filename, USER_PROMPT)
    result["compressed_note"] = compressed_note
    return result


def analyze_clip(clip_bytes: bytes, filename: str, start_s: float, end_s: float):
    """
    Sends an already-trimmed short clip (from trim_and_encode) to the Cosmos3
    VLM using the window-scoped prompt, and returns the parsed result.
    Raises VSSClientError on failure (oversized clip, bad response, HTTP error).
    """
    if len(clip_bytes) > MAX_VIDEO_BYTES:
        raise VSSClientError(
            f"Encoded window is {len(clip_bytes)/1e6:.1f}MB, still over the "
            f"~{MAX_VIDEO_BYTES/1e6:.0f}MB inline cap after compression."
        )
    prompt = WINDOW_USER_PROMPT.format(duration=end_s - start_s, start=start_s, end=end_s)
    return _call_vlm(clip_bytes, filename, prompt)
