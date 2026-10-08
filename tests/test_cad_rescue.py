"""CAD Rescue CLI checks against synthetic DXF and SVG files only."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import ezdxf


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "plugins" / "cad-rescue" / "skills" / "cad-rescue" / "scripts" / "cad_rescue.py"
DEMO_DXF = ROOT / "plugins" / "cad-rescue" / "skills" / "cad-rescue" / "assets" / "demo-plan.dxf"


class CadRescueCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="cad-rescue-test-")
        self.root = Path(self.temp.name)
        self.source = self.root / "synthetic.dxf"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_cli(self, *args: str) -> tuple[subprocess.CompletedProcess[str], dict]:
        completed = subprocess.run(
            [sys.executable, str(CLI), *map(str, args)],
            text=True,
            capture_output=True,
            check=False,
        )
        try:
            envelope = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            self.fail(
                f"CLI did not return a JSON envelope: {completed.stdout!r}; "
                f"stderr={completed.stderr!r}; {exc}"
            )
        self.assertEqual(envelope.get("schema_version"), 1)
        self.assertEqual(envelope.get("tool"), "cad-rescue")
        return completed, envelope

    def make_drawing(self) -> Path:
        doc = ezdxf.new("R2018")
        doc.units = ezdxf.units.M
        modelspace = doc.modelspace()
        x0, y0 = 1_000_000_000.0, 2_000_000_000.0
        modelspace.add_line((x0, y0), (x0 + 8, y0 + 4), dxfattribs={"layer": "Walls"})
        modelspace.add_arc(
            (x0 + 4, y0 + 2), 2, 0, 120, dxfattribs={"layer": "Curves"}
        )
        modelspace.add_ellipse(
            (x0 + 14, y0 + 5),
            major_axis=(3, 1),
            ratio=0.5,
            dxfattribs={"layer": "Curves"},
        )
        block = doc.blocks.new(name="Marker")
        block.add_line((0, 0), (2, 2))
        modelspace.add_blockref(
            "Marker",
            (x0 + 30, y0 + 10),
            dxfattribs={"layer": "Symbols", "xscale": 2, "yscale": 2},
        )
        doc.saveas(self.source)
        return self.source

    def test_inspect_reports_units_layers_and_transformed_large_coordinate_bounds(self) -> None:
        self.make_drawing()

        completed, envelope = self.run_cli("inspect", "--input", str(self.source))

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(envelope["status"], "ok")
        data = envelope["data"]
        self.assertEqual(data["format"], "dxf")
        self.assertEqual(data["units"]["name"], "Meters")
        self.assertEqual(data["entity_count"], 4)
        self.assertEqual(data["layers"]["Walls"]["entities"], 1)
        self.assertEqual(data["layers"]["Curves"]["entities"], 2)
        self.assertEqual(data["layers"]["Symbols"]["entities"], 1)
        self.assertAlmostEqual(data["extents"]["min_x"], 1_000_000_000.0)
        self.assertAlmostEqual(data["extents"]["min_y"], 2_000_000_000.0)
        self.assertAlmostEqual(data["extents"]["max_x"], 1_000_000_034.0)
        self.assertAlmostEqual(data["extents"]["max_y"], 2_000_000_014.0)

    def test_packaged_floor_plan_is_a_working_first_run_example(self) -> None:
        original_hash = hashlib.sha256(DEMO_DXF.read_bytes()).hexdigest()
        inspected, report = self.run_cli("inspect", "--input", str(DEMO_DXF))
        output = self.root / "demo-preview.svg"
        previewed, preview = self.run_cli(
            "preview", "--input", str(DEMO_DXF), "--output", str(output)
        )

        self.assertEqual(inspected.returncode, 0, inspected.stderr)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["data"]["entity_count"], 8)
        self.assertEqual(report["data"]["units"]["name"], "Meters")
        self.assertEqual(set(report["data"]["layers"]), {"Walls", "Doors", "Furniture"})
        self.assertEqual(previewed.returncode, 0, previewed.stderr)
        self.assertEqual(preview["status"], "ok")
        self.assertGreater(preview["data"]["paths_written"], 0)
        self.assertEqual(hashlib.sha256(DEMO_DXF.read_bytes()).hexdigest(), original_hash)

    @unittest.skipUnless(shutil.which("rsvg-convert"), "rsvg-convert is required to inspect the rendered preview")
    def test_preview_contains_curves_and_insert_geometry_and_renders_as_svg(self) -> None:
        self.make_drawing()
        original_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        preview = self.root / "preview.svg"

        completed, envelope = self.run_cli(
            "preview", "--input", str(self.source), "--output", str(preview)
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(envelope["status"], "ok")
        self.assertEqual(envelope["data"]["paths_written"], 4)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), original_hash)
        xml_root = ET.fromstring(preview.read_bytes())
        ns = {"svg": "http://www.w3.org/2000/svg"}
        paths = xml_root.findall(".//svg:path", ns)
        self.assertEqual(len(paths), 4)
        self.assertTrue(any("C" in element.attrib.get("d", "") for element in paths))
        self.assertTrue(all(element.attrib.get("stroke-width") == "1.25" for element in paths))
        self.assertTrue(all(element.attrib.get("vector-effect") == "non-scaling-stroke" for element in paths))
        viewbox = [float(value) for value in xml_root.attrib["viewBox"].split()]
        self.assertLess(viewbox[0], 1_000_000_000)
        self.assertGreater(viewbox[2], 34)
        self.assertGreaterEqual(viewbox[0] + viewbox[2], 1_000_000_034)

        raster = self.root / "preview.png"
        rendered = subprocess.run(
            ["rsvg-convert", "--output", str(raster), str(preview)],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        self.assertGreater(raster.stat().st_size, 100)
        self.assertTrue(raster.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))

    def test_preview_bounds_fit_rendered_geometry_when_unsupported_text_is_far_away(self) -> None:
        doc = ezdxf.new("R2018")
        modelspace = doc.modelspace()
        modelspace.add_line((0, 0), (10, 5), dxfattribs={"layer": "Walls"})
        modelspace.add_text("remote annotation", dxfattribs={"height": 1, "layer": "Notes"}).set_placement((100_000, 100_000))
        doc.saveas(self.source)
        preview = self.root / "geometry-only.svg"

        completed, envelope = self.run_cli(
            "preview", "--input", str(self.source), "--output", str(preview)
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(envelope["status"], "needs_attention")
        self.assertGreater(envelope["data"]["source_extents"]["max_x"], 100_000)
        self.assertEqual(envelope["data"]["preview_extents"]["max_x"], 10)
        self.assertEqual(envelope["data"]["preview_extents"]["max_y"], 5)
        self.assertEqual(envelope["data"]["paths_written"], 1)

    def test_svg_preview_drops_active_content_and_external_references(self) -> None:
        source = self.root / "unsafe.svg"
        source.write_text(
            """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 50">
              <script>throw new Error('must not execute')</script>
              <image href="file:///private/customer.dwg" x="0" y="0" width="50" height="50"/>
              <foreignObject><body>not drawing geometry</body></foreignObject>
              <path d="M 0 0 L 40 20" stroke="#123456" style="stroke-width: 2" onload="alert(1)"/>
            </svg>""",
            encoding="utf-8",
        )
        preview = self.root / "safe.svg"

        completed, envelope = self.run_cli(
            "preview", "--input", str(source), "--output", str(preview)
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(envelope["status"], "ok")
        safe_svg = preview.read_text(encoding="utf-8")
        lowered = safe_svg.lower()
        for forbidden in ("<script", "onload", "foreignobject", "href=", "file:", "private/customer"):
            self.assertNotIn(forbidden, lowered)
        self.assertIn("M 0 0 L 40 20", safe_svg)
        safe_root = ET.fromstring(safe_svg)
        paths = safe_root.findall(".//{http://www.w3.org/2000/svg}path")
        self.assertEqual(paths[0].attrib.get("stroke-width"), "2")
        self.assertNotIn("style", paths[0].attrib)
        self.assertEqual(envelope["data"]["extents_basis"], "source_viewbox")
        self.assertEqual(envelope["data"]["units"]["status"], "not_declared")

        doctype = self.root / "entity.svg"
        doctype.write_text(
            "<!DOCTYPE svg [<!ENTITY outside SYSTEM 'file:///etc/passwd'>]>"
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20">'
            '<path d="&outside;"/></svg>',
            encoding="utf-8",
        )
        blocked, blocked_report = self.run_cli("inspect", "--input", str(doctype))
        self.assertEqual(blocked.returncode, 0, blocked.stderr)
        self.assertEqual(blocked_report["status"], "needs_attention")
        self.assertEqual(blocked_report["data"]["error_code"], "svg_external_declarations")

    def test_empty_and_malformed_dxf_return_attention_without_fake_preview(self) -> None:
        empty = ezdxf.new("R2018")
        empty.saveas(self.source)
        inspected, report = self.run_cli("inspect", "--input", str(self.source))
        preview = self.root / "empty.svg"
        rendered, preview_report = self.run_cli(
            "preview", "--input", str(self.source), "--output", str(preview)
        )
        self.assertEqual(inspected.returncode, 0, inspected.stderr)
        self.assertEqual(report["status"], "needs_attention")
        self.assertEqual(report["data"]["entity_count"], 0)
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        self.assertEqual(preview_report["status"], "needs_attention")
        self.assertFalse(preview.exists())

        self.source.write_bytes(b"this is not a DXF drawing\n")
        malformed, malformed_report = self.run_cli("inspect", "--input", str(self.source))
        self.assertEqual(malformed.returncode, 0, malformed.stderr)
        self.assertEqual(malformed_report["status"], "needs_attention")
        self.assertTrue(malformed_report["warnings"])
        self.assertNotIn("Traceback", malformed.stdout + malformed.stderr)

    def test_existing_preview_target_is_refused_without_changing_it_or_source(self) -> None:
        self.make_drawing()
        source_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        target = self.root / "existing.svg"
        sentinel = b"leave this file exactly as it is"
        target.write_bytes(sentinel)

        completed, envelope = self.run_cli(
            "preview", "--input", str(self.source), "--output", str(target)
        )

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(envelope["status"], "error")
        self.assertEqual(envelope["data"]["error_code"], "output_exists")
        self.assertEqual(target.read_bytes(), sentinel)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), source_hash)

    def test_invalid_cli_arguments_return_json_and_exit_two(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(CLI), "inspect"],
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 2)
        envelope = json.loads(completed.stdout)
        self.assertEqual(envelope["tool"], "cad-rescue")
        self.assertEqual(envelope["status"], "error")
        self.assertEqual(envelope["data"]["error_code"], "invalid_arguments")
        self.assertTrue(completed.stderr)

    def test_dwg_without_an_explicit_runtime_reports_the_exact_optional_setup(self) -> None:
        dwg = self.root / "unavailable.dwg"
        dwg.write_bytes(b"AC1015 synthetic DWG placeholder")
        output = self.root / "converted.dxf"

        inspected, report = self.run_cli("inspect", "--input", str(dwg))
        converted, conversion = self.run_cli(
            "convert", "--input", str(dwg), "--output", str(output)
        )

        self.assertEqual(inspected.returncode, 0, inspected.stderr)
        self.assertEqual(report["status"], "needs_attention")
        self.assertEqual(report["data"]["error_code"], "converter_runtime_required")
        self.assertIn("@mlightcad/libredwg-web@0.7.9", report["data"]["setup_command"])
        self.assertEqual(converted.returncode, 0, converted.stderr)
        self.assertEqual(conversion["status"], "needs_attention")
        self.assertEqual(conversion["data"]["error_code"], "converter_runtime_required")
        self.assertFalse(output.exists())

    def test_recover_repairs_missing_section_terminator_and_preserves_source(self) -> None:
        self.make_drawing()
        raw = self.source.read_text(encoding="utf-8")
        broken = raw.replace("  0\nENDSEC\n  0\nEOF\n", "  0\nEOF\n", 1)
        self.assertNotEqual(raw, broken)
        self.source.write_text(broken, encoding="utf-8")
        source_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        output = self.root / "recovered.dxf"

        completed, envelope = self.run_cli(
            "recover", "--input", str(self.source), "--output", str(output)
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(envelope["status"], "ok")
        self.assertGreater(envelope["data"]["recovery_fixes"], 0)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), source_hash)
        repaired = ezdxf.readfile(output)
        self.assertEqual(len(repaired.modelspace()), 4)
        self.assertEqual(len(repaired.audit().errors), 0)
        self.assertTrue(any(item["path"] == str(output) for item in envelope["artifacts"]))

    def test_recover_does_not_create_an_unchanged_copy_or_overwrite_a_target(self) -> None:
        self.make_drawing()
        output = self.root / "unchanged.dxf"
        unchanged, unchanged_report = self.run_cli(
            "recover", "--input", str(self.source), "--output", str(output)
        )
        self.assertEqual(unchanged.returncode, 0, unchanged.stderr)
        self.assertEqual(unchanged_report["status"], "needs_attention")
        self.assertEqual(unchanged_report["data"]["error_code"], "nothing_to_recover")
        self.assertFalse(output.exists())


@unittest.skipUnless(
    os.environ.get("CAD_RESCUE_DWG_FIXTURE") and os.environ.get("CAD_RESCUE_LIBREDWG_RUNTIME"),
    "set CAD_RESCUE_DWG_FIXTURE and CAD_RESCUE_LIBREDWG_RUNTIME for the optional real-DWG smoke test",
)
class CadRescueOptionalDwgTests(unittest.TestCase):
    def test_explicit_libredwg_runtime_creates_a_renderable_dwg_preview(self) -> None:
        source = Path(os.environ["CAD_RESCUE_DWG_FIXTURE"]).expanduser()
        runtime = Path(os.environ["CAD_RESCUE_LIBREDWG_RUNTIME"]).expanduser()
        self.assertTrue(source.is_file())
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory(prefix="cad-rescue-dwg-smoke-") as directory:
            output = Path(directory) / "preview.svg"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(CLI),
                    "preview",
                    "--input",
                    str(source),
                    "--output",
                    str(output),
                    "--runtime-dir",
                    str(runtime),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            envelope = json.loads(completed.stdout)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn(envelope["status"], {"ok", "needs_attention"})
            self.assertEqual(envelope["data"]["source_format"], "dwg")
            self.assertEqual(envelope["data"]["conversion"]["name"], "libredwg-web")
            self.assertGreater(envelope["data"]["paths_written"], 0)
            self.assertTrue(output.is_file())
            root = ET.fromstring(output.read_bytes())
            paths = root.findall(".//{http://www.w3.org/2000/svg}path")
            self.assertEqual(len(paths), envelope["data"]["paths_written"])
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), source_hash)

            if shutil.which("rsvg-convert"):
                raster = Path(directory) / "preview.png"
                rendered = subprocess.run(
                    ["rsvg-convert", "--output", str(raster), str(output)],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(rendered.returncode, 0, rendered.stderr)
                self.assertTrue(raster.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))

if __name__ == "__main__":
    unittest.main()
