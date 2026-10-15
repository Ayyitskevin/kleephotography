import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";

/* D-8 motion layer: the marketing script never listens to scroll, the sticky
   nav state comes from an IntersectionObserver on a sentinel, and every
   animation the public sheets declare is switched off under
   prefers-reduced-motion (the skill's hard rules; FABLE-IMPROVEMENT-PLAN D-8). */

const read = (rel) => fs.readFileSync(new URL(`../../${rel}`, import.meta.url), "utf8");
const siteJs = read("static/site.js");
const sheets = { "static/screening.css": read("static/screening.css"),
                 "static/screening-room-tokens.css": read("static/screening-room-tokens.css") };

test("site.js registers no scroll listener (text)", () => {
  assert.doesNotMatch(siteJs, /addEventListener\(\s*["']scroll["']/);
  assert.doesNotMatch(siteJs, /\bonscroll\b/);
});

/* --- a tiny CSS walker: top-level rules and rules nested in @media blocks --- */
function parseCss(text) {
  const src = text.replace(/\/\*[\s\S]*?\*\//g, "");
  const rules = []; // { selector, body, media }
  let i = 0;
  const walk = (end, media) => {
    while (i < end) {
      const open = src.indexOf("{", i);
      if (open === -1 || open >= end) { i = end; return; }
      const head = src.slice(i, open).trim();
      i = open + 1;
      if (head.startsWith("@media") || head.startsWith("@supports") || head.startsWith("@layer")) {
        // find the matching close brace
        let depth = 1, j = i;
        while (j < src.length && depth) { if (src[j] === "{") depth++; else if (src[j] === "}") depth--; j++; }
        walk(j - 1, head);
        i = j;
      } else if (head.startsWith("@keyframes")) {
        // keyframes nest their own braces (from {…} to {…}); skip to the matching close
        let depth = 1, j = i;
        while (j < src.length && depth) { if (src[j] === "{") depth++; else if (src[j] === "}") depth--; j++; }
        rules.push({ selector: head, body: src.slice(i, j - 1), media, keyframes: head.split(/\s+/)[1] });
        i = j;
      } else {
        const close = src.indexOf("}", i);
        const body = src.slice(i, close);
        i = close + 1;
        for (const sel of head.split(",")) rules.push({ selector: sel.replace(/\s+/g, " ").trim(), body, media });
      }
    }
  };
  walk(src.length, null);
  return rules;
}
const isReduce = (media) => media != null && /prefers-reduced-motion\s*:\s*reduce/.test(media);
const animNames = (body) => {
  const names = [];
  for (const m of body.matchAll(/animation(?:-name)?\s*:\s*([^;]+)/g)) {
    for (const token of m[1].split(/[\s,]+/)) if (/^[A-Za-z_-][\w-]*$/.test(token) && !/^(none|infinite|alternate|linear|ease|ease-in|ease-out|ease-in-out|both|forwards|backwards|normal|reverse|running|paused)$/.test(token)) names.push(token);
  }
  return names;
};

test("every @keyframes in the public sheets is neutralised under prefers-reduced-motion", () => {
  for (const [file, text] of Object.entries(sheets)) {
    const rules = parseCss(text);
    const keyframes = rules.filter((r) => r.keyframes).map((r) => r.keyframes);
    assert.ok(keyframes.length > 0, `${file}: no @keyframes found (parser broke?)`);
    const reduceOff = new Set(rules.filter((r) => isReduce(r.media) && /animation(?:-name)?\s*:\s*none/.test(r.body)).map((r) => r.selector));
    for (const name of keyframes) {
      const users = rules.filter((r) => !r.keyframes && !isReduce(r.media) && animNames(r.body).includes(name));
      assert.ok(users.length > 0, `${file}: @keyframes ${name} is declared but never used`);
      for (const u of users) assert.ok(reduceOff.has(u.selector), `${file}: ${u.selector} animates ${name} but no prefers-reduced-motion rule sets animation: none on it`);
    }
  }
});

test("reveal and button motion use the D-8 values and are off under reduced motion", () => {
  const rules = parseCss(sheets["static/screening.css"]);
  const find = (sel, reduce) => rules.find((r) => r.selector === sel && isReduce(r.media) === reduce);
  const reveal = find(".sr [data-reveal]", false);
  assert.ok(reveal, ".sr [data-reveal] rule");
  assert.match(reveal.body, /opacity 600ms cubic-bezier\(0\.16, 1, 0\.3, 1\)/);
  assert.match(reveal.body, /transform 600ms cubic-bezier\(0\.16, 1, 0\.3, 1\)/);
  assert.match(find(".sr .reveal-hidden", false).body, /translateY\(12px\)/);
  assert.match(find(".sr [data-reveal]", true).body, /transition\s*:\s*none/);
  const active = find(".sr .sr-btn:active", false);
  assert.ok(active, ".sr .sr-btn:active rule");
  assert.match(active.body, /transform\s*:\s*scale\(0?\.98\)/);
  assert.match(find(".sr .sr-btn", false).body, /transition\s*:[^;]*transform var\(--sr-t-fast\)/);
  assert.match(find(".sr .sr-btn", true).body, /transition\s*:\s*none/);
  assert.match(find(".sr .sr-btn:active", true).body, /transform\s*:\s*none/);
  assert.ok(!rules.some((r) => /\.scroll-progress/.test(r.selector)), "no .scroll-progress rule survives");
});

/* --- behaviour: the sticky nav state comes from an IntersectionObserver --- */
class Classes {
  constructor() { this.values = new Set(); }
  contains(n) { return this.values.has(n); }
  add(n) { this.values.add(n); }
  remove(n) { this.values.delete(n); }
  toggle(n, force) { const on = force === undefined ? !this.contains(n) : !!force; on ? this.add(n) : this.remove(n); return on; }
}
function sandbox() {
  const nav = { classList: new Classes(), querySelectorAll: () => [], querySelector: () => null };
  const sentinel = { name: "sentinel" };
  const observers = [];
  class IntersectionObserver {
    constructor(cb, opts) { this.cb = cb; this.opts = opts; this.targets = []; observers.push(this); }
    observe(t) { this.targets.push(t); }
    unobserve() {}
  }
  const winListeners = [];
  const map = { "[data-nav]": nav, "[data-nav-sentinel]": sentinel };
  const document = {
    querySelector: (sel) => (Object.hasOwn(map, sel) ? map[sel] : null),
    querySelectorAll: () => [],
    addEventListener() {},
    documentElement: { classList: new Classes() },
    body: { contains: () => false, appendChild() {} },
  };
  const window = {
    matchMedia: () => ({ matches: false }),
    addEventListener: (type) => winListeners.push(type),
    IntersectionObserver,
    location: { pathname: "/" },
    sessionStorage: { getItem: () => null, setItem() {} },
  };
  const ctx = { window, document, IntersectionObserver, requestAnimationFrame: (fn) => fn(), Promise, console };
  vm.runInNewContext(siteJs, ctx, { filename: "static/site.js" });
  return { nav, sentinel, observers, winListeners };
}

test("the nav shadow is driven by an IntersectionObserver on the sentinel, not a scroll event", () => {
  const { nav, sentinel, observers, winListeners } = sandbox();
  assert.ok(!winListeners.includes("scroll"), `window listeners registered: ${winListeners.join(",")}`);
  const io = observers.find((o) => o.targets.includes(sentinel));
  assert.ok(io, "an IntersectionObserver observes [data-nav-sentinel]");
  io.cb([{ target: sentinel, isIntersecting: false }], io);
  assert.equal(nav.classList.contains("scrolled"), true, "sentinel out of view => scrolled");
  io.cb([{ target: sentinel, isIntersecting: true }], io);
  assert.equal(nav.classList.contains("scrolled"), false, "sentinel back in view => not scrolled");
});
