import assert from 'node:assert/strict';
import { createInterface } from 'node:readline';
import { chromium } from 'playwright-core';

const input = createInterface({ input: process.stdin });
const order = new Promise(resolve => input.once('line', resolve));
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
  await page.getByRole('button', { name: 'Orders', exact: true }).click();
  await page.getByText(/Execution updates connected/).waitFor();
  assert.equal(await page.getByRole('cell', { name: 'EXECUTED', exact: true }).count(), 0);
  console.log('STREAM_READY');
  const identifier = await order;
  await page.getByRole('cell', { name: identifier, exact: true }).first().waitFor({ timeout: 8000 });
  await page.getByRole('cell', { name: 'EXECUTED', exact: true }).waitFor({ timeout: 8000 });
  await page.getByRole('button', { name: 'Positions', exact: true }).click();
  await page.getByRole('cell', { name: 'OPEN', exact: true }).waitFor();
  console.log('ENTRY_STREAM_VERIFIED');
  await page.getByRole('cell', { name: 'CLOSED', exact: true }).waitFor({ timeout: 15000 });
  console.log('EXECUTION_STREAM_BROWSER_VERIFIED');
} finally {
  input.close();
  await browser.close();
}
