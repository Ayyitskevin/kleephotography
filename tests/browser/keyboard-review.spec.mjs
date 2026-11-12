import { expect } from '@playwright/test';
import { test, scan } from './helpers.mjs';

// These helpers only inspect the DOM; all focus movement/input is real keyboard input.
async function tabTo(page, target, key = 'Tab') {
  await expect(target).toBeVisible();
  for (let i = 0; i < 90; i++) {
    if (await target.evaluate(el => el === document.activeElement)) return;
    await page.keyboard.press(key);
  }
  throw new Error(`Could not reach ${target} using ${key}`);
}

async function focusedAndClear(target) {
  await expect(target).toBeFocused();
  await expect.poll(() => target.evaluate(el => {
    const rect = el.getBoundingClientRect();
    const x = rect.x + rect.width / 2, y = rect.y + rect.height / 2;
    const hit = document.elementFromPoint(x, y);
    return x >= 0 && y >= 0 && x < innerWidth && y < innerHeight
      && (hit === el || el.contains(hit));
  })).toBe(true);
}

async function activate(page, target) {
  await tabTo(page, target);
  await focusedAndClear(target);
  await page.keyboard.press('Enter');
}

async function type(page, target, text) {
  await tabTo(page, target);
  await focusedAndClear(target);
  await page.keyboard.type(text);
}

async function choose(page, target, value) {
  await tabTo(page, target);
  await focusedAndClear(target);
  const index = await target.evaluate((el, wanted) =>
    [...el.options].findIndex(option => option.value === wanted), value);
  expect(index).toBeGreaterThanOrEqual(0);
  await page.keyboard.press('Home');
  for (let i = 0; i < index; i++) await page.keyboard.press('ArrowDown');
  await expect(target).toHaveValue(value);
}

async function login(page) {
  await page.goto('/admin/login');
  await type(page, page.getByLabel('Password', { exact: true }), 'test-pw');
  await activate(page, page.getByRole('button', { name: 'Take the deck' }));
  await expect(page).toHaveURL(/\/admin\/home$/);
}

async function dailyQueue(page) {
  await page.keyboard.press('Control+k');
  await expect(page.getByLabel('Search pages and actions')).toBeFocused();
  await page.keyboard.type('Daily follow-ups');
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/\/prospects\/follow-ups$/);
}

test('navigation dialogs contain and restore keyboard focus', async ({ page }, info) => {
  await login(page);
  const more = page.locator('#sr-more');
  const moreButton = page.locator('#sr-more-open');
  await activate(page, moreButton);
  await focusedAndClear(page.getByRole('button', { name: 'Close the index' }));
  await page.keyboard.press('Shift+Tab');
  await focusedAndClear(more.getByRole('link', { name: 'Reference', exact: true }));
  await page.keyboard.press('Tab');
  await expect(page.getByRole('button', { name: 'Close the index' })).toBeFocused();
  await scan(page, info, 'keyboard-index');
  await page.keyboard.press('Escape');
  await expect(more).not.toBeVisible();
  await expect(moreButton).toBeFocused();
  await expect(moreButton).toHaveAttribute('aria-expanded', 'false');
  await page.keyboard.press('Enter');
  await page.keyboard.press('Enter'); // Native close button, not a global Enter shortcut.
  await expect(moreButton).toBeFocused();

  const commandButton = page.locator('#ns-cmd-open');
  await activate(page, commandButton);
  const search = page.getByLabel('Search pages and actions');
  await expect(search).toBeFocused();
  await page.keyboard.type('prospect');
  await page.keyboard.press('Shift+Tab');
  await expect(page.locator('#ns-pal .ns-pal-row').last()).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(search).toBeFocused();
  await scan(page, info, 'keyboard-command');
  await page.keyboard.press('Escape');
  await expect(commandButton).toBeFocused();
  await page.keyboard.press('Enter');
  await page.keyboard.type('prospect');
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('ArrowDown');
  await expect(page.locator('#ns-pal .ns-pal-row').filter({ hasText: 'Outbound prospects' })).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/\/studio\/prospects$/);

  await dailyQueue(page);
  const find = page.getByLabel('Find a prospect');
  await tabTo(page, find);
  await page.keyboard.press('Control+k');
  await expect(search).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(page.getByRole('button', { name: 'ESC — Close command menu' })).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(find).toBeFocused();
});

test('keyboard-only research, validation, contact and next action', async ({ page }, info) => {
  const business = `${info.project.name} Keyboard Bistro`;
  await login(page);
  await dailyQueue(page);
  await type(page, page.getByLabel('Find a prospect'), business);
  await activate(page, page.getByRole('button', { name: 'Search', exact: true }));
  await activate(page, page.getByRole('link', { name: 'Review one at a time' }));
  const heading = page.getByRole('heading', { name: business, exact: true });
  await focusedAndClear(heading);
  await activate(page, page.getByRole('link', { name: 'Edit prospect', exact: true }));
  await focusedAndClear(page.getByRole('heading', { name: 'Research and qualification' }));
  await type(page, page.getByLabel('Research notes', { exact: true }), 'Keyboard-only research.');
  await activate(page, page.getByRole('button', { name: 'Save changes', exact: true }));
  await focusedAndClear(heading);
  await activate(page, page.getByRole('link', { name: 'Manage this prospect’s outreach' }));
  await focusedAndClear(page.getByRole('heading', { name: `Controls recorded on ${business}` }));
  await tabTo(page, page.getByRole('checkbox', { name: 'Email', exact: true }));
  await page.keyboard.press('Space');
  const preferred = page.getByLabel('Preferred channel (advisory)');
  await choose(page, preferred, 'email');
  await type(page, page.getByLabel('Reason for this change'), 'Synthetic conflicting choice.');
  await activate(page, page.getByRole('button', { name: 'Save outreach preferences' }));
  await focusedAndClear(page.getByRole('alert'));
  await expect(page.getByRole('alert')).toContainText('A preferred channel cannot also be excluded');
  await scan(page, info, 'keyboard-error');
  await choose(page, preferred, '');
  await activate(page, page.getByRole('button', { name: 'Save outreach preferences' }));
  await focusedAndClear(heading);

  await activate(page, page.locator('summary').filter({ hasText: /^Record contact$/ }));
  await choose(page, page.getByLabel('Channel', { exact: false }), 'call');
  await choose(page, page.getByLabel('Outcome', { exact: false }), 'voicemail');
  await type(page, page.getByLabel('Contact notes', { exact: true }), 'Synthetic keyboard contact.');
  await activate(page, page.getByRole('button', { name: 'Record contact', exact: true }));
  await focusedAndClear(heading);
  await activate(page, page.getByText('Complete, reschedule or cancel', { exact: true }));
  await choose(page, page.getByLabel('What happened to this action?'), 'complete');
  await type(page, page.locator('[data-followup-command]').getByLabel('Completion note / reason'),
    'Manually completed from the keyboard.');
  await activate(page, page.getByRole('button', { name: 'Update next action', exact: true }));
  await focusedAndClear(page.getByRole('heading', { name: 'No eligible prospects in this queue' }));
  await activate(page, page.getByRole('link', { name: 'Back to queue', exact: true }));
  await activate(page, page.getByRole('link', { name: 'Needs next step', exact: true }));
  await activate(page, page.getByRole('link', { name: 'Review one at a time' }));
  await focusedAndClear(heading);
  await type(page, page.locator('input[name="title"]'), 'Call about the seasonal menu');
  await activate(page, page.getByRole('button', { name: 'Today 2037-04-15', exact: true }));
  await expect(page.locator('#schedule-due-date')).toHaveValue('2037-04-15');
  await activate(page, page.getByRole('button', { name: 'Schedule next action' }));
  await focusedAndClear(page.getByRole('heading', { name: 'No eligible prospects in this queue' }));
  await scan(page, info, 'keyboard-scheduled');
  await activate(page, page.getByRole('link', { name: 'Back to queue', exact: true }));
  await activate(page, page.getByRole('link', { name: 'Due & overdue', exact: true }));
  await expect(page.getByText('Call about the seasonal menu', { exact: true })).toBeVisible();
});
