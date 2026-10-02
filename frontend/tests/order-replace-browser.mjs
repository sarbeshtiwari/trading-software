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
  const controls = page.getByRole('region', { name: 'PAPER order replacement' });
  await controls.getByRole('combobox', { name: 'Original entry' }).selectOption(process.argv[3]);
  await controls.getByRole('combobox', { name: 'Replacement approval' }).selectOption(process.argv[4]);
  await controls.getByLabel('Replacement reason', { exact: true }).fill('Owner replaces isolated browser PAPER entry');
  await controls.getByLabel('Type REPLACE PAPER ENTRY', { exact: true }).fill('REPLACE PAPER ENTRY');
  const ready = page.waitForResponse(response => response.url().endsWith(`/orders/${process.argv[3]}/replace`) && response.request().method() === 'POST');
  const workspaceReady = page.waitForResponse(async response => {
    if (!response.url().endsWith('/workspace') || response.status() !== 200) return false;
    const state = await response.json();
    return state.orders.some(order => order.parent_order_id === process.argv[3]);
  });
  await controls.getByRole('button', { name: 'Cancel and revalidate replacement' }).click();
  const response = await ready;
  assert.equal(response.status(), 200);
  const result = await response.json();
  assert.equal(result.original_status, 'CANCELLED');
  assert.equal(result.replacement_status, 'OPEN');
  assert.equal(result.error, null);
  await controls.getByText('Original: CANCELLED. Replacement: OPEN.', { exact: true }).waitFor();
  const workspace = await (await workspaceReady).json();
  const replacement = workspace.orders.find(order => order.id === result.replacement_order_id);
  assert.equal(replacement.parent_order_id, process.argv[3]);
  assert.equal(replacement.quantity, 243);
  assert.deepEqual(errors, []);
  process.stdout.write('OWNER_REPLACE_VERIFIED\n');
} finally {
  await browser.close();
}
