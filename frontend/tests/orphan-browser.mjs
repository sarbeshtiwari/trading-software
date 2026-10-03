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
  await page.getByRole('button', { name: 'Monitoring', exact: true }).click();
  const panel = page.getByRole('region', { name: 'PAPER orphan review', exact: true });
  await panel.getByRole('button', { name: /^Review orphan / }).click();
  await panel.getByLabel('Orphan acknowledgment reason').fill('Owner reviewed orphan discovery evidence');
  await panel.getByLabel('Type ACKNOWLEDGE PAPER ORPHAN').fill('ACKNOWLEDGE PAPER ORPHAN');
  await panel.getByRole('button', { name: 'Record orphan acknowledgment', exact: true }).click();
  await panel.getByRole('status').filter({ hasText: 'Entries remain blocked' }).waitFor();
  await panel.getByRole('button', { name: /^Inspect recovery plan / }).click();
  const recovery = panel.getByRole('region', { name: 'Verified historical recovery plan' });
  await recovery.waitFor();
  assert.match(await recovery.innerText(), /original_position_id/);
  assert.match(await recovery.innerText(), /does not restore the position or arm trading/);
  await page.reload();
  await page.getByText('UNAVAILABLE_ADOPTED_POSITION_ACCOUNTING', { exact: true }).waitFor();
  await page.getByRole('button', { name: 'Monitoring', exact: true }).click();
  await page.getByRole('region', { name: 'PAPER orphan review', exact: true }).getByText(/ACKNOWLEDGED by owner/).waitFor();
  console.log('ORPHAN_VISIBILITY_BROWSER_VERIFIED');
} finally { await browser.close(); }
