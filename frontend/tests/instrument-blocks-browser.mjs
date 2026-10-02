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
  await page.getByLabel('Instrument control ID', { exact: true }).fill('ins-test');
  await page.getByRole('button', { name: 'Load instrument for review', exact: true }).click();
  await page.getByLabel('Instrument control reason', { exact: true }).fill('Owner reviews isolated PAPER instrument fixture');
  const block = page.getByRole('button', { name: 'Block reviewed instrument', exact: true });
  assert.equal(await block.isDisabled(), true);
  await page.getByLabel('Type BLOCK PAPER INSTRUMENT', { exact: true }).fill('BLOCK PAPER INSTRUMENT');
  await block.click();
  await page.getByRole('heading', { name: 'ins-test — MANUAL ENTRIES BLOCKED', exact: true }).waitFor();
  await page.getByLabel('Type UNBLOCK PAPER INSTRUMENT', { exact: true }).fill('UNBLOCK PAPER INSTRUMENT');
  await page.getByRole('button', { name: 'Release reviewed manual block', exact: true }).click();
  await page.getByRole('heading', { name: 'ins-test — NO MANUAL BLOCK', exact: true }).waitFor();
  assert.deepEqual(errors, []);
  process.stdout.write('INSTRUMENT_CONTROLS_BROWSER_VERIFIED\n');
} finally {
  await browser.close();
}
