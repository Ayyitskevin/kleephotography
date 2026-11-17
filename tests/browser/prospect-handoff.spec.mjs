import { expect } from '@playwright/test';
import { baseURL } from './playwright.config.mjs';
import { test, scan, enterReview, expectContext } from './helpers.mjs';

const root = '/admin/studio/prospects';

async function expectDetour(page, search, origin) {
  await expect(page).toHaveURL(url => url.pathname.startsWith(`${root}/${origin}/handoff`)
    && url.searchParams.get('review') === 'due' && url.searchParams.get('q') === search
    && url.searchParams.get('review_origin') === origin);
  const back = page.getByRole('link', { name: 'Back to guided review', exact: true });
  const destination = new URL(await back.getAttribute('href'), baseURL);
  expect(destination.pathname).toBe(`${root}/review-work`);
  expect(destination.searchParams.get('q')).toBe(search);
  expect(destination.searchParams.get('prospect_id')).toBe(origin);
}

async function openReview(page, search) {
  await enterReview(page, search);
  await page.getByRole('link', { name: 'Review one at a time', exact: true }).click();
  await expect(page.getByRole('heading', { name: search, exact: true })).toBeVisible();
  const edit = page.getByRole('link', { name: 'Edit prospect', exact: true });
  return new URL(await edit.getAttribute('href'), baseURL).pathname.split('/').at(-1);
}

async function prepareHandoff(page, { title, need, disposition = 'keep' }) {
  await page.getByLabel('Title for a new opportunity').fill(title);
  await page.getByLabel('What photography do they need?').fill(need);
  await page.getByLabel('What happens to the submitted follow-up?').selectOption(disposition);
  if (disposition !== 'keep') {
    await page.getByLabel('Follow-up outcome / reason').fill('Confirmed opportunity reviewed manually.');
  }
  await page.getByRole('checkbox', { name: 'I have confirmed a real photography opportunity' }).check();
}

test('draft copy, manual contact and new opportunity return to the changed review record', async ({ page, context }, info) => {
  const search = `${info.project.name} Handoff Café & Bar`;
  const title = `${info.project.name} Seasonal menu photography`;
  const origin = await openReview(page, search);
  await expect(page.locator('.sr-followup-history > summary')).toContainText('0 contact records');
  await context.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: baseURL });

  const draftPromise = page.waitForEvent('popup');
  await page.getByRole('link', { name: 'Prepare message in a new tab', exact: true }).click();
  const draft = await draftPromise;
  await draft.bringToFront();
  // Exact label text includes nested options; match the control's accessible name.
  await draft.getByRole('combobox', { name: 'Channel', exact: true }).selectOption('email');
  await draft.getByLabel('Personal sentence to include (optional)').fill('Synthetic seasonal menu photography context.');
  await draft.getByRole('button', { name: 'Prepare draft', exact: true }).click();
  await draft.getByRole('button', { name: 'Save draft', exact: true }).click();
  await expect(draft.getByRole('heading', { name: 'Edit saved draft', exact: true })).toBeVisible();
  const text = await draft.getByRole('textbox', { name: 'Email message', exact: true }).inputValue();
  await draft.getByRole('button', { name: 'Copy message', exact: true }).click();
  await expect(draft.locator('[data-draft-feedback]')).toHaveText('Message copied. Nothing sent or logged.');
  expect(await draft.evaluate(() => navigator.clipboard.readText())).toBe(text);
  await scan(draft, info, 'handoff-saved-draft-copy');
  await draft.close();

  await page.bringToFront();
  await page.reload();
  await expect(page.locator('.sr-followup-history > summary')).toContainText('0 contact records');
  await expect(page.getByText(`Review ${search}`, { exact: true }).first()).toBeVisible();
  await page.locator('summary').filter({ hasText: /^Record contact$/ }).click();
  const contact = page.locator('[data-contact-form]');
  await contact.getByLabel('Direction').selectOption('in');
  await contact.getByLabel('Channel').selectOption('email');
  await contact.getByLabel('Outcome', { exact: false }).selectOption('reply_received');
  await contact.getByRole('textbox', { name: 'Contact notes', exact: true }).fill('Synthetic reply already received: interested in menu photography.');
  await contact.getByRole('combobox', { name: 'Update next action', exact: true }).selectOption('keep');
  await contact.getByRole('button', { name: 'Record contact', exact: true }).click();
  await expectContext(page, search, origin);
  await expect(page.locator('.sr-followup-history > summary')).toContainText('1 contact record');

  await page.getByRole('link', { name: 'Start opportunity', exact: true }).click();
  await expectDetour(page, search, origin);
  await expect(page.getByRole('heading', { level: 1 })).toBeFocused();
  await page.getByRole('combobox', { name: 'Client', exact: true }).selectOption('new');
  await page.getByRole('button', { name: 'Choose client', exact: true }).click();
  await expectDetour(page, search, origin);
  await prepareHandoff(page, { title, need: 'Synthetic menu and dining room photography.', disposition: 'complete' });
  await scan(page, info, 'handoff-new-form');
  await page.getByRole('button', { name: 'Review handoff', exact: true }).click();
  await expectDetour(page, search, origin);
  await page.getByRole('button', { name: 'Back to edit', exact: true }).click();
  await expectDetour(page, search, origin);
  await expect(page.getByLabel('What photography do they need?')).toHaveValue('Synthetic menu and dining room photography.');
  await expect(page.getByLabel('What happens to the submitted follow-up?')).toHaveValue('complete');
  await page.getByRole('button', { name: 'Review handoff', exact: true }).click();
  await scan(page, info, 'handoff-new-review');

  // Retain the actual rendered submission, including its signed review and operation key.
  // Replaying it below models a lost-response retry rather than building a new command.
  const commit = page.locator('form').filter({ has: page.getByRole('button', { name: 'Create opportunity', exact: true }) });
  const retry = await commit.evaluate(form => ({
    url: form.action, values: Object.fromEntries(new FormData(form)),
  }));
  await page.getByRole('button', { name: 'Create opportunity', exact: true }).click();
  await expectContext(page, search, origin);
  await expect(page.getByRole('heading', { name: `Linked to ${title}`, exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'No eligible prospects in this queue', exact: true })).toBeVisible();
  await expect(page.locator('[data-contact-form], [data-followup-command]')).toHaveCount(0);
  await expect(page.getByRole('link', { name: 'Start opportunity', exact: true })).toHaveCount(0);
  const projectURL = await page.getByRole('link', { name: 'Open project', exact: true }).getAttribute('href');
  const repeated = await context.request.post(retry.url, {
    form: retry.values, headers: { Origin: baseURL }, maxRedirects: 0,
  });
  expect(repeated.status()).toBe(303);
  await page.goto(repeated.headers().location);
  await expectContext(page, search, origin);
  await expect(page.getByRole('link', { name: 'Open project', exact: true })).toHaveAttribute('href', projectURL);
  await scan(page, info, 'handoff-saved-changed-review');
  await page.getByRole('link', { name: 'View handoff', exact: true }).click();
  await expectDetour(page, search, origin);
  await expect(page.getByText(/Follow-up decision: Completed explicitly/)).toBeVisible();
  await page.getByRole('link', { name: 'Back to guided review', exact: true }).click();
  await expectContext(page, search, origin);

  // Inspect the persisted history after both submissions: one manual reply and one action.
  await page.goto(`${root}/${origin}`);
  await expect(page.locator('.sr-followup-history > summary')).toContainText('1 contact record · 1 action');
  await page.locator('.sr-followup-history > summary').click();
  const actions = page.getByRole('region', { name: 'Action history', exact: true });
  await expect(actions.getByText('Completed', { exact: true })).toHaveCount(1);
  await expect(page.getByText('Synthetic reply already received: interested in menu photography.', { exact: true })).toHaveCount(1);
});

test('existing client and project retain context through validation and stale review recovery', async ({ page, context }, info) => {
  const search = `${info.project.name} Reuse Handoff Bistro`;
  const projectTitle = `${info.project.name} Existing opportunity`;
  const origin = await openReview(page, search);
  const researchURL = await page.getByRole('link', { name: 'Edit prospect', exact: true }).getAttribute('href');
  await page.getByRole('link', { name: 'Start opportunity', exact: true }).click();
  await page.getByRole('link', {
    name: `Use ${info.project.name} Existing Studio · ${info.project.name} Existing contact`, exact: true,
  }).click();
  await expectDetour(page, search, origin);
  await page.getByRole('combobox', { name: 'Project', exact: true }).selectOption({ label: `Link ${projectTitle} · consultation_call` });
  await prepareHandoff(page, { title: 'This must not rename the existing project', need: '   ' });
  await page.getByRole('button', { name: 'Review handoff', exact: true }).click();
  await expectDetour(page, search, origin);
  await expect(page.getByRole('alert')).toBeFocused();
  await expect(page.getByRole('alert')).toContainText('Enter the photography need');
  await expect(page.getByLabel('Title for a new opportunity')).toHaveValue('This must not rename the existing project');
  await page.getByLabel('What photography do they need?').fill('Synthetic reuse brief retained after validation.');
  await page.getByRole('button', { name: 'Review handoff', exact: true }).click();
  await expect(page.getByText(/Its details and stage stay as they are/)).toBeVisible();

  const current = await context.newPage();
  await current.goto(researchURL);
  await current.getByRole('textbox', { name: 'Research notes', exact: true }).fill('New synthetic research arrived after the signed review.');
  await current.getByRole('button', { name: 'Save changes', exact: true }).click();
  await expectContext(current, search, origin);
  await page.getByRole('button', { name: 'Link existing project', exact: true }).click();
  await expectDetour(page, search, origin);
  await expect(page.getByRole('alert')).toBeFocused();
  await expect(page.getByRole('alert')).toContainText('changed after review');
  await expect(page.getByLabel('What photography do they need?')).toHaveValue('Synthetic reuse brief retained after validation.');
  await expect(page.getByRole('heading', { name: `Linked to ${projectTitle}`, exact: true })).toHaveCount(0);
  await scan(page, info, 'handoff-stale-review');
  const reopenedPromise = page.waitForEvent('popup');
  await page.getByRole('link', { name: 'Reopen the current handoff in a new tab', exact: true }).click();
  const reopened = await reopenedPromise;
  await expectDetour(reopened, search, origin);
  await reopened.getByRole('link', { name: 'Back to guided review', exact: true }).click();
  await expectContext(reopened, search, origin);
  await expect(reopened.getByText('New synthetic research arrived after the signed review.', { exact: true })).toBeVisible();
  await reopened.close();
  await current.close();

  await page.getByRole('button', { name: 'Review handoff', exact: true }).click();
  await expectDetour(page, search, origin);
  await page.getByRole('button', { name: 'Link existing project', exact: true }).click();
  await expectContext(page, search, origin);
  await expect(page.getByRole('heading', { name: `Linked to ${projectTitle}`, exact: true })).toBeVisible();
  await expect(page.getByText(`Review ${search}`, { exact: true }).first()).toBeVisible();
  await expect(page.locator('.sr-followup-history > summary')).toContainText('0 contact records · 1 action');
  await scan(page, info, 'handoff-existing-keep-review');
  await page.getByRole('link', { name: 'Open project', exact: true }).click();
  await expect(page.getByRole('heading', { level: 1, name: projectTitle, exact: true })).toBeVisible();
  await expect(page.locator('.pgtop-sub')).toContainText('consultation_call');
  await expect(page.locator('.pgtop-sub')).toContainText('2037-05-01');
});

test('a hold arriving after handoff review refuses the stale save and returns without work forms', async ({ page, context }, info) => {
  const search = `${info.project.name} Held Handoff Bistro`;
  const origin = await openReview(page, search);
  const preferencesURL = await page.getByRole('link', { name: 'Manage this prospect’s outreach' }).getAttribute('href');
  await page.getByRole('link', { name: 'Start opportunity', exact: true }).click();
  await page.getByRole('combobox', { name: 'Client', exact: true }).selectOption('new');
  await page.getByRole('button', { name: 'Choose client', exact: true }).click();
  await prepareHandoff(page, { title: `${info.project.name} Held proposal`, need: 'Synthetic opportunity before a newly recorded hold.' });
  await page.getByRole('button', { name: 'Review handoff', exact: true }).click();

  const current = await context.newPage();
  await current.goto(preferencesURL);
  await current.getByLabel('Outbound outreach status').selectOption('stopped');
  await current.getByLabel('Reason for this change').fill('Synthetic stop request arrived after opportunity review.');
  await current.getByRole('button', { name: 'Save outreach preferences', exact: true }).click();
  await expectContext(current, search, origin);
  await page.getByRole('button', { name: 'Create opportunity', exact: true }).click();
  await expectDetour(page, search, origin);
  await expect(page.getByRole('alert')).toBeFocused();
  await expect(page.getByRole('alert')).toContainText('changed after review');
  await page.getByRole('link', { name: 'Back to guided review', exact: true }).click();
  await expectContext(page, search, origin);
  await expect(page.getByRole('heading', { name: 'No eligible prospects in this queue', exact: true })).toBeVisible();
  await expect(page.locator('[data-contact-form], [data-followup-command]')).toHaveCount(0);
  await expect(page.getByRole('link', { name: 'Start opportunity', exact: true })).toHaveCount(0);
  await expect(page.getByRole('link', { name: 'Open project', exact: true })).toHaveCount(0);
  await scan(page, info, 'handoff-held-review');
  await current.close();
});

// Match the keyboard-review.spec.mjs contract: inspect only; no DOM focus or value writes.
async function keyboardReach(page, target, key = 'Tab') {
  await expect(target).toBeVisible();
  for (let index = 0; index < 90; index++) {
    if (await target.evaluate(element => element === document.activeElement)) return;
    await page.keyboard.press(key);
  }
  throw new Error(`Could not reach ${target} using ${key}`);
}

async function keyboardFocusClear(target) {
  await expect(target).toBeFocused();
  await expect.poll(() => target.evaluate(element => {
    const rect = element.getBoundingClientRect();
    const x = rect.x + rect.width / 2, y = rect.y + rect.height / 2;
    const view = window.visualViewport;
    const left = view ? view.offsetLeft : 0, top = view ? view.offsetTop : 0;
    const width = view ? view.width : innerWidth, height = view ? view.height : innerHeight;
    const hit = document.elementFromPoint(x, y);
    return x >= left && y >= top && x < left + width && y < top + height
      && (hit === element || element.contains(hit));
  })).toBe(true);
}

async function keyboardActivate(page, target, key = 'Tab') {
  await keyboardReach(page, target, key);
  await keyboardFocusClear(target);
  await page.keyboard.press('Enter');
}

async function keyboardReplace(page, target, value) {
  await keyboardReach(page, target);
  await keyboardFocusClear(target);
  await page.keyboard.press('Control+a');
  await page.keyboard.type(value);
}

async function keyboardChoose(page, target, value) {
  await keyboardReach(page, target);
  await keyboardFocusClear(target);
  const index = await target.evaluate((element, wanted) =>
    [...element.options].findIndex(option => option.value === wanted), value);
  expect(index).toBeGreaterThanOrEqual(0);
  await page.keyboard.press('Home');
  for (let step = 0; step < index; step++) await page.keyboard.press('ArrowDown');
  await expect(target).toHaveValue(value);
}

test('keyboard-only handoff validates, edits, saves and returns to its review record', async ({ page }, info) => {
  const search = `${info.project.name} Keyboard Handoff Bistro`;
  const title = `${info.project.name} Keyboard opportunity`;
  await page.goto('/admin/login');
  await keyboardReplace(page, page.getByLabel('Password', { exact: true }), 'test-pw');
  await keyboardActivate(page, page.getByRole('button', { name: 'Take the deck', exact: true }));
  await expect(page).toHaveURL(/\/admin\/home$/);
  await page.waitForLoadState('domcontentloaded');
  await page.keyboard.press('Control+k');
  await expect(page.getByLabel('Search pages and actions')).toBeFocused();
  await page.keyboard.type('Daily follow-ups');
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/\/prospects\/follow-ups$/);
  await keyboardReplace(page, page.getByLabel('Find a prospect'), search);
  await keyboardActivate(page, page.getByRole('button', { name: 'Search', exact: true }));
  await keyboardActivate(page, page.getByRole('link', { name: 'Review one at a time', exact: true }));
  await keyboardFocusClear(page.getByRole('heading', { name: search, exact: true }));
  const edit = page.getByRole('link', { name: 'Edit prospect', exact: true });
  const origin = new URL(await edit.getAttribute('href'), baseURL).pathname.split('/').at(-1);

  await keyboardActivate(page, page.getByRole('link', { name: 'Start opportunity', exact: true }));
  await expectDetour(page, search, origin);
  await keyboardFocusClear(page.getByRole('heading', { level: 1 }));
  await keyboardChoose(page, page.getByRole('combobox', { name: 'Client', exact: true }), 'new');
  await keyboardActivate(page, page.getByRole('button', { name: 'Choose client', exact: true }));
  await expectDetour(page, search, origin);
  await keyboardReplace(page, page.getByLabel('Title for a new opportunity'), title);
  // Nonempty whitespace passes the native required check and reaches real server validation.
  await keyboardReplace(page, page.getByLabel('What photography do they need?'), '   ');
  await keyboardChoose(page, page.getByLabel('What happens to the submitted follow-up?'), 'keep');
  const confirmation = page.getByRole('checkbox', { name: 'I have confirmed a real photography opportunity' });
  await keyboardReach(page, confirmation);
  await keyboardFocusClear(confirmation);
  await page.keyboard.press('Space');
  await expect(confirmation).toBeChecked();
  await keyboardActivate(page, page.getByRole('button', { name: 'Review handoff', exact: true }));
  await expectDetour(page, search, origin);
  await keyboardFocusClear(page.getByRole('alert'));
  await expect(page.getByRole('alert')).toContainText('Enter the photography need');
  await expect(page.getByLabel('Title for a new opportunity')).toHaveValue(title);
  await scan(page, info, 'keyboard-handoff-validation');

  await keyboardReplace(page, page.getByLabel('What photography do they need?'), 'Synthetic brief entered using only the keyboard.');
  await keyboardActivate(page, page.getByRole('button', { name: 'Review handoff', exact: true }));
  await expectDetour(page, search, origin);
  await expect(page.getByRole('heading', { name: 'Review before creating or linking', exact: true })).toBeVisible();
  await keyboardActivate(page, page.getByRole('button', { name: 'Back to edit', exact: true }), 'Shift+Tab');
  await expectDetour(page, search, origin);
  await expect(page.getByLabel('What photography do they need?')).toHaveValue('Synthetic brief entered using only the keyboard.');
  await expect(page.getByLabel('What happens to the submitted follow-up?')).toHaveValue('keep');
  await keyboardActivate(page, page.getByRole('button', { name: 'Review handoff', exact: true }));
  await expectDetour(page, search, origin);
  await expect(page.getByRole('heading', { name: 'Review before creating or linking', exact: true })).toBeVisible();
  await scan(page, info, 'keyboard-handoff-review');
  await keyboardActivate(page, page.getByRole('button', { name: 'Create opportunity', exact: true }));
  await expectContext(page, search, origin);
  await keyboardFocusClear(page.getByRole('heading', { name: search, exact: true }));
  await expect(page.getByRole('heading', { name: `Linked to ${title}`, exact: true })).toBeVisible();
  await expect(page.getByText(`Review ${search}`, { exact: true }).first()).toBeVisible();
  await keyboardActivate(page, page.getByRole('link', { name: 'View handoff', exact: true }));
  await expectDetour(page, search, origin);
  await expect(page.getByText(/Follow-up decision: Kept on the prospect/)).toBeVisible();
  await keyboardActivate(page, page.getByRole('link', { name: 'Back to guided review', exact: true }));
  await expectContext(page, search, origin);
  await keyboardFocusClear(page.getByRole('heading', { name: search, exact: true }));
  await scan(page, info, 'keyboard-handoff-return');
});
