import { test as base, expect } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { baseURL } from './playwright.config.mjs';

const root = '/admin/studio/prospects';
const test = base.extend({
  page: async ({ context, page }, use) => {
    const unexpected = [];
    await context.route('**/*', async route => {
      if (new URL(route.request().url()).origin !== baseURL) {
        unexpected.push(`External request: ${route.request().url()}`);
        await route.abort();
      } else {
        await route.continue();
      }
    });
    const observe = tab => {
      tab.on('pageerror', error => unexpected.push(error.message));
      tab.on('response', response => {
        if (response.status() >= 500) unexpected.push(`${response.status()} ${response.url()}`);
      });
    };
    observe(page);
    context.on('page', observe);
    await use(page);
    expect(unexpected, 'No external transports, browser exceptions or server errors').toEqual([]);
  },
});

async function scan(page, info, name) {
  await page.evaluate(() => document.fonts.ready);
  const viewport = await page.evaluate(width => ({
    expectedWidth: width, innerWidth, clientWidth: document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth,
    visualWidth: visualViewport.width, visualScale: visualViewport.scale,
    overflowing: [...document.querySelectorAll('body *')].flatMap(element => {
      const rect = element.getBoundingClientRect();
      if (!rect.width || rect.right <= width + 1) return [];
      const style = getComputedStyle(element);
      return [{ tag: element.tagName, class: element.className, text: element.textContent.trim().slice(0, 90),
        x: rect.x, right: rect.right, width: rect.width, minWidth: style.minWidth,
        whiteSpace: style.whiteSpace, display: style.display }];
    }).slice(0, 30),
  }), page.viewportSize().width);
  await info.attach(`${name}-viewport`, {
    body: JSON.stringify(viewport, null, 2), contentType: 'application/json',
  });
  const results = await new AxeBuilder({ page })
    .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'])
    .analyze();
  await info.attach(`${name}-axe`, {
    body: JSON.stringify(results, null, 2), contentType: 'application/json',
  });
  await info.attach(`${name}-screenshot`, {
    body: await page.screenshot({ animations: 'disabled' }), contentType: 'image/png',
  });
  expect.soft(results.violations.map(v => ({
    id: v.id, impact: v.impact,
    nodes: v.nodes.map(n => ({ target: n.target, summary: n.failureSummary })),
  })), `${name}: WCAG 2.1 A/AA automated checks`).toEqual([]);
  expect.soft(viewport.scrollWidth, `${name}: no horizontal page overflow`)
    .toBeLessThanOrEqual(viewport.expectedWidth + 1);
}

async function enterReview(page, search) {
  await page.goto('/admin/login');
  await page.getByLabel('Password', { exact: true }).fill('test-pw');
  await page.getByRole('button', { name: 'Take the deck' }).click();
  await expect(page).toHaveURL(/\/admin\/home$/);
  await page.goto(`${root}/follow-ups`);
  await page.getByLabel('Find a prospect').fill(search);
  await page.getByRole('button', { name: 'Search', exact: true }).click();
}

async function expectContext(page, search, origin) {
  await expect(page).toHaveURL(url => url.pathname === `${root}/review-work`
    && url.searchParams.get('review') === 'due' && url.searchParams.get('q') === search
    && (!origin || url.searchParams.get('prospect_id') === origin));
}

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
