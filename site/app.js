const list = document.querySelector('#tool-list');
const detail = document.querySelector('#tool-detail');
const about = document.querySelector('#tool-about');
const toast = document.querySelector('#toast');
const INDEX = /^(.*?)\[(\d+)\]$/;
const SANDBOX = {
  browser: 'Runs in your browser',
  replayed: 'FFmpeg output replayed',
  network: 'Fetches a real file',
};

let catalog;
let current;
let toastTimer;
const runs = new Map();
const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');

const escapeHtml = (value) => String(value).replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
const element = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast.textContent = 'Copied';
  } catch {
    toast.textContent = 'Clipboard unavailable. Select the command and copy it.';
  }
  toast.classList.add('visible');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove('visible'), 2600);
}

/* ---------- reading values out of a tool's JSON result ---------- */

function resolve(payload, path) {
  let node = payload;
  for (const part of path.split('.')) {
    const match = INDEX.exec(part);
    const key = match ? match[1] : part;
    const index = match ? Number(match[2]) : null;
    if (key) {
      if (node === null || typeof node !== 'object' || !(key in node)) return null;
      node = node[key];
    }
    if (index !== null) {
      if (!Array.isArray(node) || index >= node.length) return null;
      node = node[index];
    }
  }
  return node === undefined ? null : node;
}

function rowsOf(collection) {
  if (Array.isArray(collection)) return collection;
  if (collection && typeof collection === 'object') {
    return Object.entries(collection).map(([key, value]) => (
      value && typeof value === 'object' && !Array.isArray(value) ? { ...value, _key: key } : { _key: key, value }
    ));
  }
  return [];
}

function bytes(value) {
  if (value < 1000) return `${value} B`;
  if (value < 1e6) return `${(value / 1000).toFixed(value < 1e4 ? 1 : 0)} kB`;
  return `${(value / 1e6).toFixed(1)} MB`;
}

function present(value, spec = {}) {
  if (spec.format === 'count') return String(rowsOf(value).length);
  if (value === null || value === undefined) return '—';
  if (typeof value === 'boolean') return value ? 'yes' : 'no';
  let text;
  if (spec.format === 'bytes') text = bytes(Number(value));
  else if (spec.format === 'bitrate') text = `${Math.round(Number(value) / 1000).toLocaleString('en')} kbps`;
  else if (Array.isArray(value)) text = value.join(', ');
  else if (typeof value === 'number') text = value.toLocaleString('en', { maximumFractionDigits: 3 });
  else text = String(value);
  if (spec.truncate && text.length > spec.truncate) text = `${text.slice(0, spec.truncate)}…`;
  return `${spec.prefix || ''}${text}${spec.suffix || ''}`;
}

/* ---------- the Pyodide worker ---------- */

let worker;
const pending = new Map();

function runner() {
  if (worker) return worker;
  worker = new Worker('run/worker.js', { type: 'module' });
  worker.onmessage = (event) => {
    const handler = pending.get(event.data.id);
    if (handler) handler(event.data);
  };
  worker.postMessage({ id: 'boot', type: 'boot' });
  pending.set('boot', () => {});
  return worker;
}

function startRun(plugin) {
  const previous = runs.get(plugin.id);
  const state = { status: 'running', stage: 'Starting Python', steps: [], started: performance.now(), stale: previous ? previous.steps : [] };
  runs.set(plugin.id, state);
  const id = `${plugin.id}:${Date.now()}`;
  pending.set(id, (message) => {
    if (message.type === 'progress') state.stage = message.detail;
    if (message.type === 'step') state.steps.push(message);
    if (message.type === 'done') {
      state.status = 'done';
      state.elapsed = Math.round(performance.now() - state.started);
      pending.delete(id);
    }
    if (message.type === 'error') {
      state.status = 'failed';
      state.error = message.message;
      pending.delete(id);
    }
    if (current && current.id === plugin.id) paintRun(plugin, state);
  });
  const send = () => runner().postMessage({ id, type: 'run', plugin: plugin.id, demo: plugin.demo });
  if (document.readyState === 'complete') send();
  else addEventListener('load', send, { once: true });
  return state;
}

/* ---------- rendering one run ---------- */

function commandLine(plugin, argv) {
  return `python ${plugin.entrypoint} ${argv.map((value) => (/[\s"]/.test(value) ? JSON.stringify(value) : value)).join(' ')}`;
}

function readout(step, payload) {
  const list = element('dl', 'readout');
  for (const stat of step.stats || []) {
    const row = element('div');
    row.append(element('dt', null, stat.label), element('dd', null, present(resolve(payload, stat.path), stat)));
    list.append(row);
  }
  return list;
}

function evidenceTable(step, payload) {
  const rows = rowsOf(resolve(payload, step.table.path));
  if (!rows.length) return null;
  const wrapper = element('div', 'table-scroll');
  const table = element('table', 'evidence');
  const head = element('tr');
  for (const column of step.table.columns) head.append(element('th', null, column.label));
  const thead = element('thead');
  thead.append(head);
  const body = element('tbody');
  table.append(thead);
  for (const row of rows) {
    const line = element('tr');
    for (const column of step.table.columns) line.append(element('td', null, present(resolve(row, column.path), column)));
    body.append(line);
  }
  table.append(body);
  wrapper.append(table);
  return wrapper;
}

function fileLink(file, item) {
  const url = URL.createObjectURL(new Blob([file], { type: item.type || 'application/octet-stream' }));
  const link = element('a', 'file-link', item.label);
  link.href = url;
  link.download = item.path;
  if (item.type === 'text/html' || item.type === 'image/svg+xml') {
    link.removeAttribute('download');
    link.target = '_blank';
    link.rel = 'noopener';
  }
  return link;
}

function stepBlock(plugin, message) {
  const step = plugin.demo.steps[message.index];
  const block = element('section', 'step');
  const head = element('div', 'step-head');
  head.append(element('p', 'step-title', step.title));
  head.append(element('code', 'step-command', commandLine(plugin, message.argv)));
  block.append(head);

  const payload = message.payload;
  const status = payload ? payload.status : 'error';
  const meta = element('p', `step-meta is-${status.replace('_', '-')}`);
  meta.append(element('span', 'pill', status.replace('_', ' ')));
  meta.append(element('span', null, `exit ${message.outcome.exit}`), element('span', null, `${message.outcome.ms} ms`));
  block.append(meta);

  if (payload) {
    block.append(element('p', 'step-summary', payload.summary));
    if (step.stats) block.append(readout(step, payload));
    if (step.table) {
      const table = evidenceTable(step, payload);
      if (table) block.append(table);
    }
    if (step.image && message.files[step.image.path]) {
      const figure = element('figure', 'step-figure');
      const image = element('img');
      image.src = URL.createObjectURL(new Blob([message.files[step.image.path]], { type: 'image/svg+xml' }));
      image.alt = step.image.caption;
      image.loading = 'lazy';
      figure.append(image, element('figcaption', null, step.image.caption));
      block.append(figure);
    }
    const files = (step.downloads || []).filter((item) => message.files[item.path]);
    if (files.length) {
      const row = element('div', 'file-row');
      for (const item of files) row.append(fileLink(message.files[item.path], item));
      block.append(row);
    }
    const raw = element('details', 'raw');
    raw.append(element('summary', null, 'The tool’s own JSON output'));
    raw.append(element('pre', null, JSON.stringify(payload, null, 2)));
    block.append(raw);
  } else {
    block.append(element('pre', 'step-stderr', message.outcome.stderr || message.outcome.stdout || 'No output'));
  }
  return block;
}

function paintRun(plugin, state) {
  const output = detail.querySelector('#console');
  const status = detail.querySelector('#run-status');
  const button = detail.querySelector('#run-button');
  if (!output) return;
  button.disabled = state.status === 'running';
  button.textContent = state.status === 'running' ? 'Running' : 'Run again';
  if (state.status === 'running') status.textContent = `${state.stage}…`;
  else if (state.status === 'failed') status.textContent = `The run failed: ${state.error}`;
  else status.textContent = `Finished in ${state.elapsed} ms, CPython 3.14 compiled to WebAssembly.`;
  status.className = `run-status is-${state.status}`;
  // A repeat run keeps the last printout on the tape, greyed, until the new one lands.
  const stale = state.status === 'running' && !state.steps.length;
  output.classList.toggle('is-stale', stale);
  output.replaceChildren(...(stale ? state.stale : state.steps).map((message) => stepBlock(plugin, message)));
  if (state.status === 'done' && !state.printed) {
    state.printed = true;
    if (!reducedMotion.matches) {
      output.classList.remove('printing');
      void output.offsetWidth;
      output.classList.add('printing');
    }
  }
}

/* ---------- rendering one plugin ---------- */

function commandBlock(command) {
  const block = element('div', 'command');
  block.append(element('code', null, command));
  const copy = element('button', null, 'Copy');
  copy.setAttribute('aria-label', `Copy: ${command}`);
  copy.onclick = () => copyText(command);
  block.append(copy);
  return block;
}

function selectTool(id) {
  const item = catalog.plugins.find((plugin) => plugin.id === id) || catalog.plugins[0];
  current = item;
  document.documentElement.style.setProperty('--accent', item.color);
  for (const button of list.querySelectorAll('button')) {
    const active = button.dataset.id === item.id;
    button.classList.toggle('active', active);
    button.setAttribute('aria-selected', active ? 'true' : 'false');
    button.tabIndex = active ? 0 : -1;
    if (active) detail.setAttribute('aria-labelledby', button.id);
    if (active && list.scrollWidth > list.clientWidth) {
      list.scrollTo({ left: button.offsetLeft - (list.clientWidth - button.clientWidth) / 2, behavior: 'smooth' });
    }
  }

  const marketplace = `codex plugin marketplace add fortunexbt/fieldwork-plugins`;
  detail.innerHTML = `
    <header class="tape-head">
      <img src="icons/${escapeHtml(item.id)}.svg" alt="" width="34" height="34">
      <div>
        <h2>${escapeHtml(item.name)}</h2>
        <p class="tape-kind">${escapeHtml(item.category)}<span class="sandbox-badge">${escapeHtml(SANDBOX[item.demo.sandbox])}</span></p>
      </div>
      <button class="run-button" id="run-button">Run it</button>
    </header>
    <p class="runner-headline">${escapeHtml(item.demo.headline)}</p>
    <p class="run-status" id="run-status" role="status">Ready when you are.</p>
    <div id="console"></div>
    <footer class="tape-foot">
      <p class="input-credit"><strong>Input</strong> ${escapeHtml(item.demo.input.about)} ${escapeHtml(item.demo.input.credit)} <a href="${escapeHtml(item.demo.input.url)}">Source</a></p>
      ${item.demo.sandbox_note ? `<p class="sandbox-note"><strong>In the browser</strong> ${escapeHtml(item.demo.sandbox_note)}</p>` : ''}
    </footer>`;
  about.innerHTML = `
    <div class="about-text">
      <h2>${escapeHtml(item.name)}</h2>
      <p>${escapeHtml(item.description)}</p>
      <p class="about-runtime">Runtime: ${escapeHtml(item.runtime)}</p>
      <a class="file-link" href="${escapeHtml(catalog.repository)}/blob/main/plugins/${escapeHtml(item.id)}/skills/${escapeHtml(item.id)}/SKILL.md">Read the skill</a>
    </div>
    <div class="install">
      <h3>Install it in Codex</h3>
      <div id="install-commands"></div>
      <p class="install-note">Start a new chat after installing. In the desktop app the collection appears under Plugins.</p>
      <div class="prompt-row">
        <p><strong>Then ask for it</strong>${escapeHtml(item.prompts[0])}</p>
        <button id="copy-prompt">Copy prompt</button>
      </div>
    </div>`;
  about.querySelector('#install-commands').append(commandBlock(marketplace), commandBlock(`codex plugin add ${item.id}@${catalog.name}`));
  about.querySelector('#copy-prompt').onclick = () => copyText(item.prompts[0]);
  detail.querySelector('#run-button').onclick = () => paintRun(item, startRun(item));
  if (!reducedMotion.matches) {
    detail.classList.remove('feeding');
    void detail.offsetWidth;
    detail.classList.add('feeding');
  }

  const state = runs.get(item.id);
  paintRun(item, state || startRun(item));
}

function navigate(offset) {
  const index = catalog.plugins.findIndex((plugin) => plugin.id === current.id);
  const next = catalog.plugins[(index + offset + catalog.plugins.length) % catalog.plugins.length];
  location.hash = next.id;
  requestAnimationFrame(() => list.querySelector(`[data-id="${next.id}"]`).focus());
}

window.addEventListener('hashchange', () => catalog && selectTool(location.hash.slice(1)));

// The catalog is inlined by scripts/build_site.py so the first plugin renders
// before the first paint, with no fetch and no layout shift.
catalog = JSON.parse(document.querySelector('#catalog').textContent);
document.querySelector('#version').textContent = `Version ${catalog.version}`;
for (const item of catalog.plugins) {
  const button = element('button', 'tool-button');
  button.dataset.id = item.id;
  button.id = `tab-${item.id}`;
  button.setAttribute('role', 'tab');
  button.setAttribute('aria-controls', 'tool-detail');
  button.style.setProperty('--led', item.color);
  button.innerHTML = `<img src="icons/${escapeHtml(item.id)}.svg" alt="" width="26" height="26"><i class="led" aria-hidden="true"></i><span>${escapeHtml(item.name)}<small>${escapeHtml(item.short)}</small></span>`;
  button.onclick = () => {
    if (location.hash.slice(1) === item.id) selectTool(item.id);
    else location.hash = item.id;
  };
  button.onkeydown = (event) => {
    const offset = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[event.key];
    if (offset) {
      event.preventDefault();
      navigate(offset);
    }
  };
  list.append(button);
}

selectTool(location.hash.slice(1));
