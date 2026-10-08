# Fieldwork plugins

[![CI](https://github.com/fortunexbt/fieldwork-plugins/actions/workflows/ci.yml/badge.svg)](https://github.com/fortunexbt/fieldwork-plugins/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-c9a227)](LICENSE)

Eight small plugins for ChatGPT and Codex. Each one wraps a single-file Python CLI
(standard library, except CAD Rescue) behind a `SKILL.md`, so the agent runs a bounded,
inspectable command instead of improvising.

| Plugin | What it does |
| --- | --- |
| [Minecraft Performance Lab](plugins/frame-lab) | Compares Minecraft frame captures, rejects incomplete runs, reports frame-time tails and true 1% lows next to the settings that produced them. Bundles the [Minecraft Frame Bench](https://github.com/fortunexbt/minecraft-frame-bench) harness. |
| [Fit to Upload](plugins/fit-to-upload) | Probes a video, plans a byte budget for a size cap, encodes a verified MP4 with FFmpeg. Originals untouched. |
| [Tablebeam](plugins/tablebeam) | Answers filter, count, sum and grouped-aggregate questions about a CSV with exact decimal arithmetic and a view of the source rows. Runs locally. |
| [ShipProof](plugins/shipproof) | Compares expected release assets and content markers with a live URL, and prepares a repository handoff with revision and working-tree evidence. |
| [Safe Tidy](plugins/safe-tidy) | Finds byte-identical files, previews a reorganization, applies it with a receipt, undoes it later. Never deletes or overwrites. |
| [CAD Rescue](plugins/cad-rescue) | Inspects DXF, SVG and DWG drawings (layers, units, extents), builds a vector preview, recovers a validated DXF copy. DWG goes through an optional LibreDWG runtime. GPL-3.0. |
| [CI Spend Check](plugins/ci-spend-check) | Ranks GitHub Actions workflows by repeated failures, cancelled work and duration, with the calculation and links behind each finding. |
| [Asset Check](plugins/asset-check) | Checks glTF/GLB exports against explicit triangle and byte budgets and reports missing resources. |

## Design rules

- Input paths are explicit. Nothing searches the home directory.
- Output files must be new; existing files are refused.
- Every command prints a JSON envelope (`status`, `summary`, `data`, `warnings`) and,
  where it writes a file, a receipt with path, size and SHA-256.
- Reports state what they do not measure. For example, Frame Lab numbers are CPU
  frame-production intervals, not GPU or display latency, and Asset Check is not a
  substitute for frame-rate measurement on target devices.

## Layout

```
plugins/<name>/
  plugin.json            portable manifest
  .codex-plugin/         Codex manifest
  skills/<name>/         SKILL.md, scripts/, synthetic examples in assets/
scripts/smoke.sh         runs every bundled example
```

## Try it

Needs Python 3.11 or later. Each plugin documents its commands in its `SKILL.md`;
for example:

```sh
cd plugins/tablebeam/skills/tablebeam
python3 scripts/tablebeam.py profile assets/demo-sales.csv

python3 ../../../frame-lab/skills/frame-lab/scripts/frame_lab.py demo
```

CAD Rescue needs `pip install -r plugins/cad-rescue/skills/cad-rescue/requirements.txt`.
Fit to Upload needs `ffmpeg` and `ffprobe`.

## Test

```sh
python3 -m pip install -r plugins/cad-rescue/skills/cad-rescue/requirements.txt
./scripts/smoke.sh
```

The smoke script validates every manifest and runs the bundled synthetic example for
Frame Lab, Tablebeam, CI Spend Check, Asset Check, CAD Rescue and Safe Tidy. It is not
a unit-test suite. Fit to Upload and ShipProof need FFmpeg and a live URL respectively and are not covered.

## Status

Not published to the OpenAI plugin directory: submission is waiting on developer
identity verification.

## License

MIT, except `plugins/cad-rescue`, which is GPL-3.0-only (see its `LICENSE`).
