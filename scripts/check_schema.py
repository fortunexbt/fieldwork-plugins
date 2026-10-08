#!/usr/bin/env python3
"""Validate manifests against the published, versioned Agent Plugins JSON schema."""
import json
import sys
import urllib.request
from pathlib import Path

import jsonschema
from release import SCHEMA as URL

ROOT = Path(__file__).resolve().parents[1]


def main():
    with urllib.request.urlopen(URL, timeout=20) as response:
        schema = json.load(response)
    validator = jsonschema.Draft202012Validator(schema)
    failures = []
    checked = 0
    for path in sorted((ROOT / "plugins").glob("*/plugin.json")):
        checked += 1
        failures.extend(f"{path.parent.name}: {error.message}" for error in validator.iter_errors(json.loads(path.read_text())))
    print(json.dumps({"schema": URL, "checked": checked, "errors": failures}, indent=2))
    return int(bool(failures) or checked == 0)


if __name__ == "__main__":
    sys.exit(main())
