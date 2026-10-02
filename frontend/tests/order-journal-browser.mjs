import assert from 'node:assert/strict';
import { chromium } from 'playwright-core';

const browser = await chromium.launch({ channel: process.env.ATS_BROWSER_CHANNEL || 'msedge', headless: true });
try {
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto(process.argv[2]);
  await page.getByLabel('Username', { exact: true }).fill('owner');
  await page.getByLabel('Password', { exact: true }).fill(process.env.ATS_TEST_OWNER_PASSWORD);
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await page.getByRole('button', { name: 'Journal', exact: true }).click();
  await page.getByLabel('Journal kind', { exact: true }).selectOption('ORDER_ACTION');
  const filtered = page.waitForResponse(response => response.url().includes('/journal?') && response.url().includes('kind=ORDER_ACTION'));
  await page.getByRole('button', { name: 'Apply journal filters', exact: true }).click();
  const response = await filtered;
  assert.equal(response.status(), 200);
  const entries = (await response.json()).entries;
  assert.equal(entries.length, 1);
  assert.equal(entries[0].net_pnl, null);
  assert.equal(entries[0].outcome, 'CANCELLED');
  await page.getByRole('button', { name: `Inspect ${entries[0].id}`, exact: true }).click();
  await page.locator('summary').filter({ hasText: /^entry$/ }).click();
  assert.match(await page.locator('main').innerText(), /ORDER_ACTION/);
  assert.equal(await page.getByText('Append contextual correction', { exact: true }).count(), 0);
  await page.getByRole('button', { name: 'Monitoring', exact: true }).click();
  await page.getByText('Order hygiene and protection evidence', { exact: true }).click();
  await page.getByText(/SESSION_CUTOFF \/ CANCELLED/).waitFor();
  assert.match(await page.locator('main').innerText(), /filled quantity 100/);
  assert.deepEqual(errors, []);
  process.stdout.write('ORDER_JOURNAL_VERIFIED\n');
} finally { await browser.close(); }
