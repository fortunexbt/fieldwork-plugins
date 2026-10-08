#!/usr/bin/env python3
"""Assemble the static catalog from versioned metadata and verified examples."""
import argparse
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".artifacts" / "site")
    args = parser.parse_args()
    output = args.output.resolve()
    if output == ROOT or output in ROOT.parents:
        parser.error("output must not be the repository or one of its parents")
    output.mkdir(parents=True, exist_ok=True)
    catalog = json.loads((ROOT / "catalog.json").read_text())
    missing = [p["id"] for p in catalog["plugins"] if not (ROOT / "site" / "examples" / (p["id"] + ".json")).is_file()]
    if missing:
        parser.error("verified examples missing: " + ", ".join(missing))
    for source in (ROOT / "site").rglob("*"):
        if source.is_symlink():
            parser.error("site source contains a symlink")
        if source.is_file():
            target = output / source.relative_to(ROOT / "site")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    shutil.copyfile(ROOT / "catalog.json", output / "catalog.json")
    (output / "icons").mkdir(exist_ok=True)
    for plugin in catalog["plugins"]:
        shutil.copyfile(ROOT / "plugins" / plugin["id"] / "assets" / "icon.svg", output / "icons" / (plugin["id"] + ".svg"))
    (output / ".nojekyll").write_text("")
    print(json.dumps({"output": str(output), "plugins": len(catalog["plugins"]), "examples": len(catalog["plugins"])}))


if __name__ == "__main__":
    main()
