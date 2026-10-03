import assert from 'node:assert/strict';
import { chromium } from 'playwright-core';

const browser = await chromium.launch({ channel: process.env.ATS_BROWSER_CHANNEL || 'msedge', headless: true });
try {
  const page = await browser.newPage();
  await page.goto(process.argv[2]);
  await page.getByLabel('Username', { exact: true }).fill('owner');
  await page.getByLabel('Password', { exact: true }).fill(process.env.ATS_TEST_OWNER_PASSWORD);
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await page.getByText('UNAVAILABLE_ADOPTED_POSITION_ACCOUNTING', { exact: true }).waitFor();
  await page.getByRole('button', { name: 'Positions', exact: true }).click();
  await page.getByRole('cell', { name: 'ADOPTED', exact: true }).waitFor();
  const row = page.locator('tbody tr').filter({ has: page.getByRole('cell', { name: 'ADOPTED', exact: true }) });
  assert.match(await row.innerText(), /UNAVAILABLE_HISTORY_NOT_RECONSTRUCTED/);
  assert.match(await row.innerText(), /UNAVAILABLE/);
  await page.reload();
  await page.getByText('UNAVAILABLE_ADOPTED_POSITION_ACCOUNTING', { exact: true }).waitFor();
  console.log('ORPHAN_VISIBILITY_BROWSER_VERIFIED');
} finally { await browser.close(); }
