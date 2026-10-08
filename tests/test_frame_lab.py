import csv
import hashlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "plugins/frame-lab/skills/frame-lab/scripts/frame_lab.py"
)
SPEC = importlib.util.spec_from_file_location("frame_lab", SCRIPT)
frame_lab = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(frame_lab)


def profile(**changes):
    value = {
        "schema_version": 1,
        "host_id": "synthetic-machine",
        "minecraft_version": "26.3",
        "loader": "Fabric 0.18",
        "java_major": 25,
        "framebuffer_px": {"width": 1920, "height": 1080},
        "scene_id": "demo-fixed-pan",
        "settings": {
            "frame_limit": 120,
            "vsync": False,
            "render_distance": 16,
            "shader": "demo-shader",
        },
        "dataset_kind": "synthetic",
    }
    value.update(changes)
    return value


def write_capture(root, run, base_ns=8_000_000, slow_ns=20_000_000, interval_count=10_000):
    root.mkdir(parents=True, exist_ok=True)
    frames = root / f"{run}-frames.csv"
    # Public harness CSV contract: frame zero is the zero-interval anchor.
    slow_count = min(200, max(1, interval_count // 50))
    base_count = interval_count - slow_count
    with frames.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("frame", "nanotime", "interval_ns"))
        writer.writerow((0, 1_000_000_000, 0))
        now = 1_000_000_000
        for index in range(interval_count):
            interval = base_ns if index < base_count else slow_ns
            now += interval
            writer.writerow((index + 1, now, interval))

    (root / f"{run}-start.txt").write_text(
        "hook=Minecraft.renderFrame(boolean)\n"
        "seconds=120\n"
        "live_inactivity_policy=MINIMIZED\n"
        "live_throttle_reason=NONE\n"
        "live_frame_limit=120\n"
    )
    (root / f"{run}-done.txt").write_text(
        f"frames={interval_count + 1}\n"
        "unfocused_frames=0\n"
        "frame_sink_error=\n"
        "buffer_full=false\n"
        "hook=Minecraft.renderFrame(boolean) return; CPU frame-production interval\n"
    )
    (root / f"{run}-route.json").write_text(
        json.dumps(
            {
                "kind": "fixed-position demo pan",
                "seconds": 120.0,
                "positions": [{"position": [0, 64, 0], "tick": 1000}],
                "world_start": {"dimension": "overworld", "world_label": "demo"},
                "world_end": {"dimension": "overworld", "world_label": "demo"},
                "yaw_start": 0,
                "pitch": 5,
                "weather": "clear",
                "day_start": 6000,
            }
        )
    )
    (root / f"{run}-profile.json").write_text(json.dumps(profile()))
    (root / f"{run}-context.json").write_text(
        json.dumps({"environment_gate": {"passed": True}, "dataset_kind": "synthetic"})
    )
    return frames


class FrameLabTests(unittest.TestCase):
    def test_public_capture_schema_reports_true_slowest_one_percent_low(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = Path(tmp) / "evidence"
            write_capture(evidence, "demo-baseline")

            result = frame_lab.inspect_capture(evidence, "demo-baseline")

            self.assertTrue(result["valid"], result["invalid_reasons"])
            self.assertEqual(result["frame_intervals"], 10_000)
            self.assertEqual(result["p99_ms"], 20.0)
            self.assertEqual(result["one_percent_low_fps"], 50.0)
            self.assertEqual(result["metric_scope"], "cpu_frame_production")

    def test_comparison_checks_profiles_and_route_before_reporting_a_winner(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = Path(tmp) / "evidence"
            write_capture(evidence, "demo-baseline")
            write_capture(evidence, "demo-candidate", base_ns=7_500_000, slow_ns=18_000_000)

            compatible = frame_lab.compare_captures(
                evidence, ["demo-baseline", "demo-candidate"]
            )
            self.assertTrue(compatible["comparable"])
            self.assertEqual(compatible["status"], "ok")

            candidate_profile = profile(framebuffer_px={"width": 1280, "height": 720})
            (evidence / "demo-candidate-profile.json").write_text(
                json.dumps(candidate_profile)
            )
            incompatible = frame_lab.compare_captures(
                evidence, ["demo-baseline", "demo-candidate"]
            )
            self.assertFalse(incompatible["comparable"])
            self.assertTrue(
                any("framebuffer_px" in item for item in incompatible["incompatibilities"])
            )
            self.assertIsNone(incompatible["preferred_run"])

    def test_comparison_rejects_missing_background_preflight(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = Path(tmp) / "evidence"
            write_capture(evidence, "demo-baseline", interval_count=20)
            write_capture(evidence, "demo-candidate", interval_count=20)
            (evidence / "demo-candidate-context.json").unlink()

            result = frame_lab.compare_captures(evidence, ["demo-baseline", "demo-candidate"])

            self.assertFalse(result["comparable"])
            self.assertIsNone(result["preferred_run"])
            self.assertTrue(any("preflight" in item.lower() for item in result["incompatibilities"]))

    def test_invalid_focus_and_non_numeric_rows_are_rejected_without_losing_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = Path(tmp) / "evidence"
            write_capture(evidence, "demo-invalid")
            done = evidence / "demo-invalid-done.txt"
            done.write_text(done.read_text().replace("unfocused_frames=0", "unfocused_frames=2"))
            result = frame_lab.inspect_capture(evidence, "demo-invalid")
            self.assertFalse(result["valid"])
            self.assertIn("focus", " ".join(result["invalid_reasons"]).lower())

            frames = evidence / "demo-invalid-frames.csv"
            with frames.open(newline="") as source:
                rows = list(csv.reader(source))
            rows[2][2] = "NaN"
            with frames.open("w", newline="") as output:
                csv.writer(output).writerows(rows)
            malformed = frame_lab.inspect_capture(evidence, "demo-invalid")
            self.assertFalse(malformed["valid"])
            self.assertTrue(malformed["invalid_reasons"])

    def test_small_capture_does_not_claim_tail_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = Path(tmp) / "evidence"
            write_capture(evidence, "demo-small")
            frames = evidence / "demo-small-frames.csv"
            with frames.open(newline="") as source:
                rows = list(csv.reader(source))
            with frames.open("w", newline="") as output:
                csv.writer(output).writerows(rows[:1002])
            done = evidence / "demo-small-done.txt"
            done.write_text(done.read_text().replace("frames=10001", "frames=1002"))

            result = frame_lab.inspect_capture(evidence, "demo-small")

            self.assertIsNone(result["p99_ms"])
            self.assertIsNone(result["one_percent_low_fps"])
            self.assertTrue(any("sample" in item.lower() for item in result["warnings"]))

    def test_export_previews_then_writes_a_receipt_without_changing_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            evidence = root / "evidence"
            write_capture(evidence, "demo-baseline")
            write_capture(evidence, "demo-candidate", base_ns=7_500_000, slow_ns=18_000_000)
            metrics_before = {
                run: frame_lab.inspect_capture(evidence, run)
                for run in ("demo-baseline", "demo-candidate")
            }
            evidence_before = {
                path.name: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in evidence.iterdir()
                if path.is_file()
            }
            output = root / "portable-report.json"

            plan = frame_lab.export_report(
                evidence, ["demo-baseline", "demo-candidate"], output, "json", dry_run=True
            )
            self.assertFalse(output.exists())
            self.assertEqual(plan["status"], "ok")
            self.assertEqual(evidence_before, {
                path.name: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in evidence.iterdir()
                if path.is_file()
            })

            written = frame_lab.export_report(
                evidence, ["demo-baseline", "demo-candidate"], output, "json"
            )
            self.assertEqual(written["receipt"]["sha256"], hashlib.sha256(output.read_bytes()).hexdigest())
            self.assertNotIn(str(evidence), output.read_text())
            html_output = root / "portable-report.html"
            html_result = frame_lab.export_report(
                evidence, ["demo-baseline", "demo-candidate"], html_output, "html"
            )
            html_text = html_output.read_text()
            self.assertIn("<!doctype html>", html_text)
            self.assertIn("class=\"synthetic-banner\"", html_text)
            self.assertIn("class=\"status-banner validated\"", html_text)
            self.assertIn("class=\"chart-runs\"", html_text)
            self.assertIn("Slowest 1% low", html_text)
            self.assertIn("width:93.6%", html_text)
            self.assertIn("width:100.0%", html_text)
            self.assertIn("121.4 FPS", html_text)
            self.assertIn("55.6 FPS", html_text)
            self.assertNotIn("121.35922330097087", html_text)
            self.assertIn("overflow-x:auto", html_text)
            self.assertIn("Frame-time metrics table", html_text)
            self.assertNotIn("<script", html_text.lower())
            self.assertNotIn("http:", html_text.lower())
            self.assertNotIn(str(evidence), html_text)
            self.assertEqual(html_result["receipt"]["sha256"], hashlib.sha256(html_output.read_bytes()).hexdigest())
            self.assertEqual(
                {
                    run: frame_lab.inspect_capture(evidence, run)
                    for run in ("demo-baseline", "demo-candidate")
                },
                metrics_before,
            )
            self.assertEqual(evidence_before, {
                path.name: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in evidence.iterdir()
                if path.is_file()
            })
            with self.assertRaises(frame_lab.LabError) as error:
                frame_lab.export_report(
                    evidence, ["demo-baseline", "demo-candidate"], output, "json"
                )
            self.assertEqual(error.exception.code, "output_exists")

    def test_html_export_makes_capture_rejections_prominent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            evidence = root / "evidence"
            write_capture(evidence, "demo-invalid", interval_count=20)
            done = evidence / "demo-invalid-done.txt"
            done.write_text(done.read_text().replace("unfocused_frames=0", "unfocused_frames=2"))
            output = root / "rejected-capture.html"

            frame_lab.export_report(evidence, ["demo-invalid"], output, "html")

            report = output.read_text()
            self.assertIn("class=\"status-banner rejected\"", report)
            self.assertIn("Capture rejected", report)
            self.assertIn("lost focus", report.lower())

    def test_html_export_blocks_ranking_for_incompatible_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            evidence = root / "evidence"
            write_capture(evidence, "demo-baseline", interval_count=20)
            write_capture(evidence, "demo-candidate", interval_count=20)
            (evidence / "demo-candidate-profile.json").write_text(
                json.dumps(profile(framebuffer_px={"width": 1280, "height": 720}))
            )
            output = root / "incompatible-comparison.html"

            frame_lab.export_report(
                evidence, ["demo-baseline", "demo-candidate"], output, "html"
            )

            report = output.read_text()
            self.assertIn("class=\"status-banner attention\"", report)
            self.assertIn("Comparison blocked", report)
            self.assertIn("profile.framebuffer_px differs", report)
            self.assertIn("does not rank incompatible runs", report)
            self.assertNotIn("leads this pair", report)

    def test_doctor_checks_only_the_explicit_prism_and_game_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prism = root / "prism"
            game = prism / "instances" / "Bench" / ".minecraft"
            (prism / "instances").mkdir(parents=True)
            (game / "minescript").mkdir(parents=True)
            (game / "mods").mkdir()
            (game / "shaderpacks").mkdir()
            (game / "options.txt").write_text("inactivityFpsLimit:\"minimized\"\n")
            (game.parent / "mmc-pack.json").write_text(
                json.dumps(
                    {
                        "components": [
                            {"uid": "net.minecraft", "version": "26.3"},
                            {"uid": "net.fabricmc.fabric-loader", "version": "0.18.0"},
                        ]
                    }
                )
            )
            with zipfile.ZipFile(game / "mods" / "minescript-fabric.jar", "w") as archive:
                archive.writestr("fabric.mod.json", json.dumps({"id": "minescript", "version": "5.0.0"}))

            result = frame_lab.diagnose_setup(game, prism)

            self.assertFalse(result["setup_ready"])
            self.assertTrue(result["game_dir_checks"]["options_txt"])
            self.assertTrue(result["prism_dir_checks"]["instances_directory"])
            self.assertEqual(result["instance"]["minecraft_version"], "26.3")
            self.assertTrue(result["instance"]["minescript_5_detected"])
            self.assertFalse(result["active_client_touched"])

    def test_demo_cli_compares_valid_synthetic_runs(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = frame_lab.main(["demo"])
        result = json.loads(output.getvalue())
        comparison = result["data"]["comparison"]
        self.assertEqual(code, 0)
        self.assertEqual(result["data"]["dataset_kind"], "synthetic")
        self.assertTrue(comparison["comparable"])
        self.assertTrue(all(run["valid"] for run in comparison["runs"]))
        self.assertTrue(all(run["dataset_kind"] == "synthetic" for run in comparison["runs"]))
        self.assertIsNotNone(comparison["preferred_run"])
        self.assertTrue(any("synthetic" in item.lower() for item in comparison["warnings"]))

    def test_cli_inspection_uses_the_documented_json_envelope(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = Path(tmp) / "evidence"
            write_capture(evidence, "demo-baseline")
            output = io.StringIO()
            with redirect_stdout(output):
                code = frame_lab.main(
                    ["inspect", "--evidence-dir", str(evidence), "--run", "demo-baseline"]
                )
            result = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(result["schema_version"], 1)
            self.assertEqual(result["tool"], "frame-lab")
            self.assertEqual(result["status"], "ok")

    def test_inspection_of_real_raw_evidence_is_read_only(self):
        evidence_value = os.environ.get("FRAME_LAB_RAW_EVIDENCE")
        if not evidence_value:
            self.skipTest("set FRAME_LAB_RAW_EVIDENCE for the read-only integration check")
        evidence = Path(evidence_value)
        captures = sorted(evidence.glob("*-frames.csv"))
        run = None
        for item in captures:
            candidate = item.name[: -len("-frames.csv")]
            start_path = evidence / f"{candidate}-start.txt"
            done_path = evidence / f"{candidate}-done.txt"
            route_path = evidence / f"{candidate}-route.json"
            if not all(path.is_file() for path in (start_path, done_path, route_path)):
                continue
            start_fields = {
                line.split("=", 1)[0]
                for line in start_path.read_text(errors="replace").splitlines()
                if "=" in line
            }
            done_fields = {
                line.split("=", 1)[0]
                for line in done_path.read_text(errors="replace").splitlines()
                if "=" in line
            }
            if not {"live_inactivity_policy", "live_throttle_reason", "live_frame_limit"}.issubset(start_fields):
                continue
            if not {"frame_sink_error", "buffer_full"}.issubset(done_fields):
                continue
            inspected = frame_lab.inspect_capture(evidence, candidate)
            if inspected["valid"]:
                run = candidate
                break
        if run is None:
            self.fail("raw evidence directory contains no valid current-format capture")
        before = {
            path.name: (path.stat().st_size, path.stat().st_mtime_ns)
            for path in evidence.iterdir()
            if path.is_file()
        }

        result = frame_lab.inspect_capture(evidence, run)

        after = {
            path.name: (path.stat().st_size, path.stat().st_mtime_ns)
            for path in evidence.iterdir()
            if path.is_file()
        }
        self.assertGreater(result["frame_intervals"], 0)
        self.assertTrue(result["valid"], result["invalid_reasons"])
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
