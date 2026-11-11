import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../../static/prospect_drafts.js', import.meta.url), 'utf8');
const tick = () => new Promise((resolve) => setImmediate(resolve));
function fixture(options = {}) {
  const fields = { subject: { value: 'Subject' }, body: { value: 'Reviewed message?' }, token: { value: 'receipt-A' } };
  const ui = Object.fromEntries(['feedback', 'dirty', 'count'].map((key) => [key, { textContent: '' }]));
  const listeners = {}, windowListeners = {}, writes = [], requests = [];
  const form = {
    dataset: { draftId: '31', prospectId: '7', copyUrl: '/admin/studio/prospects/7/drafts/31/copy', unsaved: options.unsaved ? 'true' : 'false' },
    isConnected: true,
    elements: { namedItem: (name) => fields[name] },
    querySelector: (selector) => ui[selector.slice(12, -1)],
    querySelectorAll: () => buttons,
    addEventListener: (event, cb) => { listeners[event] = cb; },
    contains: (el) => buttons.includes(el),
  };
  const buttons = ['body', 'subject'].map((part) => ({ dataset: { draftCopy: part }, disabled: Boolean(options.disabled), closest() { return this; } }));
  const document = { querySelector: () => form, visibilityState: 'visible' };
  const response = { ok: true, headers: { get: () => 'application/json' }, json: async () => ({ draft_id: 31, prospect_id: 7, subject: fields.subject.value, body: fields.body.value }) };
  vm.runInNewContext(source, {
    document,
    window: { addEventListener: (event, cb) => { windowListeners[event] = cb; } },
    FormData: class { constructor() { this.fields = Object.fromEntries(Object.entries(fields).map(([k,v]) => [k,v.value])); } },
    fetch: async (url, init) => { requests.push({ url, init }); return options.fetch ? options.fetch(response) : response; },
    navigator: { clipboard: { writeText: async (text) => { if (options.clipboardFailure) throw new Error('denied'); writes.push(text); if (options.clipboard) await options.clipboard(); } } },
  });
  return { fields, ui, form, buttons, document, writes, requests, response,
    click: (part = 'body') => listeners.click({ target: buttons.find((b) => b.dataset.draftCopy === part) }),
    input: () => listeners.input(), submit: () => listeners.submit(),
    unload: () => { const e = { prevented: false, preventDefault() { this.prevented = true; } }; windowListeners.beforeunload(e); return e; },
  };
}

test('copies the selected editor field, including unsaved edits, after a read-only check', async () => {
  const f = fixture();
  f.fields.body.value = '  My edited text?  ';
  f.input();
  await f.click();
  assert.deepEqual(f.writes, ['  My edited text?  ']);
  assert.equal(f.requests[0].url, f.form.dataset.copyUrl);
  assert.equal(f.requests[0].init.body.fields.token, 'receipt-A');
  assert.equal(f.requests[0].init.body.fields.body, f.fields.body.value);
  assert.match(f.ui.feedback.textContent, /Nothing sent or logged.*Save to keep/);
  assert.match(f.ui.dirty.textContent, /Unsaved/);
  await f.click('subject');
  assert.equal(f.writes[1], 'Subject');
  assert.equal(f.requests.length, 2); // No separate save/contact request.
});

test('stale server context refuses copying and preserves edits', async () => {
  const f = fixture();
  f.response.ok = false;
  f.response.json = async () => ({ error: 'Recipient changed; start a fresh draft.' });
  await f.click();
  assert.deepEqual(f.writes, []);
  assert.match(f.ui.feedback.textContent, /Recipient changed/);
  assert.equal(f.fields.body.value, 'Reviewed message?');
  assert.ok(f.buttons.every((b) => !b.disabled));
});

test('a response belonging to another prospect or draft cannot reach the clipboard', async () => {
  for (const mismatch of [{ prospect_id: 8, draft_id: 31 }, { prospect_id: 7, draft_id: 32 }]) {
    const f = fixture();
    f.response.json = async () => mismatch;
    await f.click();
    assert.equal(f.writes.length, 0);
    assert.match(f.ui.feedback.textContent, /another record/);
  }
});

test('editing while verification is pending prevents copying stale text', async () => {
  let release;
  const f = fixture({ fetch: (response) => new Promise((resolve) => { release = () => resolve(response); }) });
  const pending = f.click();
  await tick();
  f.fields.body.value = 'Newer unsaved text';
  f.input();
  release();
  await pending;
  assert.equal(f.writes.length, 0);
  assert.match(f.ui.feedback.textContent, /Text changed/);
});

test('leaving the record or switching tabs while verifying prevents copying', async () => {
  for (const change of [(f) => { f.form.isConnected = false; }, (f) => { f.document.visibilityState = 'hidden'; }]) {
    let release;
    const f = fixture({ fetch: (response) => new Promise((resolve) => { release = () => resolve(response); }) });
    const pending = f.click();
    await tick();
    change(f);
    release();
    await pending;
    assert.equal(f.writes.length, 0);
  }
});

test('double clicks issue one verification request', async () => {
  let release;
  const f = fixture({ fetch: (response) => new Promise((resolve) => { release = () => resolve(response); }) });
  const pending = f.click();
  await f.click('subject');
  assert.equal(f.requests.length, 1);
  release();
  await pending;
  assert.equal(f.writes.length, 1);
});

test('expired login, network failure and unavailable clipboard give recoverable messages', async () => {
  const loggedOut = fixture();
  loggedOut.response.headers.get = () => 'text/html';
  await loggedOut.click();
  assert.match(loggedOut.ui.feedback.textContent, /Sign in/);
  assert.equal(loggedOut.writes.length, 0);
  const offline = fixture({ fetch: () => { throw new Error('Offline'); } });
  await offline.click();
  assert.match(offline.ui.feedback.textContent, /Offline/);
  assert.equal(offline.writes.length, 0);
  const noClipboard = fixture({ clipboardFailure: true });
  await noClipboard.click();
  assert.match(noClipboard.ui.feedback.textContent, /Select and copy.*manually/);
  assert.equal(noClipboard.fields.body.value, 'Reviewed message?');
});

test('edits during clipboard completion are not reported as the latest text copied', async () => {
  let release;
  const f = fixture({ clipboard: () => new Promise((resolve) => { release = resolve; }) });
  const pending = f.click();
  await tick();
  f.fields.body.value = 'Later edit';
  release();
  await pending;
  assert.deepEqual(f.writes, ['Reviewed message?']);
  assert.match(f.ui.feedback.textContent, /earlier text was copied/);
});

test('unsaved navigation is guarded and explicit save submits normally', () => {
  const f = fixture();
  assert.equal(f.unload().prevented, false);
  f.fields.body.value = 'Edited';
  f.input();
  assert.equal(f.unload().prevented, true);
  f.submit();
  assert.equal(f.unload().prevented, false);
  const preview = fixture({ unsaved: true });
  assert.equal(preview.unload().prevented, true);
});

test('blocked buttons cannot copy and a page without an editor is inert', async () => {
  const f = fixture({ disabled: true });
  await f.click();
  assert.equal(f.requests.length, 0);
  vm.runInNewContext(source, { document: { querySelector: () => null } });
});
