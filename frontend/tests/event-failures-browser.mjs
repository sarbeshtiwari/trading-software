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
  const panel = page.getByRole('region', { name: 'Retained event failures', exact: true });
  await panel.getByText('RETAINED_FAILURES', { exact: true }).waitFor();
  await panel.getByRole('cell', { name: 'execution.fill', exact: true }).waitFor();
  assert.equal(await panel.locator('tbody tr').count(), 1);
  assert.ok(!(await panel.innerText()).includes('test-only'));
  console.log('EVENT_FAILURE_MONITOR_VERIFIED');
} finally { await browser.close(); }
