"""User-facing Tablebeam CLI checks using only temporary CSV fixtures."""

from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "plugins" / "tablebeam" / "skills" / "tablebeam" / "scripts" / "tablebeam.py"


class TablebeamCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_csv(self, name: str, content: str, encoding: str = "utf-8") -> Path:
        path = self.root / name
        path.write_bytes(content.encode(encoding))
        return path

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
            self.fail(f"CLI did not return a JSON envelope: {completed.stdout!r}; stderr={completed.stderr!r}; {exc}")
        self.assertEqual(envelope.get("schema_version"), 1)
        self.assertEqual(envelope.get("tool"), "tablebeam")
        return completed, envelope

    def query(self, source: Path, spec: dict, *args: str) -> tuple[subprocess.CompletedProcess[str], dict]:
        spec_path = self.root / "query.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        return self.run_cli("query", str(source), "--spec", str(spec_path), *args)

    def test_exact_sum_filter_and_group_keep_large_identifiers_and_decimals(self) -> None:
        source = self.write_csv(
            "ledger.csv",
            "account_id,region,amount\n"
            "9007199254740993,North,0.1\n"
            "9007199254740994,North,0.2\n"
            "0007,South,9.00\n",
        )
        completed, envelope = self.query(
            source,
            {
                "where": [{"column": "account_id", "op": "eq", "value": "9007199254740993"}],
                "group_by": ["region"],
                "aggregate": {"op": "sum", "column": "amount"},
            },
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(envelope["status"], "ok")
        row = envelope["data"]["result_rows"][0]
        self.assertEqual(row["group"], {"region": "North"})
        self.assertEqual(row["value"], "0.1")
        self.assertEqual(row["source_row_coverage"]["ranges"], [[1, 1]])

        _, sum_result = self.query(source, {"aggregate": {"op": "sum", "column": "amount"}})
        self.assertEqual(sum_result["data"]["result_rows"][0]["value"], "9.3")

    def test_exact_large_integer_sum_and_decimal_threshold(self) -> None:
        source = self.write_csv(
            "large.csv",
            "n,amount\n9007199254740992,0.10\n9007199254740993,0.20\n",
        )
        _, envelope = self.query(
            source,
            {
                "where": [{"column": "n", "op": "gt", "type": "decimal", "value": "9007199254740992.5"}],
                "aggregate": {"op": "sum", "column": "n"},
            },
        )
        self.assertEqual(envelope["data"]["result_rows"][0]["value"], "9007199254740993")
        self.assertEqual(envelope["data"]["source_coverage"]["row_id_ranges"]["ranges"], [[2, 2]])

        decimals = self.write_csv(
            "precise.csv",
            "amount\n12345678901234567890123456789.12345678901234567890\n0.00000000000000000001\n",
        )
        _, precise_result = self.query(decimals, {"aggregate": {"op": "sum", "column": "amount"}})
        self.assertEqual(
            precise_result["data"]["result_rows"][0]["value"],
            "12345678901234567890123456789.12345678901234567891",
        )

    def test_profile_handles_quoted_delimiters_unicode_and_missing_cells(self) -> None:
        source = self.write_csv(
            "quoted.csv",
            'name,comment,amount\n"Zoë, Li","said ""ciao"" ☕",1.25\nNina,plain,\nKai,short\n',
        )
        completed, envelope = self.run_cli("profile", str(source))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        data = envelope["data"]
        self.assertEqual(data["profile"]["row_count"], 3)
        self.assertEqual(data["delimiter"], ",")
        by_name = {column["name"]: column for column in data["profile"]["columns"]}
        self.assertEqual(by_name["amount"]["missing_count"], 2)
        self.assertEqual(by_name["comment"]["sample_values"][0], 'said "ciao" ☕')
        self.assertIn("row_width_mismatch", {warning["code"] for warning in envelope["warnings"]})

    def test_semicolon_and_declared_encoding_are_reported_without_value_rewrite(self) -> None:
        source = self.write_csv("latin.csv", "nom;città\nAda;Forlì\n", "latin-1")
        completed, envelope = self.run_cli(
            "profile", str(source), "--encoding", "latin-1", "--delimiter", ";"
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(envelope["data"]["encoding"], "latin-1")
        self.assertEqual(envelope["data"]["delimiter"], ";")
        self.assertEqual(envelope["data"]["profile"]["columns"][1]["sample_values"], ["Forlì"])

    def test_profile_marks_identifier_like_numeric_text_as_ambiguous(self) -> None:
        source = self.write_csv("ids.csv", "customer_id,amount\n0007,1.25\n0008,2.75\n")
        _, envelope = self.run_cli("profile", str(source))
        columns = {column["name"]: column for column in envelope["data"]["profile"]["columns"]}
        self.assertEqual(columns["customer_id"]["inferred_type"], "identifier_like")
        self.assertEqual(columns["amount"]["inferred_type"], "numeric_candidate")

    def test_csv_export_escapes_formula_cells_but_json_evidence_keeps_raw_text(self) -> None:
        source = self.write_csv("formula.csv", 'label\n"=SUM(1,2)"\n  @command\n-safe text\n')
        exported = self.root / "safe.csv"
        preview, preview_envelope = self.query(
            source,
            {},
            "--export",
            str(exported),
            "--export-format",
            "csv",
            "--preview",
        )
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertFalse(exported.exists())
        self.assertFalse(preview_envelope["data"]["export_preview"]["written"])
        completed, envelope = self.query(
            source,
            {},
            "--export",
            str(exported),
            "--export-format",
            "csv",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        raw = [row["values"]["label"] for row in envelope["data"]["result_rows"]]
        self.assertEqual(raw, ["=SUM(1,2)", "  @command", "-safe text"])
        with exported.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.reader(stream))
        label_index = rows[0].index("label")
        self.assertEqual([row[label_index] for row in rows[1:]], ["'=SUM(1,2)", "'  @command", "'-safe text"])
        self.assertEqual([row[:3] for row in rows[1:]], [["1", "2", "2"], ["2", "3", "3"], ["3", "4", "4"]])
        receipt = envelope["data"]["export_receipt"]
        self.assertEqual(receipt["escaped_formula_cells"], 3)
        self.assertEqual(receipt["verified"], True)
        self.assertEqual(receipt["sha256"], preview_envelope["data"]["export_preview"]["sha256"])

    def test_grouped_average_keeps_exact_fraction_alongside_decimal_display(self) -> None:
        source = self.write_csv("avg.csv", "team,value\nA,0.1\nA,0.2\n")
        _, envelope = self.query(
            source,
            {"group_by": ["team"], "aggregate": {"op": "avg", "column": "value"}},
        )
        self.assertEqual(envelope["data"]["result_rows"][0]["value"], {
            "exact": "3/20",
            "decimal": "0.15",
        })

    def test_grouped_sum_keeps_blank_and_short_cells_in_evidence(self) -> None:
        source = self.write_csv("missing.csv", "team,amount\nA,1.00\nA,\n,2\nB\n")
        _, envelope = self.query(
            source,
            {"group_by": ["team"], "aggregate": {"op": "sum", "column": "amount"}},
        )
        rows = envelope["data"]["result_rows"]
        self.assertEqual([row["group"]["team"] for row in rows], ["A", None, "B"])
        self.assertEqual(rows[0]["value"], "1")
        self.assertEqual((rows[0]["rows"], rows[0]["value_count"], rows[0]["missing_count"]), (2, 1, 1))
        self.assertEqual(rows[0]["source_row_coverage"]["row_count"], 2)
        self.assertEqual(rows[0]["source_row_coverage"]["ranges"], [[1, 2]])
        self.assertEqual(rows[1]["value"], "2")
        self.assertIsNone(rows[2]["value"])
        self.assertIn("team=B: no values", envelope["summary"])
        self.assertIn("row_width_mismatch", {warning["code"] for warning in envelope["warnings"]})

    def test_csv_export_keeps_headers_for_empty_results_and_metadata_names_do_not_clobber_source(self) -> None:
        source = self.write_csv("colliding.csv", "_tablebeam_row_id,name\n9007199254740993,Ada\n")
        output = self.root / "empty.csv"
        completed, envelope = self.query(
            source,
            {"where": [{"column": "name", "op": "eq", "value": "nobody"}]},
            "--export",
            str(output),
            "--export-format",
            "csv",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(envelope["data"]["matched_row_count"], 0)
        with output.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.reader(stream))
        self.assertEqual(len(rows), 1)
        self.assertIn("_tablebeam_row_id", rows[0])
        self.assertIn("__tablebeam_row_id", rows[0])

        all_completed, all_envelope = self.query(source, {})
        self.assertEqual(all_completed.returncode, 0, all_completed.stderr)
        row = all_envelope["data"]["result_rows"][0]
        self.assertEqual(row["row_id"], 1)
        self.assertEqual(row["values"]["_tablebeam_row_id"], "9007199254740993")

    def test_non_decimal_measure_is_never_silently_dropped_from_sum(self) -> None:
        source = self.write_csv("mixed.csv", "amount\n1.00\npending\n")
        completed, envelope = self.query(source, {"aggregate": {"op": "sum", "column": "amount"}})
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(envelope["status"], "error")
        self.assertEqual(envelope["data"]["error_code"], "invalid_decimal")

    def test_numeric_aggregates_report_the_first_invalid_row_even_for_min_and_max(self) -> None:
        source = self.write_csv("invalid-order.csv", "amount\n1.00\npending-first\npending-later\n")
        for operation in ("sum", "avg", "min", "max"):
            completed, envelope = self.query(source, {"aggregate": {"op": operation, "column": "amount"}})
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(envelope["data"]["error_code"], "invalid_decimal")
            self.assertIn("row 2", envelope["summary"])
            self.assertIn("pending-first", envelope["summary"])
            self.assertNotIn("pending-later", envelope["summary"])

    def test_large_result_preview_is_bounded_without_truncating_full_json_export(self) -> None:
        group_count = 15_000
        source = self.write_csv("wide-result.csv", "group,amount\n" + "".join(f"G{i},1\n" for i in range(group_count)))
        export_path = self.root / "complete.json"
        completed, envelope = self.query(
            source,
            {"group_by": ["group"], "aggregate": {"op": "sum", "column": "amount"}},
            "--export",
            str(export_path),
            "--export-format",
            "json",
            "--preview",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse(export_path.exists())
        data = envelope["data"]
        self.assertEqual(data["matched_row_count"], group_count)
        self.assertEqual(data["result_group_count"], group_count)
        self.assertEqual(len(data["result_rows"]), 50)
        self.assertTrue(data["preview"]["truncated"])
        self.assertEqual(data["source_coverage"]["row_id_ranges"]["ranges"], [[1, group_count]])
        self.assertIn("First 5 groups", envelope["summary"])
        self.assertIn("14,995 other groups", envelope["summary"])
        self.assertLess(len(completed.stdout), 30_000)

        saved, saved_envelope = self.query(
            source,
            {"group_by": ["group"], "aggregate": {"op": "sum", "column": "amount"}},
            "--export",
            str(export_path),
            "--export-format",
            "json",
        )
        self.assertEqual(saved.returncode, 0, saved.stderr)
        self.assertTrue(saved_envelope["data"]["export_receipt"]["verified"])
        full = json.loads(export_path.read_text(encoding="utf-8"))["data"]
        self.assertEqual(len(full["result_rows"]), group_count)
        self.assertFalse(full["preview"]["truncated"])
        self.assertEqual(full["result_rows"][-1]["source_row_ids"], [group_count])

    def test_profile_column_preview_is_explicit_and_can_be_expanded(self) -> None:
        headers = [f"c{i}" for i in range(102)]
        source = self.write_csv("many-columns.csv", ",".join(headers) + "\n" + ",".join("x" for _ in headers) + "\n")
        _, default_profile = self.run_cli("profile", str(source))
        profile = default_profile["data"]["profile"]
        self.assertEqual(profile["column_count"], 102)
        self.assertEqual(len(profile["columns"]), 100)
        self.assertTrue(profile["preview"]["truncated"])

        _, full_profile = self.run_cli("profile", str(source), "--all-columns")
        self.assertEqual(len(full_profile["data"]["profile"]["columns"]), 102)
        self.assertFalse(full_profile["data"]["profile"]["preview"]["truncated"])

    def test_profile_bounds_long_sample_values_and_marks_the_excerpt(self) -> None:
        source = self.write_csv("long-cell.csv", "notes\n" + ("x" * 500) + "\n")
        _, envelope = self.run_cli("profile", str(source))
        column = envelope["data"]["profile"]["columns"][0]
        self.assertEqual(len(column["sample_values"][0]), 201)
        self.assertEqual(column["sample_value_character_counts"], [500])
        self.assertTrue(column["sample_values_truncated"])

    def test_duplicate_headers_complete_inspection_with_attention_and_block_query(self) -> None:
        source = self.write_csv("duplicate.csv", "value,value\n1,2\n")
        completed, profile = self.run_cli("profile", str(source))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(profile["status"], "needs_attention")
        self.assertIn("duplicate_headers", {warning["code"] for warning in profile["warnings"]})
        queried, error = self.query(source, {"aggregate": {"op": "count"}})
        self.assertEqual(queried.returncode, 1)
        self.assertEqual(error["status"], "error")
        self.assertEqual(error["data"]["error_code"], "ambiguous_headers")

    def test_output_is_never_overwritten(self) -> None:
        source = self.write_csv("one.csv", "name\nA\n")
        target = self.root / "result.json"
        target.write_text("keep me", encoding="utf-8")
        completed, envelope = self.query(
            source,
            {},
            "--export",
            str(target),
            "--export-format",
            "json",
        )
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(envelope["data"]["error_code"], "output_exists")
        self.assertEqual(target.read_text(encoding="utf-8"), "keep me")


if __name__ == "__main__":
    unittest.main()
