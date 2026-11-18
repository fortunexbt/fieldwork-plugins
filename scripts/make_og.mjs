// Renders site/og.png from the site's own stylesheet and fonts.
// Needs Playwright: npx playwright@1.64.1 install chromium, then node scripts/make_og.mjs
import { chromium } from 'playwright';
import { readFileSync, writeFileSync, unlinkSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const site = new URL('../site/', import.meta.url);
const scratch = new URL('og-source.html', site);
const catalog = JSON.parse(readFileSync(new URL('../catalog.json', import.meta.url), 'utf8'));

writeFileSync(scratch, `<!doctype html><html lang="en"><head><meta charset="utf-8">
<link rel="stylesheet" href="style.css">
<style>
  body { width: 1200px; height: 630px; display: grid; grid-template-columns: 1fr 560px; gap: 56px; padding: 60px 64px 0; overflow: hidden; }
  .mark { display: flex; align-items: center; gap: 12px; font-family: var(--narrow); font-size: 30px; font-weight: 600; }
  .mark svg { width: 28px; height: 28px; fill: none; stroke: var(--ink); stroke-width: 3.4; }
  h1 { margin-top: 64px; font-family: var(--narrow); font-size: 64px; line-height: 1.02; letter-spacing: -0.01em; }
  .sub { margin-top: 22px; font-size: 24px; line-height: 1.35; color: var(--quiet); max-width: 26ch; }
  .tape { margin: 0 14px; padding: 30px 30px 40px; }
  .tape code { display: block; font-family: var(--mono); font-size: 15px; line-height: 1.5; padding: 10px 12px; background: var(--paper-wash); }
  .tape h2 { font-family: var(--narrow); font-size: 34px; margin-bottom: 18px; }
  .readout { margin-top: 18px; grid-template-columns: 1fr; }
  .readout dt { font-size: 19px; }
  .readout dd { font-size: 18px; }
</style></head><body>
<div><div class="mark"><svg viewBox="0 0 24 24"><path d="M3 4h18M3 12h12M3 20h6"/></svg>Fieldwork</div>
<h1>Eight small tools for ChatGPT and Codex</h1>
<p class="sub">Each one is a single Python file, and the site runs it in your browser on real data.</p></div>
<div class="printer"><div class="slot"></div><div class="tape"><h2>Tablebeam</h2>
<code>$ python tablebeam.py query penguins.csv --spec penguins-query.json</code>
<dl class="readout"><div><dt>Matched rows</dt><dd>342</dd></div><div><dt>Groups</dt><dd>3</dd></div><div><dt>Adelie, mean body mass</dt><dd>558800/151 g</dd></div><div><dt>Gentoo, mean body mass</dt><dd>624350/123 g</dd></div></dl></div></div>
</body></html>`);

const browser = await chromium.launch();
try {
  const page = await browser.newPage({ viewport: { width: 1200, height: 630 }, deviceScaleFactor: 1 });
  await page.goto(scratch.href);
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: fileURLToPath(new URL('og.png', site)) });
  console.log(`og.png written for ${catalog.plugins.length} plugins`);
} finally {
  await browser.close();
  unlinkSync(scratch);
}
