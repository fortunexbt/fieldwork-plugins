---
name: fit-to-upload
description: Inspect one local video, preview an exact byte cap, and create a verified MP4 with duration and audio checks.
---

# Fit to Upload

Use this local CLI to inspect one selected video, preview a total byte budget, and create a verified MP4. It needs Python 3.11+, `ffprobe`, and `ffmpeg` with the `libx264` encoder for re-encoding. The input is explicit; the tool never searches folders.

Resolve the installed directory containing this `SKILL.md` and use its `scripts/fit_to_upload.py`; do not resolve the command from the current working directory. Set `CLI` below to that absolute path:

```sh
CLI="<installed-skill-directory>/scripts/fit_to_upload.py"
python3 "$CLI" probe --input clip.mov
python3 "$CLI" plan --input clip.mov --output upload.mp4 --max-mb 25
python3 "$CLI" encode --input clip.mov --output upload.mp4 --max-mb 25
```

Review the plan before encoding. `--max-bytes` is exact bytes; decimal `--max-mb` uses 1 MB = 1,000,000 bytes; binary `--max-mib` uses 1 MiB = 1,048,576 bytes. The cap covers the complete output file, including audio and container overhead. A fitting MP4 with the default `--audio copy` is copied unchanged. Otherwise the first video stream is re-encoded as H.264 at its existing dimensions. Audio tracks are copied by default; use `--audio aac` when MP4 cannot carry the source codec or when copied audio leaves too little size budget, and `--audio drop` only when omitting audio is acceptable. An explicit audio override also re-encodes video, and the plan reports that choice. Video re-encoding can reduce visual quality. Re-encoded output retains all audio tracks but omits subtitle, data, and attachment tracks. Originals stay in place; existing output and receipt paths are refused. A successful encode writes `OUTPUT.receipt.json` with actual size, stream metadata, duration, and fidelity checks.

For a tiny synthetic local sample, generate test bars and a tone, then use the commands above with `demo.mp4` as input:

```sh
ffmpeg -hide_banner -f lavfi -i testsrc2=size=320x180:rate=15:duration=2 -f lavfi -i sine=frequency=880:sample_rate=44100:duration=2 -shortest -c:v libx264 -preset ultrafast -crf 20 -pix_fmt yuv420p -c:a aac -b:a 96k demo.mp4
```
