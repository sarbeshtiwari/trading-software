import assert from 'node:assert/strict';
import { chromium } from 'playwright-core';

const browser = await chromium.launch({ channel: process.env.ATS_BROWSER_CHANNEL || 'msedge', headless: true });
try {
  const page = await browser.newPage();
  await page.addInitScript(() => {
    const interval = window.setInterval.bind(window);
    window.setInterval = (handler, delay, ...args) => interval(handler, delay === 10000 ? 3600000 : delay, ...args);
  });
  await page.goto(process.argv[2]);
  await page.getByLabel('Username', { exact: true }).fill('owner');
  await page.getByLabel('Password', { exact: true }).fill(process.env.ATS_TEST_OWNER_PASSWORD);
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await page.getByRole('button', { name: 'Risk', exact: true }).click();
  await page.getByText(/Execution updates connected/).waitFor();
  const row = page.getByRole('row').filter({ has: page.getByRole('cell', { name: 'SYNTHETIC', exact: true }) });
  await row.waitFor();
  assert.equal(await row.getByRole('cell', { name: 'true', exact: true }).count(), 0);
  const draft = page.getByLabel('Risk configuration JSON');
  await page.getByText('Versioned risk configuration', { exact: true }).click();
  await draft.fill('unsaved owner draft');
  console.log('RISK_STREAM_READY');
  await row.getByRole('cell', { name: 'true', exact: true }).waitFor({ timeout: 15000 });
  assert.equal(await draft.inputValue(), 'unsaved owner draft');
  console.log('RISK_STREAM_BROWSER_VERIFIED');
} finally { await browser.close(); }
