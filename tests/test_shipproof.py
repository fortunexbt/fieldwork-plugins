import gzip
import hashlib
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "plugins" / "shipproof" / "skills" / "shipproof" / "scripts" / "shipproof.py"
FRESH = b'window.__SHIPPROOF_DEMO__ = "demo-release-2026-09";\n'


class FixtureHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        route = self.path.split("?", 1)[0]
        if route in {"/fresh.js", "/redirect-target.js"}:
            status, content_type, body = 200, "application/javascript", FRESH
        elif route == "/stale.js":
            status, content_type, body = 200, "application/javascript", b"window.version = 'old-release';\n"
        elif route == "/spa.js":
            status, content_type, body = 200, "text/html; charset=utf-8", b"<!doctype html><main id='app'>SPA fallback</main>\n"
        elif route == "/large.js":
            status, content_type, body = 200, "application/javascript", b"x" * 1100
        elif route == "/compressed.js":
            status, content_type, body = 200, "application/javascript", gzip.compress(FRESH)
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        elif route == "/redirect.js":
            self.send_response(302)
            self.send_header("Location", "/redirect-target.js?cache=private-value")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        else:
            status, content_type, body = 404, "text/plain; charset=utf-8", b"not found"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "public, max-age=60")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


class ShipProofTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=3)

    def run_cli(self, *args):
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), *map(str, args)],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            timeout=20,
        )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            self.fail(f"CLI did not return its JSON envelope: {completed.stdout!r}; stderr={completed.stderr!r}")
            raise exc
        return completed.returncode, payload

    def write_expected_asset(self, folder, content=FRESH, name="app.js"):
        path = Path(folder) / name
        path.write_bytes(content)
        return path

    def test_release_hash_marker_headers_redirect_and_spa_route(self):
        with tempfile.TemporaryDirectory() as temp:
            asset = self.write_expected_asset(temp)
            code, fresh = self.run_cli(
                "verify", "--url", self.base_url + "/fresh.js?cache=ok", "--asset", asset,
                "--marker", "demo-release-2026-09", "--header", "Content-Type:application/javascript",
            )
            self.assertEqual(code, 0)
            self.assertEqual(fresh["status"], "ok")
            resource = fresh["data"]["resources"][0]
            self.assertEqual(resource["result"], "match")
            self.assertEqual(resource["status_code"], 200)
            self.assertEqual(resource["headers"]["cache-control"], "public, max-age=60")
            self.assertNotIn("cache=ok", resource["request_url"])

            code, stale = self.run_cli("verify", "--url", self.base_url + "/stale.js", "--asset", asset,
                                       "--marker", "demo-release-2026-09")
            stale_resource = stale["data"]["resources"][0]
            self.assertEqual(code, 0)
            self.assertEqual(stale["status"], "needs_attention")
            self.assertEqual(stale_resource["result"], "mismatch")
            self.assertIn("mismatch", {check["result"] for check in stale_resource["checks"]})

            code, spa = self.run_cli("verify", "--url", self.base_url + "/spa.js", "--asset", asset,
                                     "--marker", "demo-release-2026-09")
            spa_resource = spa["data"]["resources"][0]
            self.assertEqual(code, 0)
            self.assertEqual(spa_resource["status_code"], 200)
            self.assertEqual(spa_resource["headers"]["content-type"], "text/html; charset=utf-8")
            self.assertEqual(spa_resource["result"], "mismatch")

            code, redirected = self.run_cli("verify", "--url", self.base_url + "/redirect.js", "--asset", asset,
                                             "--marker", "demo-release-2026-09")
            redirect_resource = redirected["data"]["resources"][0]
            self.assertEqual(code, 0)
            self.assertEqual(redirect_resource["result"], "match")
            self.assertEqual(len(redirect_resource["redirects"]), 1)
            self.assertNotIn("private-value", json.dumps(redirect_resource))

            code, encoded = self.run_cli("verify", "--url", self.base_url + "/compressed.js", "--asset", asset)
            encoded_resource = encoded["data"]["resources"][0]
            self.assertEqual(code, 0)
            self.assertEqual(encoded_resource["result"], "unverifiable")
            self.assertEqual(encoded_resource["headers"]["content-encoding"], "gzip")
            self.assertIn("unsupported_content_encoding", encoded_resource["reasons"])

    def test_missing_route_local_asset_limits_and_credentialed_url(self):
        with tempfile.TemporaryDirectory() as temp:
            asset = self.write_expected_asset(temp)
            code, missing_route = self.run_cli("verify", "--url", self.base_url + "/missing.js", "--asset", asset)
            self.assertEqual(code, 0)
            self.assertEqual(missing_route["data"]["resources"][0]["status_code"], 404)
            self.assertEqual(missing_route["data"]["resources"][0]["result"], "mismatch")

            absent = Path(temp) / "does-not-exist.js"
            code, missing_local = self.run_cli("verify", "--url", self.base_url + "/fresh.js", "--asset", absent)
            self.assertEqual(code, 0)
            self.assertEqual(missing_local["data"]["resources"][0]["result"], "unverifiable")
            self.assertIn("expected_asset_missing", missing_local["data"]["resources"][0]["reasons"])

            oversized = self.write_expected_asset(temp, b"x" * 1100, "large.js")
            code, capped = self.run_cli("verify", "--url", self.base_url + "/large.js", "--asset", oversized,
                                        "--max-bytes", "1024")
            self.assertEqual(code, 0)
            self.assertEqual(capped["data"]["resources"][0]["result"], "unverifiable")
            self.assertIn("response_exceeds_limit", capped["data"]["resources"][0]["reasons"])

            code, invalid = self.run_cli("verify", "--url", self.base_url.replace("http://", "http://user:pass@") + "/fresh.js",
                                         "--asset", asset)
            self.assertEqual(code, 2)
            self.assertEqual(invalid["data"]["error_code"], "invalid_input")

    def test_manifest_pins_expected_local_hash_and_marker(self):
        with tempfile.TemporaryDirectory() as temp:
            asset = self.write_expected_asset(temp)
            manifest = Path(temp) / "release.json"
            manifest.write_text(json.dumps({
                "schema_version": 1,
                "resources": [{
                    "name": "web bundle",
                    "url": self.base_url + "/fresh.js",
                    "asset": asset.name,
                    "sha256": hashlib.sha256(FRESH).hexdigest(),
                    "markers": ["demo-release-2026-09"],
                    "headers": {"content-type": "application/javascript"},
                }],
            }), encoding="utf-8")
            code, result = self.run_cli("verify", "--manifest", manifest)
            self.assertEqual(code, 0)
            self.assertEqual(result["status"], "ok")
            checks = result["data"]["resources"][0]["checks"]
            self.assertIn("manifest_sha256", {item["check"] for item in checks})

            manifest.write_text(json.dumps({
                "schema_version": 1,
                "resources": [{"name": "web bundle", "url": self.base_url + "/fresh.js",
                               "asset": asset.name, "sha256": "0" * 64}],
            }), encoding="utf-8")
            code, result = self.run_cli("verify", "--manifest", manifest)
            self.assertEqual(code, 0)
            self.assertEqual(result["data"]["resources"][0]["result"], "mismatch")

    def git(self, cwd, *args):
        subprocess.run(["git", "-C", str(cwd), *args], check=True, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, timeout=10)

    def create_repo(self, root):
        root.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(root)], check=True, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, timeout=10)
        self.git(root, "config", "user.name", "ShipProof Test")
        self.git(root, "config", "user.email", "shipproof-test@example.invalid")
        (root / "src").mkdir()
        (root / "src" / "main.py").write_text('print("COMMITTED_SOURCE_CONTENT_SENTINEL")\n', encoding="utf-8")
        (root / "test_main.py").write_text("def test_placeholder():\n    assert True\n", encoding="utf-8")
        (root / "package.json").write_text(json.dumps({"scripts": {
            "build": "echo COMMAND_TEXT_MUST_NOT_BE_EXPORTED",
            "test": "echo TEST_COMMAND_TEXT_MUST_NOT_BE_EXPORTED",
            "deploy": "echo DEPLOY_COMMAND_MUST_NOT_BE_SUGGESTED",
        }}), encoding="utf-8")
        (root / "Makefile").write_text(
            "build:\n\t@echo MAKE_COMMAND_TEXT_MUST_NOT_BE_EXPORTED\n"
            "deploy:\n\t@echo DEPLOY_MAKE_TARGET_MUST_NOT_BE_SUGGESTED\n", encoding="utf-8")
        (root / ".env.example").write_text("DEMO_LABEL=synthetic-example\n", encoding="utf-8")
        self.git(root, "add", ".")
        self.git(root, "commit", "-m", "Synthetic source fixture")

    def test_clean_clone_receipt_and_dirty_paths_without_exporting_contents(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            source = temp / "source"
            self.create_repo(source)
            clone = temp / "clone"
            subprocess.run(["git", "clone", "--no-hardlinks", str(source), str(clone)], check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
            expected_hash = hashlib.sha256((source / "src" / "main.py").read_bytes()).hexdigest()

            out_dir = temp / "clean-handoff"
            code, preview = self.run_cli("handoff", "--repo", clone, "--out-dir", out_dir,
                                         "--include", "src/**/*.py", "--preview")
            self.assertEqual(code, 0)
            self.assertTrue(preview["data"]["preview"])
            resolved_out = out_dir.resolve()
            self.assertEqual(preview["data"]["planned_outputs"], [str(resolved_out / "handoff.md"), str(resolved_out / "manifest.json")])
            self.assertFalse(out_dir.exists())

            code, receipt = self.run_cli("handoff", "--repo", clone, "--out-dir", out_dir,
                                         "--include", "src/**/*.py")
            self.assertEqual(code, 0)
            self.assertEqual(receipt["status"], "ok")
            manifest_path = out_dir / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["revision"], self.git_output(clone, "rev-parse", "HEAD"))
            self.assertEqual(manifest["source_file_count"], 1)
            self.assertEqual(manifest["source_files"][0]["sha256"], expected_hash)
            self.assertFalse(manifest["limits"]["uncommitted_changes_backed_up"])
            self.assertNotIn(".env.example", json.dumps(manifest))
            report_text = (out_dir / "handoff.md").read_text(encoding="utf-8")
            serialized = json.dumps(manifest) + report_text
            for forbidden in (
                "COMMITTED_SOURCE_CONTENT_SENTINEL", "COMMAND_TEXT_MUST_NOT_BE_EXPORTED",
                "TEST_COMMAND_TEXT_MUST_NOT_BE_EXPORTED", "MAKE_COMMAND_TEXT_MUST_NOT_BE_EXPORTED",
                "DEPLOY_COMMAND_MUST_NOT_BE_SUGGESTED", "DEPLOY_MAKE_TARGET_MUST_NOT_BE_SUGGESTED",
            ):
                self.assertNotIn(forbidden, serialized)
            self.assertIn("npm run build", report_text)
            self.assertIn("make build", report_text)
            self.assertNotIn("deploy", report_text)
            self.assertIn("backup of uncommitted changes", report_text)
            self.assertEqual(self.git_output(clone, "status", "--porcelain"), "")

            code, overwrite = self.run_cli("handoff", "--repo", clone, "--out-dir", out_dir)
            self.assertEqual(code, 1)
            self.assertEqual(overwrite["data"]["error_code"], "execution_failed")

            (source / "src" / "main.py").write_text('print("WORKING_TREE_CONTENT_SENTINEL")\n', encoding="utf-8")
            (source / "scratch.txt").write_text("UNTRACKED_CONTENT_SENTINEL", encoding="utf-8")
            dirty_out = temp / "dirty-handoff"
            code, dirty = self.run_cli("handoff", "--repo", source, "--out-dir", dirty_out, "--preview")
            self.assertEqual(code, 0)
            self.assertEqual(dirty["status"], "needs_attention")
            self.assertEqual(dirty["data"]["dirty_path_count"], 2)
            self.assertFalse(dirty_out.exists())

    def git_output(self, cwd, *args):
        result = subprocess.run(["git", "-C", str(cwd), *args], check=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, timeout=10)
        return result.stdout.strip()


if __name__ == "__main__":
    unittest.main()
