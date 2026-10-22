#!/usr/bin/env python3
"""Record the Fit to Upload sample: a real clip plus real FFmpeg output on it.

A browser cannot host FFmpeg, so the site replays what ffprobe and ffmpeg printed
for this exact file. The clip is five seconds of Big Buck Bunny (c) Blender
Foundation, CC BY 3.0, cut on keyframes with stream copy so every frame is the
original encode. Pass the unzipped 320x180 release from
https://download.blender.org/peach/bigbuckbunny_movies/BigBuckBunny_320x180.mp4.zip
"""
import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "samples" / "fit-to-upload"
CLIP = OUT / "clip.mp4"
SOURCE_SHA256 = "f78f39603e6774907f2faafabf26a667f4a6fc31769ec304a8a8f7c62d280508"
START_SECONDS = "120"
DURATION_SECONDS = "5"


def record(command: list[str]) -> dict[str, object]:
    completed = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
    return {"returncode": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", type=Path, help="BigBuckBunny_320x180.mp4")
    args = parser.parse_args()
    if hashlib.sha256(args.source.read_bytes()).hexdigest() != SOURCE_SHA256:
        parser.error("source is not the published BigBuckBunny_320x180.mp4")

    OUT.mkdir(parents=True, exist_ok=True)
    CLIP.unlink(missing_ok=True)
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-ss", START_SECONDS, "-i", str(args.source), "-t", DURATION_SECONDS,
        "-map", "0", "-c", "copy", "-movflags", "+faststart", str(CLIP),
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
