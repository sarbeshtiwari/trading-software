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
  await page.getByLabel('Data origin', { exact: true }).selectOption('SYNTHETIC');
  const panel = page.getByRole('region', { name: 'Risk utilisation', exact: true });
  await panel.getByText('PAPER / SYNTHETIC: AVAILABLE', { exact: true }).waitFor();
  assert.equal(await panel.getByRole('progressbar').count(), 6);
  assert.match(await panel.innerText(), /gross exposure: 25000/);
  console.log('RISK_AVAILABLE');
  await panel.getByText('PAPER / SYNTHETIC: ACCOUNT_EVIDENCE_STALE', { exact: true }).waitFor({ timeout: 35000 });
  await panel.getByText('TRADING DISARMED — RISK_ERROR_LATCHED', { exact: true }).waitFor();
  assert.equal(await panel.getByRole('progressbar').count(), 0);
  assert.deepEqual(errors, []);
  console.log('RISK_STALE_AND_LATCHED');
} finally {
  await browser.close();
}
