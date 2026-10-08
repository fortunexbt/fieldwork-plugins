"""Safe Tidy CLI checks against synthetic temporary directories only."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "plugins" / "safe-tidy" / "skills" / "safe-tidy" / "scripts" / "safe_tidy.py"
SPEC = importlib.util.spec_from_file_location("safe_tidy_under_test", CLI)
SAFE_TIDY = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(SAFE_TIDY)


class SafeTidyCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="safe-tidy-test-")
        self.root = Path(self.temp.name) / "source"
        self.root.mkdir()
        self.out = Path(self.temp.name) / "artifacts"
        self.out.mkdir()

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
        self.assertEqual(envelope.get("tool"), "safe-tidy")
        return completed, envelope

    def make_plan(self, name: str = "plan.json") -> Path:
        plan = self.out / name
        completed, envelope = self.run_cli("plan", "--root", str(self.root), "--output", str(plan))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn(envelope["status"], {"ok", "needs_attention"})
        return plan

    @staticmethod
    def reseal_plan(plan: dict) -> dict:
        plan.pop("plan_sha256", None)
        canonical = json.dumps(plan, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        plan["plan_sha256"] = hashlib.sha256(canonical).hexdigest()
        return plan

    def test_inventory_groups_exact_content_and_never_follows_symlinks(self) -> None:
        first = self.root / "one.txt"
        second = self.root / "two.md"
        first.write_bytes(b"same\x00bytes")
        second.write_bytes(b"same\x00bytes")
        outside = self.out / "outside.bin"
        outside.write_bytes(b"must not be read through a link")
        (self.root / "linked.bin").symlink_to(outside)
        (self.root / "nested").mkdir()
        (self.root / "nested" / "hidden.txt").write_text("not recursive", encoding="utf-8")

        completed, envelope = self.run_cli("inventory", "--root", str(self.root))

        self.assertEqual(completed.returncode, 0, completed.stderr)
        data = envelope["data"]
        self.assertEqual(data["files_scanned"], 2)
        self.assertEqual(data["duplicate_groups"], [{
            "sha256": hashlib.sha256(b"same\x00bytes").hexdigest(),
            "size_bytes": 10,
            "paths": ["one.txt", "two.md"],
        }])
        self.assertNotIn("hidden.txt", json.dumps(data))
        self.assertIn("symlink", {item["reason"] for item in data["skipped"]})

    def test_plan_marks_existing_destination_name_as_conflict(self) -> None:
        (self.root / "report.pdf").write_bytes(b"new source")
        bucket = self.root / "pdf"
        bucket.mkdir()
        (bucket / "report.pdf").write_bytes(b"keep existing")

        plan_path = self.make_plan()
        plan = json.loads(plan_path.read_text(encoding="utf-8"))

        self.assertEqual(plan["moves"], [])
        self.assertEqual(plan["conflicts"], [{
            "source": "report.pdf",
            "destination": "pdf/report.pdf",
            "reason": "destination_exists",
        }])
        self.assertEqual((bucket / "report.pdf").read_bytes(), b"keep existing")

    def test_apply_and_undo_preserve_original_bytes_and_do_not_overwrite(self) -> None:
        expected = {"draft.txt": b"draft\x00bytes", "scan.pdf": b"%PDF synthetic"}
        for name, payload in expected.items():
            (self.root / name).write_bytes(payload)
        plan_path = self.make_plan()
        receipt_path = self.out / "receipt.json"

        applied, apply_envelope = self.run_cli(
            "apply", "--root", str(self.root), "--plan", str(plan_path), "--receipt", str(receipt_path)
        )
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertEqual(apply_envelope["status"], "ok")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt["kind"], "undo-receipt")
        self.assertEqual(receipt["plan_sha256"], json.loads(plan_path.read_text(encoding="utf-8"))["plan_sha256"])
        self.assertTrue(all(
            move["source_sha256"] == move["destination_sha256"] == move["sha256"]
            for move in receipt["moves"]
        ))
        self.assertEqual((self.root / "txt" / "draft.txt").read_bytes(), expected["draft.txt"])
        self.assertEqual((self.root / "pdf" / "scan.pdf").read_bytes(), expected["scan.pdf"])
        self.assertFalse((self.root / "draft.txt").exists())

        undone, undo_envelope = self.run_cli(
            "undo", "--root", str(self.root), "--receipt", str(receipt_path)
        )
        self.assertEqual(undone.returncode, 0, undone.stderr)
        self.assertEqual(undo_envelope["status"], "ok")
        self.assertEqual(json.loads(receipt_path.read_text(encoding="utf-8"))["kind"], "undo-receipt")
        for name, payload in expected.items():
            self.assertEqual((self.root / name).read_bytes(), payload)

    def test_apply_rejects_tampered_plan_path_outside_root(self) -> None:
        (self.root / "safe.txt").write_bytes(b"source")
        outside = self.out / "secret.txt"
        outside.write_bytes(b"outside stays untouched")
        plan_path = self.make_plan()
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["moves"][0]["source"] = "../artifacts/secret.txt"
        self.reseal_plan(plan)
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        receipt_path = self.out / "tampered-receipt.json"

        completed, envelope = self.run_cli(
            "apply", "--root", str(self.root), "--plan", str(plan_path), "--receipt", str(receipt_path)
        )

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(envelope["status"], "error")
        self.assertEqual(envelope["data"]["error_code"], "invalid_plan")
        self.assertEqual(outside.read_bytes(), b"outside stays untouched")
        self.assertFalse(receipt_path.exists())

    def test_apply_rejects_changed_source_after_plan(self) -> None:
        source = self.root / "notes.txt"
        source.write_bytes(b"before plan")
        plan_path = self.make_plan()
        source.write_bytes(b"changed after plan")
        receipt_path = self.out / "changed-receipt.json"

        completed, envelope = self.run_cli(
            "apply", "--root", str(self.root), "--plan", str(plan_path), "--receipt", str(receipt_path)
        )

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(envelope["data"]["error_code"], "source_changed")
        self.assertEqual(source.read_bytes(), b"changed after plan")
        self.assertFalse((self.root / "txt" / "notes.txt").exists())

    def test_apply_refuses_a_target_that_appears_after_plan(self) -> None:
        (self.root / "notes.txt").write_bytes(b"source")
        plan_path = self.make_plan()
        bucket = self.root / "txt"
        bucket.mkdir()
        (bucket / "notes.txt").write_bytes(b"new user target")
        receipt_path = self.out / "conflict-receipt.json"

        completed, envelope = self.run_cli(
            "apply", "--root", str(self.root), "--plan", str(plan_path), "--receipt", str(receipt_path)
        )

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(envelope["data"]["error_code"], "destination_exists")
        self.assertEqual((self.root / "notes.txt").read_bytes(), b"source")
        self.assertEqual((bucket / "notes.txt").read_bytes(), b"new user target")

    def test_apply_refuses_repo_roots_and_receipt_undo_refuses_changed_targets(self) -> None:
        (self.root / ".git").mkdir()
        (self.root / "readme.txt").write_text("repository data", encoding="utf-8")
        completed, envelope = self.run_cli("inventory", "--root", str(self.root))
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(envelope["data"]["error_code"], "unsafe_root")

        repo_marker = self.root / ".git"
        repo_marker.rmdir()
        plan_path = self.make_plan()
        receipt_path = self.out / "changed-target-receipt.json"
        applied, _ = self.run_cli(
            "apply", "--root", str(self.root), "--plan", str(plan_path), "--receipt", str(receipt_path)
        )
        self.assertEqual(applied.returncode, 0, applied.stderr)
        target = self.root / "txt" / "readme.txt"
        target.write_text("user edit", encoding="utf-8")

        undone, undo_envelope = self.run_cli(
            "undo", "--root", str(self.root), "--receipt", str(receipt_path)
        )
        self.assertEqual(undone.returncode, 1)
        self.assertEqual(undo_envelope["data"]["error_code"], "target_changed")
        self.assertEqual(target.read_text(encoding="utf-8"), "user edit")
        self.assertFalse((self.root / "readme.txt").exists())

    def test_receipt_undo_recovers_a_partially_applied_batch(self) -> None:
        (self.root / "a.txt").write_bytes(b"first")
        (self.root / "b.pdf").write_bytes(b"second")
        plan_path = self.make_plan()
        receipt_path = self.out / "partial-receipt.json"
        real_move = SAFE_TIDY._move_link_then_unlink
        call_count = 0

        def stop_after_first(source, destination, move):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return real_move(source, destination, move)
            raise KeyboardInterrupt()

        args = type("Args", (), {
            "root": str(self.root),
            "plan": str(plan_path),
            "receipt": str(receipt_path),
        })()
        with mock.patch.object(SAFE_TIDY, "_move_link_then_unlink", side_effect=stop_after_first):
            code, envelope = SAFE_TIDY.command_apply(args)

        self.assertEqual(code, 1)
        self.assertEqual(envelope["status"], "needs_attention")
        self.assertEqual(envelope["data"]["moves_completed"], 1)
        self.assertTrue((self.root / "txt" / "a.txt").exists())
        self.assertTrue((self.root / "b.pdf").exists())

        undone, undo_envelope = self.run_cli(
            "undo", "--root", str(self.root), "--receipt", str(receipt_path)
        )
        self.assertEqual(undone.returncode, 0, undone.stderr)
        self.assertEqual(undo_envelope["data"]["files_restored"], 1)
        self.assertEqual((self.root / "a.txt").read_bytes(), b"first")
        self.assertEqual((self.root / "b.pdf").read_bytes(), b"second")

    def test_receipt_undo_recovers_interruption_between_link_and_unlink(self) -> None:
        source = self.root / "draft.txt"
        source.write_bytes(b"unchanged bytes")
        plan_path = self.make_plan()
        receipt_path = self.out / "mid-move-receipt.json"

        def link_then_interrupt(source_path, destination, move):
            if not os.path.lexists(destination):
                os.link(source_path, destination, follow_symlinks=False)
            raise KeyboardInterrupt()

        args = type("Args", (), {
            "root": str(self.root),
            "plan": str(plan_path),
            "receipt": str(receipt_path),
        })()
        with mock.patch.object(SAFE_TIDY, "_move_link_then_unlink", side_effect=link_then_interrupt):
            code, envelope = SAFE_TIDY.command_apply(args)
        self.assertEqual(code, 1)
        self.assertEqual(envelope["data"]["moves_completed"], 0)
        self.assertTrue(source.exists())
        self.assertTrue((self.root / "txt" / "draft.txt").exists())

        undone, undo_envelope = self.run_cli("undo", "--root", str(self.root), "--receipt", str(receipt_path))
        self.assertEqual(undone.returncode, 0, undone.stderr)
        self.assertEqual(undo_envelope["data"]["files_restored"], 1)
        self.assertEqual(source.read_bytes(), b"unchanged bytes")
        self.assertFalse((self.root / "txt" / "draft.txt").exists())

    def test_root_symlink_and_apple_provider_root_are_refused(self) -> None:
        link = self.out / "source-link"
        link.symlink_to(self.root, target_is_directory=True)
        completed, envelope = self.run_cli("inventory", "--root", str(link))
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(envelope["data"]["error_code"], "unsafe_root")

        if sys.platform == "darwin":
            with mock.patch.object(SAFE_TIDY, "_fileprovider_marker", return_value=True):
                with self.assertRaises(SAFE_TIDY.SafeTidyError) as caught:
                    SAFE_TIDY._resolve_root(str(self.root))
            self.assertEqual(caught.exception.code, "provider_managed_root")


if __name__ == "__main__":
    unittest.main()
