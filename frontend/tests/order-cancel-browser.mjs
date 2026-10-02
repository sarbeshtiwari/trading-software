import assert from 'node:assert/strict';
import { chromium } from 'playwright-core';

const browser = await chromium.launch({ channel: process.env.ATS_BROWSER_CHANNEL || 'msedge', headless: true });
const bulk = process.argv[4] === 'bulk';
try {
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto(process.argv[2]);
  await page.getByLabel('Username', { exact: true }).fill('owner');
  await page.getByLabel('Password', { exact: true }).fill(process.env.ATS_TEST_OWNER_PASSWORD);
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await page.getByRole('button', { name: 'Orders', exact: true }).click();
  const controls = page.getByRole('region', { name: 'PAPER order cancellation' });
  await controls.waitFor();
  await controls.getByRole('combobox').selectOption(bulk ? '__ALL__' : process.argv[3]);
  await page.getByLabel('Cancellation reason', { exact: true }).fill('Owner cancels isolated browser PAPER entry');
  const phrase = bulk ? 'CANCEL PAPER ENTRIES' : 'CANCEL PAPER ENTRY';
  await page.getByLabel(`Type ${phrase}`, { exact: true }).fill(phrase);
  const path = bulk ? '/orders/cancel-entries' : `/orders/${process.argv[3]}/cancel`;
  const responseReady = page.waitForResponse(response => response.url().endsWith(path) && response.request().method() === 'POST');
  await page.getByRole('button', { name: 'Cancel entry remainder', exact: true }).click();
  const response = await responseReady;
  assert.equal(response.status(), 200);
  const payload = await response.json();
  if (bulk) { assert.equal(payload.resolved, true); assert.equal(payload.outcomes.length, 1); }
  const result = bulk ? payload.outcomes[0] : payload;
  assert.equal(result.status, 'CANCELLED');
  assert.equal(result.filled_quantity, 100);
  assert.equal(result.error, null);
  await page.getByText(bulk ? 'Bulk cancellation: recorded targets resolved.' : 'Recorded outcome: CANCELLED; filled quantity: 100.', { exact: true }).waitFor();
  await page.getByRole('button', { name: 'Positions', exact: true }).click();
  await page.getByText('100', { exact: true }).first().waitFor();
  assert.deepEqual(errors, []);
  process.stdout.write('OWNER_CANCEL_VERIFIED\n');
} finally {
  await browser.close();
}
