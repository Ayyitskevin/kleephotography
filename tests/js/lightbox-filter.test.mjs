import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";

const source = fs.readFileSync(new URL("../../static/lightbox.js", import.meta.url), "utf8");

class Classes {
  constructor() { this.values = new Set(); }
  contains(name) { return this.values.has(name); }
  add(name) { this.values.add(name); }
  remove(name) { this.values.delete(name); }
  toggle(name, force) {
    const on = force === undefined ? !this.contains(name) : force;
    if (on) this.add(name); else this.remove(name);
    return on;
  }
}

/* offsetParent is the grid's own visibility test (visibleTile) — null is what a
   browser reports for a tile the active portfolio filter has display:none'd. */
class El {
  constructor(name = "el") {
    this.name = name;
    this.listeners = new Map();
    this.children = [];
    this.queries = {};
    this.dataset = {};
    this.style = {};
    this.attrs = {};
    this.classList = new Classes();
    this.className = "";
    this.textContent = "";
    this.hidden = false;
    this.offsetParent = {};
    this._innerHTML = "";
  }
  addEventListener(type, fn) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(fn);
  }
  fire(type, extra = {}) {
    const event = { target: this, preventDefault() {}, ...extra };
    return Promise.all((this.listeners.get(type) || []).map((fn) => fn(event)));
  }
  querySelector(selector) {
    if (Object.hasOwn(this.queries, selector)) return this.queries[selector];
    if (!selector.startsWith(".")) return null;
    const name = selector.slice(1);
    return this.children.find((c) => c instanceof El && c.className.split(" ").includes(name)) || null;
  }
  querySelectorAll() { return []; }
  appendChild(child) { this.children.push(child); return child; }
  append(...children) { this.children.push(...children); }
  set innerHTML(value) { this._innerHTML = value; if (value === "") this.children = []; }
  get innerHTML() { return this._innerHTML; }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  getAttribute(name) { return this.attrs[name] ?? null; }
  focus() {}
  getClientRects() { return [{}]; }
  closest() { return null; }
}

/* Photo tiles on the marketing lightbox: no action bar and no comments pane,
   so the only bar element the fixture supplies is the live region. */
function fixture(count) {
  const el = (name) => new El(name);
  const lb = el("lightbox"), stage = el("stage"), live = el("live");
  const close = el("close"), prev = el("prev"), next = el("next");
  lb.hidden = true;
  lb.queries = { ".lb-stage": stage, ".lb-live": live, ".lb-close": close,
    ".lb-prev": prev, ".lb-next": next };
  const tiles = [];
  for (let n = 1; n <= count; n++) {
    const tileEl = el(`tile-${n}`), image = el(`image-${n}`);
    tileEl.dataset = { id: String(n), web: `/photo/${n}` };
    image.alt = `Photo ${n}`;
    tileEl.queries = { img: image };
    tiles.push({ tile: tileEl, image });
  }
  const document = {
    body: { style: {} }, activeElement: el("focus"),
    getElementById: (id) => id === "lightbox" ? lb : null,
    querySelectorAll: (selector) => selector === ".tile" ? tiles.map((t) => t.tile) : [],
    createElement: (tag) => el(tag), addEventListener() {},
  };
  vm.runInNewContext(source, { document, window: {}, setInterval: () => 1, clearInterval() {} },
    { filename: "static/lightbox.js" });
  return { tiles, live, prev, next };
}

function hide(ui, ...indexes) {
  indexes.forEach((i) => { ui.tiles[i].tile.offsetParent = null; });
}

test("an unfiltered grid announces the plain grid position", async () => {
  const ui = fixture(4);
  await ui.tiles[0].image.fire("click");
  assert.equal(ui.live.textContent, "Photo 1 — 1 of 4");
  await ui.next.fire("click");
  assert.equal(ui.live.textContent, "Photo 2 — 2 of 4");
  await ui.prev.fire("click");
  assert.equal(ui.live.textContent, "Photo 1 — 1 of 4");
});

test("a filtered grid announces the position within what's shown", async () => {
  const ui = fixture(4);
  hide(ui, 1, 2);
  await ui.tiles[0].image.fire("click");
  assert.equal(ui.live.textContent, "Photo 1 — 1 of 2");
  await ui.next.fire("click");
  assert.equal(ui.live.textContent, "Photo 4 — 2 of 2");
  await ui.next.fire("click");
  assert.equal(ui.live.textContent, "Photo 1 — 1 of 2");
});

/* step() already skips hidden tiles, so an unfiltered count also announces a
   sequence with holes in it — "1 of 6" then "3 of 6" then "6 of 6". */
test("arrowing across a filtered grid never skips an announced number", async () => {
  const ui = fixture(6);
  hide(ui, 1, 3, 4);
  await ui.tiles[0].image.fire("click");
  const heard = [ui.live.textContent];
  for (let n = 0; n < 2; n++) {
    await ui.next.fire("click");
    heard.push(ui.live.textContent);
  }
  assert.deepEqual(heard, ["Photo 1 — 1 of 3", "Photo 3 — 2 of 3", "Photo 6 — 3 of 3"]);
});

test("a tile outside the visible set falls back to the whole grid", async () => {
  const ui = fixture(4);
  hide(ui, 1);
  await ui.tiles[1].image.fire("click");
  assert.equal(ui.live.textContent, "Photo 2 — 2 of 4");
});
