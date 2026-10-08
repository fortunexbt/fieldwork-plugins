const list = document.querySelector('#tool-list');
const detail = document.querySelector('#tool-detail');
const dialog = document.querySelector('#install-dialog');
const toast = document.querySelector('#toast');
let catalog;
let selection = 0;
let toastTimer;
const escapeHtml = (value) => String(value).replace(/[&<>"']/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast.textContent = 'Copied';
  } catch {
    toast.textContent = 'Clipboard unavailable. Select and copy the command.';
  }
  toast.classList.add('visible');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove('visible'), 2800);
}

function downloadUrl(item) {
  return `${catalog.repository}/releases/download/v${catalog.version}/${item.id}-${catalog.version}.zip`;
}

function install(item) {
  document.querySelector('#install-title').textContent = `Install ${item.name}`;
  const command = `codex plugin add ${item.id}@${catalog.name}`;
  document.querySelector('#install-command').textContent = command;
  document.querySelector('#copy-install').onclick = () => copyText(command);
  document.querySelector('#download-dialog').href = downloadUrl(item);
  dialog.showModal();
}

async function selectTool(id) {
  const item = catalog.plugins.find((plugin) => plugin.id === id) || catalog.plugins[0];
  const token = ++selection;
  document.documentElement.style.setProperty('--accent', item.color);
  for (const button of list.querySelectorAll('button')) {
    const active = button.dataset.id === item.id;
    button.classList.toggle('active', active);
    button.setAttribute('aria-current', active ? 'true' : 'false');
    if (active && list.scrollWidth > list.clientWidth) {
      list.scrollLeft = button.offsetLeft - (list.clientWidth - button.clientWidth) / 2;
    }
  }
  detail.innerHTML = `<div class="detail-top"><div class="detail-kicker"><img src="icons/${escapeHtml(item.id)}.svg" alt="">${escapeHtml(item.category)}</div><span class="package-version">v${escapeHtml(catalog.version)}</span></div>
    <h2>${escapeHtml(item.name)}</h2><p class="detail-summary">${escapeHtml(item.description)}</p>
    <div class="actions"><button class="primary-button" id="install-tool">Install plugin <span aria-hidden="true">↗</span></button><a class="secondary-button" href="${escapeHtml(downloadUrl(item))}">Download ZIP <span aria-hidden="true">↓</span></a><a class="secondary-button" href="${escapeHtml(catalog.repository)}/blob/main/plugins/${escapeHtml(item.id)}/skills/${escapeHtml(item.id)}/SKILL.md">Read the skill</a></div>
    <div class="runtime"><strong>Runs with</strong><span>${escapeHtml(item.runtime)}</span></div>
    <div class="example"><div class="example-bar"><span>EXAMPLE RESULT</span><span>GENERATED FROM SYNTHETIC INPUT</span></div><div class="example-content" id="example-content"><p class="example-placeholder">Opening the saved example…</p></div></div>
    <div class="prompt-row"><p><strong>TRY THIS PROMPT</strong>${escapeHtml(item.prompts[0])}</p><button id="copy-prompt">Copy prompt</button></div><button class="share-tool" id="share-tool">Copy link to this tool ↗</button>`;
  detail.querySelector('#install-tool').onclick = () => install(item);
  detail.querySelector('#copy-prompt').onclick = () => copyText(item.prompts[0]);
  detail.querySelector('#share-tool').onclick = () => copyText(`${catalog.website}/#${item.id}`);
  try {
    const response = await fetch(`examples/${item.id}.json`);
    if (!response.ok) throw new Error(`Example unavailable (${response.status})`);
    const example = await response.json();
    if (token !== selection) return;
    const target = detail.querySelector('#example-content');
    target.innerHTML = `<h3 class="example-title">${escapeHtml(example.title)}</h3><p class="example-note">${escapeHtml(example.summary)}</p>`;
    if (example.preview_image) {
      const preview = document.createElement('img');
      preview.className = 'sample-preview';
      preview.src = example.preview_image.url;
      preview.alt = example.preview_image.label;
      target.append(preview);
    }
    if (example.stats?.length) {
      const grid = document.createElement('div');
      grid.className = 'result-grid';
      grid.innerHTML = example.stats.map((stat) => `<div class="result-stat"><span>${escapeHtml(stat.label)}</span><strong>${escapeHtml(stat.value)}</strong></div>`).join('');
      target.append(grid);
    }
    if (example.columns?.length && example.rows?.length) {
      const table = document.createElement('table');
      table.className = 'evidence-table';
      table.innerHTML = `<thead><tr>${example.columns.map((column) => `<th>${escapeHtml(column)}</th>`).join('')}</tr></thead><tbody>${example.rows.map((row) => `<tr>${row.map((cell) => `<td>${escapeHtml(cell)}</td>`).join('')}</tr>`).join('')}</tbody>`;
      target.append(table);
    }
    if (example.artifacts?.length) {
      const artifacts = document.createElement('div');
      artifacts.className = 'sample-artifacts';
      for (const artifact of example.artifacts) {
        const link = document.createElement('a');
        link.href = artifact.url;
        link.textContent = artifact.label + ' ↗';
        artifacts.append(link);
      }
      target.append(artifacts);
    }
    const evidence = document.createElement('details');
    evidence.className = 'raw-evidence';
    const summary = document.createElement('summary');
    summary.textContent = 'Inspect the underlying result';
    const raw = document.createElement('pre');
    raw.textContent = JSON.stringify(example.receipt, null, 2);
    evidence.append(summary, raw);
    target.append(evidence);
  } catch (error) {
    if (token === selection) detail.querySelector('#example-content').textContent = `${error.message}. The source repository includes the sample and command to reproduce it.`;
  }
}

document.querySelector('.dialog-close').onclick = () => dialog.close();
dialog.addEventListener('click', (event) => { if (event.target === dialog) dialog.close(); });
for (const button of document.querySelectorAll('[data-copy]')) button.onclick = () => copyText(button.dataset.copy);
window.addEventListener('hashchange', () => catalog && selectTool(location.hash.slice(1)));

fetch('catalog.json').then((response) => {
  if (!response.ok) throw new Error('The collection could not be loaded.');
  return response.json();
}).then((data) => {
  catalog = data;
  document.querySelector('#version').textContent = `${catalog.plugins.length} plugins · v${catalog.version}`;
  for (const item of catalog.plugins) {
    const button = document.createElement('button');
    button.className = 'tool-button';
    button.dataset.id = item.id;
    button.innerHTML = `<img src="icons/${escapeHtml(item.id)}.svg" alt=""><span>${escapeHtml(item.name)}<small>${escapeHtml(item.category)}</small></span>`;
    button.onclick = () => {
      if (location.hash.slice(1) === item.id) selectTool(item.id);
      else location.hash = item.id;
    };
    list.append(button);
  }
  return selectTool(location.hash.slice(1));
}).catch((error) => {
  detail.textContent = `${error.message} You can still browse and install the plugins from the source repository.`;
});
