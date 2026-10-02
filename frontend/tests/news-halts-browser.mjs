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
  await page.getByRole('button', { name: 'Risk', exact: true }).click();
  await page.getByRole('heading', { name: 'ins-test — ENTRIES BLOCKED — NEWS HALT', exact: true }).waitFor();
  const release = page.getByRole('button', { name: 'Release reviewed halt for ins-test', exact: true });
  assert.equal(await release.isDisabled(), true);
  await page.getByLabel('News halt review reason', { exact: true }).fill('Owner reviewed isolated held-position news evidence');
  await page.getByLabel('Type REVIEWED NEWS HALT', { exact: true }).fill('REVIEWED NEWS HALT');
  const completed = page.waitForResponse(response => response.request().method() === 'POST' && response.url().endsWith('/release'));
  await release.click();
  const response = await completed;
  assert.equal(response.status(), 200);
  assert.equal((await response.json()).blocked, false);
  await page.getByRole('heading', { name: 'ins-test — NEWS HALT RELEASED', exact: true }).waitFor();
  assert.deepEqual(errors, []);
  process.stdout.write('NEWS_HALT_BROWSER_VERIFIED\n');
} finally {
  await browser.close();
}
