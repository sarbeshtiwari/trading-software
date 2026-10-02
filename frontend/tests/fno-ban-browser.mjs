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
  await page.getByLabel('Ban report origin', { exact: true }).selectOption('SYNTHETIC');
  const reviewed = page.waitForResponse(response => response.url().includes('/risk/fno-bans?'));
  await page.getByRole('button', { name: 'Review ban report state', exact: true }).click();
  const state = await (await reviewed).json();
  const [year, month, day] = state.trade_date.split('-');
  const months = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'];
  await page.getByLabel('NSE ban CSV', { exact: true }).fill(`Securities in Ban For Trade Date ${day}-${months[Number(month) - 1]}-${year}:\n1,TEST\n`);
  await page.getByLabel('Report known at (ISO timestamp with timezone)', { exact: true }).fill(state.as_of);
  await page.getByLabel('Ban report review reason', { exact: true }).fill('Owner reviews isolated browser ban report fixture');
  const submit = page.getByRole('button', { name: 'Admit reviewed ban report', exact: true });
  assert.equal(await submit.isDisabled(), true);
  await page.getByLabel('Type ADMIT PAPER NSE BAN REPORT', { exact: true }).fill('ADMIT PAPER NSE BAN REPORT');
  const recorded = page.waitForResponse(response => response.url().endsWith('/risk/fno-bans') && response.request().method() === 'PUT');
  await submit.click();
  const response = await recorded;
  assert.equal(response.status(), 200, await response.text());
  assert.equal((await response.json()).status, 'AVAILABLE');
  await page.getByRole('region', { name: 'F&O ban report controls' }).getByText(/"AVAILABLE"/).waitFor();
  assert.deepEqual(errors, []);
  process.stdout.write('FNO_BAN_BROWSER_VERIFIED\n');
} finally {
  await browser.close();
}
