# Third-party notices

The files in this directory are copied from **Minecraft Frame Bench**, Copyright (c) 2026 fortunexbt, under the MIT License. Source: <https://github.com/fortunexbt/minecraft-frame-bench>, commit `2e2f21b03bf1a630c5b103d55555064285763a29`.

Copied harness files: `build.py`, `src/FrameAgent.java`, `src/FrameSink.java`, `src/controller.swift`, `scripts/capture.py`, `minescript/m4move.py`, `minescript/m4policy.pyj`, `minescript/m4pan.py`, and `tests/PolicyTest.java`. The full license is included as `LICENSE`.

The upstream `summarize.py` is not included. `scripts/frame_lab.py` provides the suite's read-only, validated analysis and report export.

ASM 9.10.1 is a separate BSD-licensed build dependency. It is not bundled. The harness builder checks the expected JAR hash and references the user's existing classpath copy. Minecraft, Java, Prism Launcher, Minescript, mods, shaders, worlds, and game data are not distributed.
