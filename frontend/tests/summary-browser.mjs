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
  const reportResponse = page.waitForResponse(response => response.url().endsWith('/api/v1/reports/paper/daily'));
  await page.getByRole('button', { name: 'Monitoring', exact: true }).click();
  const response = await reportResponse;
  assert.equal(response.status(), 200);
  const reports = (await response.json()).reports;
  assert.equal(reports.length, 1);
  assert.equal(reports[0].summary.fill_count, 2);
  assert.equal(reports[0].summary.net_realised_pnl, '856.56');
  await page.getByText('856.56', { exact: true }).waitFor();
  assert.match(await page.locator('main').innerText(), /daily loss: 0/);
  assert.match(await page.locator('main').innerText(), /drawdown: 17.60/);
  assert.match(await page.locator('main').innerText(), /PAPER \/ SIMULATED/);
  const polled = await page.waitForResponse(response => response.url().endsWith('/api/v1/reports/paper/daily'), { timeout: 15000 });
  assert.equal((await polled.json()).reports[0].audit_id, reports[0].audit_id);
  await page.getByRole('button', { name: 'Audit', exact: true }).click();
  await page.getByLabel('Decision, proposal, trade or audit ID').fill(reports[0].summary.fill_ids[0]);
  await page.getByRole('button', { name: 'Inspect ID', exact: true }).click();
  await page.getByRole('heading', { name: `Trail ${reports[0].summary.fill_ids[0]}`, exact: true }).waitFor();
  const decision = page.locator('summary').filter({ hasText: /^DECISION / });
  await decision.click();
  assert.match(await page.locator('main').innerText(), /position_size/);
  assert.match(await page.locator('main').innerText(), /risk_calculation/);
  await page.locator('summary').filter({ hasText: /^Journal / }).click();
  assert.match(await page.locator('main').innerText(), /856.56/);
  assert.deepEqual(errors, []);
  process.stdout.write('SUMMARY_VERIFIED\n');
} finally { await browser.close(); }
