import assert from 'node:assert/strict';
import { chromium } from 'playwright-core';

const browser = await chromium.launch({ channel: process.env.ATS_BROWSER_CHANNEL || 'msedge', headless: true });
try {
  const page = await browser.newPage();
  await page.goto(process.argv[2]);
  await page.getByLabel('Username', { exact: true }).fill('owner');
  await page.getByLabel('Password', { exact: true }).fill(process.env.ATS_TEST_OWNER_PASSWORD);
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await page.getByRole('button', { name: 'Monitoring', exact: true }).click();
  const panel = page.getByRole('region', { name: 'Health transition history', exact: true });
  await panel.getByRole('cell', { name: /database: FAIL/ }).waitFor();
  assert.equal(await panel.locator('tbody tr').count(), 3);
  assert.match(await panel.innerText(), /intervals between observations are not verified health/);
  console.log('HEALTH_HISTORY_BROWSER_VERIFIED');
} finally { await browser.close(); }
