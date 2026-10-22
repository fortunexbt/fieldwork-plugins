# Licenses and provenance

The suite's original code is MIT-licensed except for CAD Rescue, which is distributed
under GPL-3.0-only. Read the license in each plugin archive before redistributing it.

- **Minecraft Performance Lab** includes selected unchanged source files from
  [Minecraft Frame Bench](https://github.com/fortunexbt/minecraft-frame-bench),
  MIT. Its original license and source revision notice are bundled beside the
  harness. Minecraft, shader packs, Java, Minescript and ASM binaries are not bundled.
- **Tablebeam** is a standalone implementation informed by the author's earlier
  Tablebeam project. Its deterministic calculation code does not require pandas or
  an external AI provider.
- **Fit to Upload** invokes a separately installed FFmpeg and FFprobe. Those tools
  and their configured codecs retain their own licenses; binaries are not bundled.
- **CAD Rescue** uses separately installed ezdxf (MIT) for DXF processing. Its optional
  DWG adapter invokes a separately installed `@mlightcad/libredwg-web` runtime,
  which declares GPL-3.0. CAD Rescue's own package is GPL-3.0-only. The suite does not
  distribute that runtime or claim ownership of upstream CAD data or libraries.

The remaining plugins use the Python standard library and, for optional live
repository operations, separately installed Git or GitHub CLI. The catalog icons
and the test fixtures are original unless an adjacent notice says otherwise.

## Demo inputs in `samples/`

The catalog site runs each tool on one of these files. None was edited except where
noted.

- `tablebeam/penguins.csv`: Palmer Penguins by Allison Horst, Alison Hill and Kristen
  Gorman, from [allisonhorst/palmerpenguins](https://github.com/allisonhorst/palmerpenguins)
  (`inst/extdata/penguins.csv`, commit `8957207`). Data collected by Kristen Gorman with
  Palmer Station LTER. CC0 1.0.
- `ci-spend-check/barter-runs.json`: the GitHub Actions API record of every workflow run
  of the public repository [fortunexbt/barter](https://github.com/fortunexbt/barter) (MIT),
  saved on 2026-10-23 with `ci_spend_check.py fetch fortunexbt/barter --limit 100 --jobs`.
  It includes the commit metadata the API returns, such as author names and emails.
- `asset-check/`: the [Fox](https://github.com/KhronosGroup/glTF-Sample-Assets/tree/main/Models/Fox)
  model from Khronos glTF-Sample-Assets (commit `15cd02d`). Model by PixelMannen, CC0 1.0;
  rigging and animation by tomkranis, CC BY 4.0; glTF conversion by AsoboStudio and
  scurest, CC BY 4.0. The upstream `LICENSE.md` is beside it.
- `cad-rescue/usa.dxf`: from the [ezdxf](https://github.com/mozman/ezdxf) examples
  (`examples/addons/drawing/data/usa.dxf`, commit `7b20261`), MIT, copyright Manfred
  Moitzi. The upstream `LICENSE` is beside it.
- `shipproof/pyodide.mjs`: from the npm package `pyodide@314.0.7`, MPL-2.0. The source
  form is at [pyodide/pyodide tag 314.0.7](https://github.com/pyodide/pyodide/tree/314.0.7).
- `fit-to-upload/clip.mp4`: five seconds (2:00 to 2:05) of
  [Big Buck Bunny](https://peach.blender.org/), (c) 2008 Blender Foundation, CC BY 3.0.
  Cut from `BigBuckBunny_320x180.mp4` with stream copy by `scripts/make_samples.py`; the
  frames are not re-encoded. `recorded-binaries.json` is real `ffprobe` and `ffmpeg` output
  for that clip.
- `minecraft-capture/`: the twelve files of capture session `mmc-r12-112954` from the
  author's [minecraft-apple-silicon-framework](https://github.com/fortunexbt/minecraft-apple-silicon-framework)
  (MIT), recorded on 2026-10-06 with Minecraft 26.3.

The catalog site bundles two typefaces under the SIL Open Font License 1.1:
[DM Sans](https://fonts.google.com/specimen/DM+Sans) by Colophon Foundry, Jonny Pinhorn
and Indian Type Foundry, and [IBM Plex Mono](https://github.com/IBM/plex) by IBM. The
Latin subsets in `site/fonts/` came from Google Fonts unchanged. To run a tool in the
page the site loads [Pyodide](https://pyodide.org) (MPL-2.0) from jsDelivr at runtime.
