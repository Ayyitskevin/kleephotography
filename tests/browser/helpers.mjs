import { test as base, expect } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { baseURL } from './playwright.config.mjs';

const root = '/admin/studio/prospects';
export const test = base.extend({
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

export async function scan(page, info, name) {
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

export async function enterReview(page, search) {
  await page.goto('/admin/login');
  await page.getByLabel('Password', { exact: true }).fill('test-pw');
  await page.getByRole('button', { name: 'Take the deck' }).click();
  await expect(page).toHaveURL(/\/admin\/home$/);
  await page.goto(`${root}/follow-ups`);
  await page.getByLabel('Find a prospect').fill(search);
  await page.getByRole('button', { name: 'Search', exact: true }).click();
}

export async function expectContext(page, search, origin) {
  await expect(page).toHaveURL(url => url.pathname === `${root}/review-work`
    && url.searchParams.get('review') === 'due' && url.searchParams.get('q') === search
    && (!origin || url.searchParams.get('prospect_id') === origin));
}
