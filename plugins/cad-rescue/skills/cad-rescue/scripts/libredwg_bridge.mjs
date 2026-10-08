// SPDX-License-Identifier: GPL-3.0-only
// Isolated subprocess adapter for the separately installed GPL-3.0 LibreDWG WASM package.

import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const emit = (value) => process.stdout.write(`${JSON.stringify(value)}\n`);
const fail = (errorCode, summary, exitCode = 1) => {
  emit({ schema_version: 1, tool: "cad-rescue-libredwg-bridge", status: "error", error_code: errorCode, summary });
  process.exitCode = exitCode;
};

function parseArguments(argv) {
  const values = {};
  for (let index = 0; index < argv.length; index += 1) {
    const key = argv[index];
    if (!["--input", "--output", "--runtime-dir"].includes(key) || !argv[index + 1]) {
      throw new Error("Expected --input, --output and --runtime-dir values.");
    }
    values[key.slice(2)] = argv[index + 1];
    index += 1;
  }
  if (!values.input || !values.output || !values["runtime-dir"]) {
    throw new Error("Expected --input, --output and --runtime-dir values.");
  }
  return values;
}

async function main() {
  let args;
  try {
    args = parseArguments(process.argv.slice(2));
  } catch (error) {
    fail("invalid_arguments", error.message, 2);
    return;
  }

  const runtimeRoot = path.resolve(args["runtime-dir"]);
  const packageRoot = path.join(runtimeRoot, "node_modules", "@mlightcad", "libredwg-web");
  const packageJsonPath = path.join(packageRoot, "package.json");
  const esmEntry = path.join(packageRoot, "dist", "libredwg-web.js");
  const wasmDirectory = path.join(packageRoot, "wasm");
  if (!fs.existsSync(packageJsonPath) || !fs.existsSync(esmEntry) || !fs.existsSync(path.join(wasmDirectory, "libredwg-web.wasm"))) {
    fail("converter_runtime_missing", "The runtime directory must contain the installed LibreDWG package and WASM files.");
    return;
  }

  let packageMetadata;
  try {
    packageMetadata = JSON.parse(fs.readFileSync(packageJsonPath, "utf8"));
  } catch {
    fail("converter_runtime_invalid", "The installed LibreDWG package metadata could not be read.");
    return;
  }
  if (packageMetadata.version !== "0.7.9" || packageMetadata.license !== "GPL-3.0") {
    fail("converter_version_unsupported", "CAD Rescue requires @mlightcad/libredwg-web 0.7.9, which declares GPL-3.0.");
    return;
  }

  let inputBytes;
  try {
    inputBytes = fs.readFileSync(path.resolve(args.input));
  } catch {
    fail("input_unavailable", "The selected DWG file could not be read.");
    return;
  }
  if (inputBytes.length === 0) {
    fail("input_empty", "The selected DWG file is empty.");
    return;
  }

  try {
    const moduleUrl = pathToFileURL(esmEntry).href;
    const { LibreDwg } = await import(moduleUrl);
    const libredwg = await LibreDwg.create(wasmDirectory);
    // The library logs parser diagnostics with console.*. Keep those out of the
    // JSON protocol while preserving them on stderr for a local CLI user.
    console.log = (...items) => console.error(...items);
    console.warn = (...items) => console.error(...items);
    console.info = (...items) => console.error(...items);
    const dxf = libredwg.dwg_write_dxf(inputBytes);
    if (!dxf || dxf.length === 0) {
      fail("dxf_export_unavailable", "LibreDWG could not export this DWG to DXF with the installed WASM build.");
      return;
    }
    const output = path.resolve(args.output);
    fs.writeFileSync(output, Buffer.from(dxf), { flag: "wx" });
    emit({
      schema_version: 1,
      tool: "cad-rescue-libredwg-bridge",
      status: "ok",
      version: packageMetadata.version,
      output_bytes: dxf.length,
    });
  } catch (error) {
    fail("dxf_export_failed", `LibreDWG could not convert this DWG (${error?.name || "Error"}).`);
  }
}

await main();
