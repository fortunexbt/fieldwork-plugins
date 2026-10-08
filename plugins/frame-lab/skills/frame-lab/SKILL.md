---
name: frame-lab
description: Inspect Minecraft Frame Bench captures, reject incomplete runs, compare compatible profiles, diagnose an explicit Prism setup, and export a portable report.
---

# Minecraft Performance Lab

Use `scripts/frame_lab.py` with Python 3.11 or later. It uses only the standard library. Analysis reads the selected evidence directory and never launches Minecraft or changes capture files.

## Support

Analysis and JSON or HTML report export use the public harness format: `*-frames.csv`, `*-start.txt`, `*-done.txt`, `*-route.json`, and optional `*-context.json`. The Java sampler and control route have been validated on macOS with Minecraft 26.2 or 26.3, Java 25, and Minescript 5. The setup diagnosis checks an explicitly selected Prism Launcher data directory and an explicitly selected instance `.minecraft` directory. Other game versions, launchers, and capture operating systems are not qualified by this skill.

Capture setup needs `java`, `javac`, and `jar` from a Java 25 JDK, `swiftc`, macOS `top`, and the exact external ASM 9.10.1 JAR. `doctor` probes the supplied paths and required executables. Analysis and export need no external packages.

The metric is CPU frame production, including frame limiting. It does not measure GPU execution, displayed frames, generated frames, or input latency. A passing capture records the harness's `MINIMIZED` inactive-FPS policy, no live throttle, no focus loss, no sampler error, no full buffer, and a completed route. Invalid captures remain inspectable with their measurements and rejection reasons.

## Diagnose before setup

Point the command at one Prism data root and one game directory. It does not search your home directory or inspect other instances.

```sh
python3 scripts/frame_lab.py doctor \
  --prism-dir "/path/to/PrismLauncher" \
  --game-dir "/path/to/PrismLauncher/instances/Bench/.minecraft" \
  --jdk "/path/to/jdk-25" \
  --asm "/path/from/the/active/game/classpath/asm-9.10.1.jar"
```

The diagnosis reads paths and asks the selected `java` executable for its version. It does not attach to or start the client. ASM must be the existing 9.10.1 JAR used by that game classpath; the bundled builder checks its published SHA-256 and does not copy it into the agent.

## Capture on a disposable instance

The capture harness is supplied under `assets/harness/`. Build it on the Mac that will run the game:

```sh
cd assets/harness
python3 build.py --jdk "/path/to/jdk-25" --asm "/path/to/asm-9.10.1.jar"
```

Use a disposable Prism instance and a stopped-world snapshot. Preserve the source world. Add the generated `frame-agent.jar` JVM argument only to that instance, with the agent output set to the same `minescript/bench-output` directory used by the route scripts. Copy only `m4move.py`, `m4policy.pyj`, and `m4pan.py` into that instance's `.minecraft/minescript/`; compare existing files before copying and keep a backup. Do not replace an existing daily instance or active save.

Set Minecraft's inactive-FPS policy to **Minimized**, restart, load the saved test scene, and warm the world and shader. The optional capture driver checks the foreground client and CPU-idle/swap preflight, then types a route into Minecraft. Run it only when you intend to start a capture:

```sh
python3 scripts/capture.py run-id --game "/path/to/Bench/.minecraft" --seconds 120 --route pan
```

The controller uses existing macOS Accessibility permission and does not request or alter permissions. `capture.py` can send input to the foreground client; it is not part of diagnosis or analysis. The agent's 500,000-sample buffer is a hard limit. Keep the route, world snapshot, settings, framebuffer size, and background workload consistent. Repeat each candidate and alternate run order; one pair does not estimate run-to-run variation.

For a non-automated route, the harness supports `\m4policy`, `\m4pan RUN_ID SECONDS YAW PITCH DAY WEATHER`, and `\m4move RUN_ID SECONDS walk|sprint|fly|swim|shuttle` in Minecraft chat.

## Inspect and compare

```sh
python3 scripts/frame_lab.py inspect --evidence-dir "/path/to/bench-output" --run run-id
python3 scripts/frame_lab.py compare --evidence-dir "/path/to/bench-output" --runs baseline candidate
```

Comparison requires a `<run-id>-profile.json` beside each capture. Fill one in for each run with the effective machine, game, loader, framebuffer, scene, and graphics settings. Use aliases and labels; do not put a world seed, absolute path, account name, or other private value in a profile that you plan to share.

```json
{
  "schema_version": 1,
  "host_id": "same-machine-alias",
  "minecraft_version": "26.3",
  "loader": "Fabric 0.18",
  "java_major": 25,
  "framebuffer_px": {"width": 2560, "height": 1440},
  "scene_id": "same-stopped-world-and-route-label",
  "settings": {
    "frame_limit": 120,
    "render_distance": 16,
    "simulation_distance": 12,
    "vsync": true,
    "shader": "shader-name-and-version",
    "resource_pack": "pack-name-and-version",
    "weather": "clear",
    "time_of_day": 6000
  },
  "dataset_kind": "measured"
}
```

The profile is a comparison checklist, not a source of measurements. Runs must also use the same recorded route, starting world state, live frame limit, and requested duration. A comparison is rejected if these differ or the capture profiles are missing. If a comparison is eligible, the command shows average FPS, p95, p99, and the slowest-1% low. p95 is withheld below 1,000 intervals. p99 and the slowest-1% low are withheld below 10,000 intervals so the tail average has at least 100 samples. The slowest-1% low is `1000 / mean(milliseconds in the slowest ceil(N × 0.01) intervals)`; it is not the inverse of p99.

Export first to preview the exact destination, size, and SHA-256 without writing. Choose a new output path; existing files are never overwritten.

```sh
python3 scripts/frame_lab.py export --evidence-dir "/path/to/bench-output" \
  --runs baseline candidate --output "/path/to/reports/comparison.html" \
  --format html --dry-run
python3 scripts/frame_lab.py export --evidence-dir "/path/to/bench-output" \
  --runs baseline candidate --output "/path/to/reports/comparison.html" \
  --format html
```

The completed command returns a receipt with the report path, byte count, and SHA-256. Reports omit source-directory paths and contain no linked assets. `python3 scripts/frame_lab.py demo` exercises the same parser with temporary synthetic captures; its output is explicitly marked synthetic and never describes a real game run.

## Teardown

Stop the disposable client. Remove the agent JVM argument and only the route files you added, then launch normally and verify the instance works without instrumentation. If the native controller is still holding keys, run `assets/harness/build/controller release`. Keep evidence and the stopped-world snapshot until you have deliberately archived them; no cleanup command is included.

## Attribution

The bundled Java agent, Swift controller, Python capture driver, and Minescript routes are copied from the MIT-licensed [Minecraft Frame Bench](https://github.com/fortunexbt/minecraft-frame-bench). See `assets/harness/NOTICE.md` and `assets/harness/LICENSE`. ASM is a separately installed BSD-licensed dependency; Minecraft and Minescript are not distributed here.
