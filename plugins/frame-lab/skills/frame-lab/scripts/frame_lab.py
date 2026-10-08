#!/usr/bin/env python3
"""Inspect and compare captures from the MIT Minecraft Frame Bench harness."""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
import os
import platform
import re
import shutil
import statistics
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any

TOOL = "frame-lab"
SCHEMA_VERSION = 1
MAX_CAPTURE_ROWS = 500_000
TAIL_SAMPLE_MINIMUM = 10_000
P95_SAMPLE_MINIMUM = 1_000
MAX_INTERVAL_NS = (1 << 63) - 1
ASM_9_10_1_SHA256 = "ed825d10ab1399c8c0cb669e688cf0c8c82629b4c8399b58352b68e92ca10fcb"
SAFE_RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")
PROFILE_REQUIRED = {
    "schema_version",
    "host_id",
    "minecraft_version",
    "loader",
    "java_major",
    "framebuffer_px",
    "scene_id",
    "settings",
    "dataset_kind",
}
PROFILE_FIELDS = (
    "host_id",
    "minecraft_version",
    "loader",
    "java_major",
    "framebuffer_px",
    "scene_id",
    "settings",
    "dataset_kind",
)
METRIC_SCOPE = "cpu_frame_production"
METRIC_LIMIT = (
    "CPU frame-production intervals, including the frame limiter; not GPU execution, "
    "displayed frames, generated frames, or input latency."
)
REPORT_FPS_SERIES = (
    ("Average FPS", "average_fps", "average"),
    ("Slowest 1% low FPS", "one_percent_low_fps", "tail"),
)


class LabError(Exception):
    """Expected execution error that can be returned without a traceback."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as stream:
        return json.load(stream, parse_constant=_reject_json_constant)


def _json_key(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _finite_number(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False


def _safe_run_id(value: str) -> str:
    if not SAFE_RUN_ID.fullmatch(value):
        raise argparse.ArgumentTypeError("run IDs may contain only letters, digits, underscores, and hyphens")
    return value


def _read_key_values(path: Path, label: str, invalid: list[str]) -> dict[str, str] | None:
    if not path.is_file():
        invalid.append(f"Missing {label} sidecar.")
        return None
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        invalid.append(f"Could not read the {label} sidecar as UTF-8 text.")
        return None
    for line in lines:
        if not line.strip():
            continue
        if "=" not in line:
            invalid.append(f"The {label} sidecar contains a malformed line.")
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in values:
            invalid.append(f"The {label} sidecar has a missing or duplicated field name.")
            continue
        values[key] = value.strip()
    return values


def _parse_int_field(
    values: dict[str, str] | None, key: str, label: str, invalid: list[str], *, minimum: int = 0
) -> int | None:
    if values is None or key not in values:
        invalid.append(f"The {label} sidecar is missing {key}.")
        return None
    try:
        result = int(values[key], 10)
    except (TypeError, ValueError):
        invalid.append(f"The {label} sidecar has a non-integer {key} value.")
        return None
    if result < minimum:
        invalid.append(f"The {label} sidecar has an out-of-range {key} value.")
        return None
    return result


def _parse_seconds(value: str | None, label: str, invalid: list[str]) -> float | None:
    if value is None:
        invalid.append(f"The {label} duration is missing.")
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        invalid.append(f"The {label} duration is not numeric.")
        return None
    if not math.isfinite(seconds) or seconds <= 0 or seconds > 1200:
        invalid.append(f"The {label} duration is non-finite or outside the supported range.")
        return None
    return seconds


def _load_intervals(path: Path, invalid: list[str]) -> tuple[list[int], int, int | None]:
    if not path.is_file():
        invalid.append("Missing frame interval CSV.")
        return [], 0, None

    intervals: list[int] = []
    row_count = 0
    last_nano: int | None = None
    try:
        with path.open("r", encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            fieldnames = reader.fieldnames or []
            required = {"frame", "nanotime", "interval_ns"}
            if not required.issubset(fieldnames):
                invalid.append("The frame CSV does not match the harness columns: frame, nanotime, interval_ns.")
                return [], 0, None

            for row in reader:
                row_count += 1
                if row_count > MAX_CAPTURE_ROWS:
                    invalid.append("The frame CSV exceeds the harness buffer limit and is incomplete.")
                    break
                if None in row:
                    invalid.append("The frame CSV contains an overlong row.")
                    continue
                try:
                    frame = int(row.get("frame", ""), 10)
                    nanotime = int(row.get("nanotime", ""), 10)
                    interval_ns = int(row.get("interval_ns", ""), 10)
                except (TypeError, ValueError):
                    invalid.append("The frame CSV contains a missing or non-integer sample.")
                    continue
                if frame < 0 or nanotime <= 0 or interval_ns < 0 or interval_ns > MAX_INTERVAL_NS:
                    invalid.append("The frame CSV contains an out-of-range sample.")
                    continue
                if frame != row_count - 1:
                    invalid.append("Frame indexes are missing, duplicated, or out of order.")
                if last_nano is not None and nanotime <= last_nano:
                    invalid.append("Frame timestamps are not strictly increasing.")
                last_nano = nanotime
                if row_count == 1:
                    if frame != 0 or interval_ns != 0:
                        invalid.append("The CSV is missing the harness zero-interval anchor row.")
                    if interval_ns > 0:
                        intervals.append(interval_ns)
                    continue
                if interval_ns == 0:
                    invalid.append("A measured frame interval is zero.")
                    continue
                intervals.append(interval_ns)
    except (OSError, UnicodeError, csv.Error):
        invalid.append("Could not read the frame CSV.")
        return [], row_count, None

    if row_count == 0:
        invalid.append("The frame CSV is empty.")
    if not intervals:
        invalid.append("The capture contains no positive frame intervals.")
    return intervals, row_count, last_nano


def _read_route(path: Path, invalid: list[str]) -> dict[str, Any] | None:
    if not path.is_file():
        invalid.append("Missing route completion sidecar.")
        return None
    try:
        route = _load_json(path)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        invalid.append("The route sidecar is malformed or contains a non-finite value.")
        return None
    if not isinstance(route, dict):
        invalid.append("The route sidecar is not a JSON object.")
        return None
    kind = route.get("kind")
    duration = route.get("seconds")
    positions = route.get("positions")
    if not isinstance(kind, str) or not kind.strip():
        invalid.append("The route sidecar is missing its route kind.")
    if not _finite_number(duration) or float(duration) <= 0:
        invalid.append("The route sidecar has no finite positive duration.")
    if not isinstance(positions, list) or not positions:
        invalid.append("The route sidecar has no recorded route positions.")
    elif any(
        not isinstance(sample, dict)
        or not isinstance(sample.get("position"), list)
        or len(sample["position"]) != 3
        or not all(_finite_number(coordinate) for coordinate in sample["position"])
        for sample in positions
    ):
        invalid.append("The route sidecar contains a missing or invalid recorded position.")
    if not isinstance(route.get("world_start"), dict) or not isinstance(route.get("world_end"), dict):
        invalid.append("The route sidecar is missing start or end world state.")
    return route


def _capture_context(path: Path, invalid: list[str], warnings: list[str]) -> tuple[bool, bool | None]:
    if not path.exists():
        warnings.append("No capture context sidecar is present; background CPU and swap preflight cannot be checked.")
        return False, None
    try:
        context = _load_json(path)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        invalid.append("The optional capture context sidecar is malformed or contains a non-finite value.")
        return True, None
    if not isinstance(context, dict):
        invalid.append("The optional capture context sidecar is not a JSON object.")
        return True, None
    gate = context.get("environment_gate")
    if gate is None:
        return True, None
    if not isinstance(gate, dict) or not isinstance(gate.get("passed"), bool):
        invalid.append("The capture context has an incomplete environment preflight result.")
        return True, None
    if gate["passed"] is False:
        invalid.append("The recorded CPU-idle/swap preflight failed.")
    return True, gate["passed"]


def _parse_profile(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not path.is_file():
        return None, "missing"
    try:
        profile = _load_json(path)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None, "malformed"
    if not isinstance(profile, dict):
        return None, "not_object"
    if set(profile) != PROFILE_REQUIRED:
        return None, "fields"
    if profile.get("schema_version") != 1:
        return None, "schema_version"
    for name in ("host_id", "minecraft_version", "loader", "scene_id"):
        if not isinstance(profile.get(name), str) or not profile[name].strip():
            return None, name
    java_major = profile.get("java_major")
    if not isinstance(java_major, int) or isinstance(java_major, bool) or java_major < 1:
        return None, "java_major"
    framebuffer = profile.get("framebuffer_px")
    if not isinstance(framebuffer, dict) or set(framebuffer) != {"width", "height"}:
        return None, "framebuffer_px"
    if any(
        not isinstance(framebuffer.get(key), int)
        or isinstance(framebuffer.get(key), bool)
        or framebuffer[key] <= 0
        for key in ("width", "height")
    ):
        return None, "framebuffer_px"
    if not isinstance(profile.get("settings"), dict) or not profile["settings"]:
        return None, "settings"
    if profile.get("dataset_kind") not in {"measured", "synthetic"}:
        return None, "dataset_kind"
    try:
        _json_key(profile)
    except (TypeError, ValueError):
        return None, "non_finite_value"
    return profile, None


def _route_category(route: dict[str, Any] | None) -> str | None:
    if not route:
        return None
    if "yaw_start" in route:
        return "pan"
    kind = str(route.get("kind", "")).split(";", 1)[0].strip().lower()
    if kind in {"walk", "sprint", "fly", "swim", "shuttle"}:
        return kind
    return "other"


def _route_signature(route: dict[str, Any] | None) -> dict[str, Any] | None:
    if route is None:
        return None
    signature: dict[str, Any] = {"kind": route.get("kind")}
    for key in ("weather", "day_start", "yaw_start", "pitch", "world_start"):
        if key in route:
            signature[key] = route[key]
    if "initial" in route:
        signature["initial_position"] = route["initial"]
    else:
        positions = route.get("positions")
        if isinstance(positions, list) and positions:
            first = positions[0]
            if isinstance(first, dict):
                signature["initial_position"] = first.get("position", first)
            else:
                signature["initial_position"] = first
    return signature


def inspect_capture(evidence_dir: Path | str, run_id: str) -> dict[str, Any]:
    """Inspect one public-format capture without modifying its evidence files."""
    if not SAFE_RUN_ID.fullmatch(run_id):
        raise LabError("invalid_run_id", "The run ID contains unsupported characters.")
    root = Path(evidence_dir).expanduser()
    if not root.is_dir():
        raise LabError("evidence_directory", "The selected evidence directory does not exist or is not a directory.")

    invalid: list[str] = []
    warnings: list[str] = []
    frames_path = root / f"{run_id}-frames.csv"
    start = _read_key_values(root / f"{run_id}-start.txt", "start", invalid)
    done = _read_key_values(root / f"{run_id}-done.txt", "completion", invalid)
    route = _read_route(root / f"{run_id}-route.json", invalid)
    intervals, row_count, _ = _load_intervals(frames_path, invalid)
    context_present, environment_gate = _capture_context(
        root / f"{run_id}-context.json", invalid, warnings
    )
    if (root / f"{run_id}-refused.txt").exists():
        invalid.append("A harness refusal marker exists for this run.")

    required_done = ("frames", "unfocused_frames", "frame_sink_error", "buffer_full", "hook")
    for name in required_done:
        if done is None or name not in done:
            invalid.append(f"The completion sidecar is missing {name}.")
    done_frames = _parse_int_field(done, "frames", "completion", invalid) if done is not None else None
    unfocused = _parse_int_field(done, "unfocused_frames", "completion", invalid) if done is not None else None
    if unfocused is not None and unfocused != 0:
        invalid.append("The client lost focus during the capture.")
    if done is not None and done.get("frame_sink_error") != "":
        invalid.append("The frame sampler reported an error.")
    if done is not None and done.get("buffer_full") != "false":
        invalid.append("The frame sampler buffer was full or its completion status is invalid.")
    if done_frames is not None and row_count != done_frames:
        invalid.append("The CSV row count does not match the sampler completion count.")

    required_start = ("hook", "seconds", "live_inactivity_policy", "live_throttle_reason", "live_frame_limit")
    for name in required_start:
        if start is None or name not in start:
            invalid.append(f"The start sidecar is missing {name}.")
    requested_seconds = _parse_seconds(start.get("seconds") if start else None, "requested", invalid)
    if start is not None:
        if start.get("live_inactivity_policy") != "MINIMIZED":
            invalid.append("The live inactive-FPS policy was not recorded as MINIMIZED.")
        if start.get("live_throttle_reason") != "NONE":
            invalid.append("The client was throttled or its live throttle state is unknown.")
    hook_value = (done or {}).get("hook") or (start or {}).get("hook")
    hook = None
    if isinstance(hook_value, str) and hook_value.startswith("Minecraft."):
        if "renderFrame" in hook_value:
            hook = "renderFrame"
        elif "runTick" in hook_value:
            hook = "runTick"
    if hook is None:
        invalid.append("The sampler hook is missing or is not a supported Minecraft frame hook.")

    route_seconds = None
    if route is not None and _finite_number(route.get("seconds")):
        route_seconds = float(route["seconds"])
        if route_seconds <= 0 or route_seconds > 1200:
            invalid.append("The recorded route duration is outside the supported range.")

    intervals_ms = [value / 1_000_000.0 for value in intervals]
    metrics: dict[str, Any] = {
        "frame_intervals": len(intervals),
        "captured_rows": row_count,
        "requested_seconds": requested_seconds,
        "route_seconds": route_seconds,
        "actual_seconds": sum(intervals) / 1_000_000_000.0 if intervals else None,
        "average_fps": None,
        "median_ms": None,
        "p95_ms": None,
        "p99_ms": None,
        "one_percent_low_fps": None,
        "slowest_one_percent_samples": None,
        "stalls_33ms": None,
        "stalls_50ms": None,
        "stalls_100ms": None,
    }
    if intervals_ms:
        mean_interval_ms = statistics.fmean(intervals_ms)
        if mean_interval_ms > 0 and math.isfinite(mean_interval_ms):
            metrics["average_fps"] = 1000.0 / mean_interval_ms
        ordered = sorted(intervals_ms)
        midpoint = len(ordered) // 2
        metrics["median_ms"] = ordered[midpoint] if len(ordered) % 2 else (ordered[midpoint - 1] + ordered[midpoint]) / 2
        for quantile, field, minimum in (
            (0.95, "p95_ms", P95_SAMPLE_MINIMUM),
            (0.99, "p99_ms", TAIL_SAMPLE_MINIMUM),
        ):
            if len(intervals_ms) >= minimum:
                rank = max(1, math.ceil(quantile * len(ordered)))
                metrics[field] = ordered[rank - 1]
        if len(intervals_ms) >= TAIL_SAMPLE_MINIMUM:
            slow_count = math.ceil(len(intervals_ms) * 0.01)
            slowest = ordered[-slow_count:]
            metrics["one_percent_low_fps"] = 1000.0 / statistics.fmean(slowest)
            metrics["slowest_one_percent_samples"] = slow_count
        else:
            warnings.append(
                "p99 and the slowest 1% low need at least 10,000 intervals so the tail contains at least 100 samples."
            )
        if len(intervals_ms) < P95_SAMPLE_MINIMUM:
            warnings.append("p95 is unavailable until the capture has at least 1,000 intervals.")
        metrics["stalls_33ms"] = sum(value > 33.3 for value in intervals_ms)
        metrics["stalls_50ms"] = sum(value > 50.0 for value in intervals_ms)
        metrics["stalls_100ms"] = sum(value > 100.0 for value in intervals_ms)

    profile, profile_issue = _parse_profile(root / f"{run_id}-profile.json")
    if profile_issue == "missing":
        warnings.append("No comparison profile is present; this capture cannot establish an apples-to-apples comparison.")
    elif profile_issue is not None:
        warnings.append(f"The comparison profile is invalid ({profile_issue}).")

    valid = not invalid
    tail_available = metrics["p99_ms"] is not None and metrics["one_percent_low_fps"] is not None
    if not valid:
        summary = f"Capture rejected: {len(invalid)} validation issue(s); measurements are retained for inspection."
    elif not tail_available:
        summary = f"Capture parsed with {len(intervals)} intervals; tail metrics need a larger sample."
    else:
        summary = f"Capture passed validation with {len(intervals)} positive frame intervals."

    return {
        "run": run_id,
        "valid": valid,
        "invalid_reasons": list(dict.fromkeys(invalid)),
        "warnings": list(dict.fromkeys(warnings)),
        "route_category": _route_category(route),
        "sampler_hook": hook,
        "environment_gate": "passed" if environment_gate is True else "failed" if environment_gate is False else "not_recorded",
        "context_present": context_present,
        "profile_valid": profile is not None,
        "dataset_kind": profile.get("dataset_kind") if profile else None,
        "metric_scope": METRIC_SCOPE,
        "metric_limit": METRIC_LIMIT,
        "summary": summary,
        **metrics,
        # Internal-only fields are consumed by compare_captures and removed from CLI reports.
        "_profile": profile,
        "_route_signature": _route_signature(route),
        "_live_frame_limit": (start or {}).get("live_frame_limit"),
    }


def _public_inspection(capture: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in capture.items() if not key.startswith("_")}


def _changed_profile_fields(profiles: list[dict[str, Any]]) -> list[str]:
    changed = []
    for field in PROFILE_FIELDS:
        expected = _json_key(profiles[0].get(field))
        if any(_json_key(profile.get(field)) != expected for profile in profiles[1:]):
            changed.append(field)
    return changed


def _dominates(left: dict[str, Any], right: dict[str, Any]) -> bool:
    metrics = ("average_fps", "one_percent_low_fps")
    left_values = [left.get(name) for name in metrics]
    right_values = [right.get(name) for name in metrics]
    if any(value is None for value in left_values + right_values):
        return False
    return all(a >= b for a, b in zip(left_values, right_values)) and any(
        a > b for a, b in zip(left_values, right_values)
    )


def compare_captures(evidence_dir: Path | str, run_ids: list[str]) -> dict[str, Any]:
    """Compare two or more captures and reject confounded or incomplete pairs."""
    if len(run_ids) < 2:
        raise LabError("compare_needs_two_runs", "Comparison requires at least two run IDs.")
    if len(set(run_ids)) != len(run_ids):
        raise LabError("duplicate_run_id", "Comparison run IDs must be unique.")
    captures = [inspect_capture(evidence_dir, run_id) for run_id in run_ids]
    incompatibilities: list[str] = []
    warnings = [warning for capture in captures for warning in capture["warnings"]]
    for capture in captures:
        if not capture["valid"]:
            incompatibilities.append(f"{capture['run']}: capture validation failed")
        if capture["p99_ms"] is None or capture["one_percent_low_fps"] is None:
            incompatibilities.append(f"{capture['run']}: insufficient samples for p99 and slowest 1% low")
        if not capture["profile_valid"]:
            incompatibilities.append(f"{capture['run']}: missing or invalid comparison profile")
    profiles = [capture["_profile"] for capture in captures]
    if all(isinstance(profile, dict) for profile in profiles):
        for field in _changed_profile_fields(profiles):
            incompatibilities.append(f"profile.{field} differs between runs")
    signatures = [capture["_route_signature"] for capture in captures]
    if all(isinstance(signature, dict) for signature in signatures):
        for field in sorted(set().union(*(signature.keys() for signature in signatures))):
            expected = _json_key(signatures[0].get(field))
            if any(_json_key(signature.get(field)) != expected for signature in signatures[1:]):
                incompatibilities.append(f"route.{field} differs between runs")
    else:
        incompatibilities.append("route metadata is missing")
    if len({capture["sampler_hook"] for capture in captures}) != 1:
        incompatibilities.append("sampler hook differs between runs")
    if len({_json_key(capture["_live_frame_limit"]) for capture in captures}) != 1:
        incompatibilities.append("live frame limit differs between runs")
    if len({_json_key(capture["requested_seconds"]) for capture in captures}) != 1:
        incompatibilities.append("requested capture duration differs between runs")
    if any(capture["environment_gate"] == "failed" for capture in captures):
        incompatibilities.append("recorded capture preflight failed")
    if any(capture["environment_gate"] != "passed" for capture in captures):
        incompatibilities.append("background CPU/swap preflight is not recorded for every run")

    incompatibilities = list(dict.fromkeys(incompatibilities))
    comparable = not incompatibilities
    preferred_run = None
    interpretation = "incomparable"
    deltas: dict[str, dict[str, float | None]] = {}
    if comparable:
        baseline = captures[0]
        for capture in captures[1:]:
            deltas[capture["run"]] = {
                "average_fps": round(capture["average_fps"] - baseline["average_fps"], 3),
                "p95_ms": round(capture["p95_ms"] - baseline["p95_ms"], 3),
                "p99_ms": round(capture["p99_ms"] - baseline["p99_ms"], 3),
                "one_percent_low_fps": round(
                    capture["one_percent_low_fps"] - baseline["one_percent_low_fps"], 3
                ),
            }
        leaders = [
            candidate
            for candidate in captures
            if all(candidate is other or _dominates(candidate, other) for other in captures)
        ]
        if len(leaders) == 1:
            preferred_run = leaders[0]["run"]
            interpretation = "dominates_average_and_slowest_one_percent_low"
        elif len(captures) == 2:
            interpretation = "mixed_tradeoff_or_tie"
        else:
            interpretation = "no_unique_dominant_run"
        warnings.append(
            "One capture per setup does not estimate run-to-run variation; repeat runs and alternate candidate order."
        )
        warnings.append(
            "The CPU-idle/swap preflight is a point-in-time check and cannot prove that capture-time interference was absent."
        )
        if any(capture["dataset_kind"] == "synthetic" for capture in captures):
            warnings.append("This comparison includes synthetic demonstration data, not a Minecraft measurement.")
        if any(capture["environment_gate"] == "not_recorded" for capture in captures):
            warnings.append("At least one capture has no recorded background CPU/swap preflight.")

    return {
        "status": "ok" if comparable else "needs_attention",
        "comparable": comparable,
        "baseline_run": run_ids[0],
        "runs": [_public_inspection(capture) for capture in captures],
        "incompatibilities": incompatibilities,
        "deltas_from_baseline": deltas,
        "preferred_run": preferred_run,
        "interpretation": interpretation,
        "metric_scope": METRIC_SCOPE,
        "metric_limit": METRIC_LIMIT,
        "warnings": list(dict.fromkeys(warnings)),
    }


def _major_version(output: str) -> int | None:
    match = re.search(r"(?:version\s+)?[\"']?(\d+)(?:[.\"'\s]|$)", output, re.IGNORECASE)
    return int(match.group(1)) if match else None


def _java_probe(jdk_dir: Path | None) -> dict[str, Any]:
    if jdk_dir is not None:
        root = jdk_dir.expanduser()
        executables = {name: root / "bin" / name for name in ("java", "javac", "jar")}
        found = {name: path.is_file() and os.access(path, os.X_OK) for name, path in executables.items()}
        java_path = executables["java"]
    else:
        found = {name: shutil.which(name) is not None for name in ("java", "javac", "jar")}
        located = shutil.which("java")
        java_path = Path(located) if located else None
    major = None
    version_error = None
    if java_path is not None and java_path.is_file() and os.access(java_path, os.X_OK):
        try:
            completed = subprocess.run(
                [str(java_path), "-version"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            major = _major_version((completed.stderr or "") + "\n" + (completed.stdout or ""))
            if completed.returncode != 0:
                version_error = "java -version returned an error"
        except (OSError, subprocess.TimeoutExpired):
            version_error = "java -version could not be completed"
    return {
        "executables_found": found,
        "major_version": major,
        "matches_supported_java": major == 25,
        "version_probe_error": version_error,
    }


def _prism_instance_info(game: Path, prism: Path) -> dict[str, Any]:
    instance_root: Path | None = None
    try:
        relative = game.resolve().relative_to((prism / "instances").resolve())
        if relative.parts:
            instance_root = (prism / "instances" / relative.parts[0]).resolve()
    except (OSError, ValueError):
        if game.name == ".minecraft" and (game.parent / "mmc-pack.json").is_file():
            instance_root = game.parent
    pack = instance_root / "mmc-pack.json" if instance_root else None
    minecraft_version = None
    loader_id = None
    loader_version = None
    if pack is not None and pack.is_file():
        try:
            document = _load_json(pack)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            document = None
        components = document.get("components") if isinstance(document, dict) else None
        if isinstance(components, list):
            for component in components:
                if not isinstance(component, dict):
                    continue
                uid = component.get("uid")
                version = component.get("version")
                if uid == "net.minecraft" and isinstance(version, str):
                    minecraft_version = version
                elif isinstance(uid, str) and any(
                    token in uid.lower() for token in ("fabric-loader", "quilt-loader", "forge", "neoforge")
                ):
                    if isinstance(version, str):
                        loader_id = uid
                        loader_version = version

    minescript_version = None
    mods_dir = game / "mods"
    if mods_dir.is_dir():
        for jar in mods_dir.glob("*.jar"):
            if "minescript" not in jar.name.lower():
                continue
            try:
                with zipfile.ZipFile(jar) as archive:
                    manifest = json.loads(archive.read("fabric.mod.json"), parse_constant=_reject_json_constant)
                if isinstance(manifest, dict) and manifest.get("id") == "minescript":
                    candidate = manifest.get("version")
                    if isinstance(candidate, str):
                        minescript_version = candidate
                        break
            except (OSError, zipfile.BadZipFile, KeyError, UnicodeError, json.JSONDecodeError, ValueError):
                continue
    return {
        "instance_metadata_found": pack is not None and pack.is_file(),
        "minecraft_version": minecraft_version,
        "minecraft_version_supported": minecraft_version in {"26.2", "26.3"},
        "loader_id": loader_id,
        "loader_version": loader_version,
        "minescript_version": minescript_version,
        "minescript_5_detected": bool(minescript_version and re.match(r"5(?:\.|$)", minescript_version)),
    }


def diagnose_setup(
    game_dir: Path | str, prism_dir: Path | str, jdk_dir: Path | str | None = None, asm_path: Path | str | None = None
) -> dict[str, Any]:
    """Read-only diagnosis of explicitly selected Prism and game directories."""
    game = Path(game_dir).expanduser()
    prism = Path(prism_dir).expanduser()
    game_exists = game.is_dir()
    prism_exists = prism.is_dir()
    game_checks = {
        "directory_exists": game_exists,
        "options_txt": game_exists and (game / "options.txt").is_file(),
        "mods_directory": game_exists and (game / "mods").is_dir(),
        "minescript_directory": game_exists and (game / "minescript").is_dir(),
        "shaderpacks_directory": game_exists and (game / "shaderpacks").is_dir(),
    }
    prism_checks = {
        "directory_exists": prism_exists,
        "instances_directory": prism_exists and (prism / "instances").is_dir(),
        "launcher_marker": prism_exists
        and any((prism / name).exists() for name in ("prismlauncher.cfg", "prismlauncher.exe", "PrismLauncher.app")),
    }
    java = _java_probe(Path(jdk_dir).expanduser() if jdk_dir is not None else None)
    instance = _prism_instance_info(game, prism)
    asm = {"provided": asm_path is not None, "file_exists": False, "matches_expected_sha256": False}
    if asm_path is not None:
        candidate = Path(asm_path).expanduser()
        asm["file_exists"] = candidate.is_file()
        if candidate.is_file():
            try:
                digest = hashlib.sha256()
                with candidate.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                asm["matches_expected_sha256"] = digest.hexdigest() == ASM_9_10_1_SHA256
            except OSError:
                asm["read_error"] = True

    macos = platform.system() == "Darwin"
    swiftc_available = shutil.which("swiftc") is not None
    top_available = shutil.which("top") is not None
    prism_root_recognized = prism_checks["instances_directory"] or prism_checks["launcher_marker"]
    game_configured = game_checks["directory_exists"] and game_checks["options_txt"]
    setup_ready = all(
        (
            macos,
            prism_root_recognized,
            game_configured,
            game_checks["minescript_directory"],
            instance["instance_metadata_found"],
            instance["minecraft_version_supported"],
            instance["loader_id"] is not None,
            instance["minescript_5_detected"],
            jdk_dir is not None,
            all(java["executables_found"].values()),
            java["matches_supported_java"],
            swiftc_available,
            top_available,
            asm["file_exists"],
            asm["matches_expected_sha256"],
        )
    )
    findings = []
    if not macos:
        findings.append("Capture support is macOS-only; this CLI still supports inspection and comparison here.")
    if not prism_exists:
        findings.append("The explicit Prism Launcher path does not exist.")
    elif not prism_root_recognized:
        findings.append("The selected Prism path has no instances directory or recognized launcher marker.")
    if not game_exists:
        findings.append("The explicit Minecraft game directory does not exist.")
    elif not game_checks["options_txt"]:
        findings.append("The selected game directory has no options.txt; choose the instance .minecraft directory.")
    if game_exists and not game_checks["minescript_directory"]:
        findings.append("Minescript scripts are not present in this game directory.")
    if not instance["instance_metadata_found"]:
        findings.append("The selected game path is not linked to a Prism instance with mmc-pack.json.")
    elif instance["minecraft_version"] is None:
        findings.append("The Prism instance Minecraft version could not be read.")
    elif not instance["minecraft_version_supported"]:
        findings.append("Only Minecraft 26.2 and 26.3 are in the validated capture matrix.")
    if instance["loader_id"] is None:
        findings.append("The Prism instance loader could not be identified from its component metadata.")
    if instance["minescript_version"] is None:
        findings.append("A Minescript mod version could not be identified in the explicit mods directory.")
    elif not instance["minescript_5_detected"]:
        findings.append("Control routes require Minescript 5; another mod version was found.")
    if not all(java["executables_found"].values()):
        findings.append("A Java, javac, or jar executable is missing; the harness build needs a complete JDK.")
    if jdk_dir is None:
        findings.append("Pass --jdk with the complete JDK directory used by the harness build.")
    if java["major_version"] is None:
        findings.append("The Java major version could not be confirmed as 25.")
    elif not java["matches_supported_java"]:
        findings.append("The harness build was tested with Java 25; the selected runtime has another major version.")
    if not swiftc_available:
        findings.append("swiftc is unavailable; the optional macOS input preflight helper cannot be built.")
    if not top_available:
        findings.append("top is unavailable; the capture driver's CPU-idle/swap preflight cannot run.")
    if not asm["provided"]:
        findings.append("Pass the exact ASM 9.10.1 JAR from the launcher's game classpath to check its build dependency.")
    elif not asm["matches_expected_sha256"]:
        findings.append("The selected ASM JAR is unreadable or does not match the pinned ASM 9.10.1 hash.")
    findings.append(
        "The diagnosis reads only the supplied paths and Java version; it does not launch, attach to, or modify Minecraft."
    )
    return {
        "setup_ready": setup_ready,
        "capture_supported_on_host": macos,
        "validated_matrix": {
            "os": "macOS",
            "minecraft": ["26.2", "26.3"],
            "java_major": 25,
            "control_routes": "Minescript 5",
            "launcher_workflow": "Prism path diagnosis; other launcher setup is not verified here",
        },
        "game_dir_checks": game_checks,
        "prism_dir_checks": prism_checks,
        "instance": instance,
        "java": java,
        "swiftc_available": swiftc_available,
        "top_available": top_available,
        "asm": asm,
        "findings": findings,
        "active_client_touched": False,
    }


def _report_payload(evidence_dir: Path | str, run_ids: list[str]) -> dict[str, Any]:
    if len(run_ids) == 1:
        capture = inspect_capture(evidence_dir, run_ids[0])
        return {"schema_version": 1, "tool": TOOL, "inspection": _public_inspection(capture)}
    comparison = compare_captures(evidence_dir, run_ids)
    return {"schema_version": 1, "tool": TOOL, "comparison": comparison}


def _report_text(value: Any, fallback: str = "Unavailable") -> str:
    return html.escape(str(value if value is not None else fallback), quote=True)


def _report_number(value: Any, decimals: int | None = 1, suffix: str = "") -> str:
    if not _finite_number(value):
        return "Unavailable"
    if decimals is None:
        return f"{int(value):,}{suffix}"
    return f"{float(value):,.{decimals}f}{suffix}"


def _report_metric_rows(runs: list[dict[str, Any]]) -> str:
    rows = []
    for run in runs:
        validity = "valid" if run.get("valid") else "rejected"
        rows.append(
            "<tr>"
            f"<th scope=\"row\">{_report_text(run.get('run'))}</th>"
            f"<td><span class=\"pill {validity}\">{validity.title()}</span></td>"
            f"<td>{_report_number(run.get('frame_intervals'), None)}</td>"
            f"<td>{_report_number(run.get('average_fps'), suffix=' FPS')}</td>"
            f"<td>{_report_number(run.get('median_ms'), 2, ' ms')}</td>"
            f"<td>{_report_number(run.get('p95_ms'), 2, ' ms')}</td>"
            f"<td>{_report_number(run.get('p99_ms'), 2, ' ms')}</td>"
            f"<td>{_report_number(run.get('one_percent_low_fps'), suffix=' FPS')}</td>"
            "</tr>"
        )
    return "".join(rows)


def _report_fps_chart(runs: list[dict[str, Any]]) -> str:
    values = [
        float(run[key])
        for run in runs
        for _, key, _ in REPORT_FPS_SERIES
        if _finite_number(run.get(key)) and float(run[key]) >= 0
    ]
    if not values:
        return "<p class=\"empty-state\">No finite FPS values are available to chart.</p>"
    scale = max(values)
    run_cards = []
    for run in runs:
        metric_rows = []
        for label, key, kind in REPORT_FPS_SERIES:
            value = run.get(key)
            if _finite_number(value) and float(value) >= 0:
                width = (float(value) / scale * 100) if scale else 0
                bar = f"<span class=\"bar {kind}\" style=\"width:{width:.1f}%\"></span>"
                shown = _report_number(value, suffix=" FPS")
            else:
                bar = ""
                shown = "Unavailable"
            metric_rows.append(
                "<div class=\"chart-line\">"
                f"<span class=\"chart-label\">{label}</span>"
                f"<span class=\"track\" aria-hidden=\"true\">{bar}</span>"
                f"<span class=\"chart-value\">{shown}</span>"
                "</div>"
            )
        run_cards.append(
            "<article class=\"chart-run\">"
            f"<h3>{_report_text(run.get('run'))}</h3>"
            + "".join(metric_rows)
            + "</article>"
        )
    return "<div class=\"chart-runs\">" + "".join(run_cards) + "</div>"


def _report_fps_legend() -> str:
    items = "".join(
        f"<span class=\"legend-item\"><span class=\"swatch {kind}\"></span>{_report_text(label)}</span>"
        for label, _, kind in REPORT_FPS_SERIES
    )
    return f"<div class=\"legend\">{items}</div>"


def _html_report(payload: dict[str, Any]) -> bytes:
    result = payload.get("comparison") or payload.get("inspection") or {}
    runs = result.get("runs") or [result]
    issues = result.get("incompatibilities") or [
        issue for run in runs for issue in run.get("invalid_reasons", [])
    ]
    warnings = result.get("warnings") or [
        warning for run in runs for warning in run.get("warnings", [])
    ]
    has_invalid_run = any(not run.get("valid") for run in runs)
    comparable = result.get("comparable")
    if has_invalid_run:
        status_class, status_title = "rejected", "Capture rejected"
        status_detail = "The recorded measurements are shown for inspection, but one or more capture checks failed."
    elif comparable is False:
        status_class, status_title = "attention", "Comparison blocked"
        status_detail = "Capture profiles, routes, or preflight evidence do not support a fair ranking."
    elif comparable is True:
        status_class, status_title = "validated", "Comparison eligible"
        preferred = result.get("preferred_run")
        if preferred:
            status_detail = f"{_report_text(preferred)} leads this pair on both average FPS and slowest 1% low."
        else:
            status_detail = "No run dominates both average FPS and slowest 1% low in this pair."
    else:
        status_class, status_title = "validated", "Capture validated" if runs[0].get("valid") else "Capture rejected"
        status_detail = _report_text(result.get("summary"), "Capture validation result")
        if runs[0].get("valid") and result.get("p99_ms") is None:
            status_class = "attention"

    is_synthetic = result.get("dataset_kind") == "synthetic" or any(
        run.get("dataset_kind") == "synthetic" for run in runs
    )
    synthetic_banner = (
        "<aside class=\"synthetic-banner\" role=\"note\"><strong>Synthetic demonstration data</strong>"
        "<span>These generated values show the report format. They are not Minecraft measurements.</span></aside>"
        if is_synthetic
        else ""
    )
    issue_section = ""
    if issues:
        issue_section = (
            "<section class=\"notice-panel rejection-list\"><h2>Rejection and compatibility issues</h2><ul>"
            + "".join(f"<li>{_report_text(issue)}</li>" for issue in dict.fromkeys(issues))
            + "</ul></section>"
        )
    warning_section = ""
    if warnings:
        displayed_warnings = [
            warning
            for warning in dict.fromkeys(warnings)
            if not (is_synthetic and warning == "This comparison includes synthetic demonstration data, not a Minecraft measurement.")
        ]
    else:
        displayed_warnings = []
    if displayed_warnings:
        warning_section = (
            "<section class=\"notice-panel warning-list\"><h2>Limits and warnings</h2><ul>"
            + "".join(f"<li>{_report_text(warning)}</li>" for warning in displayed_warnings)
            + "</ul></section>"
        )

    comparison_note = ""
    if comparable is False:
        comparison_note = "<p class=\"comparison-note\">Bars show parsed values only; the report does not rank incompatible runs.</p>"
    elif comparable is True:
        comparison_note = (
            "<p class=\"comparison-note\">Bars use each run's parsed FPS values on one shared scale. "
            "A leading pair does not estimate run-to-run variation.</p>"
        )
    elif runs[0].get("valid"):
        comparison_note = "<p class=\"comparison-note\">p99 and the slowest-1% low may be unavailable until enough intervals are recorded.</p>"

    body = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Minecraft Frame Lab report</title>
  <style>
    :root {{ color-scheme: light; --ink:#162236; --muted:#63728a; --line:#d9e1ec; --paper:#f4f7fb; --panel:#fff; --navy:#10233c; --blue:#1768ac; --cyan:#00a6a6; --green:#137447; --amber:#875400; --red:#a42e35; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; background:var(--paper); color:var(--ink); font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
    main {{ width:min(1120px,100%); margin:0 auto; padding:clamp(20px,5vw,56px) clamp(16px,4vw,40px) 64px; }}
    .eyebrow {{ margin:0 0 8px; color:var(--blue); font-size:.76rem; font-weight:800; letter-spacing:.14em; text-transform:uppercase; }}
    h1 {{ margin:0; color:var(--navy); font-size:clamp(2rem,5vw,3.2rem); letter-spacing:-.045em; line-height:1.08; }}
    .subtitle {{ margin:14px 0 28px; color:var(--muted); max-width:70ch; }}
    h2 {{ margin:0 0 16px; font-size:1.18rem; letter-spacing:-.02em; }}
    .synthetic-banner,.status-banner,.metric-definition,.notice-panel,.chart-panel,.table-panel {{ border:1px solid var(--line); border-radius:16px; background:var(--panel); }}
    .synthetic-banner {{ display:grid; gap:2px; margin:0 0 16px; padding:16px 20px; border-color:#e6c678; background:#fff5d9; color:#573b00; }}
    .synthetic-banner strong {{ font-size:.92rem; }} .synthetic-banner span {{ font-size:.9rem; }}
    .status-banner {{ display:grid; grid-template-columns:auto 1fr; gap:14px; align-items:start; margin:0 0 20px; padding:18px 20px; border-left:5px solid var(--green); }}
    .status-banner.rejected {{ border-left-color:var(--red); background:#fff5f5; }} .status-banner.attention {{ border-left-color:#c18500; background:#fff9e8; }}
    .status-mark {{ display:grid; place-items:center; width:30px; height:30px; border-radius:50%; color:white; background:var(--green); font-weight:800; }}
    .rejected .status-mark {{ background:var(--red); }} .attention .status-mark {{ background:#a56c00; }}
    .status-title {{ display:block; margin:0 0 2px; font-size:1rem; }} .status-detail {{ color:var(--muted); }}
    .metric-definition {{ margin:0 0 20px; padding:11px 16px; }} .metric-definition p {{ margin:0; color:var(--muted); font-size:.92rem; }}
    .metric-definition strong {{ color:var(--ink); }}
    .notice-panel {{ margin:16px 0; padding:18px 20px; }} .notice-panel ul {{ margin:0; padding-left:22px; }} .notice-panel li+li {{ margin-top:6px; }}
    .rejection-list {{ border-color:#e8b4b7; background:#fff7f7; }} .rejection-list h2 {{ color:var(--red); }}
    .warning-list {{ border-color:#e8d39f; background:#fffbef; }} .warning-list h2 {{ color:var(--amber); }}
    .section-head {{ display:flex; flex-wrap:wrap; align-items:baseline; justify-content:space-between; gap:6px 16px; margin:34px 0 12px; }}
    .section-head h2 {{ margin:0; }} .section-head p {{ margin:0; color:var(--muted); font-size:.88rem; }}
    .chart-panel,.table-panel {{ overflow:hidden; }} .chart-panel {{ padding:20px; }}
    .legend {{ display:flex; flex-wrap:wrap; gap:8px 18px; margin:0 0 20px; color:var(--muted); font-size:.88rem; }}
    .legend-item {{ display:inline-flex; align-items:center; gap:8px; }} .swatch {{ width:11px; height:11px; border-radius:3px; }} .swatch.average {{ background:var(--blue); }} .swatch.tail {{ background:var(--cyan); }}
    .chart-runs {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(min(100%,380px),1fr)); gap:12px; }}
    .chart-run {{ min-width:0; padding:16px; border:1px solid var(--line); border-radius:12px; background:#fbfcfe; }}
    .chart-run h3 {{ overflow-wrap:anywhere; margin:0 0 14px; font-size:.98rem; }}
    .chart-line {{ display:grid; grid-template-columns:minmax(100px,130px) minmax(60px,1fr) minmax(78px,92px); gap:10px; align-items:center; margin:10px 0; font-size:.85rem; }}
    .chart-label {{ color:var(--muted); }} .track {{ display:block; width:100%; height:12px; overflow:hidden; border-radius:99px; background:#e8edf4; }}
    .bar {{ display:block; height:100%; min-width:0; border-radius:inherit; }} .bar.average {{ background:var(--blue); }} .bar.tail {{ background:var(--cyan); }}
    .chart-value {{ text-align:right; font-variant-numeric:tabular-nums; font-weight:750; white-space:nowrap; }}
    .comparison-note {{ margin:14px 2px 0; color:var(--muted); font-size:.88rem; }} .empty-state {{ margin:0; color:var(--muted); }}
    .table-scroll {{ max-width:100%; overflow-x:auto; overscroll-behavior-x:contain; }}
    table {{ width:100%; min-width:870px; border-collapse:collapse; font-size:.9rem; font-variant-numeric:tabular-nums; }}
    caption {{ padding:16px 18px 8px; text-align:left; color:var(--muted); }} th,td {{ padding:12px 14px; border-bottom:1px solid var(--line); text-align:right; white-space:nowrap; }}
    thead th {{ background:#edf2f8; color:#43546e; font-size:.76rem; letter-spacing:.04em; text-transform:uppercase; }}
    tbody th {{ max-width:220px; overflow:hidden; text-overflow:ellipsis; text-align:left; }} tbody tr:last-child th,tbody tr:last-child td {{ border-bottom:0; }}
    .pill {{ display:inline-block; padding:3px 9px; border-radius:99px; font-size:.75rem; font-weight:800; }} .pill.valid {{ color:#0e5a37; background:#daf2e5; }} .pill.rejected {{ color:#8b2228; background:#f9dfe1; }}
    .footnote {{ margin:24px 0 0; color:var(--muted); font-size:.85rem; }}
    @media(max-width:600px) {{ .chart-panel {{ padding:14px; }} .chart-run {{ padding:12px; }} .chart-line {{ grid-template-columns:95px minmax(36px,1fr) 76px; gap:7px; font-size:.78rem; }} .status-banner {{ padding:15px; }} }}
    @media(prefers-reduced-motion:reduce) {{ *,*::before,*::after {{ scroll-behavior:auto!important; }} }}
  </style>
</head>
<body>
<main>
  <header>
    <p class="eyebrow">Frame Lab · performance report</p>
    <h1>Minecraft frame production</h1>
    <p class="subtitle">A review of captured frame intervals, validation checks, and comparable run profiles.</p>
  </header>
{synthetic_banner}
  <section class="status-banner {status_class}" role="status">
    <span class="status-mark" aria-hidden="true">{'×' if status_class == 'rejected' else '!' if status_class == 'attention' else '✓'}</span>
    <span><strong class="status-title">{status_title}</strong><span class="status-detail">{status_detail}</span></span>
  </section>
  <section class="metric-definition">
    <p><strong>{_report_text(METRIC_LIMIT)}</strong></p>
  </section>
{issue_section}
  <div class="section-head"><h2>FPS comparison</h2><p>Bars share one scale and use the parsed run values.</p></div>
  <section class="chart-panel">
    {_report_fps_legend()}
    {_report_fps_chart(runs)}
{comparison_note}
  </section>
  <div class="section-head"><h2>Frame-time metrics</h2><p>Scroll horizontally to see all columns on narrow screens.</p></div>
  <section class="table-panel">
    <div class="table-scroll" role="region" aria-label="Frame-time metrics table" tabindex="0">
      <table>
        <caption>Positive frame intervals only. Percentiles use nearest-rank values.</caption>
        <thead><tr><th scope="col">Run</th><th scope="col">Validation</th><th scope="col">Intervals</th><th scope="col">{_report_text(REPORT_FPS_SERIES[0][0])}</th><th scope="col">Median</th><th scope="col">p95</th><th scope="col">p99</th><th scope="col">{_report_text(REPORT_FPS_SERIES[1][0])}</th></tr></thead>
        <tbody>{_report_metric_rows(runs)}</tbody>
      </table>
    </div>
  </section>
{warning_section}
  <p class="footnote">Unavailable tail values mean the capture did not contain enough samples. A single run per setup does not establish run-to-run stability.</p>
</main>
</body>
</html>
"""
    return body.encode("utf-8")


def export_report(
    evidence_dir: Path | str, run_ids: list[str], destination: Path | str, output_format: str, *, dry_run: bool = False
) -> dict[str, Any]:
    """Preview or create a new portable report, leaving all capture inputs intact."""
    payload = _report_payload(evidence_dir, run_ids)
    if output_format == "html":
        content = _html_report(payload)
        media_type = "text/html"
    else:
        content = (json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
        media_type = "application/json"
    target = Path(destination).expanduser()
    if not target.parent.is_dir():
        raise LabError("output_directory", "The report output directory must already exist.")
    plan = {
        "action": "create_portable_report",
        "destination": str(target.absolute()),
        "format": output_format,
        "run_ids": list(run_ids),
        "bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "capture_inputs_modified": False,
        "overwrite": False,
    }
    if dry_run:
        return {"status": "ok", "plan": plan, "receipt": None, "media_type": media_type}
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError as exc:
        raise LabError("output_exists", "The report destination already exists; choose a new path.") from exc
    except OSError as exc:
        raise LabError("output_write", "Could not create the report at the selected destination.") from exc
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        try:
            target.unlink()
        except OSError:
            pass
        raise LabError("output_write", "The report could not be written completely.") from exc
    receipt = {
        "path": str(target.absolute()),
        "bytes": len(content),
        "sha256": plan["sha256"],
        "run_ids": list(run_ids),
        "capture_inputs_modified": False,
    }
    return {"status": "ok", "plan": plan, "receipt": receipt, "media_type": media_type}


def _write_demo_capture(root: Path, run: str, base_ns: int, slow_ns: int) -> None:
    root.mkdir(parents=True, exist_ok=True)
    with (root / f"{run}-frames.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("frame", "nanotime", "interval_ns"))
        now = 1_000_000_000
        writer.writerow((0, now, 0))
        slow_count = 200
        base_count = (120_000_000_000 - slow_count * slow_ns) // base_ns
        interval_count = base_count + slow_count
        for index in range(interval_count):
            interval = base_ns if index < base_count else slow_ns
            now += interval
            writer.writerow((index + 1, now, interval))
    (root / f"{run}-start.txt").write_text(
        "hook=Minecraft.renderFrame(boolean) return; CPU frame-production interval\n"
        "seconds=120\nlive_inactivity_policy=MINIMIZED\nlive_throttle_reason=NONE\nlive_frame_limit=120\n",
        encoding="utf-8",
    )
    (root / f"{run}-done.txt").write_text(
        f"frames={interval_count + 1}\nunfocused_frames=0\nframe_sink_error=\nbuffer_full=false\n"
        "hook=Minecraft.renderFrame(boolean) return; CPU frame-production interval\n",
        encoding="utf-8",
    )
    (root / f"{run}-route.json").write_text(
        json.dumps(
            {
                "kind": "fixed-position synthetic demo pan",
                "seconds": 120.0,
                "positions": [{"position": [0, 64, 0], "tick": 1000}],
                "world_start": {"dimension": "overworld", "world_label": "synthetic-demo"},
                "world_end": {"dimension": "overworld", "world_label": "synthetic-demo"},
                "yaw_start": 0,
                "pitch": 5,
                "weather": "clear",
                "day_start": 6000,
            }
        ),
        encoding="utf-8",
    )
    profile = {
        "schema_version": 1,
        "host_id": "synthetic-example-host",
        "minecraft_version": "26.3",
        "loader": "Fabric example",
        "java_major": 25,
        "framebuffer_px": {"width": 1920, "height": 1080},
        "scene_id": "synthetic-fixed-pan",
        "settings": {"frame_limit": 120, "render_distance": 16, "shader": "synthetic-example"},
        "dataset_kind": "synthetic",
    }
    (root / f"{run}-profile.json").write_text(json.dumps(profile), encoding="utf-8")
    (root / f"{run}-context.json").write_text(
        json.dumps({"environment_gate": {"passed": True}, "dataset_kind": "synthetic"}),
        encoding="utf-8",
    )


def run_demo() -> dict[str, Any]:
    """Run the analyzer on clearly labelled synthetic public-format evidence."""
    with tempfile.TemporaryDirectory(prefix="frame-lab-demo-") as temporary:
        root = Path(temporary)
        _write_demo_capture(root, "demo-baseline", 8_000_000, 20_000_000)
        _write_demo_capture(root, "demo-candidate", 7_500_000, 18_000_000)
        comparison = compare_captures(root, ["demo-baseline", "demo-candidate"])
        return {
            "dataset_kind": "synthetic",
            "summary": "Synthetic format demonstration only; it contains no Minecraft measurements.",
            "comparison": comparison,
        }


def _envelope(
    status: str,
    summary: str,
    data: dict[str, Any],
    artifacts: list[dict[str, str]] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL,
        "status": status,
        "summary": summary,
        "data": data,
        "artifacts": artifacts or [],
        "warnings": warnings or [],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect Minecraft CPU frame-production captures and compare compatible runs."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="diagnose explicit Prism and Minecraft paths without launching the game")
    doctor.add_argument("--game-dir", type=Path, required=True, help="exact instance .minecraft directory")
    doctor.add_argument("--prism-dir", type=Path, required=True, help="exact Prism Launcher data directory")
    doctor.add_argument("--jdk", type=Path, help="optional complete Java 25 JDK directory")
    doctor.add_argument("--asm", type=Path, help="optional ASM 9.10.1 JAR from the active game classpath")

    inspect = subparsers.add_parser("inspect", help="validate and summarize one capture")
    inspect.add_argument("--evidence-dir", type=Path, required=True, help="exact harness evidence directory")
    inspect.add_argument("--run", type=_safe_run_id, required=True, help="capture run ID")

    compare = subparsers.add_parser("compare", help="compare at least two captures")
    compare.add_argument("--evidence-dir", type=Path, required=True, help="exact harness evidence directory")
    compare.add_argument("--runs", type=_safe_run_id, nargs="+", required=True, help="baseline first, then candidates")

    export = subparsers.add_parser("export", help="preview or write a portable JSON or HTML report")
    export.add_argument("--evidence-dir", type=Path, required=True, help="exact harness evidence directory")
    export.add_argument("--runs", type=_safe_run_id, nargs="+", required=True, help="one run or baseline followed by candidates")
    export.add_argument("--output", type=Path, required=True, help="new report path; existing files are never overwritten")
    export.add_argument("--format", choices=("json", "html"), default="json", help="portable report format")
    export.add_argument("--dry-run", action="store_true", help="show the exact output plan and content hash without writing")

    subparsers.add_parser("demo", help="show the analysis flow on temporary synthetic data")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            data = diagnose_setup(args.game_dir, args.prism_dir, args.jdk, args.asm)
            status = "ok" if data["setup_ready"] else "needs_attention"
            summary = "The selected paths meet the checked capture prerequisites." if data["setup_ready"] else "Capture setup needs attention; see the path and dependency checks."
            result = _envelope(status, summary, data, warnings=data["findings"])
        elif args.command == "inspect":
            data = inspect_capture(args.evidence_dir, args.run)
            status = "ok" if data["valid"] and data["p99_ms"] is not None else "needs_attention"
            result = _envelope(status, data["summary"], _public_inspection(data), warnings=data["warnings"])
        elif args.command == "compare":
            if len(args.runs) < 2:
                parser.error("compare requires at least two --runs values")
            data = compare_captures(args.evidence_dir, args.runs)
            result = _envelope(data["status"], "Capture comparison completed." if data["comparable"] else "Captures cannot be compared fairly with the available evidence.", data, warnings=data["warnings"])
        elif args.command == "export":
            data = export_report(args.evidence_dir, args.runs, args.output, args.format, dry_run=args.dry_run)
            artifacts = []
            if data["receipt"]:
                artifacts.append({"path": data["receipt"]["path"], "label": "Portable Minecraft frame report", "media_type": data["media_type"]})
            summary = "Report plan previewed; no file was written." if args.dry_run else "Portable report created with an input-preservation receipt."
            result = _envelope("ok", summary, data, artifacts=artifacts)
        else:
            data = run_demo()
            result = _envelope("ok", data["summary"], data, warnings=data["comparison"]["warnings"])
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except LabError as exc:
        error = _envelope("error", str(exc), {"error_code": exc.code})
        print(json.dumps(error, ensure_ascii=False, indent=2, allow_nan=False))
        return 1
    except OSError:
        error = _envelope("error", "An input or output file could not be accessed.", {"error_code": "io_error"})
        print(json.dumps(error, ensure_ascii=False, indent=2, allow_nan=False))
        return 1
    except Exception:
        error = _envelope("error", "The input could not be processed safely.", {"error_code": "invalid_input"})
        print(json.dumps(error, ensure_ascii=False, indent=2, allow_nan=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
