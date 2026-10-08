"""Smoke each built plugin ZIP from an isolated temporary extraction."""

from __future__ import annotations

import csv
import hashlib
import http.server
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree

if __package__:
    from .release import dump_json
else:
    from release import dump_json


ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
EXCLUDED_DIRS = {"__pycache__", ".git", ".pytest_cache", ".venv", "node_modules"}
VALID_CLI_STATUSES = {"ok", "needs_attention"}
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024


class SmokeFailure(RuntimeError):
    pass


class DependencyUnavailable(RuntimeError):
    pass


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_sha256(root: Path) -> str:
    """Hash packaged paths and content hashes in stable path order."""
    digest = hashlib.sha256()
    files = [
        path
        for path in root.rglob("*")
        if path.is_file() and not any(part in EXCLUDED_DIRS for part in path.relative_to(root).parts)
    ]
    for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(relative + b"\0" + sha256_file(path).encode("ascii") + b"\n")
    return digest.hexdigest()


def plugin_archive_map(dist: Path) -> tuple[dict[str, Path], dict[str, str]]:
    """Use the build manifest when available, otherwise require one ZIP per plugin."""
    archives: dict[str, Path] = {}
    release_hashes: dict[str, str] = {}
    manifest_path = dist / "releases.json"
    if manifest_path.is_file():
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            for item in payload["plugins"]:
                plugin_id = item["name"]
                archives[plugin_id] = dist / item["file"]
                release_hashes[plugin_id] = item["sha256"]
        except (KeyError, TypeError, ValueError) as exc:
            raise SmokeFailure("dist/releases.json is malformed") from exc
    else:
        for plugin_id in PLUGIN_IDS:
            matches = sorted(dist.glob(f"{plugin_id}-*.zip"))
            if len(matches) == 1:
                archives[plugin_id] = matches[0]
    return archives, release_hashes


def safe_extract(archive_path: Path, destination: Path) -> None:
    """Extract only ordinary relative files, with a bounded uncompressed size."""
    seen: set[str] = set()
    expanded = 0
    with zipfile.ZipFile(archive_path) as archive:
        for item in archive.infolist():
            name = item.filename
            member = PurePosixPath(name)
            mode = (item.external_attr >> 16) & 0o170000
            if member.is_absolute() or ".." in member.parts or "\\" in name:
                raise SmokeFailure(f"unsafe archive path in {archive_path.name}")
            if stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}:
                raise SmokeFailure(f"non-regular archive member in {archive_path.name}")
            if name in seen:
                raise SmokeFailure(f"duplicate archive member in {archive_path.name}")
            seen.add(name)
            expanded += item.file_size
            if expanded > MAX_ARCHIVE_BYTES:
                raise SmokeFailure(f"archive expands beyond {MAX_ARCHIVE_BYTES} bytes")
        archive.extractall(destination)


def base_environment() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def nonempty_file_bytes(path: Path, label: str) -> int:
    if not path.is_file():
        raise SmokeFailure(f"{label} was not created")
    size = path.stat().st_size
    if size <= 0:
        raise SmokeFailure(f"{label} is empty")
    return size


def output_record(label: str, path: Path) -> dict[str, Any]:
    size = nonempty_file_bytes(path, label)
    return {"label": label, "bytes": size, "sha256": sha256_file(path)}


def json_file(path: Path, label: str) -> dict[str, Any]:
    nonempty_file_bytes(path, label)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise SmokeFailure(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise SmokeFailure(f"{label} must contain a JSON object")
    return value


def run_json_cli(
    result: dict[str, Any],
    name: str,
    skill: Path,
    args: list[str],
    *,
    statuses: set[str] = VALID_CLI_STATUSES,
    timeout: int = 45,
) -> dict[str, Any]:
    command = [PYTHON, *args]
    completed = subprocess.run(
        command,
        cwd=skill,
        env=base_environment(),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    raw = completed.stdout.strip()
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise SmokeFailure(f"{name} did not return one valid JSON value") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("schema_version"), int):
        raise SmokeFailure(f"{name} JSON is missing the documented schema envelope")
    status = payload.get("status")
    if completed.returncode != 0 or status not in statuses:
        raise SmokeFailure(f"{name} failed with exit {completed.returncode} and status {status!r}")
    if not isinstance(payload.get("summary"), str) or not isinstance(payload.get("data"), dict):
        raise SmokeFailure(f"{name} JSON is missing summary or data")
    result["commands"].append(
        {
            "name": name,
            "exit_code": completed.returncode,
            "status": status,
            "summary": payload["summary"],
            "stdout_bytes": len(completed.stdout.encode("utf-8")),
            "stdout_sha256": sha256_bytes(completed.stdout.encode("utf-8")),
            "stderr_bytes": len(completed.stderr.encode("utf-8")),
            "stderr_sha256": sha256_bytes(completed.stderr.encode("utf-8")),
        }
    )
    return payload


def run_help(result: dict[str, Any], skill: Path, script: str) -> None:
    completed = subprocess.run(
        [PYTHON, script, "--help"],
        cwd=skill,
        env=base_environment(),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    help_text = completed.stdout + completed.stderr
    if completed.returncode != 0 or "usage:" not in help_text.lower():
        raise SmokeFailure(f"{script} --help failed")
    result["commands"].append(
        {
            "name": f"{Path(script).name} --help",
            "exit_code": 0,
            "status": "completed",
            "stdout_bytes": len(completed.stdout.encode("utf-8")),
            "stdout_sha256": sha256_bytes(completed.stdout.encode("utf-8")),
        }
    )


def verify_output_receipt(
    result: dict[str, Any], payload: dict[str, Any], path: Path, label: str
) -> None:
    output = output_record(label, path)
    result["outputs"].append(output)
    artifacts = payload.get("artifacts", [])
    if isinstance(artifacts, list):
        matching = [item for item in artifacts if isinstance(item, dict) and item.get("path") == str(path)]
        if matching and "bytes" in matching[0] and matching[0]["bytes"] != output["bytes"]:
            raise SmokeFailure(f"{label} byte count differs from the CLI artifact receipt")
        if matching and "sha256" in matching[0] and matching[0]["sha256"] != output["sha256"]:
            raise SmokeFailure(f"{label} hash differs from the CLI artifact receipt")


def smoke_frame(result: dict[str, Any], skill: Path, scratch: Path, _dependencies: dict[str, Any]) -> None:
    del scratch
    payload = run_json_cli(result, "synthetic demo", skill, ["scripts/frame_lab.py", "demo"])
    if payload["data"].get("dataset_kind") != "synthetic":
        raise SmokeFailure("Frame Lab demo did not identify its data as synthetic")
    if payload["data"].get("comparison", {}).get("comparable") is not True:
        raise SmokeFailure("Frame Lab synthetic runs were not comparable")


def smoke_fit(result: dict[str, Any], skill: Path, scratch: Path, dependencies: dict[str, Any]) -> None:
    ffmpeg = dependencies["ffmpeg"].get("available") and dependencies.get("ffmpeg_libx264")
    if not ffmpeg or not dependencies["ffprobe"].get("available"):
        missing = [
            key for key in ("ffmpeg", "ffprobe") if not dependencies[key].get("available")
        ]
        if dependencies["ffmpeg"].get("available") and not dependencies.get("ffmpeg_libx264"):
            missing.append("ffmpeg libx264 encoder")
        raise DependencyUnavailable(", ".join(missing))

    source = scratch / "tiny-source.mp4"
    output = scratch / "tiny-fit.mp4"
    generated = subprocess.run(
        [
            dependencies["ffmpeg"]["command"],
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=15:duration=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100:duration=1",
            "-shortest",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "32",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "32k",
            str(source),
        ],
        cwd=skill,
        env=base_environment(),
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    if generated.returncode != 0:
        raise SmokeFailure("ffmpeg could not generate the synthetic MP4")
    source_hash = sha256_file(source)
    cap = source.stat().st_size
    result["outputs"].append(output_record("generated synthetic MP4 input", source))
    result["checks"].append({"name": "exact-byte-cap", "max_bytes": cap})

    plan = run_json_cli(
        result,
        "exact-cap plan",
        skill,
        ["scripts/fit_to_upload.py", "plan", "--input", str(source), "--output", str(output), "--max-bytes", str(cap)],
    )
    if plan["data"].get("already_fits") is not True or plan["data"].get("action") != "copy unchanged":
        raise SmokeFailure("Fit to Upload did not recognize the MP4 as already fitting its exact cap")
    encoded = run_json_cli(
        result,
        "exact-cap encode",
        skill,
        ["scripts/fit_to_upload.py", "encode", "--input", str(source), "--output", str(output), "--max-bytes", str(cap)],
        timeout=60,
    )
    receipt_path = Path(str(output) + ".receipt.json")
    verify_output_receipt(result, encoded, output, "verified MP4 output")
    verify_output_receipt(result, encoded, receipt_path, "Fit to Upload verification receipt")
    receipt = json_file(receipt_path, "Fit to Upload verification receipt")
    if receipt.get("status") != "ok" or output.stat().st_size > cap:
        raise SmokeFailure("Fit to Upload receipt does not verify an output under the exact cap")
    if sha256_file(output) != source_hash or sha256_file(source) != source_hash:
        raise SmokeFailure("already-fitting MP4 was not copied byte-for-byte or its source changed")


def smoke_tablebeam(result: dict[str, Any], skill: Path, scratch: Path, _dependencies: dict[str, Any]) -> None:
    csv_path = skill / "assets" / "demo-sales.csv"
    spec_path = skill / "assets" / "demo-query.json"
    source_hashes = {path.name: sha256_file(path) for path in (csv_path, spec_path)}
    export = scratch / "tablebeam-demo.csv"
    payload = run_json_cli(
        result,
        "bundled demo query",
        skill,
        [
            "scripts/tablebeam.py",
            "query",
            "assets/demo-sales.csv",
            "--spec",
            "assets/demo-query.json",
            "--export",
            str(export),
            "--export-format",
            "csv",
        ],
    )
    data = payload["data"]
    if data.get("matched_row_count", 0) < 1 or data.get("preview", {}).get("total", 0) < 1:
        raise SmokeFailure("Tablebeam bundled query returned no matching source rows")
    receipt = data.get("export_receipt", {})
    exported = output_record("Tablebeam query CSV", export)
    result["outputs"].append(exported)
    if receipt.get("verified") is not True or receipt.get("sha256") != exported["sha256"] or receipt.get("bytes") != exported["bytes"]:
        raise SmokeFailure("Tablebeam export receipt does not match the written CSV")
    with export.open("r", encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.reader(stream))
    if len(rows) < 2 or not rows[0]:
        raise SmokeFailure("Tablebeam export does not contain a header and result row")
    if any(sha256_file(path) != source_hashes[path.name] for path in (csv_path, spec_path)):
        raise SmokeFailure("Tablebeam changed a bundled demo input")


def smoke_shipproof(result: dict[str, Any], skill: Path, scratch: Path, _dependencies: dict[str, Any]) -> None:
    bundled_asset = skill / "examples" / "release" / "assets" / "app.js"
    original = bundled_asset.read_bytes()
    asset = scratch / "served-app.js"
    asset.write_bytes(original)
    expected_hash = sha256_bytes(original)
    marker = "demo-release-2026-09"
    if marker.encode("utf-8") not in original:
        raise SmokeFailure("bundled ShipProof asset is missing its documented marker")

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != "/assets/app.js":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript")
            self.send_header("Content-Length", str(len(original)))
            self.end_headers()
            self.wfile.write(original)

        def log_message(self, fmt: str, *args: Any) -> None:
            del fmt, args

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/assets/app.js"
        payload = run_json_cli(
            result,
            "temporary HTTP asset verification",
            skill,
            ["scripts/shipproof.py", "verify", "--url", url, "--asset", str(asset), "--marker", marker],
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    resources = payload["data"].get("resources", [])
    if len(resources) != 1 or resources[0].get("result") != "match" or resources[0].get("status_code") != 200:
        raise SmokeFailure("ShipProof did not match the temporary HTTP response to the local asset")
    if not any(check.get("check") == "sha256" and check.get("result") == "match" and check.get("actual") == expected_hash for check in resources[0].get("checks", [])):
        raise SmokeFailure("ShipProof did not verify the local and served SHA-256 values")
    result["outputs"].append(output_record("temporary HTTP expected asset", asset))


def is_inside_git(root: Path) -> bool:
    return any((parent / ".git").exists() for parent in (root, *root.parents))


def smoke_safe_tidy(result: dict[str, Any], skill: Path, scratch: Path, _dependencies: dict[str, Any]) -> None:
    if is_inside_git(scratch):
        raise SmokeFailure("Safe Tidy fixture must be outside every Git worktree")
    root = scratch / "tidy-input"
    root.mkdir()
    fixtures = {"brief.txt": b"fieldwork safe tidy fixture\n", "tone.MP3": b"synthetic audio bytes\n"}
    before = {name: sha256_bytes(content) for name, content in fixtures.items()}
    for name, content in fixtures.items():
        (root / name).write_bytes(content)
    plan_path = scratch / "tidy-plan.json"
    receipt_path = scratch / "tidy-undo.json"
    plan_result = run_json_cli(
        result,
        "Safe Tidy plan preview",
        skill,
        ["scripts/safe_tidy.py", "plan", "--root", str(root), "--output", str(plan_path)],
    )
    plan = json_file(plan_path, "Safe Tidy plan")
    expected_moves = {"brief.txt": "txt/brief.txt", "tone.MP3": "mp3/tone.MP3"}
    moves = {item["source"]: item["destination"] for item in plan.get("moves", [])}
    if moves != expected_moves or plan.get("conflicts") or plan_result["data"].get("moves") is None:
        raise SmokeFailure("Safe Tidy plan did not show the exact two synthetic file moves")
    result["outputs"].append(output_record("reviewed Safe Tidy plan", plan_path))

    applied = run_json_cli(
        result,
        "Safe Tidy apply",
        skill,
        ["scripts/safe_tidy.py", "apply", "--root", str(root), "--plan", str(plan_path), "--receipt", str(receipt_path)],
    )
    receipt = json_file(receipt_path, "Safe Tidy undo receipt")
    if applied["data"].get("moves_applied") != 2 or len(receipt.get("moves", [])) != 2:
        raise SmokeFailure("Safe Tidy did not apply both reviewed moves and create an undo receipt")
    for source, destination in expected_moves.items():
        moved = root / destination
        if not moved.is_file() or sha256_file(moved) != before[source] or (root / source).exists():
            raise SmokeFailure("Safe Tidy apply did not preserve the exact source bytes")
        result["outputs"].append(output_record(f"Safe Tidy moved {source}", moved))
    result["outputs"].append(output_record("Safe Tidy undo receipt", receipt_path))

    undone = run_json_cli(
        result,
        "Safe Tidy undo",
        skill,
        ["scripts/safe_tidy.py", "undo", "--root", str(root), "--receipt", str(receipt_path)],
    )
    if undone["data"].get("files_restored") != 2:
        raise SmokeFailure("Safe Tidy undo did not restore both files")
    for name, content in fixtures.items():
        restored = root / name
        if not restored.is_file() or restored.read_bytes() != content or sha256_file(restored) != before[name]:
            raise SmokeFailure("Safe Tidy undo did not restore the original file bytes")
        result["outputs"].append(output_record(f"Safe Tidy restored {name}", restored))


def smoke_cad(result: dict[str, Any], skill: Path, scratch: Path, dependencies: dict[str, Any]) -> None:
    ezdxf = dependencies["ezdxf"]
    if not ezdxf.get("available"):
        raise DependencyUnavailable("ezdxf 1.4.4 or later (before 2.0) is unavailable in the active Python")
    bundled_source = skill / "assets" / "demo-plan.dxf"
    bundled_hash = sha256_file(bundled_source)
    source = scratch / "synthetic-safe-geometry.dxf"
    preview = scratch / "synthetic-safe-geometry.svg"
    source.write_bytes(bundled_source.read_bytes())
    source_hash = sha256_file(source)
    result["outputs"].append(output_record("generated synthetic DXF input", source))

    inspected = run_json_cli(result, "generated DXF inspect", skill, ["scripts/cad_rescue.py", "inspect", "--input", str(source)])
    if inspected["data"].get("entity_count", 0) < 2:
        raise SmokeFailure("CAD Rescue did not inspect the two generated model-space entities")
    previewed = run_json_cli(
        result,
        "generated DXF safe preview",
        skill,
        ["scripts/cad_rescue.py", "preview", "--input", str(source), "--output", str(preview)],
    )
    receipt = previewed.get("data", {}).get("receipt", {})
    svg = output_record("generated CAD SVG preview", preview)
    result["outputs"].append(svg)
    if receipt.get("size_bytes") != svg["bytes"] or receipt.get("sha256") != svg["sha256"]:
        raise SmokeFailure("CAD Rescue preview receipt does not match the generated SVG")
    try:
        document = ElementTree.parse(preview)
    except ElementTree.ParseError as exc:
        raise SmokeFailure("CAD Rescue preview is not valid SVG XML") from exc
    allowed = {"svg", "g", "rect", "path"}
    tags = {node.tag.rsplit("}", 1)[-1] for node in document.iter()}
    if not tags <= allowed or "svg" not in tags or "path" not in tags:
        raise SmokeFailure("CAD Rescue preview contains unexpected content or omitted safe geometry")
    if previewed.get("data", {}).get("paths_written", 0) < 1:
        raise SmokeFailure("CAD Rescue preview did not render any geometry paths")
    if sha256_file(source) != source_hash or sha256_file(bundled_source) != bundled_hash:
        raise SmokeFailure("CAD Rescue changed the generated DXF source")


def smoke_ci(result: dict[str, Any], skill: Path, scratch: Path, _dependencies: dict[str, Any]) -> None:
    sample = skill / "assets" / "sample-runs.json"
    sample_hash = sha256_file(sample)
    report = scratch / "ci-spend-report.json"
    payload = run_json_cli(
        result,
        "bundled CI evidence analysis",
        skill,
        ["scripts/ci_spend_check.py", "analyze", "assets/sample-runs.json", "--output", str(report)],
        statuses={"needs_attention"},
    )
    data = payload["data"]
    if data.get("runs_analyzed", 0) < 1 or not data.get("workflows"):
        raise SmokeFailure("CI Spend Check did not analyze the bundled run evidence")
    saved = json_file(report, "CI Spend Check report")
    saved_output = output_record("CI Spend Check JSON report", report)
    result["outputs"].append(saved_output)
    if saved.get("status") != payload.get("status") or saved.get("data", {}).get("runs_analyzed") != data.get("runs_analyzed"):
        raise SmokeFailure("CI Spend Check saved report differs from its CLI JSON result")
    if sha256_file(sample) != sample_hash:
        raise SmokeFailure("CI Spend Check changed its bundled sample input")


def smoke_asset(result: dict[str, Any], skill: Path, scratch: Path, _dependencies: dict[str, Any]) -> None:
    source = skill / "assets" / "tiny-scene.gltf"
    source_hash = sha256_file(source)
    report = scratch / "tiny-scene-report.html"
    receipt_path = scratch / "tiny-scene-receipt.json"
    payload = run_json_cli(
        result,
        "bundled glTF inspection",
        skill,
        [
            "scripts/asset_check.py",
            "inspect",
            "--input",
            "assets/tiny-scene.gltf",
            "--max-triangles",
            "1",
            "--max-bytes",
            "4096",
            "--report",
            str(report),
            "--receipt",
            str(receipt_path),
        ],
    )
    data = payload["data"]
    scene = data.get("scene", {})
    active_index = scene.get("active_scene_index")
    scenes = scene.get("scenes", [])
    if active_index is None or active_index >= len(scenes) or scenes[active_index].get("triangle_candidates") != 1:
        raise SmokeFailure("Asset Check did not validate the bundled one-triangle scene")
    if any(data.get("budgets", {}).get(key, {}).get("result") != "within" for key in ("triangles", "bytes")):
        raise SmokeFailure("Asset Check did not verify both supplied budgets")
    report_record = output_record("Asset Check HTML report", report)
    result["outputs"].append(report_record)
    if not report.read_text(encoding="utf-8").lstrip().lower().startswith("<!doctype html"):
        raise SmokeFailure("Asset Check report is not a standalone HTML document")
    receipt_record = output_record("Asset Check JSON receipt", receipt_path)
    result["outputs"].append(receipt_record)
    saved = json_file(receipt_path, "Asset Check JSON receipt")
    if saved.get("status") != payload.get("status") or saved.get("data", {}).get("budgets") != data.get("budgets"):
        raise SmokeFailure("Asset Check JSON receipt differs from the CLI result")
    if sha256_file(source) != source_hash:
        raise SmokeFailure("Asset Check changed the bundled glTF source")


def probe_dependencies() -> dict[str, Any]:
    dependencies: dict[str, Any] = {}
    for executable_name in ("ffmpeg", "ffprobe"):
        command = shutil.which(executable_name)
        dependencies[executable_name] = {"available": command is not None, "command": command}
        if command:
            probe = subprocess.run([command, "-version"], capture_output=True, text=True, timeout=15, check=False)
            dependencies[executable_name]["version"] = (probe.stdout or probe.stderr).splitlines()[0][:160] if probe.returncode == 0 else "unavailable"
            dependencies[executable_name]["available"] = probe.returncode == 0
    if dependencies["ffmpeg"]["available"]:
        probe = subprocess.run(
            [dependencies["ffmpeg"]["command"], "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        dependencies["ffmpeg_libx264"] = probe.returncode == 0 and bool(re.search(r"\blibx264\b", probe.stdout))
    else:
        dependencies["ffmpeg_libx264"] = False

    if Path(PYTHON).is_file():
        probe = subprocess.run(
            [PYTHON, "-c", "import sys; print('.'.join(map(str, sys.version_info[:3])))"],
            capture_output=True,
            text=True,
            env=base_environment(),
            timeout=10,
            check=False,
        )
        dependencies["python"] = {
            "available": probe.returncode == 0,
            "command": "current interpreter",
            "version": probe.stdout.strip() if probe.returncode == 0 else None,
        }
        ez = subprocess.run(
            [PYTHON, "-c", "import ezdxf; print(ezdxf.__version__)"],
            capture_output=True,
            text=True,
            env=base_environment(),
            timeout=10,
            check=False,
        )
        version = ez.stdout.strip() if ez.returncode == 0 else None
        numbers = re.match(r"^(\d+)\.(\d+)\.(\d+)", version or "")
        acceptable = bool(numbers and (tuple(map(int, numbers.groups())) >= (1, 4, 4)) and int(numbers.group(1)) < 2)
        dependencies["ezdxf"] = {"available": acceptable, "version": version}
    else:
        dependencies["python"] = {"available": False, "command": "current interpreter", "version": None}
        dependencies["ezdxf"] = {"available": False, "version": None}
    return dependencies


PLUGIN_RUNS = {
    "frame-lab": (smoke_frame, "scripts/frame_lab.py"),
    "fit-to-upload": (smoke_fit, "scripts/fit_to_upload.py"),
    "tablebeam": (smoke_tablebeam, "scripts/tablebeam.py"),
    "shipproof": (smoke_shipproof, "scripts/shipproof.py"),
    "safe-tidy": (smoke_safe_tidy, "scripts/safe_tidy.py"),
    "cad-rescue": (smoke_cad, "scripts/cad_rescue.py"),
    "ci-spend-check": (smoke_ci, "scripts/ci_spend_check.py"),
    "asset-check": (smoke_asset, "scripts/asset_check.py"),
}
PLUGIN_IDS = tuple(PLUGIN_RUNS)


def smoke_plugin(plugin_id: str, archive_path: Path | None, release_hash: str | None, dependencies: dict[str, Any]) -> dict[str, Any]:
    item: dict[str, Any] = {"plugin": plugin_id, "status": "failed", "commands": [], "outputs": [], "checks": []}
    source = ROOT / "plugins" / plugin_id
    try:
        if not source.is_dir():
            raise SmokeFailure("source plugin directory is missing")
        item["source_tree_sha256"] = tree_sha256(source)
        if archive_path is None or not archive_path.is_file():
            raise SmokeFailure("built release ZIP is missing")
        archive_hash = sha256_file(archive_path)
        item["archive"] = {"file": archive_path.name, "bytes": archive_path.stat().st_size, "sha256": archive_hash}
        if release_hash and archive_hash != release_hash:
            raise SmokeFailure("archive SHA-256 differs from dist/releases.json")
        with tempfile.TemporaryDirectory(prefix=f"fieldwork-{plugin_id}-") as temporary:
            isolated = Path(temporary)
            if isolated.is_relative_to(ROOT):
                raise SmokeFailure("temporary extraction unexpectedly resides inside the repository")
            unpacked = isolated / "package"
            unpacked.mkdir()
            safe_extract(archive_path, unpacked)
            extracted_hash = tree_sha256(unpacked)
            item["extracted_tree_sha256"] = extracted_hash
            if extracted_hash != item["source_tree_sha256"]:
                raise SmokeFailure("source tree and extracted archive contents differ")
            skill = unpacked / "skills" / plugin_id
            if not skill.is_dir():
                raise SmokeFailure("skill directory is missing from the release archive")
            if not dependencies.get("python", {}).get("available"):
                raise DependencyUnavailable("the active Python interpreter is unavailable")
            item["isolation"] = {
                "fresh_temporary_directory": True,
                "outside_repository": True,
                "working_directory": "unpacked skill directory",
                "pythonpath": "unset",
            }
            smoke_case, help_script = PLUGIN_RUNS[plugin_id]
            run_help(item, skill, help_script)
            scratch = isolated / "smoke-output"
            scratch.mkdir()
            smoke_case(item, skill, scratch, dependencies)
        item["status"] = "completed"
    except DependencyUnavailable as exc:
        item["status"] = "dependency-skipped"
        item["reason"] = str(exc)
    except SmokeFailure as exc:
        item["status"] = "failed"
        item["error"] = str(exc)[:500]
    except (OSError, subprocess.SubprocessError, zipfile.BadZipFile) as exc:
        item["status"] = "failed"
        item["error"] = type(exc).__name__
    return item


def main() -> int:
    dist = ROOT / "dist"
    archive_map, release_hashes = plugin_archive_map(dist)
    dependencies = probe_dependencies()
    plugins = [
        smoke_plugin(plugin_id, archive_map.get(plugin_id), release_hashes.get(plugin_id), dependencies)
        for plugin_id in PLUGIN_IDS
    ]
    if any(item["status"] == "failed" for item in plugins):
        outcome = "failed"
    elif any(item["status"] == "dependency-skipped" for item in plugins):
        outcome = "completed_with_dependency_skips"
    else:
        outcome = "completed"
    summary = {
        "schema_version": 1,
        "created_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "result": outcome,
        "python": dependencies.get("python"),
        "dependencies": {
            key: {k: v for k, v in value.items() if k != "command"} if isinstance(value, dict) else value
            for key, value in dependencies.items()
            if key not in {"python", "ezdxf"}
        } | {"ezdxf": dependencies.get("ezdxf")},
        "plugins": plugins,
    }
    output = ROOT / ".artifacts" / "isolation" / "summary.json"
    dump_json(output, summary)
    print(json.dumps({"result": outcome, "summary": ".artifacts/isolation/summary.json", "plugins": [{"plugin": item["plugin"], "status": item["status"]} for item in plugins]}, ensure_ascii=False))
    return 1 if outcome == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
