---
name: asset-check
description: Inspect one glTF or GLB export for accessor-backed geometry, scene instances, material textures, resource sizes, and user-supplied budgets.
---

# Asset Check

Run `scripts/asset_check.py` with Python 3.11 or later. It uses only the standard library and reads one explicitly selected `.gltf` or `.glb` file. It does not open a 3D renderer, fetch remote URIs, or search other directories.

## First run

The included synthetic example has one indexed triangle and one embedded texture. From this skill directory, inspect it and save a visual report plus a JSON receipt:

```sh
python3 scripts/asset_check.py inspect \
  --input assets/tiny-scene.gltf \
  --max-triangles 1 \
  --max-bytes 4096 \
  --report tiny-scene.asset-check.html \
  --receipt tiny-scene.asset-check.json
```

Both output paths must be new files. The original export is left untouched. The command also prints the same JSON receipt to stdout.

## Inspect an export

```sh
python3 scripts/asset_check.py inspect \
  --input "/path/to/export/scene.glb" \
  --max-triangles 250000 \
  --max-bytes 12000000 \
  --report "/path/to/review/scene-report.html" \
  --receipt "/path/to/review/scene-receipt.json"
```

Supply only limits you chose for the destination. The triangle limit compares against the selected default scene after counting reachable mesh nodes and `EXT_mesh_gpu_instancing` multiplicity. The byte limit compares the source file plus each unique local external resource file. Without a limit the result says `not_set`; when a measure cannot be established it says `unknown`. An over-budget inspection completes with `status: needs_attention` and exit code 0. Malformed input or an output error returns an error envelope and exit code 1; invalid command arguments exit 2.

The receipt reports mesh and primitive counts, declared accessor counts, material texture slots, required extensions, referenced image/buffer sizes, missing resources, and each primitive's drawable topology. `TRIANGLE_STRIP` and `TRIANGLE_FAN` candidates are counted separately from `TRIANGLES`; points and line modes are reported as points or line segments. Node matrices and TRS change placement or scale, not vertex cardinality. Reused meshes and GPU instances multiply scene totals.

Geometry measures come from range-checked accessor metadata; the tool checks index/accessor references and byte spans but does not scan every vertex or index value. Triangle counts are therefore candidate primitive counts and may include degenerate faces. The report is a static overview, not a mesh viewer or a universal performance score. `KHR_draco_mesh_compression` and `KHR_meshopt_compression` data is not decoded; affected geometry stays unknown when a decoder would be needed.

External relative URIs are checked only within the selected file's directory. Paths that resolve outside that directory, including symlinks, are reported without opening them. HTTP and other remote URIs are reported without fetching. Data URIs are counted from the selected glTF JSON. JSON metadata is limited to 64 MiB; larger files and versions other than glTF 2.0 are not supported.

## Verify the command

From the suite repository root:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_asset_check*.py' -v
```
