import { expect } from '@playwright/test';
import { baseURL } from './playwright.config.mjs';
import { test, scan, enterReview, expectContext } from './helpers.mjs';

test('review, edit, record a manual outcome, complete and hold', async ({ page }, info) => {
  const search = `${info.project.name} Café & Bar`;
  await enterReview(page, search);
  await scan(page, info, 'queue');
  await page.getByRole('link', { name: 'Review one at a time' }).click();
  await expect(page.getByRole('heading', { name: `${search} A`, exact: true })).toBeVisible();
  const edit = page.getByRole('link', { name: 'Edit prospect', exact: true });
  const origin = new URL(await edit.getAttribute('href'), baseURL).pathname.split('/').at(-1);
  // Native summary keyboard activation, with its disclosed controls in the axe scan.
  const command = page.getByText('Complete, reschedule or cancel', { exact: true });
  await command.focus();
  await page.keyboard.press('Enter');
  await page.getByLabel('What happened to this action?').selectOption('reschedule');
  await expect(page.locator('[data-followup-command]').getByLabel('New due date', { exact: true })).toBeVisible();
  await scan(page, info, 'review-expanded');

  await edit.click();
  await page.getByLabel('Research notes', { exact: true }).fill('Synthetic menu photography research.');
  await scan(page, info, 'research');
  await page.getByRole('button', { name: 'Save changes', exact: true }).click();
  await expectContext(page, search, origin);
  await expect(page.getByText('Synthetic menu photography research.', { exact: true })).toBeVisible();
  await page.getByRole('link', { name: 'Manage this prospect’s outreach' }).click();
  await page.getByRole('checkbox', { name: 'Email', exact: true }).check();
  await page.getByLabel('Reason for this change').fill('Synthetic exclusion; calls remain eligible.');
  await scan(page, info, 'preferences');
  await page.getByRole('button', { name: 'Save outreach preferences' }).click();
  await expectContext(page, search, origin);
  await expect(page.getByText('Excluded channels: email', { exact: true })).toBeVisible();

  await page.locator('summary').filter({ hasText: /^Record contact$/ }).click();
  await page.getByLabel('Channel', { exact: false }).selectOption('call');
  await page.getByLabel('Outcome', { exact: false }).selectOption('voicemail');
  await page.getByLabel('Contact notes', { exact: true }).fill('Synthetic voicemail already left.');
  await scan(page, info, 'manual-contact');
  await page.getByRole('button', { name: 'Record contact', exact: true }).click();
  await expectContext(page, search, origin);
  await page.locator('summary').filter({ hasText: /^History/ }).click();
  await expect(page.getByText('Synthetic voicemail already left.', { exact: true })).toBeVisible();
  await expect(page.getByText(`Review ${search} A`, { exact: true }).first()).toBeVisible();

  await page.getByText('Complete, reschedule or cancel', { exact: true }).click();
  await page.getByLabel('What happened to this action?').selectOption('complete');
  await page.locator('[data-followup-command]').getByLabel('Completion note / reason')
    .fill('Manually reviewed and completed.');
  await page.getByRole('button', { name: 'Update next action', exact: true }).click();
  await expectContext(page, search, origin);
  await expect(page.getByRole('heading', { name: 'This prospect is no longer in this queue' })).toBeVisible();
  await scan(page, info, 'changed-eligibility');
  await page.getByRole('link', { name: 'Review next eligible prospect' }).click();
  await expect(page.getByRole('heading', { name: `${search} B`, exact: true })).toBeVisible();
  await page.getByRole('link', { name: 'Manage this prospect’s outreach' }).click();
  await page.getByLabel('Outbound outreach status').selectOption('stopped');
  await page.getByLabel('Reason for this change').fill('Synthetic stop request.');
  await page.getByRole('button', { name: 'Save outreach preferences' }).click();
  await expect(page.getByRole('heading', { name: 'No eligible prospects in this queue' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Record contact', exact: true })).toHaveCount(0);
  await scan(page, info, 'empty-queue');
  await page.getByRole('link', { name: 'Back to queue', exact: true }).click();
  await page.getByRole('link', { name: 'On hold', exact: true }).click();
  await expect(page.getByRole('heading', { name: `${search} B`, exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: `${search} A`, exact: true })).toHaveCount(0);
});

test('preference validation and stale forms preserve review context', async ({ page, context }, info) => {
  const search = `${info.project.name} Stale Bistro`;
  await enterReview(page, search);
  await page.getByRole('link', { name: 'Review one at a time' }).click();
  await page.getByRole('link', { name: 'Manage this prospect’s outreach' }).click();
  await page.getByRole('checkbox', { name: 'Email', exact: true }).check();
  await page.getByLabel('Preferred channel (advisory)').selectOption('email');
  await page.getByLabel('Reason for this change').fill('Synthetic conflicting preferences.');
  await page.getByRole('button', { name: 'Save outreach preferences' }).click();
  await expect(page.getByRole('alert')).toBeFocused();
  await expect(page.getByRole('alert')).toContainText('A preferred channel cannot also be excluded');
  await expect(page.getByLabel('Reason for this change')).toHaveValue('Synthetic conflicting preferences.');
  await scan(page, info, 'validation-error');

  const current = await context.newPage();
  await current.goto(page.url());
  await current.getByLabel('Reason for this change').fill('Another tab reviewed this record.');
  await current.getByRole('button', { name: 'Save outreach preferences' }).click();
  await expectContext(current, search);
  await page.getByLabel('Preferred channel (advisory)').selectOption('');
  await page.getByRole('button', { name: 'Save outreach preferences' }).click();
  await expect(page.getByRole('alert')).toBeFocused();
  await expect(page.getByRole('alert')).toContainText('Preferences or linked client records changed');
  await expect(page.getByRole('checkbox', { name: 'Email', exact: true })).toBeChecked();
  await scan(page, info, 'stale-form');
  const popupPromise = page.waitForEvent('popup');
  await page.getByRole('link', { name: 'Reopen current preferences in a new tab' }).click();
  const reopened = await popupPromise;
  await expect(reopened.getByRole('checkbox', { name: 'Email', exact: true })).not.toBeChecked();
  await reopened.getByRole('link', { name: 'Back to guided review', exact: true }).click();
  await expectContext(reopened, search);
  await expect(reopened.getByRole('heading', { name: search, exact: true })).toBeVisible();
  await current.close();
  await reopened.close();
});
