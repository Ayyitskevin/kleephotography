import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";

const behaviors = fs.readFileSync(new URL("../../static/behaviors.js", import.meta.url), "utf8");

/* Just enough DOM for the delegated click handler: closest() over the
   attribute selectors behaviors.js asks for ("[x]" and "tag[x]"), parents,
   and contains(). The studio board card is an <article data-goto>; its
   quick-actions peek is a <details data-goto-ignore> inside it. */
class El {
  constructor(tag, attrs = {}) {
    this.tagName = tag.toUpperCase();
    this.attrs = { ...attrs };
    this.parentNode = null;
    this.children = [];
    this.listeners = new Map();
  }
  append(child) { child.parentNode = this; this.children.push(child); return child; }
  hasAttribute(name) { return Object.hasOwn(this.attrs, name); }
  getAttribute(name) { return this.hasAttribute(name) ? this.attrs[name] : null; }
  matches(selector) {
    return selector.split(",").some((part) => {
      const m = part.trim().match(/^([a-z]*)\[([\w-]+)\]$/);
      if (!m) return false;
      return (!m[1] || this.tagName === m[1].toUpperCase()) && this.hasAttribute(m[2]);
    });
  }
  closest(selector) {
    for (let node = this; node; node = node.parentNode) {
      if (node.matches && node.matches(selector)) return node;
    }
    return null;
  }
  contains(node) {
    for (let n = node; n; n = n.parentNode) if (n === this) return true;
    return false;
  }
}

function board() {
  const listeners = new Map();
  const add = (type, fn) => {
    if (!listeners.has(type)) listeners.set(type, []);
    listeners.get(type).push(fn);
  };
  const document = {
    addEventListener: add,
    removeEventListener: () => {},
    querySelector: () => null,
    querySelectorAll: () => [],
    getElementById: () => null,
    createElement: (tag) => new El(tag),
  };
  const window = {
    document,
    location: { href: "/admin/studio", reload: () => {} },
    addEventListener: () => {},
    removeEventListener: () => {},
    setTimeout: () => 0,
    clearTimeout: () => {},
    setInterval: () => 1,
    clearInterval: () => {},
  };
  window.window = window;
  vm.createContext(window);
  vm.runInContext(behaviors, window, { filename: "static/behaviors.js" });

  const card = new El("article", { "data-goto": "/admin/studio/projects/7" });
  const title = card.append(new El("div"));
  const peek = card.append(new El("details", { "data-goto-ignore": "" }));
  const summary = peek.append(new El("summary"));
  const select = peek.append(new El("div")).append(new El("select"));
  const click = (target) => {
    const ev = { type: "click", target, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
    for (const fn of listeners.get("click") || []) fn(ev);
    return ev;
  };
  return { window, title, summary, select, click };
}

test("a click on the card body still opens the project", () => {
  const ui = board();
  ui.click(ui.title);
  assert.equal(ui.window.location.href, "/admin/studio/projects/7");
});

test("opening the quick-actions peek does not navigate away", () => {
  const ui = board();
  ui.click(ui.summary);
  assert.equal(ui.window.location.href, "/admin/studio");
});

test("using a control inside the peek does not navigate away", () => {
  const ui = board();
  ui.click(ui.select);
  assert.equal(ui.window.location.href, "/admin/studio");
});
