import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";

const source = fs.readFileSync(new URL("../../static/lightbox.js", import.meta.url), "utf8");
const tick = () => new Promise((resolve) => setImmediate(resolve));
const flush = async () => { await tick(); await tick(); };

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

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
    this.value = "";
    this.hidden = false;
    this.checked = false;
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
  play() { return Promise.resolve(); }
}

function note(body, id = 1) {
  return { id, parent_id: null, timecode: 4, body, author_role: "client", status: "open" };
}

function response(status, payload, malformed = false) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: () => malformed ? Promise.reject(new Error("bad json")) : Promise.resolve(payload),
  };
}

function fixture() {
  const el = (name) => new El(name);
  const lb = el("lightbox"), stage = el("stage"), fav = el("fav"), dl = el("dl");
  const mp4 = el("mp4"), play = el("play"), proof = el("proof"), close = el("close");
  const prev = el("prev"), next = el("next"), wrap = el("comments"), list = el("list");
  const form = el("form"), body = el("body"), at = el("at"), tc = el("tc");
  const parent = el("parent"), cancel = el("cancel"), count = el("count"), filter = el("filter");
  lb.hidden = true;
  lb.dataset.slug = "gallery";
  tc.value = "0";
  cancel.hidden = true;
  lb.queries = { ".lb-stage": stage, ".lb-fav": fav, ".lb-dl": dl, ".lb-dl-mp4": mp4,
    ".lb-play": play, ".lb-proof": proof, ".lb-comments": wrap, ".lb-close": close,
    ".lb-prev": prev, ".lb-next": next };
  wrap.queries = { ".vc-list": list, ".vc-form": form, ".vc-count": count, ".vc-filter": filter };
  form.queries = { ".vc-body": body, ".vc-at": at, ".vc-timecode": tc,
    ".vc-parent": parent, ".vc-cancel-reply": cancel };
  const tile = (id) => {
    const tileEl = el(`tile-${id}`), image = el(`image-${id}`);
    tileEl.dataset = { id, kind: "video", web: `/video/${id}` };
    image.alt = `Video ${id}`;
    tileEl.queries = { img: image, ".fav-btn": null, "button.icon-btn": null };
    return { tile: tileEl, image };
  };
  const a = tile("A"), b = tile("B"), calls = [], reloads = [];
  const document = {
    body: { style: {} }, activeElement: el("focus"),
    getElementById: (id) => id === "lightbox" ? lb : null,
    querySelectorAll: (selector) => selector === ".tile" ? [a.tile, b.tile] : [],
    createElement: (tag) => el(tag), addEventListener() {},
  };
  class FakeFormData { constructor() { this.entries = []; } append(k, v) { this.entries.push([k, v]); } }
  const fetch = (url, options = {}) => {
    const pending = deferred();
    calls.push({ url, method: options.method || "GET", pending });
    return pending.promise;
  };
  vm.runInNewContext(source, { document, fetch, FormData: FakeFormData,
    window: { location: { reload: () => reloads.push(true) } },
    htmx: { ajax: () => Promise.resolve() }, setInterval: () => 1, clearInterval() {} },
  { filename: "static/lightbox.js" });
  return { a, b, body, form, list, prev, next, calls, reloads };
}

function callsFor(ui, method, asset) {
  return ui.calls.filter((c) => c.method === method && c.url.endsWith(`/comments/${asset}`));
}

function commentBodies(root) {
  const found = [];
  const visit = (node) => {
    if (!(node instanceof El)) return;
    if (node.className.split(" ").includes("vc-text")) found.push(node.textContent);
    node.children.forEach(visit);
  };
  visit(root);
  return found;
}

async function draftAndRoundTrip(ui, text) {
  await ui.a.image.fire("click");
  ui.body.value = text;
  await ui.body.fire("input");
  const submitted = ui.form.fire("submit");
  await ui.next.fire("click");
  await ui.prev.fire("click");
  return { submitted };
}

test("accepted A post wins after A to B to A and rejects older gets", async () => {
  const ui = fixture();
  const { submitted } = await draftAndRoundTrip(ui, "draft A");
  const [a1, a2] = callsFor(ui, "GET", "A"), [b] = callsFor(ui, "GET", "B");
  const [post] = callsFor(ui, "POST", "A");
  a2.pending.resolve(response(200, []));
  await flush();
  post.pending.resolve(response(200, [note("posted A", 9)]));
  await submitted;
  await flush();
  assert.equal(ui.body.value, "");
  callsFor(ui, "GET", "A")[2].pending.resolve(response(200, [note("posted A", 9)]));
  await flush();
  assert.deepEqual(commentBodies(ui.list), ["posted A"]);
  a1.pending.resolve(response(200, [note("old A", 2)]));
  b.pending.resolve(response(200, [note("old B", 3)]));
  await flush();
  assert.deepEqual(commentBodies(ui.list), ["posted A"]);
});

test("accepted A post settled on B clears A's captured draft without mutating B", async () => {
  const ui = fixture();
  await ui.a.image.fire("click");
  ui.body.value = "draft A";
  await ui.body.fire("input");
  const submitted = ui.form.fire("submit");
  await ui.next.fire("click");
  const [bGet] = callsFor(ui, "GET", "B"), [post] = callsFor(ui, "POST", "A");
  bGet.pending.resolve(response(200, [note("current B", 5)]));
  post.pending.resolve(response(200, [note("posted A", 9)]));
  await submitted;
  await flush();
  assert.deepEqual(commentBodies(ui.list), ["current B"]);
  await ui.prev.fire("click");
  assert.equal(ui.body.value, "");
  const [a1, a2] = callsFor(ui, "GET", "A");
  a2.pending.resolve(response(200, [note("posted A", 9)]));
  a1.pending.resolve(response(200, [note("old A", 2)]));
  await flush();
});

test("delayed post body cannot replace a newer A server snapshot", async () => {
  const ui = fixture();
  await ui.a.image.fire("click");
  ui.body.value = "draft A";
  await ui.body.fire("input");
  const submitted = ui.form.fire("submit"), postJson = deferred();
  const [post] = callsFor(ui, "POST", "A");
  post.pending.resolve({ ok: true, status: 200, json: () => postJson.promise });
  await flush();
  await ui.next.fire("click");
  await ui.prev.fire("click");
  const [a1, a2] = callsFor(ui, "GET", "A"), [b] = callsFor(ui, "GET", "B");
  const newer = [note("posted A", 9), note("newer studio reply", 10)];
  a2.pending.resolve(response(200, newer));
  b.pending.resolve(response(200, [note("old B", 3)]));
  await flush();
  postJson.resolve([note("posted A", 9)]);
  await submitted;
  await flush();
  assert.deepEqual(commentBodies(ui.list), ["posted A", "newer studio reply"]);
  const a3 = callsFor(ui, "GET", "A")[2];
  assert.ok(a3);
  a3.pending.resolve(response(200, newer));
  a1.pending.resolve(response(200, [note("old A", 2)]));
  await flush();
  assert.deepEqual(commentBodies(ui.list), ["posted A", "newer studio reply"]);
});

test("a pending returned-A get forces a fresh post-commit load", async () => {
  const ui = fixture();
  await ui.a.image.fire("click");
  ui.body.value = "draft A";
  await ui.body.fire("input");
  const submitted = ui.form.fire("submit"), postJson = deferred();
  const [post] = callsFor(ui, "POST", "A");
  post.pending.resolve({ ok: true, status: 200, json: () => postJson.promise });
  await flush();
  await ui.next.fire("click");
  await ui.prev.fire("click");
  const [a1, a2] = callsFor(ui, "GET", "A"), [b] = callsFor(ui, "GET", "B");
  postJson.resolve([note("posted A", 9)]);
  await submitted;
  await flush();
  assert.deepEqual(commentBodies(ui.list), []);
  const a3 = callsFor(ui, "GET", "A")[2];
  assert.ok(a3);
  const newest = [note("posted A", 9), note("newer studio reply", 10)];
  a3.pending.resolve(response(200, newest));
  a2.pending.resolve(response(200, [note("discarded pending A", 7)]));
  a1.pending.resolve(response(200, [note("old A", 2)]));
  b.pending.resolve(response(200, [note("old B", 3)]));
  await flush();
  assert.deepEqual(commentBodies(ui.list), ["posted A", "newer studio reply"]);
});

test("delayed malformed post body recovers after a newer A render", async () => {
  const ui = fixture();
  await ui.a.image.fire("click");
  ui.body.value = "draft A";
  await ui.body.fire("input");
  const submitted = ui.form.fire("submit"), postJson = deferred();
  const [post] = callsFor(ui, "POST", "A");
  post.pending.resolve({ ok: true, status: 200, json: () => postJson.promise });
  await flush();
  await ui.next.fire("click");
  await ui.prev.fire("click");
  const [a1, a2] = callsFor(ui, "GET", "A"), [b] = callsFor(ui, "GET", "B");
  const newer = [note("posted A", 9), note("newer studio reply", 10)];
  a2.pending.resolve(response(200, newer));
  b.pending.resolve(response(200, [note("old B", 3)]));
  await flush();
  postJson.reject(new Error("malformed json"));
  await submitted;
  await flush();
  const error = ui.form.querySelector(".vc-error");
  assert.equal(error?.hidden, false);
  assert.match(error?.textContent || "", /posted, but comments couldn't refresh/);
  assert.deepEqual(commentBodies(ui.list), ["posted A", "newer studio reply"]);
  const a3 = callsFor(ui, "GET", "A")[2];
  assert.ok(a3);
  a3.pending.resolve(response(200, newer));
  a1.pending.resolve(response(200, [note("old A", 2)]));
  await flush();
  assert.deepEqual(commentBodies(ui.list), ["posted A", "newer studio reply"]);
  assert.equal(error.hidden, true);
});

for (const failure of ["network", "500"]) {
  test(`${failure} A post settled on B is owned by A and unlocks retry`, async () => {
    const ui = fixture();
    await ui.a.image.fire("click");
    ui.body.value = "draft A";
    await ui.body.fire("input");
    const submitted = ui.form.fire("submit");
    await ui.next.fire("click");
    const [bGet] = callsFor(ui, "GET", "B");
    bGet.pending.resolve(response(200, [note("current B", 5)]));
    await flush();
    const [post] = callsFor(ui, "POST", "A");
    if (failure === "network") post.pending.reject(new Error("offline"));
    else post.pending.resolve(response(500, { detail: "no" }));
    await submitted;
    await flush();
    assert.deepEqual(commentBodies(ui.list), ["current B"]);
    assert.equal(ui.form.querySelector(".vc-error")?.hidden, true);
    await ui.prev.fire("click");
    assert.equal(ui.body.value, "draft A");
    const error = ui.form.querySelector(".vc-error");
    assert.equal(error?.hidden, false);
    assert.match(error?.textContent || "", /couldn't confirm/i);
    assert.match(error?.textContent || "", /check .* before/i);
    const [a1, a2] = callsFor(ui, "GET", "A");
    a2.pending.reject(new Error("comment load failed"));
    a1.pending.resolve(response(200, []));
    await flush();
    assert.match(error?.textContent || "", /couldn't confirm/i);
    const retry = ui.form.fire("submit");
    assert.equal(callsFor(ui, "POST", "A").length, 2);
    callsFor(ui, "POST", "A")[1].pending.resolve(response(500, {}));
    await retry;
    await flush();
    assert.equal(ui.body.value, "draft A");
  });
}

test("malformed accepted A post clears its draft and invalidates stale gets", async () => {
  const ui = fixture();
  await ui.a.image.fire("click");
  ui.body.value = "draft A";
  await ui.body.fire("input");
  const submitted = ui.form.fire("submit");
  const [post] = callsFor(ui, "POST", "A");
  post.pending.resolve(response(200, null, true));
  await submitted;
  await flush();
  assert.equal(ui.body.value, "");
  const error = ui.form.querySelector(".vc-error");
  assert.equal(error?.hidden, false);
  assert.match(error?.textContent || "", /posted, but comments couldn't refresh/);
  callsFor(ui, "GET", "A")[0].pending.resolve(response(200, [note("stale A")]));
  await flush();
  assert.deepEqual(commentBodies(ui.list), []);
  await ui.next.fire("click");
  await ui.prev.fire("click");
  const freshA = callsFor(ui, "GET", "A").at(-1);
  freshA.pending.resolve(response(200, [note("posted A", 9)]));
  callsFor(ui, "GET", "B").at(-1).pending.resolve(response(200, []));
  await flush();
  assert.deepEqual(commentBodies(ui.list), ["posted A"]);
  assert.equal(error.hidden, true);
});

test("latest returned A get wins over the first A snapshot", async () => {
  const ui = fixture();
  await ui.a.image.fire("click");
  await ui.next.fire("click");
  await ui.prev.fire("click");
  const [a1, a2] = callsFor(ui, "GET", "A"), [b] = callsFor(ui, "GET", "B");
  a2.pending.resolve(response(200, [note("current A", 7)]));
  await flush();
  a1.pending.resolve(response(200, [note("old A", 2)]));
  b.pending.resolve(response(200, [note("old B", 3)]));
  await flush();
  assert.deepEqual(commentBodies(ui.list), ["current A"]);
});

test("only the current get failure reports a load error", async () => {
  const ui = fixture();
  await ui.a.image.fire("click");
  await ui.next.fire("click");
  callsFor(ui, "GET", "A")[0].pending.reject(new Error("stale A"));
  callsFor(ui, "GET", "B")[0].pending.resolve(response(200, [note("current B", 5)]));
  await flush();
  assert.deepEqual(commentBodies(ui.list), ["current B"]);
  assert.equal(ui.form.querySelector(".vc-error")?.hidden, true);
  await ui.prev.fire("click");
  callsFor(ui, "GET", "A")[1].pending.reject(new Error("current A"));
  await flush();
  assert.match(ui.form.querySelector(".vc-error")?.textContent || "", /Comments couldn't load/);
});

test("a non-ok get never renders an array-shaped error body", async () => {
  const ui = fixture();
  await ui.a.image.fire("click");
  callsFor(ui, "GET", "A")[0].pending.resolve(response(500, [note("error body")]));
  await flush();
  assert.deepEqual(commentBodies(ui.list), []);
  assert.match(ui.form.querySelector(".vc-error")?.textContent || "", /Comments couldn't load/);
});
