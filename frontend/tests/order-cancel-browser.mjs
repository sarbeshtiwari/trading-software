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
  await page.getByRole('button', { name: 'Orders', exact: true }).click();
  const controls = page.getByRole('region', { name: 'PAPER order cancellation' });
  await controls.waitFor();
  await controls.getByRole('combobox').selectOption(process.argv[3]);
  await page.getByLabel('Cancellation reason', { exact: true }).fill('Owner cancels isolated browser PAPER entry');
  await page.getByLabel('Type CANCEL PAPER ENTRY', { exact: true }).fill('CANCEL PAPER ENTRY');
  const responseReady = page.waitForResponse(response => response.url().endsWith(`/orders/${process.argv[3]}/cancel`) && response.request().method() === 'POST');
  await page.getByRole('button', { name: 'Cancel entry remainder', exact: true }).click();
  const response = await responseReady;
  assert.equal(response.status(), 200);
  const result = await response.json();
  assert.equal(result.status, 'CANCELLED');
  assert.equal(result.filled_quantity, 100);
  assert.equal(result.error, null);
  await page.getByText('Recorded outcome: CANCELLED; filled quantity: 100.', { exact: true }).waitFor();
  await page.getByRole('button', { name: 'Positions', exact: true }).click();
  await page.getByText('100', { exact: true }).first().waitFor();
  assert.deepEqual(errors, []);
  process.stdout.write('OWNER_CANCEL_VERIFIED\n');
} finally {
  await browser.close();
}
