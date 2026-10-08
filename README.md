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
scripts/                 release tooling and smoke scripts
tests/                   unit tests
site/                    catalog site, published to GitHub Pages
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

Needs Python 3.11 or later. Homebrew and other system Pythons refuse `pip install`
outside a virtual environment, so create one first:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt -r plugins/cad-rescue/skills/cad-rescue/requirements.txt
python -m unittest discover -s tests
./scripts/smoke.sh
```

The 81 unit tests cover all eight plugins and the release tooling. Two are skipped unless
you set `CAD_RESCUE_DWG_FIXTURE` and `CAD_RESCUE_LIBREDWG_RUNTIME`, or
`FRAME_LAB_RAW_EVIDENCE`; they need a real DWG file, a LibreDWG runtime or Minecraft
captures. The Fit to Upload tests need `ffmpeg`, and the CAD Rescue preview tests use
`rsvg-convert`.

`scripts/smoke.sh` validates every manifest and runs the bundled synthetic example for
Frame Lab, Tablebeam, CI Spend Check, Asset Check, CAD Rescue and Safe Tidy. Fit to Upload
and ShipProof need FFmpeg and a live URL and are not covered by it.

CI runs the same tests on Python 3.11 and 3.13, then builds the eight release ZIPs and
smoke-tests each one from a clean extraction (`python scripts/release.py build`,
`python scripts/smoke_archives.py`).

## Site

The catalog at https://fortunexbt.github.io/fieldwork-plugins/ is built from `site/` and
`catalog.json` by `python scripts/build_site.py` and deployed by the Pages workflow on
each push to `main`.

## Status

Not published to the OpenAI plugin directory: submission is waiting on developer
identity verification.

## License

MIT, except `plugins/cad-rescue`, which is GPL-3.0-only (see its `LICENSE`).
