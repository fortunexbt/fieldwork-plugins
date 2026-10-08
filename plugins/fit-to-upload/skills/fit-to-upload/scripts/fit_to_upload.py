#!/usr/bin/env python3
"""Probe, budget, and safely encode a local video for an upload byte cap."""

from __future__ import annotations

import argparse
import filecmp
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from decimal import Decimal, DivisionByZero, InvalidOperation, Overflow
from pathlib import Path
from typing import Any


TOOL = "fit-to-upload"
SCHEMA_VERSION = 1
MIN_VIDEO_BITRATE = 20_000
MAX_SIZE_RETRIES = 3
DEFAULT_AAC_BITRATE = 96_000
UNKNOWN_COPY_AUDIO_BITRATE = 128_000


class ToolError(Exception):
    def __init__(self, code: str, message: str, *, recommendation: str | None = None):
        super().__init__(message)
        self.code = code
        self.recommendation = recommendation


class Cancelled(Exception):
    pass


def envelope(status: str, summary: str, data: dict[str, Any] | None = None,
             artifacts: list[dict[str, str]] | None = None,
             warnings: list[str] | None = None) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL,
        "status": status,
        "summary": summary,
        "data": data or {},
        "artifacts": artifacts or [],
        "warnings": warnings or [],
    }


def emit(result: dict[str, Any]) -> None:
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        emit(envelope("error", "Invalid command arguments.", {"error_code": "invalid_arguments", "message": message}))
        raise SystemExit(2)


def positive_decimal(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("must be a positive decimal number") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive decimal number")
    return parsed


def positive_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive whole number of bytes") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive whole number of bytes")
    return parsed


def add_cap_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--max-bytes", type=positive_integer, help="Exact maximum output size in bytes.")
    group.add_argument("--max-mb", type=positive_decimal, help="Maximum size in decimal MB (1 MB = 1,000,000 bytes).")
    group.add_argument("--max-mib", type=positive_decimal, help="Maximum size in binary MiB (1 MiB = 1,048,576 bytes).")


def parser_for_cli() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(
        prog="fit-to-upload",
        description="Inspect a local video, preview a byte budget, and create a verified MP4 under a size cap.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    probe = subparsers.add_parser("probe", help="Inspect one explicitly selected media file.")
    probe.add_argument("--input", required=True, help="Local media file to inspect.")

    for name, help_text in (
        ("plan", "Preview the exact size budget and fidelity choices without writing files."),
        ("encode", "Encode or copy one selected file, verify it, and write a receipt."),
    ):
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument("--input", required=True, help="Local input video; it is never modified.")
        command.add_argument("--output", required=True, help="New .mp4 destination. Existing paths are refused.")
        add_cap_arguments(command)
        command.add_argument("--audio", choices=("copy", "aac", "drop"), default="copy",
                             help="Copy every audio track when possible, encode it as AAC, or omit audio.")
        if name == "encode":
            command.add_argument("--receipt", help="Optional new receipt path; defaults to OUTPUT.receipt.json.")
    return parser


def cap_bytes(args: argparse.Namespace) -> tuple[int, str]:
    if args.max_bytes is not None:
        return args.max_bytes, "bytes"
    if args.max_mb is not None:
        return int(args.max_mb * Decimal(1_000_000)), "MB"
    return int(args.max_mib * Decimal(1_048_576)), "MiB"


def executable(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise ToolError(f"{name}_missing", f"Required executable '{name}' was not found on PATH.")
    return path


def require_h264_encoder(ffmpeg: str) -> None:
    try:
        completed = subprocess.run([ffmpeg, "-hide_banner", "-h", "encoder=libx264"],
                                   capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ToolError("h264_encoder_unavailable", "Could not check whether ffmpeg provides the required libx264 encoder.") from exc
    if completed.returncode != 0 or "Encoder libx264" not in (completed.stdout + completed.stderr):
        raise ToolError("h264_encoder_unavailable", "This ffmpeg build does not provide the libx264 encoder needed for MP4 output.")


def selected_file(value: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.exists() or not path.is_file():
        raise ToolError("input_not_found", f"Input file does not exist or is not a regular file: {path}")
    return path


def json_number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def integer(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None


def probe_media(path: Path, ffprobe: str) -> dict[str, Any]:
    command = [
        ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)
    except FileNotFoundError as exc:
        raise ToolError("ffprobe_missing", "ffprobe could not be started.") from exc
    except subprocess.TimeoutExpired as exc:
        raise ToolError("probe_timeout", "ffprobe did not finish within 60 seconds.") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip()[-1500:] or "ffprobe returned an error without details."
        raise ToolError("probe_failed", f"Could not read media metadata: {detail}")
    try:
        raw = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ToolError("probe_failed", "ffprobe returned invalid JSON metadata.") from exc

    raw_streams = raw.get("streams")
    if not isinstance(raw_streams, list):
        raw_streams = []
    streams: list[dict[str, Any]] = []
    for stream in raw_streams:
        if not isinstance(stream, dict):
            continue
        if stream.get("codec_type") not in ("video", "audio", "subtitle", "data", "attachment"):
            continue
        item: dict[str, Any] = {
            "index": integer(stream.get("index")),
            "type": stream.get("codec_type"),
            "codec": stream.get("codec_name"),
        }
        if stream.get("codec_type") == "video":
            item["width"] = integer(stream.get("width"))
            item["height"] = integer(stream.get("height"))
            item["frame_rate"] = stream.get("avg_frame_rate")
            item["pixel_format"] = stream.get("pix_fmt")
        if stream.get("codec_type") == "audio":
            item["sample_rate"] = integer(stream.get("sample_rate"))
            item["channels"] = integer(stream.get("channels"))
        bitrate = integer(stream.get("bit_rate"))
        if bitrate:
            item["bitrate_bps"] = bitrate
        stream_duration = json_number(stream.get("duration"))
        if stream_duration is not None:
            item["duration_seconds"] = stream_duration
        streams.append(item)

    raw_format = raw.get("format") if isinstance(raw.get("format"), dict) else {}
    duration = json_number(raw_format.get("duration"))
    if duration is None:
        durations = [item.get("duration_seconds") for item in streams if item.get("duration_seconds") is not None]
        duration = max(durations) if durations else None
    format_info = {
        "name": raw_format.get("format_name"),
        "long_name": raw_format.get("format_long_name"),
        "duration_seconds": duration,
    }
    return {"size_bytes": path.stat().st_size, "duration_seconds": duration, "streams": streams,
            "format": format_info}


def _first_video(info: dict[str, Any]) -> dict[str, Any] | None:
    return next((stream for stream in info["streams"] if stream.get("type") == "video"), None)


def _audio_streams(info: dict[str, Any]) -> list[dict[str, Any]]:
    return [stream for stream in info["streams"] if stream.get("type") == "audio"]


def output_paths(args: argparse.Namespace) -> tuple[Path, Path]:
    output = Path(args.output).expanduser().absolute()
    if output.suffix.lower() != ".mp4":
        raise ToolError("unsupported_output", "The verified encode currently writes MP4 files; use an output path ending in .mp4.")
    if not output.parent.is_dir():
        raise ToolError("output_parent_missing", f"Output directory does not exist: {output.parent}")
    receipt = Path(args.receipt).expanduser().absolute() if getattr(args, "receipt", None) else Path(str(output) + ".receipt.json")
    if not receipt.parent.is_dir():
        raise ToolError("receipt_parent_missing", f"Receipt directory does not exist: {receipt.parent}")
    if receipt == output:
        raise ToolError("receipt_conflict", "The receipt path must be different from the video output path.")
    try:
        if receipt.resolve() == Path(args.input).expanduser().resolve():
            raise ToolError("receipt_conflict", "The receipt path would overwrite the original input file.")
    except OSError:
        pass
    if output.resolve() == Path(args.input).expanduser().resolve():
        raise ToolError("output_conflict", "The output path resolves to the original input file.")
    return output, receipt


def _audio_rate(info: dict[str, Any], mode: str) -> int:
    tracks = _audio_streams(info)
    if mode == "drop" or not tracks:
        return 0
    if mode == "aac":
        return DEFAULT_AAC_BITRATE * len(tracks)
    return sum(stream.get("bitrate_bps", UNKNOWN_COPY_AUDIO_BITRATE) for stream in tracks)


def _target_bitrate(info: dict[str, Any], cap: int, audio_mode: str) -> int | None:
    duration = info.get("duration_seconds")
    if not duration or duration <= 0:
        return None
    overhead = max(2048, min(65536, int(cap * 0.025)))
    mux_budget = max(0, cap - overhead)
    try:
        total_bps = Decimal(mux_budget * 8) / Decimal(str(duration))
        return int((total_bps - Decimal(_audio_rate(info, audio_mode))) * Decimal("0.90"))
    except (DivisionByZero, InvalidOperation, Overflow, ValueError):
        return None


def _plan_data(input_path: Path, output: Path, receipt: Path, info: dict[str, Any], cap: int,
               cap_unit: str, audio_mode: str) -> tuple[dict[str, Any], list[str], str | None]:
    fits_under_cap = info["size_bytes"] <= cap
    is_mp4_source = input_path.suffix.lower() == ".mp4" and "mp4" in str(info["format"].get("name") or "").split(",")
    fits = fits_under_cap and is_mp4_source and audio_mode == "copy"
    warnings: list[str] = []
    video = _first_video(info)
    audio_count = len(_audio_streams(info))
    other_count = sum(stream.get("type") not in ("video", "audio") for stream in info["streams"])
    if other_count:
        warnings.append("The MP4 encode retains the first video and all audio tracks; subtitle, data, and attachment tracks are omitted.")
    if audio_mode == "copy" and audio_count:
        warnings.append("Audio is copied when re-encoding; some input audio codecs may not mux into MP4. Retry with --audio aac if ffmpeg rejects the copy.")
    if fits:
        return ({"input": str(input_path), "output": str(output), "receipt": str(receipt),
                 "source_bytes": info["size_bytes"], "max_bytes": cap, "cap_unit": cap_unit,
                 "source_under_cap": True, "already_fits": True, "action": "copy unchanged", "video_bitrate_bps": None,
                 "audio_choice": "preserve all streams unchanged", "duration_seconds": info["duration_seconds"],
                 "source_streams": info["streams"]}, warnings, None)
    if fits_under_cap and not is_mp4_source:
        warnings.append("The source is under the byte cap but is not already an MP4, so it will be converted to the requested MP4 output.")
    if fits_under_cap and is_mp4_source and audio_mode != "copy":
        warnings.append(f"The requested --audio {audio_mode} choice will be applied; video will be re-encoded at the original dimensions.")
    if video is None:
        warnings.append("This file has no video stream to re-encode. Choose a larger cap or use a media tool that supports audio-only compression.")
        return ({"input": str(input_path), "output": str(output), "receipt": str(receipt),
                 "source_bytes": info["size_bytes"], "max_bytes": cap, "cap_unit": cap_unit,
                 "source_under_cap": fits_under_cap, "already_fits": False, "action": "cannot re-encode audio-only media", "video_bitrate_bps": None,
                 "audio_choice": audio_mode, "duration_seconds": info["duration_seconds"],
                 "source_streams": info["streams"]}, warnings, "video_required")
    target = _target_bitrate(info, cap, audio_mode)
    if not info.get("duration_seconds") or info["duration_seconds"] <= 0:
        warnings.append("The duration could not be determined, so a size budget cannot be calculated safely.")
        impossible = "duration_unknown"
    elif target is None or target < MIN_VIDEO_BITRATE:
        warnings.append("The cap leaves less than the minimum 20 kb/s video budget after audio and container allowance. Try --audio aac, --audio drop, or a larger cap.")
        impossible = "cap_impossible"
    else:
        impossible = None
    data = {"input": str(input_path), "output": str(output), "receipt": str(receipt),
            "source_bytes": info["size_bytes"], "max_bytes": cap, "cap_unit": cap_unit,
            "source_under_cap": fits_under_cap, "already_fits": False, "action": "two-pass H.264 encode", "video_bitrate_bps": target,
            "audio_choice": audio_mode, "duration_seconds": info["duration_seconds"],
            "dimensions_preserved": {"width": video.get("width"), "height": video.get("height")},
            "source_streams": info["streams"], "container_allowance_bytes": max(2048, min(65536, int(cap * 0.025)))}
    return data, warnings, impossible


def _parse_time_seconds(value: str) -> float | None:
    try:
        hours, minutes, seconds = value.split(":", 2)
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except (ValueError, TypeError):
        return None


def run_ffmpeg(command: list[str], duration: float, phase: str) -> None:
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1)
    except OSError as exc:
        raise ToolError("ffmpeg_start_failed", f"Could not start ffmpeg: {exc}") from exc
    except KeyboardInterrupt as exc:
        raise Cancelled() from exc
    print(f"fit-to-upload: {phase} started", file=sys.stderr)

    last_percent = -10
    details: list[str] = []
    progress: dict[str, str] = {}
    try:
        assert process.stderr is not None
        for line in process.stderr:
            stripped = line.strip()
            if "=" in stripped:
                key, value = stripped.split("=", 1)
                if key in ("out_time", "out_time_ms", "progress"):
                    progress[key] = value
                if key == "progress":
                    seconds = _parse_time_seconds(progress.get("out_time", ""))
                    if seconds is not None and duration > 0:
                        percent = min(99, int(100 * seconds / duration))
                        if percent >= last_percent + 10:
                            print(f"fit-to-upload: {phase} {percent}%", file=sys.stderr)
                            last_percent = percent
                    progress.clear()
                continue
            if stripped:
                details.append(stripped)
                details = details[-20:]
                print(f"fit-to-upload: ffmpeg: {stripped}", file=sys.stderr)
        return_code = process.wait()
    except KeyboardInterrupt as exc:
        if process.poll() is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            process.wait()
        raise Cancelled() from exc
    except Exception:
        if process.poll() is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            process.wait()
        raise
    if return_code != 0:
        detail = " | ".join(details[-8:]) or f"ffmpeg exited with code {return_code}"
        recommendation = "Try --audio aac if the copied audio codec is not supported in MP4." if "copy" in command else None
        raise ToolError("ffmpeg_failed", f"ffmpeg failed during {phase}: {detail}", recommendation=recommendation)
    print(f"fit-to-upload: {phase} complete", file=sys.stderr)


def _ffmpeg_command(ffmpeg: str, input_path: Path, output_path: Path, passlog: Path,
                    bitrate: int, pass_number: int, audio_mode: str) -> list[str]:
    command = [ffmpeg, "-hide_banner", "-v", "error", "-nostats", "-progress", "pipe:2", "-i", str(input_path),
               "-map", "0:v:0", "-sn", "-dn", "-c:v", "libx264", "-preset", "veryfast",
               "-b:v", str(bitrate), "-maxrate", str(max(bitrate, int(bitrate * 1.25))),
               "-bufsize", str(max(bitrate * 2, 40_000)), "-pix_fmt", "yuv420p",
               "-pass", str(pass_number), "-passlogfile", str(passlog)]
    if pass_number == 1:
        command.extend(["-an", "-f", "null", os.devnull])
        return command
    if audio_mode == "copy":
        command.extend(["-map", "0:a?", "-c:a", "copy"])
    elif audio_mode == "aac":
        command.extend(["-map", "0:a?", "-c:a", "aac", "-b:a", str(DEFAULT_AAC_BITRATE)])
    else:
        command.append("-an")
    command.extend(["-movflags", "+faststart", "-f", "mp4", str(output_path)])
    return command


def _fps(video: dict[str, Any] | None) -> float:
    if not video:
        return 30.0
    raw = video.get("frame_rate")
    if isinstance(raw, str) and "/" in raw:
        try:
            numerator, denominator = raw.split("/", 1)
            value = float(numerator) / float(denominator)
            if math.isfinite(value) and value > 0:
                return value
        except (ValueError, ZeroDivisionError):
            pass
    return 30.0


def verify_encoded(input_info: dict[str, Any], output_info: dict[str, Any], audio_mode: str,
                   cap: int, *, video_reencoded: bool = True) -> None:
    if output_info["size_bytes"] <= 0:
        raise ToolError("output_verification_failed", "ffmpeg produced an empty output file.")
    if output_info["size_bytes"] > cap:
        raise ToolError("cap_not_met", f"Output is {output_info['size_bytes']} bytes, above the {cap}-byte cap.")
    source_video = _first_video(input_info)
    result_video = _first_video(output_info)
    if not source_video or not result_video:
        raise ToolError("output_verification_failed", "The output is missing its video stream.")
    if source_video.get("width") != result_video.get("width") or source_video.get("height") != result_video.get("height"):
        raise ToolError("output_verification_failed", "The encoded video dimensions changed unexpectedly.")
    if video_reencoded and result_video.get("codec") != "h264":
        raise ToolError("output_verification_failed", "The MP4 output does not contain the expected H.264 video stream.")
    if not video_reencoded and result_video.get("codec") != source_video.get("codec"):
        raise ToolError("output_verification_failed", "The unchanged video codec does not match the source.")
    source_audio = _audio_streams(input_info)
    result_audio = _audio_streams(output_info)
    expected_audio = 0 if audio_mode == "drop" else len(source_audio)
    if len(result_audio) != expected_audio:
        raise ToolError("output_verification_failed", f"Expected {expected_audio} audio streams, found {len(result_audio)}.")
    if audio_mode == "copy" and [s.get("codec") for s in result_audio] != [s.get("codec") for s in source_audio]:
        raise ToolError("output_verification_failed", "Copied audio codec evidence does not match the source streams.")
    if audio_mode == "copy" and [(s.get("sample_rate"), s.get("channels")) for s in result_audio] != [
        (s.get("sample_rate"), s.get("channels")) for s in source_audio
    ]:
        raise ToolError("output_verification_failed", "Copied audio sample rate or channel count does not match the source streams.")
    if audio_mode == "aac" and any(stream.get("codec") != "aac" for stream in result_audio):
        raise ToolError("output_verification_failed", "The requested AAC audio encode was not produced.")
    source_duration = input_info.get("duration_seconds")
    result_duration = output_info.get("duration_seconds")
    if source_duration is None or result_duration is None:
        raise ToolError("output_verification_failed", "Duration could not be verified for the source and output.")
    tolerance = max(0.15, 2.0 / _fps(source_video))
    if abs(source_duration - result_duration) > tolerance:
        raise ToolError("output_verification_failed", f"Duration changed from {source_duration:.3f}s to {result_duration:.3f}s.")


def _commit_new_file(staged: Path, destination: Path, label: str) -> None:
    try:
        os.link(staged, destination)
    except FileExistsError as exc:
        raise ToolError("output_exists", f"Refusing to overwrite existing {label}: {destination}") from exc
    except OSError as exc:
        raise ToolError("atomic_commit_failed", f"Could not atomically publish {label} at {destination}: {exc}") from exc


def encode(args: argparse.Namespace) -> tuple[dict[str, Any], list[dict[str, str]]]:
    input_path = selected_file(args.input)
    output, receipt = output_paths(args)
    if os.path.lexists(output):
        raise ToolError("output_exists", f"Refusing to overwrite existing output: {output}")
    if os.path.lexists(receipt):
        raise ToolError("receipt_exists", f"Refusing to overwrite existing receipt: {receipt}")
    ffmpeg, ffprobe = executable("ffmpeg"), executable("ffprobe")
    source_info = probe_media(input_path, ffprobe)
    cap, cap_unit = cap_bytes(args)
    plan, warnings, impossible = _plan_data(input_path, output, receipt, source_info, cap, cap_unit, args.audio)
    if impossible:
        raise ToolError(impossible, warnings[-1] if warnings else "The selected media cannot fit this cap.",
                        recommendation="Try --audio aac, --audio drop, or choose a larger cap.")
    if not plan["already_fits"]:
        require_h264_encoder(ffmpeg)

    duration = source_info.get("duration_seconds") or 0.0
    with tempfile.TemporaryDirectory(prefix=f".{output.name}.fit-to-upload-", dir=str(output.parent)) as temporary:
        temp_dir = Path(temporary)
        staged_output = temp_dir / output.name
        if plan["already_fits"]:
            print("fit-to-upload: source already meets the cap; copying without re-encoding", file=sys.stderr)
            try:
                shutil.copy2(input_path, staged_output)
            except OSError as exc:
                raise ToolError("staging_failed", f"Could not stage the unchanged source: {exc}") from exc
            output_info = probe_media(staged_output, ffprobe)
            if staged_output.stat().st_size != source_info["size_bytes"]:
                raise ToolError("output_verification_failed", "The unchanged copy has a different byte count from the source.")
            if not filecmp.cmp(staged_output, input_path, shallow=False):
                raise ToolError("output_verification_failed", "The unchanged copy differs from the source bytes.")
            operation = "copy"
            actual_video_bitrate = None
            verify_encoded(source_info, output_info, "copy", cap, video_reencoded=False) if _first_video(source_info) else None
        else:
            target = plan["video_bitrate_bps"]
            assert isinstance(target, int)
            for attempt in range(MAX_SIZE_RETRIES + 1):
                passlog = temp_dir / f"pass-{attempt + 1}"
                staged_output.unlink(missing_ok=True)
                run_ffmpeg(_ffmpeg_command(ffmpeg, input_path, staged_output, passlog, target, 1, args.audio), duration, f"pass 1/2, attempt {attempt + 1}")
                run_ffmpeg(_ffmpeg_command(ffmpeg, input_path, staged_output, passlog, target, 2, args.audio), duration, f"pass 2/2, attempt {attempt + 1}")
                if not staged_output.is_file():
                    raise ToolError("ffmpeg_failed", "ffmpeg finished without creating the expected MP4 output.")
                actual_bytes = staged_output.stat().st_size
                if actual_bytes <= cap:
                    output_info = probe_media(staged_output, ffprobe)
                    verify_encoded(source_info, output_info, args.audio, cap)
                    actual_video_bitrate = target
                    operation = "two_pass_h264"
                    break
                if attempt >= MAX_SIZE_RETRIES:
                    raise ToolError("cap_not_met", f"After {MAX_SIZE_RETRIES + 1} encodes the output is still {actual_bytes} bytes, above the {cap}-byte cap.",
                                    recommendation="Try --audio aac or --audio drop, or choose a larger cap.")
                revised = int(target * cap / actual_bytes * 0.90)
                print(f"fit-to-upload: {actual_bytes} bytes exceeds {cap}; retrying at {revised} b/s based on measured output size", file=sys.stderr)
                if revised < MIN_VIDEO_BITRATE:
                    raise ToolError("cap_not_met", f"Measured output requires less than the minimum {MIN_VIDEO_BITRATE} b/s video budget.",
                                    recommendation="Try --audio aac, --audio drop, or choose a larger cap.")
                target = revised
            else:
                raise ToolError("cap_not_met", "The byte cap was not met within the bounded retry count.")

        fidelity = {
            "video": "copied unchanged because the source already fits" if operation == "copy" else "re-encoded as H.264; source dimensions retained; visual quality may decrease",
            "audio": "copied unchanged with the source file" if operation == "copy" else ("copied every source audio track" if args.audio == "copy" else ("encoded every source audio track as AAC" if args.audio == "aac" else "audio omitted by request")),
            "duration": "verified within two source frames or 150 ms",
            "subtitles_and_attachments": "copied unchanged" if operation == "copy" else "not retained by the MP4 encode",
        }
        result_data = {
            "input": str(input_path), "output": str(output), "receipt": str(receipt),
            "operation": operation, "source_bytes": source_info["size_bytes"],
            "actual_bytes": output_info["size_bytes"], "max_bytes": cap,
            "duration_seconds": output_info["duration_seconds"], "streams": output_info["streams"],
            "fidelity": fidelity, "video_bitrate_bps": actual_video_bitrate,
            "validation": {"within_cap": output_info["size_bytes"] <= cap,
                           "duration_preserved": True, "dimensions_preserved": _first_video(source_info) is None or (
                               _first_video(source_info).get("width") == _first_video(output_info).get("width") and
                               _first_video(source_info).get("height") == _first_video(output_info).get("height")),
                           "audio_stream_count": len(_audio_streams(output_info))},
            "created_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        result = envelope("ok", f"Verified {output_info['size_bytes']} byte MP4 under the {cap}-byte cap.", result_data,
                          [{"path": str(output), "label": "Encoded MP4", "media_type": "video/mp4"},
                           {"path": str(receipt), "label": "Verification receipt", "media_type": "application/json"}], warnings)
        receipt_fd, receipt_temp_name = tempfile.mkstemp(prefix=f".{receipt.name}.", suffix=".tmp", dir=str(receipt.parent))
        receipt_temp = Path(receipt_temp_name)
        output_committed = False
        try:
            with os.fdopen(receipt_fd, "w", encoding="utf-8") as receipt_file:
                receipt_file.write(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + "\n")
            _commit_new_file(staged_output, output, "output")
            output_committed = True
            _commit_new_file(receipt_temp, receipt, "receipt")
        except ToolError:
            if output_committed:
                try:
                    output.unlink()
                except OSError:
                    pass
            raise
        finally:
            receipt_temp.unlink(missing_ok=True)
    return result, result["artifacts"]


def run(args: argparse.Namespace) -> int:
    try:
        if args.command == "probe":
            path = selected_file(args.input)
            info = probe_media(path, executable("ffprobe"))
            emit(envelope("ok", f"Inspected {len(info['streams'])} media stream(s) in a {info['size_bytes']} byte file.",
                          {"input": str(path), **info}))
            return 0

        if args.command == "plan":
            input_path = selected_file(args.input)
            output, receipt = output_paths(args)
            ffprobe = executable("ffprobe")
            info = probe_media(input_path, ffprobe)
            cap, unit = cap_bytes(args)
            data, warnings, impossible = _plan_data(input_path, output, receipt, info, cap, unit, args.audio)
            if not impossible and not data["already_fits"]:
                require_h264_encoder(executable("ffmpeg"))
            if os.path.lexists(output):
                warnings.append("The requested output path already exists; choose a new destination before encoding.")
            if os.path.lexists(receipt):
                warnings.append("The receipt path already exists; choose a new receipt path before encoding.")
            status = "needs_attention" if impossible or os.path.lexists(output) or os.path.lexists(receipt) else "ok"
            summary = "The source already meets the cap and can be copied unchanged." if data["already_fits"] else "Previewed the H.264 bitrate budget without writing files."
            if impossible:
                summary = warnings[-1] if warnings else "The requested cap cannot be met with this media and audio choice."
                data["error_code"] = impossible
                data["recommendation"] = "Try --audio aac, --audio drop, or choose a larger cap."
            emit(envelope(status, summary, data, warnings=warnings))
            return 0
        result, _ = encode(args)
        emit(result)
        return 0
    except Cancelled:
        emit(envelope("error", "Encoding was cancelled; temporary files were removed and no output was published.",
                      {"error_code": "cancelled"}))
        return 1
    except ToolError as exc:
        data: dict[str, Any] = {"error_code": exc.code}
        if exc.recommendation:
            data["recommendation"] = exc.recommendation
        emit(envelope("error", str(exc), data, warnings=[exc.recommendation] if exc.recommendation else []))
        return 1
    except OSError as exc:
        emit(envelope("error", f"File operation failed: {exc}", {"error_code": "file_operation_failed"}))
        return 1


def main() -> int:
    args = parser_for_cli().parse_args()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
