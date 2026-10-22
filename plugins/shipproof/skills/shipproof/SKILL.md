---
name: shipproof
description: Verify selected deployed assets against local release files, or prepare a metadata-only Git handoff.
---

# ShipProof

Use ShipProof when someone asks whether a specific release URL serves an expected local asset, or asks for a compact repository handoff. It uses Python's standard library. Repo Handoff also requires Git on `PATH`.

The command is `python3 scripts/shipproof.py` from this skill directory, or the equivalent path in an installed copy. It requires Python 3.11 or newer; Repo Handoff also requires Git on `PATH`. It prints one JSON envelope to stdout. A completed check exits 0; invalid input exits 2; execution errors exit 1. A completed check can have `status: needs_attention` when a resource mismatches or cannot be verified.

## Verify a release asset

For a single exact asset URL, pass both the URL and the expected local file:

```sh
python3 scripts/shipproof.py verify \
  --url 'https://cdn.example.test/assets/app.js' \
  --asset './dist/assets/app.js' \
  --marker 'release-2026-09'
```

Markers are UTF-8 byte strings that must occur in both the local file and fetched response. A matching SHA-256 is the main byte-for-byte check; expected markers and optional safe response headers provide additional checks. The request asks for an identity-encoded response, follows up to five HTTP(S) redirects, reports the status and a filtered set of response headers, and accepts at most 2 MiB by default (up to 10 MiB when `--max-bytes` is raised). A server that still applies an unsupported content encoding is reported as unverifiable. URL username/password credentials are rejected; query strings are omitted from reported URLs. Requests are read-only GETs.

Use a manifest for multiple independently mapped resources. Asset paths are relative to the manifest file. `sha256` is optional; when present it pins the local asset too. `expected_status` defaults to 200. Header expectations are limited to non-sensitive response headers.

```json
{
  "schema_version": 1,
  "resources": [
    {
      "name": "Pyodide loader",
      "url": "https://cdn.jsdelivr.net/pyodide/v314.0.7/full/pyodide.mjs",
      "asset": "pyodide/pyodide.mjs",
      "sha256": "6f1d60f7bf529beb300f0f47983c921d3982363640ba20af0e38efdddbc66109",
      "markers": ["314.0.7"],
      "headers": {"content-type": "text/javascript; charset=utf-8"}
    }
  ]
}
```

Run it with:

```sh
python3 scripts/shipproof.py verify --manifest ./shipproof-manifest.json
```

Here `asset` is `pyodide.mjs` from the npm tarball of `pyodide@314.0.7` and `sha256` is its hash. For your own release, replace it with the hash of the selected file or omit the field. A route returning HTTP 200 can still mismatch because it returned an HTML fallback, stale bytes, or the wrong headers. A missing or inaccessible response is reported as mismatch or unverifiable according to the evidence available. This deterministic check does not run a browser or establish that a visual scenario, client-side interaction, or authenticated user flow works. Record a separate browser receipt when those claims matter.

## Create a repository handoff

First preview the exact two output paths. Choose an output directory outside the checkout:

```sh
python3 scripts/shipproof.py handoff \
  --repo '../my-project' \
  --out-dir '../my-project-handoff' \
  --include 'src/**/*.py' \
  --preview
```

If the preview is right, run the same command without `--preview`. It refuses to overwrite existing `handoff.md` or `manifest.json`. The Markdown report and JSON manifest contain the revision, branch, dirty path status, recognized build/test command names, and selected tracked source-file sizes and SHA-256 hashes. They contain no source contents. Untracked files are listed only as dirty paths and are not hashed or copied. Modified tracked files are hashed from the working tree, but the handoff does not back up their contents. Sensitive filenames, generated folders, symlinks, and files past the size limits are excluded. No project build or test command is executed.

## Example requests

- “Does this CDN URL serve the exact `dist/app.js` from my build?” → use `verify --url URL --asset FILE`.
- “Compare these three production asset routes with the release manifest.” → use `verify --manifest FILE`.
- “Prepare a handoff for this checkout, with hashes for `src/` only.” → preview, then run `handoff --repo DIR --out-dir DIR --include 'src/**'`.

Result of a real run on 2026-10-23, checking that jsDelivr serves the `pyodide.mjs` published in the npm package (`verify --url https://cdn.jsdelivr.net/pyodide/v314.0.7/full/pyodide.mjs --asset pyodide.mjs --marker 314.0.7 --header 'Content-Type:text/javascript; charset=utf-8'`), with the response headers omitted:

```json
{
  "schema_version": 1,
  "tool": "shipproof",
  "status": "ok",
  "summary": "All 1 release resource(s) matched the selected local assets.",
  "data": {
    "resources": [
      {
        "name": "release asset",
        "result": "match",
        "request_url": "https://cdn.jsdelivr.net/pyodide/v314.0.7/full/pyodide.mjs",
        "final_url": "https://cdn.jsdelivr.net/pyodide/v314.0.7/full/pyodide.mjs",
        "status_code": 200,
        "redirects": [],
        "checks": [
          {"check": "http_status", "result": "match", "expected": 200, "actual": 200},
          {"check": "sha256", "result": "match", "expected": "6f1d60f7bf529beb300f0f47983c921d3982363640ba20af0e38efdddbc66109", "actual": "6f1d60f7bf529beb300f0f47983c921d3982363640ba20af0e38efdddbc66109"},
          {"check": "marker", "marker": "314.0.7", "result": "match", "expected_asset": "match", "served_response": "match"},
          {"check": "header", "name": "content-type", "result": "match", "expected": "text/javascript; charset=utf-8", "actual": "text/javascript; charset=utf-8"}
        ],
        "reasons": []
      }
    ]
  },
  "artifacts": [],
  "warnings": []
}
```
