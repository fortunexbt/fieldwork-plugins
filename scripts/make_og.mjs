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
  body { width: 1200px; height: 630px; display: flex; flex-direction: column; justify-content: space-between; padding: 64px 72px; }
  .mark { display: flex; align-items: center; gap: 14px; font-size: 30px; font-weight: 700; letter-spacing: -0.03em; }
  .mark svg { width: 30px; height: 30px; fill: none; stroke: var(--accent); stroke-width: 3; }
  h1 { font-size: 86px; line-height: .98; letter-spacing: -0.04em; max-width: 17ch; }
  .sub { margin-top: 22px; font-size: 24px; color: var(--quiet); max-width: 44ch; }
  .strip { display: flex; align-items: center; justify-content: space-between; gap: 28px;
           background: var(--panel); color: var(--panel-ink); border-radius: 14px; padding: 20px 26px; }
  .strip code { font-family: var(--mono); font-size: 19px; color: var(--accent-lit); }
  .strip span { font-family: var(--mono); font-size: 16px; color: var(--panel-quiet); white-space: nowrap; }
</style></head><body>
<div class="mark"><svg viewBox="0 0 24 24"><path d="M3 4h18M3 12h12M3 20h6"/></svg>Fieldwork</div>
<div><h1>Eight small tools. Run the real code here.</h1>
<p class="sub">Plugins for ChatGPT and Codex. Each one is a single Python file, and the site runs it in your browser.</p></div>
<div class="strip"><code>$ python tablebeam.py query penguins.csv --spec penguins-query.json</code><span>ok &middot; 3 groups, 342 rows</span></div>
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
