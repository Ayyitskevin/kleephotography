import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";

const source = fs.readFileSync(new URL("../../static/copy_link.js", import.meta.url), "utf8");
const tick = () => new Promise((resolve) => setImmediate(resolve));
const flush = async () => { await tick(); await tick(); };

class El {
  constructor(name = "el") {
    this.name = name;
    this.listeners = new Map();
    this.children = [];
    this.parentNode = null;
    this.className = "";
    this.textContent = "";
    this.dataset = {};
    this.hidden = false;
  }
  addEventListener(type, fn) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(fn);
  }
  appendChild(child) { child.parentNode = this; this.children.push(child); return child; }
  get parentElement() { return this.parentNode; }
  hasClass(name) { return this.className.split(" ").includes(name); }
  descendants() { return this.children.flatMap((c) => [c, ...c.descendants()]); }
  closest(selector) {
    const name = selector.slice(1);
    for (let node = this; node; node = node.parentNode) if (node.hasClass(name)) return node;
    return null;
  }
  querySelector(selector) {
    const name = selector.slice(1);
    return this.descendants().find((c) => c.hasClass(name)) || null;
  }
}

/* bubble-phase propagation: target, ancestors, document — the delegated
   listener sits on document, which is the point (the .copy-link button itself
   may not have existed when the script ran) */
function fixture({ clipboard = true } = {}) {
  const body = new El("body");
  const writes = [];
  const timers = [];
  let nextTimer = 1;
  const document = {
    body,
    listeners: new Map(),
    addEventListener: El.prototype.addEventListener,
    querySelectorAll(selector) {
      const name = selector.slice(1);
      return body.descendants().filter((c) => c.hasClass(name));
    },
  };
  const navigator = {
    clipboard: clipboard
      ? { writeText: (text) => { writes.push(text); return Promise.resolve(); } }
      : { writeText: () => Promise.reject(new Error("not allowed")) },
  };
  const sandbox = {
    document,
    navigator,
    setTimeout: (fn, ms) => { const id = nextTimer++; timers.push({ id, fn, ms, live: true }); return id; },
    clearTimeout: (id) => { const t = timers.find((x) => x.id === id); if (t) t.live = false; },
  };
  const run = () => vm.runInNewContext(source, sandbox, { filename: "static/copy_link.js" });
  const fire = async (target) => {
    const event = { type: "click", target, preventDefault() {} };
    for (let node = target; node; node = node.parentNode) {
      for (const fn of (node.listeners.get("click") || []).slice()) fn(event);
    }
    for (const fn of (document.listeners.get("click") || []).slice()) fn(event);
    await flush();
  };
  /* the shape templates/admin/_copy_link.html emits: the button and its
     feedback span are siblings under one parent */
  const row = (payload) => {
    const wrap = body.appendChild(new El("wrap"));
    const btn = wrap.appendChild(new El("btn"));
    btn.className = "link copy-link";
    btn.dataset.copy = payload;
    const fb = wrap.appendChild(new El("feedback"));
    fb.className = "copy-feedback muted";
    fb.textContent = "Copied!";
    fb.hidden = true;
    return { btn, fb };
  };
  const expire = () => timers.filter((t) => t.live).forEach((t) => { t.live = false; t.fn(); });
  return { body, writes, timers, run, fire, row, expire };
}

test("a button swapped in after load still copies", async () => {
  const ui = fixture();
  ui.run();
  const { btn, fb } = ui.row("https://kleephotography.com/g/abc");
  await ui.fire(btn);
  assert.deepEqual(ui.writes, ["https://kleephotography.com/g/abc"]);
  assert.equal(fb.textContent, "Copied!");
  assert.equal(fb.hidden, false);
});

test("a button present at load still copies", async () => {
  const ui = fixture();
  const { btn, fb } = ui.row("https://kleephotography.com/p/xyz\nPIN: 1234");
  ui.run();
  await ui.fire(btn);
  assert.deepEqual(ui.writes, ["https://kleephotography.com/p/xyz\nPIN: 1234"]);
  assert.equal(fb.hidden, false);
});

test("the confirmation clears itself after 2.5s", async () => {
  const ui = fixture();
  ui.run();
  const { btn, fb } = ui.row("https://kleephotography.com/i/9");
  await ui.fire(btn);
  assert.deepEqual(ui.timers.map((t) => t.ms), [2500]);
  ui.expire();
  assert.equal(fb.hidden, true);
});

test("an unavailable clipboard says so instead of claiming a copy", async () => {
  const ui = fixture({ clipboard: false });
  ui.run();
  const { btn, fb } = ui.row("https://kleephotography.com/c/7");
  await ui.fire(btn);
  assert.deepEqual(ui.writes, []);
  assert.match(fb.textContent, /Copy failed/);
  assert.equal(fb.hidden, false);
});

test("a click outside a copy-link button is ignored", async () => {
  const ui = fixture();
  ui.run();
  ui.row("https://kleephotography.com/g/abc");
  const other = ui.body.appendChild(new El("elsewhere"));
  await ui.fire(other);
  assert.deepEqual(ui.writes, []);
  assert.deepEqual(ui.timers, []);
});
