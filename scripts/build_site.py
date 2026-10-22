#!/usr/bin/env python3
"""Assemble the catalog site, including the tools it runs in the browser."""
import argparse
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def script_for(plugin_id: str) -> Path:
    return ROOT / "plugins" / plugin_id / "skills" / plugin_id / "scripts" / (plugin_id.replace("-", "_") + ".py")


def copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".artifacts" / "site")
    args = parser.parse_args()
    output = args.output.resolve()
    if output == ROOT or output in ROOT.parents:
        parser.error("output must not be the repository or one of its parents")
    output.mkdir(parents=True, exist_ok=True)
    catalog = json.loads((ROOT / "catalog.json").read_text())

    for source in (ROOT / "site").rglob("*"):
        if source.is_symlink():
            parser.error("site source contains a symlink")
        if source.is_file():
            copy(source, output / source.relative_to(ROOT / "site"))
    copy(ROOT / "catalog.json", output / "catalog.json")

    for plugin in catalog["plugins"]:
        plugin_id = plugin["id"]
        copy(ROOT / "plugins" / plugin_id / "assets" / "icon.svg", output / "icons" / (plugin_id + ".svg"))
        copy(script_for(plugin_id), output / "cli" / (plugin_id + ".py"))
        demo = plugin["demo"]
        for item in demo.get("inputs", []):
            copy(ROOT / item["source"], output / "samples" / plugin_id / item["path"])
        if "recorded_binaries" in demo:
            copy(ROOT / demo["recorded_binaries"], output / "samples" / plugin_id / "recorded-binaries.json")

    (output / ".nojekyll").write_text("")
    print(json.dumps({"output": str(output), "plugins": len(catalog["plugins"])}))


if __name__ == "__main__":
    main()
