import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

CLI = Path(__file__).resolve().parents[1] / "plugins/ci-spend-check/skills/ci-spend-check/scripts/ci_spend_check.py"


def run_fixture(run_id, conclusion, minutes=10, workflow=1, attempt=1):
    return {"id": run_id, "run_attempt": attempt, "workflow_id": workflow, "name": "Build", "status": "completed", "conclusion": conclusion, "html_url": f"https://github.com/example/project/actions/runs/{run_id}", "run_started_at": "2026-01-01T10:00:00Z", "updated_at": f"2026-01-01T10:{minutes:02}:00Z"}


class CIEvidenceTests(unittest.TestCase):
    def invoke(self, payload, *args):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "runs.json"
            source.write_text(json.dumps(payload))
            result = subprocess.run([sys.executable, str(CLI), "analyze", str(source), *args], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            return json.loads(result.stdout)

    def test_failed_elapsed_is_not_billed_cost(self):
        result = self.invoke({"workflow_runs": [run_fixture(1, "failure"), run_fixture(2, "failure"), run_fixture(3, "success", 5)]})
        data = result["data"]
        self.assertEqual(data["runs_analyzed"], 3)
        self.assertEqual(data["workflows"][0]["failed_runs"], 2)
        self.assertEqual(data["workflows"][0]["failed_elapsed_minutes"], 20)
        self.assertIsNone(data["billed_cost"])
        self.assertIn("elapsed", data["duration_basis"])

    def test_duplicate_run_attempt_is_counted_once(self):
        row = run_fixture(1, "failure")
        result = self.invoke({"workflow_runs": [row, row, run_fixture(1, "success", attempt=2)]})
        self.assertEqual(result["data"]["runs_analyzed"], 2)
        self.assertEqual(result["data"]["duplicate_records_ignored"], 1)

    def test_job_runtime_excludes_skips_and_pending(self):
        row = run_fixture(1, "failure")
        row["jobs"] = [{"id": 10, "status": "completed", "conclusion": "failure", "started_at": "2026-01-01T10:00:00Z", "completed_at": "2026-01-01T10:02:00Z"}, {"id": 11, "status": "queued", "started_at": None, "completed_at": None}]
        result = self.invoke([row])
        self.assertEqual(result["data"]["workflows"][0]["measured_job_minutes"], 2)
        self.assertEqual(result["data"]["unknown_job_durations"], 1)

    def test_missing_duration_is_unknown_not_zero(self):
        row = run_fixture(1, "failure")
        row.pop("updated_at")
        result = self.invoke([row])
        self.assertEqual(result["data"]["unknown_run_durations"], 1)
        self.assertIsNone(result["data"]["workflows"][0]["failed_elapsed_minutes"])

    def test_sample_total_count_does_not_claim_full_history(self):
        result = self.invoke({"total_count": 1000, "workflow_runs": [run_fixture(1, "success")]})
        self.assertTrue(result["data"]["sampled"])
        self.assertEqual(result["data"]["available_run_count"], 1000)

    def test_empty_history_is_explicit(self):
        result = self.invoke({"total_count": 0, "workflow_runs": []})
        self.assertEqual(result["status"], "needs_attention")
        self.assertEqual(result["data"]["workflows"], [])

    def test_malformed_conclusion_returns_error_envelope(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "runs.json"
            row = run_fixture(1, "failure")
            row["conclusion"] = []
            source.write_text(json.dumps([row]))
            result = subprocess.run([sys.executable, str(CLI), "analyze", str(source)], text=True, capture_output=True)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(json.loads(result.stdout)["status"], "error")
            self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
