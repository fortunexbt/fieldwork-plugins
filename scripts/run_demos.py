#!/usr/bin/env python3
"""Run every catalog demo against the real tools, the way the site runs them.

The site drives each plugin from the `demo` block in catalog.json and executes
the unmodified script in Pyodide. This runs the same steps with the local
interpreter, so a demo that drifts from its tool fails in CI rather than in a
browser. Every demo must name where its input came from, and every value the
page shows must be present in the tool's own output.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = re.compile(r"^(.*?)\[(\d+)\]$")


def resolve(payload, path):
    """Read a dotted path, mirroring the resolver the site uses."""
    current = payload
    for part in path.split("."):
        match = INDEX.match(part)
        index = None
        if match:
            part, index = match.group(1), int(match.group(2))
        if part:
            if not isinstance(current, dict) or part not in current:
                return None
            current = current[part]
        if index is not None:
            if not isinstance(current, list) or index >= len(current):
                return None
            current = current[index]
    return current


def script_for(plugin_id):
    return ROOT / "plugins" / plugin_id / "skills" / plugin_id / "scripts" / (plugin_id.replace("-", "_") + ".py")


def rows_of(collection):
    if isinstance(collection, dict):
        return [dict(value, _key=key) if isinstance(value, dict) else {"_key": key, "value": value}
                for key, value in collection.items()]
    return collection if isinstance(collection, list) else []


def check_view(step, payload, failures, label):
    for stat in step.get("stats", []):
        if resolve(payload, stat["path"]) is None:
            failures.append(f"{label}: stat {stat['path']} is missing from the result")
    table = step.get("table")
    if table:
        rows = rows_of(resolve(payload, table["path"]))
        if not rows:
            failures.append(f"{label}: table {table['path']} resolved to no rows")
            return
        for column in table["columns"]:
            if all(resolve(row, column["path"]) is None for row in rows):
                failures.append(f"{label}: table column {column['path']} is missing from every row")


def check_input(plugin, failures):
    credit = plugin["demo"].get("input", {})
    for field in ("about", "credit", "url"):
        if not str(credit.get(field, "")).strip():
            failures.append(f"{plugin['id']}: demo input has no {field}")
    if not str(credit.get("url", "")).startswith("https://"):
        failures.append(f"{plugin['id']}: demo input url must be an https link to the source")
    if not plugin["demo"].get("inputs"):
        failures.append(f"{plugin['id']}: demo has no input files")


def run_demo(plugin, work):
    demo = plugin["demo"]
    failures = []
    check_input(plugin, failures)
    for item in demo.get("inputs", []):
        target = work / item["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / item["source"], target)

    recorded = demo.get("recorded_binaries")
    if recorded:
        saved = json.loads((ROOT / recorded).read_text())
        clip = next(item for item in demo["inputs"] if item["path"].endswith(".mp4"))
        measured = (ROOT / clip["source"]).stat().st_size
        declared = int(json.loads(saved["ffprobe"]["stdout"])["format"]["size"])
        if declared != measured:
            failures.append(f"{plugin['id']}: recorded ffprobe reports {declared} bytes, the clip is {measured}")

    for number, step in enumerate(demo["steps"], start=1):
        label = f"{plugin['id']} step {number}"
        completed = subprocess.run([sys.executable, str(script_for(plugin["id"])), *step["argv"]],
                                   capture_output=True, text=True, timeout=180, cwd=work, check=False)
        if completed.returncode != 0:
            failures.append(f"{label}: exit {completed.returncode}: {completed.stderr.strip()[-400:] or completed.stdout.strip()[-400:]}")
            continue
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            failures.append(f"{label}: stdout is not JSON: {completed.stdout[:200]}")
            continue
        if payload.get("status") == "error":
            failures.append(f"{label}: tool reported an error: {payload.get('summary')}")
            continue
        check_view(step, payload, failures, label)
        for item in [*step.get("downloads", []), *([step["image"]] if "image" in step else [])]:
            if not (work / item["path"]).is_file():
                failures.append(f"{label}: expected output {item['path']} was not written")
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin", help="Run one catalog plugin instead of all of them.")
    args = parser.parse_args()
    catalog = json.loads((ROOT / "catalog.json").read_text())
    plugins = [item for item in catalog["plugins"] if not args.plugin or item["id"] == args.plugin]
    if not plugins:
        parser.error("plugin is not in the catalog")

    failures = []
    for plugin in plugins:
        for manifest in (ROOT / "plugins" / plugin["id"] / "plugin.json",
                         ROOT / "plugins" / plugin["id"] / ".codex-plugin" / "plugin.json"):
            json.loads(manifest.read_text())
        with tempfile.TemporaryDirectory(prefix="fieldwork-demo-") as temporary:
            found = run_demo(plugin, Path(temporary))
        print(f"{plugin['id']}: {'ok' if not found else 'FAILED'}")
        failures.extend(found)

    for failure in failures:
        print("  " + failure, file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
