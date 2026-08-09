import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";

const behaviors = fs.readFileSync(new URL("../../static/behaviors.js", import.meta.url), "utf8");
const galleryHtml = fs.readFileSync(new URL("../../templates/public/gallery.html", import.meta.url), "utf8");
/* the gallery's own htmx:responseError guard, lifted verbatim out of its
   inline <script> so the no-double-toast contract is pinned against the page
   that actually ships, not a paraphrase of it */
const galleryScript = galleryHtml.match(/<script nonce="\{\{ csp_nonce \}\}">([\s\S]*?)<\/script>/)[1];

class Classes {
  constructor() { this.values = new Set(); }
  contains(name) { return this.values.has(name); }
  add(name) { this.values.add(name); }
  remove(name) { this.values.delete(name); }
}

class El {
  constructor(name = "el") {
    this.name = name;
    this.listeners = new Map();
    this.children = [];
    this.parentNode = null;
    this.className = "";
    this.textContent = "";
    this.attrs = {};
    this.classList = new Classes();
  }
  addEventListener(type, fn) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(fn);
  }
  removeEventListener(type, fn) {
    const list = this.listeners.get(type) || [];
    const at = list.indexOf(fn);
    if (at >= 0) list.splice(at, 1);
  }
  appendChild(child) { child.parentNode = this; this.children.push(child); return child; }
  removeChild(child) {
    const at = this.children.indexOf(child);
    if (at >= 0) this.children.splice(at, 1);
    child.parentNode = null;
    return child;
  }
  get firstChild() { return this.children[0] || null; }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  getAttribute(name) { return Object.hasOwn(this.attrs, name) ? this.attrs[name] : null; }
  contains(node) { return node === this || this.children.some((c) => c.contains(node)); }
  hasClass(name) { return this.className.split(" ").includes(name); }
}

/* bubble-phase propagation only — target, ancestors, document, window; that
   ordering IS the contract under test (window runs last, after every scoped
   listener, including ones bound mid-session) */
function fixture({ gallery = false } = {}) {
  const body = new El("body");
  const reloads = [];
  const document = {
    listeners: new Map(),
    body,
    createElement: (tag) => new El(tag),
    querySelector(selector) {
      if (!selector.startsWith(".")) return null;
      const name = selector.slice(1);
      return body.children.find((c) => c.hasClass(name)) || null;
    },
    querySelectorAll: () => [],
    addEventListener: El.prototype.addEventListener,
    removeEventListener: El.prototype.removeEventListener,
    getElementById: () => null,
  };
  const window = {
    listeners: new Map(),
    document,
    location: { reload: () => reloads.push(true) },
    setTimeout: () => 0,
    clearTimeout: () => {},
    addEventListener: El.prototype.addEventListener,
    removeEventListener: El.prototype.removeEventListener,
  };
  window.window = window;
  window.setInterval = () => 1;
  window.clearInterval = () => {};
  vm.createContext(window);
  vm.runInContext(behaviors, window, { filename: "static/behaviors.js" });
  if (gallery) vm.runInContext(galleryScript, window, { filename: "templates/public/gallery.html" });

  /* htmx stamps detail.elt with the element the request came from on EVERY
     event it raises (`function ce(e,t,r){…r["elt"]=e;…}` in htmx.min.js
     1.9.12) — the poll guard reads it, so the fixture must too. */
  const fire = (target, type, detail) => {
    const event = {
      type, detail: { ...detail, elt: target }, target,
      defaultPrevented: false,
      preventDefault() { this.defaultPrevented = true; },
    };
    const chain = [];
    for (let node = target; node; node = node.parentNode) chain.push(node);
    chain.push(document, window);
    for (const node of chain) {
      for (const fn of (node.listeners.get(type) || []).slice()) fn(event);
    }
    return event;
  };
  const toasts = () => {
    const host = document.querySelector(".sr-toasts");
    return host ? host.children : [];
  };
  const el = (name, attrs) => {
    const node = body.appendChild(new El(name));
    if (attrs) Object.assign(node.attrs, attrs);
    return node;
  };
  return { window, document, body, reloads, fire, toasts, el };
}

/* A user-initiated request carries the DOM event down into requestConfig
   (htmx's issueAjaxRequest 4th arg); the poll scheduler calls the same
   handler with the element alone, so triggeringEvent stays undefined. */
const responded = (status) => ({
  xhr: { status },
  pathInfo: { requestPath: "/x" },
  requestConfig: { triggeringEvent: { type: "click" } },
});
const polled = (status) => ({
  xhr: { status },
  pathInfo: { requestPath: "/x" },
  requestConfig: { triggeringEvent: undefined },
});

test("a 500 swap failure toasts instead of dying silently", () => {
  const ui = fixture();
  ui.fire(ui.el("sign-form"), "htmx:responseError", responded(500));
  assert.equal(ui.toasts().length, 1);
  assert.match(ui.toasts()[0].textContent, /didn't go through/);
  assert.match(ui.toasts()[0].textContent, /Try again/);
  assert.ok(ui.toasts()[0].hasClass("sr-toast--danger"));
  assert.equal(ui.toasts()[0].getAttribute("role"), "status");
});

test("a request that never lands gets its own offline wording", () => {
  const ui = fixture();
  ui.fire(ui.el("accept-form"), "htmx:sendError", { xhr: { status: 0 }, pathInfo: { requestPath: "/x" } });
  assert.equal(ui.toasts().length, 1);
  assert.match(ui.toasts()[0].textContent, /Couldn't reach the server/);
  assert.match(ui.toasts()[0].textContent, /connection/);
});

/* The copy has to be true for reads too — hx-get step navigation
   (public/_book_card.html) and the 409 contract-integrity refusal
   (app/public/docs.py), where retrying can never succeed. */
test("no message claims a write, and a 4xx doesn't promise a retry", () => {
  for (const status of [400, 403, 409, 422]) {
    const ui = fixture();
    ui.fire(ui.el("book-card"), "htmx:responseError", responded(status));
    const text = ui.toasts()[0].textContent;
    assert.match(text, /didn't go through/);
    assert.match(text, /refresh the page/i);
    assert.doesNotMatch(text, /save/i);
    assert.doesNotMatch(text, /try again/i);
  }
  for (const status of [500, 502, 503]) {
    const ui = fixture();
    ui.fire(ui.el("sign-form"), "htmx:responseError", responded(status));
    assert.doesNotMatch(ui.toasts()[0].textContent, /save/i);
  }
});

test("the gallery's 403 reload wins — no toast rides it", () => {
  const ui = fixture({ gallery: true });
  ui.fire(ui.el("fav-btn"), "htmx:responseError", responded(403));
  assert.deepEqual(ui.reloads, [true]);
  assert.equal(ui.toasts().length, 0);
});

test("the gallery's 410 reload wins — no toast rides it", () => {
  const ui = fixture({ gallery: true });
  ui.fire(ui.el("fav-btn"), "htmx:responseError", responded(410));
  assert.deepEqual(ui.reloads, [true]);
  assert.equal(ui.toasts().length, 0);
});

test("a 500 inside the gallery still toasts — the scoped guard ignores it", () => {
  const ui = fixture({ gallery: true });
  ui.fire(ui.el("fav-btn"), "htmx:responseError", responded(500));
  assert.deepEqual(ui.reloads, []);
  assert.equal(ui.toasts().length, 1);
  assert.match(ui.toasts()[0].textContent, /didn't go through/);
});

/* ── background polls stand down ─────────────────────────────────────────
   htmx does NOT stop polling on error (it cancels only on HTTP 286, and that
   branch is inside the swap path a 4xx/5xx never reaches), so a toast here
   repeats for the whole outage — every 5s on the invoice page, and once per
   pending tile on a gallery. #_invoice_status is the sharp end: the client
   has already paid. */
for (const [label, spec] of [["every 5s", "every 5s"], ["every 8s", "every 8s"]]) {
  test(`a poll failure (${label}) produces no toast`, () => {
    const ui = fixture();
    const region = ui.el("inv-status", { "hx-trigger": spec });
    for (const status of [500, 502, 503, 403]) ui.fire(region, "htmx:responseError", polled(status));
    ui.fire(region, "htmx:sendError", polled(0));
    assert.equal(ui.toasts().length, 0);
  });
}

test("a restart-length outage never nags the client who just paid", () => {
  const ui = fixture();
  const region = ui.el("inv-status", { "hx-trigger": "every 5s" });
  for (let i = 0; i < 12; i++) ui.fire(region, "htmx:responseError", polled(502));
  assert.equal(ui.toasts().length, 0);
});

test("one hiccup across N pending rendition tiles stays silent", () => {
  const ui = fixture();
  for (let i = 0; i < 6; i++) {
    ui.fire(ui.el("rec-tile-" + i, { "hx-trigger": "every 8s" }), "htmx:responseError", polled(502));
  }
  assert.equal(ui.toasts().length, 0);
});

test("a user-initiated failure on the SAME element still toasts", () => {
  const ui = fixture();
  /* mixed spec: the poll half stays quiet, the click half must not */
  const region = ui.el("inv-status", { "hx-trigger": "click, every 5s" });
  ui.fire(region, "htmx:responseError", polled(500));
  assert.equal(ui.toasts().length, 0);
  ui.fire(region, "htmx:responseError", responded(500));
  assert.equal(ui.toasts().length, 1);
  assert.match(ui.toasts()[0].textContent, /didn't go through/);
});

test("a non-polling element is unaffected by the guard", () => {
  const ui = fixture();
  for (const spec of ["change", "click", "every-other-thing", "revealed"]) {
    const ui2 = fixture();
    ui2.fire(ui2.el("field", { "hx-trigger": spec }), "htmx:responseError", responded(500));
    assert.equal(ui2.toasts().length, 1, `expected a toast for hx-trigger="${spec}"`);
  }
  ui.fire(ui.el("plain"), "htmx:responseError", responded(500));
  assert.equal(ui.toasts().length, 1);
});

/* Pin the guard to the markup that actually ships: if someone changes one of
   these poll specs, this fails instead of the nag quietly coming back. */
test("every self-polling region in the repo is recognised as a poll", () => {
  const polling = [
    "templates/public/_invoice_status.html",
    "templates/public/_rec_tile.html",
    "templates/public/_portal_crops.html",
    "templates/admin/_delivery_check.html",
  ];
  for (const rel of polling) {
    const html = fs.readFileSync(new URL("../../" + rel, import.meta.url), "utf8");
    const spec = html.match(/hx-trigger="([^"]*)"/);
    assert.ok(spec, `${rel} no longer declares an hx-trigger`);
    const ui = fixture();
    ui.fire(ui.el("poller", { "hx-trigger": spec[1] }), "htmx:responseError", polled(502));
    assert.equal(ui.toasts().length, 0, `${rel} hx-trigger="${spec[1]}" still toasts`);
  }
});

/* the board-drag guard binds its listener on document at drop time — long
   after behaviors.js loaded — so only a window-level global still runs last */
test("a scoped guard bound mid-session claims the failure", () => {
  const ui = fixture();
  const own = [];
  ui.document.addEventListener("htmx:responseError", function (ev) {
    ev.preventDefault();
    own.push(ev.detail.pathInfo.requestPath);
  });
  ui.fire(ui.el("studio-row"), "htmx:responseError", responded(500));
  assert.deepEqual(own, ["/x"]);
  assert.equal(ui.toasts().length, 0);
});

test("successful swaps are untouched", () => {
  const ui = fixture({ gallery: true });
  const target = ui.el("doc-state");
  for (const type of ["htmx:beforeRequest", "htmx:afterOnLoad", "htmx:afterSwap", "htmx:afterRequest", "htmx:afterSettle"]) {
    ui.fire(target, type, { xhr: { status: 200 }, pathInfo: { requestPath: "/x" } });
  }
  assert.equal(ui.toasts().length, 0);
  assert.deepEqual(ui.reloads, []);
});

test("the toast stack never grows past the shared primitive's three", () => {
  const ui = fixture();
  const target = ui.el("tile");
  for (let i = 0; i < 5; i++) ui.fire(target, "htmx:responseError", responded(500));
  assert.equal(ui.toasts().length, 3);
});
