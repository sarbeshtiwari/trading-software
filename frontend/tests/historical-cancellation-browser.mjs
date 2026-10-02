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
  await page.getByRole('button', { name: 'Backtests', exact: true }).click();
  await page.getByLabel('Historical plan', { exact: true }).selectOption('fixture01');
  await page.getByLabel('Research reason', { exact: true }).fill('Isolated cancellation browser research');
  await page.getByLabel('Type RUN HISTORICAL PAPER', { exact: true }).fill('RUN HISTORICAL PAPER');
  await page.getByRole('button', { name: 'Launch isolated PAPER research', exact: true }).click();
  await page.getByRole('button', { name: 'Cancel fixture01', exact: true }).click();
  await page.getByLabel('Cancellation reason', { exact: true }).fill('Owner stops browser fixture research');
  await page.getByLabel('Type CANCEL HISTORICAL RESEARCH', { exact: true }).fill('CANCEL HISTORICAL RESEARCH');
  const response = page.waitForResponse(item => item.url().endsWith('/historical-jobs/fixture01/cancel'));
  await page.getByRole('button', { name: 'Confirm research cancellation', exact: true }).click();
  const cancelled = await response;
  assert.equal(cancelled.status(), 200, await cancelled.text());
  assert.equal((await cancelled.json()).status, 'INTERRUPTED');
  await page.getByText(/fixture01: INTERRUPTED.*TERMINAL.*REPORT NOT PUBLISHED.*OWNER_CANCELLED/).waitFor();
  await page.reload();
  await page.getByRole('button', { name: 'Backtests', exact: true }).click();
  await page.getByText(/fixture01: INTERRUPTED.*TERMINAL.*REPORT NOT PUBLISHED.*OWNER_CANCELLED/).waitFor();
  assert.equal(await page.getByRole('button', { name: 'Cancel fixture01', exact: true }).count(), 0);
  assert.deepEqual(errors, []);
  console.log('HISTORICAL_CANCELLATION_VERIFIED');
} finally {
  await browser.close();
}
