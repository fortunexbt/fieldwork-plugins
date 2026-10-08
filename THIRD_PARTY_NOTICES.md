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
and synthetic fixtures are original unless an adjacent notice says otherwise.
