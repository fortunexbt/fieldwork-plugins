import importlib.util
import json
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("release", ROOT / "scripts" / "release.py")


class ReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = importlib.util.module_from_spec(SPEC)
        SPEC.loader.exec_module(cls.mod)

    def fixture(self, root):
        plugin = root / "sample"
        skill = plugin / "skills" / "sample"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: sample\ndescription: Analyze a selected sample file.\n---\nRun the sample command.\n")
        (plugin / "assets").mkdir()
        (plugin / "assets" / "icon.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64"><rect width="64" height="64"/></svg>')
        (plugin / "LICENSE").write_text("MIT License\nCopyright test fixture\n")
        manifest = {"$schema": self.mod.SCHEMA, "name": "sample", "version": "0.1.0", "description": "Analyze a sample", "author": {"name": "Fixture"}, "extensions": {"com.openai": {"interface": {"displayName": "Sample", "shortDescription": "Analyze a sample", "longDescription": "Analyze a selected sample file and return evidence.", "developerName": "Fixture", "category": "Productivity", "defaultPrompt": ["Analyze the sample."], "composerIcon": "./assets/icon.svg", "logo": "./assets/icon.svg"}}}}
        (plugin / "plugin.json").write_text(json.dumps(manifest))
        return plugin

    def test_build_is_deterministic_and_extractable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plugin = self.fixture(root)
            a = self.mod.package_plugin(plugin, root / "one")
            b = self.mod.package_plugin(plugin, root / "two")
            self.assertEqual(a["sha256"], b["sha256"])
            with zipfile.ZipFile(a["path"]) as z:
                self.assertIn("plugin.json", z.namelist())
                self.assertIn("skills/sample/SKILL.md", z.namelist())
                self.assertFalse(any(x.startswith("/") or ".." in Path(x).parts for x in z.namelist()))

    def test_missing_skill_fails_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = self.fixture(Path(tmp))
            (plugin / "skills" / "sample" / "SKILL.md").unlink()
            self.assertTrue(any("skill" in x.lower() for x in self.mod.validate_plugin(plugin)))

    def test_symlink_cannot_enter_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plugin = self.fixture(root)
            (root / "private.txt").write_text("not distributable")
            (plugin / "linked.txt").symlink_to(root / "private.txt")
            with self.assertRaises(ValueError):
                self.mod.package_plugin(plugin, root / "out")

    def test_secrets_and_private_paths_block_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = self.fixture(Path(tmp))
            (plugin / ".env").write_text("TOKEN=private")
            (plugin / "notes.md").write_text("Private path /Users/example-person/Downloads/customer.mov")
            errors = self.mod.validate_plugin(plugin)
            self.assertTrue(any(".env" in x for x in errors))
            self.assertTrue(any("private" in x.lower() for x in errors))

    def test_oversized_listing_and_unsafe_reference_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = self.fixture(Path(tmp))
            p = plugin / "plugin.json"
            manifest = json.loads(p.read_text())
            interface = manifest["extensions"]["com.openai"]["interface"]
            interface["shortDescription"] = "x" * 31
            interface["logo"] = "../outside.svg"
            p.write_text(json.dumps(manifest))
            errors = self.mod.validate_plugin(plugin)
            self.assertTrue(any("shortDescription" in x for x in errors))
            self.assertTrue(any("logo" in x for x in errors))


if __name__ == "__main__":
    unittest.main()
