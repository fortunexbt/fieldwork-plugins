// Runs the plugin command-line tools in Pyodide, off the main thread.
import { loadPyodide } from 'https://cdn.jsdelivr.net/pyodide/v314.0.7/full/pyodide.mjs';

const PYODIDE = 'https://cdn.jsdelivr.net/pyodide/v314.0.7/full/';
const BASE = new URL('../', self.location.href);

let pyodide;
let booting;
let runCounter = 0;
const installed = new Set();

function post(id, type, payload) {
  self.postMessage({ id, type, ...payload });
}

async function boot(id) {
  if (pyodide) return pyodide;
  if (!booting) {
    booting = (async () => {
      post(id, 'progress', { stage: 'python', detail: 'Downloading CPython 3.14 for WebAssembly' });
      const runtime = await loadPyodide({ indexURL: PYODIDE });
      const harness = await (await fetch(new URL('run/harness.py', BASE))).text();
      runtime.runPython(harness);
      runtime.globals.get('install_link_fallback')();
      runtime.FS.mkdirTree('/work');
      pyodide = runtime;
      return runtime;
    })();
  }
  return booting;
}

async function bytesOf(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`Could not load ${url} (${response.status})`);
  return new Uint8Array(await response.arrayBuffer());
}

async function installTool(id) {
  if (installed.has(id)) return;
  const source = await (await fetch(new URL(`cli/${id}.py`, BASE))).text();
  pyodide.globals.get('install')(id, source);
  installed.add(id);
}

async function execute(id, plugin, demo) {
  const runtime = await boot(id);
  const requires = demo.requires || [];
  if (requires.length) {
    post(id, 'progress', { stage: 'package', detail: `Installing ${requires.join(', ')}` });
    await runtime.loadPackage('micropip');
    const micropip = runtime.pyimport('micropip');
    await micropip.install(runtime.toPy(requires));
  }

  post(id, 'progress', { stage: 'inputs', detail: 'Writing the sample inputs' });
  const directory = `/work/run${++runCounter}`;
  runtime.FS.mkdirTree(directory);
  runtime.FS.chdir(directory);
  const writeBytes = runtime.globals.get('write_bytes');
  for (const input of demo.inputs || []) {
    writeBytes(input.path, await bytesOf(new URL(`samples/${plugin}/${input.path}`, BASE)));
  }

  let recordings = '{}';
  if (demo.recorded_binaries) {
    recordings = await (await fetch(new URL(`samples/${plugin}/recorded-binaries.json`, BASE))).text();
  }
  runtime.globals.get('prepare')(recordings, demo.sandbox === 'network');
  await installTool(plugin);

  post(id, 'progress', { stage: 'running', detail: 'Running the tool' });
  let index = 0;
  for (const step of demo.steps) {
    const outcome = JSON.parse(runtime.globals.get('run')(plugin, JSON.stringify(step.argv)));
    let payload = null;
    try { payload = JSON.parse(outcome.stdout); } catch { payload = null; }
    const files = {};
    for (const item of [...(step.downloads || []), ...(step.image ? [step.image] : [])]) {
      try {
        files[item.path] = runtime.FS.readFile(item.path);
      } catch {
        files[item.path] = null;
      }
    }
    post(id, 'step', { index, argv: step.argv, outcome, payload, files });
    index += 1;
  }
  post(id, 'done', {});
}

// Runs share one interpreter, so they are handled strictly one at a time.
let queue = Promise.resolve();

async function handle({ id, type, plugin, demo }) {
  try {
    if (type === 'boot') {
      await boot(id);
      post(id, 'ready', {});
    } else if (type === 'run') {
      await execute(id, plugin, demo);
    }
  } catch (error) {
    post(id, 'error', { message: String(error && error.message ? error.message : error) });
  }
}

self.onmessage = (event) => {
  queue = queue.then(() => handle(event.data));
};
