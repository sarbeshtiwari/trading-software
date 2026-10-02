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
  await page.getByRole('button', { name: 'Market / F&O', exact: true }).click();
  await page.getByLabel('Research group event ID', { exact: true }).fill(process.env.ATS_TEST_NEWS_GROUP);
  await page.getByRole('button', { name: 'Load research job', exact: true }).click();
  await page.getByText(/Research job state: FINISHED/).waitFor();
  await page.getByText(/Outcome: PROVIDER_UNAVAILABLE/).waitFor();
  await page.getByLabel('Research recovery reason', { exact: true }).fill('Owner approves isolated browser fixture retry');
  const completed = page.waitForResponse(response => response.request().method() === 'POST' && response.url().endsWith('/control'));
  await page.getByRole('button', { name: 'Authorize one research retry', exact: true }).click();
  const response = await completed;
  assert.equal(response.status(), 200);
  const receipt = await response.json();
  assert.equal(receipt.state, 'RETRY_AUTHORIZED');
  assert.equal(receipt.retry_authorizations, 1);
  assert.equal(receipt.attempts, 1);
  assert.equal(receipt.billing_verification, 'UNVERIFIED');
  await page.getByText(/Research job state: RETRY_AUTHORIZED/).waitFor();
  assert.equal(await page.getByRole('button', { name: 'Authorize one research retry', exact: true }).isDisabled(), true);
  assert.deepEqual(errors, []);
  process.stdout.write('NEWS_RECOVERY_BROWSER_VERIFIED\n');
} finally {
  await browser.close();
}
