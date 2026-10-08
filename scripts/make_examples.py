#!/usr/bin/env python3
"""Produce catalog examples by actually invoking the plugin tools on synthetic inputs."""
import argparse
import functools
import hashlib
import http.server
import json
import runpy
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def cli(plugin, *args):
    path = ROOT / "plugins" / plugin / "skills" / plugin / "scripts" / (plugin.replace("-", "_") + ".py")
    result = subprocess.run([sys.executable, str(path), *map(str, args)], capture_output=True, text=True, timeout=90)
    if result.returncode:
        raise RuntimeError(f"{plugin}: {result.stdout[-1000:]} {result.stderr[-1000:]}")
    return json.loads(result.stdout)


def normalized(value, replacements):
    if isinstance(value, dict):
        return {key: normalized(item, replacements) for key, item in value.items() if key not in {"created_at_utc"}}
    if isinstance(value, list):
        return [normalized(item, replacements) for item in value]
    if isinstance(value, str):
        for source, target in replacements:
            value = value.replace(source, target)
    return value


def example(title, summary, stats, columns, rows, receipt):
    return {"dataset_kind": "synthetic", "normalization": "Temporary paths and loopback server ports normalized for publication; not an executable receipt.", "title": title, "summary": summary, "stats": [{"label": label, "value": str(value)} for label, value in stats], "columns": columns, "rows": rows, "receipt": receipt}


def publish_sample(source, name, label):
    output = ROOT / "site" / "examples" / "files"
    output.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, output / name)
    return {"url": "examples/files/" + name, "label": label}


def make_ci_spend_check(tmp):
    source = ROOT / "plugins/ci-spend-check/skills/ci-spend-check/assets/sample-runs.json"
    result = cli("ci-spend-check", "analyze", source)
    data = result["data"]
    first = data["workflows"][0]
    return example("Three failures worth investigating.", "This synthetic history contains repeated build failures. The report shows elapsed and job time separately; no monetary saving is inferred.", [("Run attempts", data["runs_analyzed"]), ("Repeated failures", first["failed_runs"]), ("Job minutes", first["measured_job_minutes"])], ["Workflow", "Failures", "Run elapsed"], [[row["name"], str(row["failed_runs"]), f'{row["run_elapsed_minutes"]} min'] for row in data["workflows"]], result)


def make_fit_to_upload(tmp):
    source, target = tmp / "sample.mp4", tmp / "upload.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=15:duration=2", "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=44100:duration=2", "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(source)], capture_output=True, check=True, timeout=30)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    cap = source.stat().st_size - 1
    result = cli("fit-to-upload", "encode", "--input", source, "--output", target, "--max-bytes", cap)
    data = result["data"]
    if target.stat().st_size > cap or hashlib.sha256(source.read_bytes()).hexdigest() != before:
        raise RuntimeError("Fit to Upload example failed output/original verification")
    view = example("Under the limit. Original preserved.", "A two-second generated video was one byte over the selected limit. The tool encoded a copy, retained its dimensions and copied the audio stream.", [("Output bytes", f'{data["actual_bytes"]:,}'), ("Byte limit", f'{cap:,}'), ("Duration", f'{data["duration_seconds"]} s')], ["Check", "Result"], [["Within cap", str(data["validation"]["within_cap"])], ["Original SHA-256", "Unchanged"], ["Audio", "Stream copied"]], result)
    view["artifacts"] = [publish_sample(target, "fit-to-upload.mp4", "Play the generated output")]
    return view


def make_frame_lab(tmp):
    result = cli("frame-lab", "demo")
    comparison = result["data"]["comparison"]
    if not comparison["comparable"] or not all(row["valid"] for row in comparison["runs"]):
        raise RuntimeError("Frame Lab demonstration must contain valid, comparable synthetic captures")
    baseline, candidate = comparison["runs"][:2]
    frame_module = runpy.run_path(str(ROOT / "plugins/frame-lab/skills/frame-lab/scripts/frame_lab.py"))
    frame_module["_write_demo_capture"](tmp, "demo-baseline", 8_000_000, 20_000_000)
    frame_module["_write_demo_capture"](tmp, "demo-candidate", 7_500_000, 18_000_000)
    report = tmp / "frame-lab.html"
    exported = cli("frame-lab", "export", "--evidence-dir", tmp, "--runs", "demo-baseline", "demo-candidate", "--output", report, "--format", "html")
    view = example("Compare the slow frames, too.", "These generated frame intervals demonstrate the report format. They are not Minecraft performance measurements or a promised improvement.", [("Baseline 1% low", f'{baseline["one_percent_low_fps"]:.1f}'), ("Candidate 1% low", f'{candidate["one_percent_low_fps"]:.1f}'), ("Comparison", "Valid")], ["Synthetic capture", "Average FPS", "p99 frame time"], [[row["run"], f'{row["average_fps"]:.1f}', f'{row["p99_ms"]:.1f} ms'] for row in comparison["runs"]], {"analysis": result, "export": exported})
    view["artifacts"] = [publish_sample(report, "frame-lab.html", "Open the full comparison report")]
    return view


def make_tablebeam(tmp):
    source = tmp / "orders.csv"
    source.write_text("order_id,region,amount\n9007199254740993,North,0.10\n9007199254740994,North,0.20\n9007199254740995,South,12.35\n", encoding="utf-8")
    spec = tmp / "query.json"
    spec.write_text(json.dumps({"group_by": ["region"], "aggregate": {"op": "sum", "column": "amount"}}))
    result = cli("tablebeam", "query", source, "--spec", spec)
    data = result["data"]
    rows = data["result_rows"]
    return example("0.10 + 0.20 = exactly 0.30.", "An exact decimal calculation over synthetic orders. Long order identifiers remain strings, and each group points back to its contributing records.", [("Source rows", data["matched_row_count"]), ("Groups", data["result_group_count"]), ("Arithmetic", "Exact")], ["Region", "Total", "Source rows"], [[str(row["group"]["region"]), str(row["value"]), str(row["rows"])] for row in rows], result)


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass


def make_shipproof(tmp):
    local = tmp / "release.js"
    local.write_text('window.RELEASE = "synthetic-release-1";\n')
    handler = functools.partial(QuietHandler, directory=str(tmp))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    try:
        result = cli("shipproof", "verify", "--url", origin + "/release.js", "--asset", local, "--marker", "synthetic-release-1")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    if result["status"] != "ok":
        raise RuntimeError("ShipProof sample asset did not match")
    resource = result["data"]["resources"][0]
    result = normalized(result, [(origin, "http://localhost:PORT")])
    return example("The served bytes match the release.", "This example starts a real local HTTP server and verifies its synthetic JavaScript asset. It establishes the byte match; it does not claim a browser user-flow test.", [("HTTP status", resource["status_code"]), ("Asset", resource["result"]), ("Checks", len(resource["checks"]))], ["Evidence", "Result"], [[check["check"], check["result"]] for check in resource["checks"]], result)


def make_safe_tidy(tmp):
    folder = tmp / "inbox"
    folder.mkdir()
    originals = {"notes.txt": b"a synthetic note\n", "notes-copy.txt": b"a synthetic note\n", "totals.csv": b"item,total\nexample,12\n"}
    for name, data in originals.items():
        (folder / name).write_bytes(data)
    inventory = cli("safe-tidy", "inventory", "--root", folder)
    plan = cli("safe-tidy", "plan", "--root", folder, "--output", tmp / "plan.json")
    applied = cli("safe-tidy", "apply", "--root", folder, "--plan", tmp / "plan.json", "--receipt", tmp / "receipt.json")
    undone = cli("safe-tidy", "undo", "--root", folder, "--receipt", tmp / "receipt.json")
    if any((folder / name).read_bytes() != data for name, data in originals.items()):
        raise RuntimeError("Safe Tidy sample did not restore original bytes")
    return example("Three moves. A verified way back.", "The tool organized three generated files, then restored their original names and bytes using its receipt. Duplicate detection never deleted a file.", [("Planned moves", len(plan["data"]["moves"])), ("Duplicate groups", len(inventory["data"]["duplicate_groups"])), ("Restored files", undone["data"]["files_restored"])], ["Source", "Planned destination"], [[row["source"], row["destination"]] for row in plan["data"]["moves"]], {"inventory": inventory, "plan": plan, "apply": applied, "undo": undone})


def make_cad_rescue(tmp):
    import ezdxf

    source, preview = tmp / "sample-plan.dxf", tmp / "sample-plan.svg"
    drawing = ezdxf.new("R2018")
    drawing.units = ezdxf.units.M
    drawing.layers.new("Walls")
    drawing.layers.new("Doors")
    drawing.layers.new("Furniture")
    model = drawing.modelspace()
    model.add_lwpolyline([(0, 0), (12, 0), (12, 8), (0, 8)], close=True, dxfattribs={"layer": "Walls"})
    for start, end in [((7, 0), (7, 4)), ((7, 5.2), (7, 8)), ((7, 4), (12, 4))]:
        model.add_line(start, end, dxfattribs={"layer": "Walls"})
    model.add_arc((7, 4), 1.2, 90, 180, dxfattribs={"layer": "Doors"})
    model.add_line((7, 4), (5.8, 4), dxfattribs={"layer": "Doors"})
    model.add_circle((3.5, 4), 1.2, dxfattribs={"layer": "Furniture"})
    model.add_lwpolyline([(8, 5), (11, 5), (11, 7), (8, 7)], close=True, dxfattribs={"layer": "Furniture"})
    drawing.saveas(source)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    inspected = cli("cad-rescue", "inspect", "--input", source)
    rendered = cli("cad-rescue", "preview", "--input", source, "--output", preview)
    if inspected["status"] != "ok" or rendered["status"] != "ok" or before != hashlib.sha256(source.read_bytes()).hexdigest():
        raise RuntimeError("CAD Rescue sample did not produce a complete preview with the original preserved")
    data = inspected["data"]
    view = example("A drawing you can inspect.", "This generated floor plan contains eight vector entities across three drawing layers. CAD Rescue reports its units and extents, then creates an SVG from the actual geometry.", [("Entities", data["entity_count"]), ("Units", data["units"]["name"]), ("Preview paths", rendered["data"]["paths_written"])], ["Layer", "Entities"], [[name, str(layer["entities"])] for name, layer in data["layers"].items() if layer["entities"]], {"inspection": inspected, "preview": rendered})
    view["preview_image"] = publish_sample(preview, "cad-rescue.svg", "Generated floor-plan preview")
    view["artifacts"] = [publish_sample(source, "cad-rescue.dxf", "Download the synthetic DXF"), view["preview_image"]]
    return view


def make_asset_check(tmp):
    source = tmp / "asset-check.gltf"
    shutil.copyfile(ROOT / "plugins/asset-check/skills/asset-check/assets/tiny-scene.gltf", source)
    report = tmp / "asset-check.html"
    result = cli("asset-check", "inspect", "--input", source, "--max-triangles", 1, "--max-bytes", 4096, "--report", report)
    if result["status"] != "ok":
        raise RuntimeError("Asset Check sample did not pass its explicit budgets")
    data = result["data"]
    view = example("Know the asset before it hits the scene.", "One synthetic indexed triangle, one embedded texture, and explicit budgets. Counts come from validated structure and accessor ranges; they are not frame-rate predictions.", [("Triangle candidates", data["budgets"]["triangles"]["measured"]), ("Asset bytes", data["file_footprint"]["total_asset_bytes"]), ("Materials", data["materials"]["material_count"])], ["Budget", "Measured", "Limit", "Result"], [[name, str(budget["measured"]), str(budget["limit"]), budget["result"]] for name, budget in data["budgets"].items()], result)
    view["artifacts"] = [publish_sample(report, "asset-check.html", "Open the full asset report"), publish_sample(source, "asset-check.gltf", "Download the synthetic glTF")]
    return view


MAKERS = {"ci-spend-check": make_ci_spend_check, "fit-to-upload": make_fit_to_upload, "frame-lab": make_frame_lab, "tablebeam": make_tablebeam, "shipproof": make_shipproof, "safe-tidy": make_safe_tidy, "cad-rescue": make_cad_rescue, "asset-check": make_asset_check}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin", choices=sorted(MAKERS))
    args = parser.parse_args()
    output = ROOT / "site" / "examples"
    output.mkdir(exist_ok=True)
    selected = [args.plugin] if args.plugin else list(MAKERS)
    for plugin in selected:
        with tempfile.TemporaryDirectory(prefix="fieldwork-example-") as temporary:
            value = MAKERS[plugin](Path(temporary))
            value = normalized(value, [(temporary, "<sample-directory>"), (str(ROOT), "<plugin-source>")])
            (output / f"{plugin}.json").write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
        print(plugin + ": generated from real command output")


if __name__ == "__main__":
    main()
