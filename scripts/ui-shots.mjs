// Screenshot the page manifest with Playwright, for visual before/after diffs.
// Invoked by scripts/ui-shots.sh — not meant to be run directly (needs a live
// BASE_URL and playwright installed under UI_SHOTS_NPM_DIR).
//
// Env in:  BASE_URL        e.g. http://127.0.0.1:8499   (required)
//          OUT_DIR         screenshot destination        (required)
//          ADMIN_PASSWORD  admin login password          (default "pw")
//          GALLERY_SLUG    optional — also shoot /g/<slug> (PIN gate when set)
// Env out: writes <name>-desktop.png / <name>-mobile.png + _manifest.json.

import { createRequire } from "node:module";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

// Bare "playwright" resolves from the isolated npm dir (ESM resolution is
// relative to this file, not the cwd — so createRequire points there).
const require = createRequire(join(process.env.UI_SHOTS_NPM_DIR || "/tmp/ui-shots-npm", "/"));
const { chromium } = require("playwright");

const BASE = (process.env.BASE_URL || "").replace(/\/$/, "");
const OUT = process.env.OUT_DIR || "";
const PASSWORD = process.env.ADMIN_PASSWORD || "pw";
const GALLERY_SLUG = process.env.GALLERY_SLUG || "";
if (!BASE || !OUT) {
  console.error("BASE_URL and OUT_DIR are required");
  process.exit(2);
}

// Keep in sync with templates/admin/_nav.html hrefs + app/public/site.py.
const PUBLIC_PAGES = [
  ["home", "/"],
  ["portfolio", "/portfolio"],
  ["real-estate", "/real-estate"],
  ["portraits", "/portraits"],
  ["food-beverage", "/food-beverage"],
  ["services", "/services"],
  ["about", "/about"],
  ["contact", "/contact"],
  ["book", "/book"],
  ["reels", "/reels"],
  ["press", "/press"],
  ["admin-login", "/admin/login"],
];
const ADMIN_PAGES = [
  ["admin-home", "/admin/home"],
  ["admin-studio", "/admin/studio"],
  ["admin-financials", "/admin/financials"],
  ["admin-inbox", "/admin/inbox"],
  ["admin-scheduling", "/admin/scheduling"],
  ["admin-settings", "/admin/settings"],
  ["admin-galleries", "/admin/galleries"],
];
if (GALLERY_SLUG) PUBLIC_PAGES.push(["gallery", `/g/${GALLERY_SLUG}`]);

const PROFILES = [
  ["desktop", { viewport: { width: 1440, height: 900 } }],
  ["mobile", { viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2 }],
];

mkdirSync(OUT, { recursive: true });
const results = [];

// Trigger lazy-loaded media before the full-page capture stitches.
async function settle(page) {
  await page.waitForLoadState("networkidle", { timeout: 6000 }).catch(() => {});
  await page.evaluate(async () => {
    await new Promise((done) => {
      let y = 0;
      const step = () => {
        y += 600;
        window.scrollTo(0, y);
        if (y < document.body.scrollHeight) setTimeout(step, 60);
        else {
          window.scrollTo(0, 0);
          done();
        }
      };
      step();
    });
  });
  await page.waitForTimeout(1500);
}

async function shoot(context, name, path, profile) {
  const page = await context.newPage();
  let status = 0;
  try {
    const resp = await page.goto(BASE + path, { waitUntil: "load", timeout: 30000 });
    status = resp ? resp.status() : 0;
    await settle(page);
    const file = `${name}-${profile}.png`;
    await page.screenshot({ path: join(OUT, file), fullPage: true });
    results.push({ name, path, profile, status, file });
    console.log(`  ${status} ${name}-${profile}.png  (${path})`);
  } catch (err) {
    results.push({ name, path, profile, status, error: String(err).split("\n")[0] });
    console.log(`  ERR ${name}-${profile}  (${path}): ${String(err).split("\n")[0]}`);
  } finally {
    await page.close();
  }
}

const browser = await chromium.launch();
try {
  // Admin login once via the request API (POST /admin/login, form field
  // "password" — see app/admin/auth.py), then reuse the storage state.
  const boot = await browser.newContext();
  const login = await boot.request.post(`${BASE}/admin/login`, { form: { password: PASSWORD } });
  const probe = await boot.request.get(`${BASE}/admin/home`);
  if (!login.ok() && login.status() !== 303) {
    console.error(`admin login failed (HTTP ${login.status()})`);
    process.exit(1);
  }
  if (probe.status() !== 200) {
    console.error(`admin session not sticking (GET /admin/home -> ${probe.status()})`);
    process.exit(1);
  }
  const adminState = await boot.storageState();
  await boot.close();

  for (const [profile, opts] of PROFILES) {
    console.log(`==> ${profile} public`);
    const pub = await browser.newContext(opts);
    for (const [name, path] of PUBLIC_PAGES) await shoot(pub, name, path, profile);
    await pub.close();

    console.log(`==> ${profile} admin`);
    const adm = await browser.newContext({ ...opts, storageState: adminState });
    for (const [name, path] of ADMIN_PAGES) await shoot(adm, name, path, profile);
    await adm.close();
  }
} finally {
  await browser.close();
}

writeFileSync(join(OUT, "_manifest.json"), JSON.stringify({ base: BASE, when: new Date().toISOString(), results }, null, 2));
const bad = results.filter((r) => r.error || r.status >= 400);
console.log(`==> ${results.length} shots -> ${OUT} (${bad.length} with errors/HTTP>=400)`);
if (bad.length) console.log("    " + bad.map((r) => `${r.name}-${r.profile}:${r.status || "ERR"}`).join("  "));
