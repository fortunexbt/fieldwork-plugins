#!/usr/bin/env bash
# Runs each plugin's bundled synthetic example end to end and fails on any non-zero exit.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
python3 -c 'import ezdxf' 2>/dev/null || {
  echo "ezdxf is missing. Activate the virtual environment from the README Test section, or install plugins/cad-rescue/skills/cad-rescue/requirements.txt." >&2
  exit 1
}
out="$(mktemp -d)"
trap 'rm -rf "$out"' EXIT

run() { echo "+ $*"; "$@" >/dev/null; }

for p in "$root"/plugins/*/; do
  python3 -c 'import json,sys; json.load(open(sys.argv[1])); json.load(open(sys.argv[2]))' \
    "$p/plugin.json" "$p/.codex-plugin/plugin.json"
done

run python3 "$root/plugins/frame-lab/skills/frame-lab/scripts/frame_lab.py" demo

cd "$root/plugins/tablebeam/skills/tablebeam"
run python3 scripts/tablebeam.py profile assets/demo-sales.csv
run python3 scripts/tablebeam.py query assets/demo-sales.csv --spec assets/demo-query.json \
  --export "$out/north-by-owner.csv" --export-format csv

cd "$root/plugins/ci-spend-check/skills/ci-spend-check"
run python3 scripts/ci_spend_check.py analyze assets/sample-runs.json

cd "$root/plugins/asset-check/skills/asset-check"
run python3 scripts/asset_check.py inspect --input assets/tiny-scene.gltf \
  --max-triangles 1 --max-bytes 4096 \
  --report "$out/scene.html" --receipt "$out/scene.json"

cd "$root/plugins/cad-rescue/skills/cad-rescue"
run python3 scripts/cad_rescue.py inspect --input assets/demo-plan.dxf
run python3 scripts/cad_rescue.py preview --input assets/demo-plan.dxf --output "$out/plan.svg"

mkdir "$out/tidy"; echo a >"$out/tidy/a.txt"; echo a >"$out/tidy/b.txt"
run python3 "$root/plugins/safe-tidy/skills/safe-tidy/scripts/safe_tidy.py" inventory --root "$out/tidy"

echo "smoke ok"
