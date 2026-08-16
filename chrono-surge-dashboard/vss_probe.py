"""
vss_probe.py — one-shot test of the real VSS API pipeline.
Run on the NVIDIA machine: python3 vss_probe.py <path-to-video>
"""
import sys
import json
import requests

BASE = "http://localhost:8000"

def main(video_path):
    filename = video_path.split("/")[-1]

    # 1. Get upload URL
    r = requests.post(f"{BASE}/api/v1/videos", json={"filename": filename})
    print("=== upload URL response ===")
    print(r.status_code, r.text)
    r.raise_for_status()
    upload_url = r.json()["url"]

    # 2. POST the file bytes to the VST upload URL
    with open(video_path, "rb") as f:
        r2 = requests.post(upload_url, files={"file": (filename, f)})
    print("=== chunk upload response ===")
    print(r2.status_code, r2.text[:1000])

    # Try to find a sensor_id in the upload response, or in the URL itself
    sensor_id = None
    try:
        body = r2.json()
        sensor_id = body.get("sensor_id") or body.get("id")
    except Exception:
        pass
    if not sensor_id:
        # sensor id is sometimes embedded in the upload_url path
        sensor_id = upload_url.rstrip("/").split("/")[-1]
    print("Guessed sensor_id:", sensor_id)

    # 3. Mark upload complete
    r3 = requests.post(
        f"{BASE}/api/v1/videos/{sensor_id}/complete",
        json={"filename": filename},
    )
    print("=== complete response ===")
    print(r3.status_code, r3.text)

    # 4. Ask VSS about the video, referencing filename/sensor_id in the prompt
    prompt = (
        f"For the uploaded video '{filename}' (sensor id {sensor_id}), "
        "list every distinct object visible, how many times each appears, "
        "and whether each is present at the end of the video or missing. "
        "Respond as JSON with fields: object_name, count, present_at_end."
    )
    r4 = requests.post(f"{BASE}/generate", json={"input_message": prompt})
    print("=== generate response ===")
    print(r4.status_code, r4.text[:3000])


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python3 vss_probe.py <path-to-video>")
        sys.exit(1)
    main(sys.argv[1])
