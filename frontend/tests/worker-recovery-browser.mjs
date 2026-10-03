import assert from 'node:assert/strict';
import { chromium } from 'playwright-core';

const browser = await chromium.launch({ channel: process.env.ATS_BROWSER_CHANNEL || 'msedge', headless: true });
try {
  const page = await browser.newPage();
  await page.goto(process.argv[2]);
  await page.getByLabel('Username', { exact: true }).fill('owner');
  await page.getByLabel('Password', { exact: true }).fill(process.env.ATS_TEST_OWNER_PASSWORD);
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await page.getByRole('button', { name: 'Risk', exact: true }).click();
  await page.getByRole('combobox', { name: /^Emergency action/ }).selectOption('RECOVER_WORKER');
  await page.getByLabel('Emergency reason', { exact: true }).fill('Owner reviewed isolated database interruption in browser');
  await page.getByLabel('Type RECOVER PAPER WORKER', { exact: true }).fill('RECOVER PAPER WORKER');
  const result = page.waitForResponse(response => response.url().endsWith('/api/v1/emergency') && response.request().method() === 'POST');
  await page.getByRole('button', { name: 'Execute authenticated emergency action', exact: true }).click();
  const response = await result;
  assert.equal(response.status(), 200);
  const body = await response.json();
  assert.equal(body.execution, 'WORKER_RECOVERED_ENTRIES_REMAIN_DISABLED');
  assert.equal(body.entries_blocked, true);
  await page.getByText('WORKER_RECOVERED_ENTRIES_REMAIN_DISABLED', { exact: true }).waitFor();
  assert.match(await page.locator('main').innerText(), /ENTRIES BLOCKED/);
  console.log('WORKER_RECOVERY_ENTRIES_BLOCKED_VERIFIED');
} finally { await browser.close(); }
