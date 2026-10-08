#!/usr/bin/env python3
"""Read GitHub Actions evidence and rank repeated failures. Never modifies GitHub."""
import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def envelope(status, summary, data, artifacts=None, warnings=None):
    return {"schema_version": 1, "tool": "ci-spend-check", "status": status, "summary": summary, "data": data, "artifacts": artifacts or [], "warnings": warnings or []}


def elapsed_seconds(start, end):
    try:
        a = datetime.fromisoformat(start.replace("Z", "+00:00"))
        b = datetime.fromisoformat(end.replace("Z", "+00:00"))
        if a.tzinfo is None or b.tzinfo is None or b < a:
            return None
        return (b - a).total_seconds()
    except (AttributeError, TypeError, ValueError):
        return None


def analyze(payload):
    rows = payload.get("workflow_runs", []) if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("Expected a workflow_runs array or a JSON array of run objects.")
    available = payload.get("total_count") if isinstance(payload, dict) else None
    if available is not None and (isinstance(available, bool) or not isinstance(available, int) or available < 0):
        raise ValueError("total_count must be a nonnegative integer when provided.")
    workflows = {}
    seen = set()
    duplicates = unknown_runs = unknown_jobs = missing_ids = 0
    warnings = []
    for row in rows:
        run_id = row.get("id")
        if not isinstance(run_id, (int, str)) or isinstance(run_id, bool) or not str(run_id).strip():
            missing_ids += 1
            continue
        key = (str(run_id), str(row.get("run_attempt", 1)))
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        workflow_id = str(row.get("workflow_id", row.get("path", row.get("name", "unknown"))))
        group = workflows.setdefault(workflow_id, {"workflow_id": workflow_id, "name": str(row.get("name") or "Unnamed workflow"), "runs": 0, "completed_runs": 0, "failed_runs": 0, "cancelled_runs": 0, "successful_runs": 0, "run_elapsed_seconds": [], "failed_elapsed_seconds": [], "job_seconds": [], "evidence": []})
        group["runs"] += 1
        completed = row.get("status") == "completed"
        conclusion = row.get("conclusion")
        if conclusion is not None and not isinstance(conclusion, str):
            raise ValueError(f"Run {run_id}: conclusion must be a string or null.")
        failed = completed and conclusion in {"failure", "timed_out", "startup_failure", "action_required"}
        group["completed_runs"] += int(completed)
        group["failed_runs"] += int(failed)
        group["cancelled_runs"] += int(completed and conclusion == "cancelled")
        group["successful_runs"] += int(completed and conclusion == "success")
        duration = elapsed_seconds(row.get("run_started_at"), row.get("updated_at")) if completed else None
        if duration is None:
            unknown_runs += 1
        else:
            group["run_elapsed_seconds"].append(duration)
            if failed:
                group["failed_elapsed_seconds"].append(duration)
        jobs = row.get("jobs", [])
        if isinstance(jobs, dict):
            jobs = jobs.get("jobs", [])
        if not isinstance(jobs, list):
            raise ValueError(f"Run {run_id}: jobs must be an array.")
        if row.get("jobs_truncated"):
            warnings.append(f"Run {run_id}: job totals cover a truncated sample, not every job.")
        job_ids = set()
        for job in jobs:
            if not isinstance(job, dict):
                raise ValueError(f"Run {run_id}: each job must be an object.")
            job_id = job.get("id")
            if job_id is not None and str(job_id) in job_ids:
                continue
            if job_id is not None:
                job_ids.add(str(job_id))
            job_time = elapsed_seconds(job.get("started_at"), job.get("completed_at")) if job.get("status") == "completed" and job.get("conclusion") != "skipped" else None
            if job_time is None:
                unknown_jobs += 1
            else:
                group["job_seconds"].append(job_time)
        group["evidence"].append({"run_id": run_id, "attempt": row.get("run_attempt", 1), "status": row.get("status"), "conclusion": conclusion, "url": row.get("html_url"), "elapsed_minutes": round(duration / 60, 3) if duration is not None else None})
    results = []
    for group in workflows.values():
        for source, target in [("run_elapsed_seconds", "run_elapsed_minutes"), ("failed_elapsed_seconds", "failed_elapsed_minutes"), ("job_seconds", "measured_job_minutes")]:
            durations = group.pop(source)
            group[target] = round(sum(durations) / 60, 3) if durations else None
        group["failure_rate"] = round(group["failed_runs"] / group["completed_runs"], 4) if group["completed_runs"] else None
        group["recommendations"] = []
        if group["failed_runs"] >= 2:
            group["recommendations"].append("Compare the linked failure logs for a repeated cause before changing triggers or disabling the workflow.")
        if group["cancelled_runs"]:
            group["recommendations"].append("Check whether cancellations are intentional concurrency replacement; cancellation alone does not prove waste.")
        if group["run_elapsed_minutes"] and not group["measured_job_minutes"]:
            group["recommendations"].append("Collect completed job timestamps to separate job runtime from overall run elapsed time.")
        results.append(group)
    results.sort(key=lambda x: (-x["failed_runs"], -(x["failed_elapsed_minutes"] or 0), x["workflow_id"]))
    if missing_ids:
        warnings.append(f"Skipped {missing_ids} records without a usable run ID.")
    if unknown_runs:
        warnings.append(f"{unknown_runs} run durations are unknown or not completed; they were not recorded as zero.")
    if unknown_jobs:
        warnings.append(f"{unknown_jobs} job durations were not measurable (including skipped or pending jobs).")
    analyzed = len(seen)
    sampled = available is not None and available > len({item[0] for item in seen})
    if sampled:
        warnings.append("This is a sample of repository history, not a complete lifetime audit.")
    data = {"runs_analyzed": analyzed, "available_run_count": available, "sampled": sampled, "duplicate_records_ignored": duplicates, "unknown_run_durations": unknown_runs, "unknown_job_durations": unknown_jobs, "duration_basis": "Run elapsed = run_started_at to updated_at for completed runs. Measured job runtime uses completed job timestamps when supplied. Neither is billed cost.", "billed_cost": None, "workflows": results}
    failed = sum(group["failed_runs"] for group in results)
    status = "needs_attention" if failed or not analyzed or missing_ids else "ok"
    summary = f"Inspected {analyzed} run attempts across {len(results)} workflows; {failed} failed attempts need review." if analyzed else "No usable workflow runs were supplied."
    return envelope(status, summary, data, warnings=warnings)


def gh_get(endpoint):
    if not shutil.which("gh"):
        raise ValueError("GitHub CLI is missing. Supply exported run JSON or install and authenticate gh separately.")
    result = subprocess.run(["gh", "api", "--method", "GET", endpoint], capture_output=True, text=True, timeout=60)
    if result.returncode:
        # Do not echo arbitrary CLI diagnostics that could contain private headers.
        raise ValueError("GitHub read failed. Check gh auth status and repository access; no settings were changed.")
    return json.loads(result.stdout)


def collect(repo, limit, include_jobs):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or any(part in {".", ".."} for part in repo.split("/")):
        raise ValueError("Repository must be owner/name, without a URL or extra path.")
    payload = gh_get(f"repos/{repo}/actions/runs?per_page={limit}")
    rows = payload.get("workflow_runs", [])
    if include_jobs:
        for row in rows:
            run_id, attempt = row.get("id"), row.get("run_attempt", 1)
            if not isinstance(run_id, int) or not isinstance(attempt, int):
                raise ValueError("GitHub returned an invalid run identifier.")
            jobs = gh_get(f"repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100")
            row["jobs"] = jobs.get("jobs", [])
            if jobs.get("total_count", 0) > len(row["jobs"]):
                row["jobs_truncated"] = True
    result = analyze(payload)
    result["data"]["repository"] = repo
    return payload, result


def save(path, value):
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    read = commands.add_parser("analyze", help="Analyze exported GitHub Actions JSON without network access")
    read.add_argument("input", type=Path)
    read.add_argument("--output", type=Path, help="Write a new result JSON file (never overwrite)")
    fetch = commands.add_parser("fetch", help="Read a bounded sample from GitHub using existing gh authentication")
    fetch.add_argument("repo")
    fetch.add_argument("--limit", type=int, default=30, choices=range(1, 101), metavar="1..100")
    fetch.add_argument("--jobs", action="store_true", help="Also read job timestamps; adds one request per run")
    fetch.add_argument("--output", type=Path, help="Save the captured raw run data to a new JSON file")
    args = parser.parse_args()
    try:
        if args.output and args.output.exists():
            raise ValueError("Output already exists. Choose a new filename.")
        if args.command == "analyze":
            raw = args.input.read_bytes()
            payload = json.loads(raw)
            result = analyze(payload)
            result["data"]["input_sha256"] = hashlib.sha256(raw).hexdigest()
            saved = result
        else:
            payload, result = collect(args.repo, args.limit, args.jobs)
            saved = payload
        if args.output:
            save(args.output, saved)
            result["artifacts"].append({"path": str(args.output), "label": "Captured runs" if args.command == "fetch" else "CI evidence report", "media_type": "application/json"})
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(json.dumps(envelope("error", str(exc), {"error_code": "ci_evidence_failed"}), ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
