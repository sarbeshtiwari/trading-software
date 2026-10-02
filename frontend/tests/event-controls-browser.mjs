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
  await page.getByLabel('Event instrument ID', { exact: true }).fill('ins-test');
  await page.getByLabel('Event data origin', { exact: true }).selectOption('SYNTHETIC');
  const reviewed = page.waitForResponse(response => response.url().includes('/risk/event-controls/ins-test?'));
  await page.getByRole('button', { name: 'Review event control', exact: true }).click();
  const response = await reviewed;
  assert.equal(response.status(), 200, await response.text());
  const editor = page.getByLabel('Event control JSON', { exact: true });
  await editor.waitFor();
  const control = JSON.parse(await editor.inputValue());
  assert.equal(control.enabled, true);
  control.enabled = false;
  control.reason = 'Owner reviewed isolated browser fixture calendar';
  await editor.fill(JSON.stringify(control));
  const publish = page.getByRole('button', { name: 'Publish reviewed event control', exact: true });
  assert.equal(await publish.isDisabled(), true);
  await page.getByLabel('Type DISABLE PAPER EVENT CONTROL', { exact: true }).fill('DISABLE PAPER EVENT CONTROL');
  await publish.click();
  await page.getByRole('heading', { name: 'ins-test / SYNTHETIC — DISABLED', exact: true }).waitFor();
  assert.deepEqual(errors, []);
  process.stdout.write('EVENT_CONTROLS_BROWSER_VERIFIED\n');
} finally {
  await browser.close();
}
