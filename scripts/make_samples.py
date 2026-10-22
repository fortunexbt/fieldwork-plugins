#!/usr/bin/env python3
"""Record the sample inputs the browser demos cannot produce for themselves.

Everything else the site runs comes straight from each plugin's committed
assets. Fit to Upload needs a real MP4 plus the output of real FFmpeg binaries,
because a browser cannot host FFmpeg; this script captures both from the
installed tools so the replayed output is a genuine recording.
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "samples" / "fit-to-upload"
CLIP = OUT / "clip.mp4"


def record(command: list[str]) -> dict[str, object]:
    completed = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
    return {"returncode": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    CLIP.unlink(missing_ok=True)
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24:duration=6",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=6",
        "-shortest", "-c:v", "libx264", "-preset", "veryslow", "-crf", "26",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart",
        str(CLIP),
    ], capture_output=True, check=True, timeout=180)

    probe = record(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(CLIP)])
    parsed = json.loads(probe["stdout"])
    parsed["format"]["filename"] = CLIP.name
    probe["stdout"] = json.dumps(parsed, indent=4) + "\n"
    encoder = record(["ffmpeg", "-hide_banner", "-h", "encoder=libx264"])

    (OUT / "recorded-binaries.json").write_text(
        json.dumps({"ffprobe": probe, "ffmpeg": encoder}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"clip_bytes": CLIP.stat().st_size, "ffprobe_bytes": len(probe["stdout"])}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
