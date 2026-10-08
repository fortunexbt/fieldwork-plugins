import base64
import copy
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "plugins" / "asset-check" / "skills" / "asset-check" / "scripts" / "asset_check.py"


def data_uri(payload, mime_type="application/octet-stream"):
    return f"data:{mime_type};base64,{base64.b64encode(payload).decode('ascii')}"


def make_glb(document, binary=b""):
    json_chunk = json.dumps(document, separators=(",", ":")).encode("utf-8")
    json_chunk += b" " * ((-len(json_chunk)) % 4)
    binary_chunk = binary + b"\0" * ((-len(binary)) % 4)
    chunks = struct.pack("<II", len(json_chunk), 0x4E4F534A) + json_chunk
    if binary_chunk:
        chunks += struct.pack("<II", len(binary_chunk), 0x004E4942) + binary_chunk
    total_length = 12 + len(chunks)
    return struct.pack("<4sII", b"glTF", 2, total_length) + chunks


def scene_glb_document():
    positions = struct.pack(
        "<12f",
        0, 0, 0,
        1, 0, 0,
        1, 1, 0,
        0, 1, 0,
    )
    triangle_indices = struct.pack("<3H", 0, 1, 2)
    strip_indices = struct.pack("<4H", 0, 1, 2, 3)
    binary = positions + triangle_indices + b"\0\0" + strip_indices + struct.pack(
        "<6f", 0, 0, 0, 2, 0, 0
    )
    assert len(binary) == 88
    document = {
        "asset": {"version": "2.0", "generator": "synthetic test"},
        "scene": 0,
        "scenes": [{"name": "Room", "nodes": [0, 2]}],
        "nodes": [
            {
                "name": "Translated parent",
                "matrix": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 10, 0, 0, 1],
                "children": [1],
            },
            {
                "name": "Instanced triangle pair",
                "mesh": 0,
                "extensions": {"EXT_mesh_gpu_instancing": {"attributes": {"TRANSLATION": 3}}},
            },
            {"name": "Reused mesh", "mesh": 0, "scale": [2, 2, 2]},
        ],
        "meshes": [
            {
                "name": "Two primitive mesh",
                "primitives": [
                    {"attributes": {"POSITION": 0}, "indices": 1, "material": 0, "mode": 4},
                    {"attributes": {"POSITION": 0}, "indices": 2, "material": 0, "mode": 5},
                ],
            }
        ],
        "buffers": [{"byteLength": len(binary)}],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": 48},
            {"buffer": 0, "byteOffset": 48, "byteLength": 6},
            {"buffer": 0, "byteOffset": 56, "byteLength": 8},
            {"buffer": 0, "byteOffset": 64, "byteLength": 24},
        ],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": 4, "type": "VEC3"},
            {"bufferView": 1, "componentType": 5123, "count": 3, "type": "SCALAR"},
            {"bufferView": 2, "componentType": 5123, "count": 4, "type": "SCALAR"},
            {"bufferView": 3, "componentType": 5126, "count": 2, "type": "VEC3"},
        ],
        "materials": [
            {"name": "Paint", "pbrMetallicRoughness": {"baseColorTexture": {"index": 0}}}
        ],
        "textures": [{"source": 0}],
        "images": [{"uri": data_uri(b"synthetic-image", "image/png"), "name": "tiny swatch"}],
        "extensionsUsed": ["EXT_mesh_gpu_instancing"],
        "extensionsRequired": ["EXT_mesh_gpu_instancing"],
    }
    return document, binary


class AssetCheckCharacterizationTests(unittest.TestCase):
    def test_real_glb_counts_scene_reuse_gpu_instances_and_writes_reports(self):
        with tempfile.TemporaryDirectory(prefix="asset-check-glb-") as directory:
            root = Path(directory)
            document, binary = scene_glb_document()
            source = root / "two-primitives.glb"
            source.write_bytes(make_glb(document, binary))
            source_bytes = source.read_bytes()
            report = root / "two-primitives-report.html"
            receipt_path = root / "two-primitives-receipt.json"

            completed = self.run_cli(
                "inspect", "--input", source,
                "--max-triangles", "9", "--max-bytes", str(source.stat().st_size),
                "--report", report, "--receipt", receipt_path,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            receipt = json.loads(completed.stdout)
            self.assertEqual(receipt["schema_version"], 1)
            self.assertEqual(receipt["tool"], "asset-check")
            self.assertEqual(receipt["status"], "ok")
            self.assertEqual(receipt["data"]["geometry"]["mesh_count"], 1)
            self.assertEqual(receipt["data"]["geometry"]["primitive_count"], 2)
            self.assertEqual(receipt["data"]["geometry"]["triangle_candidates_in_mesh_primitives"], 3)
            selected_scene = receipt["data"]["scene"]["scenes"][0]
            self.assertEqual(selected_scene["mesh_node_count"], 2)
            self.assertEqual(selected_scene["gpu_instance_count"], 2)
            self.assertEqual(selected_scene["triangle_candidates"], 9)
            self.assertEqual(selected_scene["transforms"][0]["type"], "matrix")
            self.assertTrue(receipt["data"]["materials"]["texture_uses"])
            self.assertEqual(receipt["data"]["budgets"]["triangles"]["result"], "within")
            self.assertEqual(receipt["data"]["budgets"]["bytes"]["result"], "within")
            self.assertNotIn(base64.b64encode(b"synthetic-image").decode("ascii"), completed.stdout)
            self.assertTrue(report.is_file())
            html = report.read_text(encoding="utf-8")
            self.assertIn("Scene draw overview", html)
            self.assertIn("Payload sizes", html)
            self.assertIn("bar-track", html)
            self.assertIn("Topology and primitives", html)
            self.assertIn("Source: two-primitives.glb", html)
            self.assertNotIn("<pre", html.lower())
            self.assertNotIn(base64.b64encode(b"synthetic-image").decode("ascii"), html)
            self.assertEqual(json.loads(receipt_path.read_text(encoding="utf-8")), receipt)
            self.assertEqual({item["media_type"] for item in receipt["artifacts"]}, {"text/html", "application/json"})
            self.assertEqual(source.read_bytes(), source_bytes)

    def test_every_primitive_mode_reports_its_own_topology(self):
        with tempfile.TemporaryDirectory(prefix="asset-check-modes-") as directory:
            root = Path(directory)
            positions = struct.pack("<18f", *([0.0] * 18))
            modes = [
                {"attributes": {"POSITION": 0}, "mode": mode}
                for mode in range(7)
            ]
            document = {
                "asset": {"version": "2.0"}, "scene": 0,
                "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}],
                "meshes": [{"primitives": modes}],
                "buffers": [{"byteLength": len(positions), "uri": data_uri(positions)}],
                "bufferViews": [{"buffer": 0, "byteLength": len(positions)}],
                "accessors": [{"bufferView": 0, "componentType": 5126, "count": 6, "type": "VEC3"}],
            }
            source = root / "modes.gltf"
            source.write_text(json.dumps(document), encoding="utf-8")

            completed = self.run_cli("inspect", "--input", source)

            self.assertEqual(completed.returncode, 0, completed.stderr)
            data = json.loads(completed.stdout)["data"]
            topology = data["geometry"]["topology"]
            self.assertEqual([row["mode"] for row in data["geometry"]["primitives"]], [
                "POINTS", "LINES", "LINE_LOOP", "LINE_STRIP", "TRIANGLES", "TRIANGLE_STRIP", "TRIANGLE_FAN"
            ])
            self.assertEqual(topology["POINTS"]["points"], 6)
            self.assertEqual(topology["LINES"]["line_segments"], 3)
            self.assertEqual(topology["LINE_LOOP"]["line_segments"], 6)
            self.assertEqual(topology["LINE_STRIP"]["line_segments"], 5)
            self.assertEqual(topology["TRIANGLES"]["triangle_candidates"], 2)
            self.assertEqual(topology["TRIANGLE_STRIP"]["triangle_candidates"], 4)
            self.assertEqual(topology["TRIANGLE_FAN"]["triangle_candidates"], 4)

    def test_malformed_glb_chunk_and_accessor_reference_fail_cleanly(self):
        with tempfile.TemporaryDirectory(prefix="asset-check-invalid-") as directory:
            root = Path(directory)
            document, binary = scene_glb_document()
            valid = make_glb(document, binary)
            broken_chunk = bytearray(valid)
            declared_length = struct.unpack_from("<I", broken_chunk, 8)[0]
            struct.pack_into("<I", broken_chunk, 8, declared_length + 4)
            chunk_path = root / "bad-chunk.glb"
            chunk_path.write_bytes(broken_chunk)

            bad_reference_doc = copy.deepcopy(document)
            bad_reference_doc["meshes"][0]["primitives"][0]["attributes"]["POSITION"] = 99
            accessor_path = root / "bad-accessor.glb"
            accessor_path.write_bytes(make_glb(bad_reference_doc, binary))
            bad_range_doc = copy.deepcopy(document)
            bad_range_doc["accessors"][0]["byteOffset"] = 40
            range_path = root / "bad-range.glb"
            range_path.write_bytes(make_glb(bad_range_doc, binary))

            chunk_result = self.run_cli("inspect", "--input", chunk_path)
            accessor_result = self.run_cli("inspect", "--input", accessor_path)
            range_result = self.run_cli("inspect", "--input", range_path)

            self.assertEqual(chunk_result.returncode, 1)
            self.assertEqual(json.loads(chunk_result.stdout)["status"], "error")
            self.assertIn("GLB", json.loads(chunk_result.stdout)["summary"])
            self.assertEqual(accessor_result.returncode, 1)
            self.assertEqual(json.loads(accessor_result.stdout)["status"], "error")
            self.assertIn("accessor", json.loads(accessor_result.stdout)["summary"].lower())
            self.assertEqual(range_result.returncode, 1)
            self.assertIn("byte range", json.loads(range_result.stdout)["summary"].lower())

    def test_external_buffer_size_is_included_with_the_json_source(self):
        with tempfile.TemporaryDirectory(prefix="asset-check-external-") as directory:
            root = Path(directory)
            binary = struct.pack("<9f", 0, 0, 0, 1, 0, 0, 0, 1, 0)
            (root / "mesh.bin").write_bytes(binary)
            document = {
                "asset": {"version": "2.0"}, "scene": 0,
                "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}],
                "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}],
                "buffers": [{"uri": "mesh.bin", "byteLength": len(binary)}],
                "bufferViews": [{"buffer": 0, "byteLength": len(binary)}],
                "accessors": [{"bufferView": 0, "componentType": 5126, "count": 3, "type": "VEC3"}],
            }
            source = root / "external.gltf"
            source.write_text(json.dumps(document), encoding="utf-8")
            expected_total = source.stat().st_size + len(binary)

            completed = self.run_cli("inspect", "--input", source, "--max-bytes", str(expected_total))

            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(completed.stdout)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["data"]["file_footprint"]["external_bytes"], len(binary))
            self.assertEqual(result["data"]["file_footprint"]["total_asset_bytes"], expected_total)
            self.assertEqual(result["data"]["budgets"]["bytes"]["result"], "within")

    def test_existing_report_is_preserved_and_not_overwritten(self):
        with tempfile.TemporaryDirectory(prefix="asset-check-output-") as directory:
            root = Path(directory)
            document, binary = scene_glb_document()
            source = root / "source.glb"
            source.write_bytes(make_glb(document, binary))
            report = root / "existing-report.html"
            sentinel = b"keep this report"
            report.write_bytes(sentinel)

            completed = self.run_cli("inspect", "--input", source, "--report", report)

            self.assertEqual(completed.returncode, 1)
            result = json.loads(completed.stdout)
            self.assertEqual(result["data"]["error_code"], "output_exists")
            self.assertEqual(report.read_bytes(), sentinel)

    def test_external_missing_and_parent_traversal_are_reported_without_reading_outside(self):
        with tempfile.TemporaryDirectory(prefix="asset-check-resources-") as directory:
            root = Path(directory)
            asset_dir = root / "asset"
            asset_dir.mkdir()
            outside_buffer = root / "private.bin"
            sentinel = b"DO NOT READ OR COPY THIS"
            outside_buffer.write_bytes(sentinel)
            document = {
                "asset": {"version": "2.0"}, "scene": 0,
                "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}],
                "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}],
                "buffers": [{"byteLength": 36, "uri": "../private.bin"}],
                "bufferViews": [{"buffer": 0, "byteLength": 36}],
                "accessors": [{"bufferView": 0, "componentType": 5126, "count": 3, "type": "VEC3"}],
                "images": [
                    {"uri": "missing.png"},
                    {"uri": "https://user:password@example.invalid/texture.png?token=private"},
                ],
            }
            source = asset_dir / "unsafe.gltf"
            source.write_text(json.dumps(document), encoding="utf-8")

            completed = self.run_cli("inspect", "--input", source, "--max-bytes", "100000")

            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(completed.stdout)
            self.assertEqual(result["status"], "needs_attention")
            resources = result["data"]["file_footprint"]["resources"]
            self.assertEqual([item["status"] for item in resources], ["outside_directory", "missing", "remote_not_fetched"])
            self.assertIsNone(result["data"]["file_footprint"]["total_asset_bytes"])
            self.assertEqual(result["data"]["budgets"]["bytes"]["result"], "unknown")
            self.assertEqual(outside_buffer.read_bytes(), sentinel)
            self.assertNotIn("DO NOT READ", completed.stdout)
            self.assertNotIn("password", completed.stdout)
            self.assertNotIn("token=private", completed.stdout)

    def test_required_draco_geometry_stays_unknown_without_decoder(self):
        with tempfile.TemporaryDirectory(prefix="asset-check-draco-") as directory:
            root = Path(directory)
            compressed = b"DRACO"
            document = {
                "asset": {"version": "2.0"}, "scene": 0,
                "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}],
                "meshes": [{"primitives": [{
                    "attributes": {"POSITION": 0}, "indices": 1,
                    "extensions": {"KHR_draco_mesh_compression": {"bufferView": 0, "attributes": {"POSITION": 0}}},
                }]}],
                "buffers": [{"byteLength": len(compressed)}],
                "bufferViews": [{"buffer": 0, "byteLength": len(compressed)}],
                "accessors": [
                    {"componentType": 5126, "count": 3, "type": "VEC3"},
                    {"componentType": 5123, "count": 3, "type": "SCALAR"},
                ],
                "extensionsUsed": ["KHR_draco_mesh_compression"],
                "extensionsRequired": ["KHR_draco_mesh_compression"],
            }
            source = root / "compressed.glb"
            source.write_bytes(make_glb(document, compressed))

            completed = self.run_cli("inspect", "--input", source, "--max-triangles", "100")

            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(completed.stdout)
            self.assertEqual(result["status"], "needs_attention")
            primitive = result["data"]["geometry"]["primitives"][0]
            self.assertEqual(primitive["geometry_status"], "compressed_decoder_unavailable")
            self.assertIsNone(primitive["triangle_candidates"])
            self.assertEqual(result["data"]["required_extensions"], ["KHR_draco_mesh_compression"])
            self.assertEqual(result["data"]["budgets"]["triangles"]["result"], "unknown")

    def test_budgets_compare_only_explicit_limits_and_report_overages(self):
        with tempfile.TemporaryDirectory(prefix="asset-check-budget-") as directory:
            root = Path(directory)
            document, binary = scene_glb_document()
            source = root / "budget.glb"
            source.write_bytes(make_glb(document, binary))
            size = source.stat().st_size

            within = self.run_cli("inspect", "--input", source, "--max-triangles", "9", "--max-bytes", str(size))
            over = self.run_cli("inspect", "--input", source, "--max-triangles", "8", "--max-bytes", str(size - 1))
            unset = self.run_cli("inspect", "--input", source)

            self.assertEqual(json.loads(within.stdout)["data"]["budgets"]["triangles"]["result"], "within")
            over_result = json.loads(over.stdout)
            self.assertEqual(over_result["status"], "needs_attention")
            self.assertEqual(over_result["data"]["budgets"]["triangles"]["result"], "over")
            self.assertEqual(over_result["data"]["budgets"]["bytes"]["result"], "over")
            self.assertEqual(json.loads(unset.stdout)["data"]["budgets"]["triangles"]["result"], "not_set")
            self.assertEqual(json.loads(unset.stdout)["data"]["budgets"]["bytes"]["result"], "not_set")

    @staticmethod
    def run_cli(*arguments):
        return subprocess.run(
            [sys.executable, str(CLI), *(str(argument) for argument in arguments)],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )


if __name__ == "__main__":
    unittest.main()
