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
  const workspaceResponse = page.waitForResponse(response => response.url().endsWith('/api/v1/workspace'));
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  const workspace = await (await workspaceResponse).json();
  const strategy = workspace.strategies.find(row => row.strategy_id === 'closed-candle-breakout');
  assert.equal(strategy.auto_disabled, true);
  assert.equal(strategy.enabled_paper, false);
  assert.equal(workspace.orders.length, 2);
  assert.equal(workspace.positions[0].net_quantity, 0);
  assert.equal(workspace.advisory_calls.length, 1);
  assert.equal(workspace.advisory_calls[0].provider, 'fallback');
  assert.equal(workspace.advisory_calls[0].proposal_id, workspace.orders[1].proposal_id ?? workspace.orders[0].proposal_id);
  await page.getByRole('button', { name: 'Decisions', exact: true }).click();
  await page.getByText(workspace.advisory_calls[0].id, { exact: true }).waitFor();
  await page.getByText('FALLBACK_USED', { exact: true }).waitFor();
  await page.getByRole('button', { name: 'Strategies', exact: true }).click();
  const reviewResponse = page.waitForResponse(response => response.url().includes('/degradation?'));
  await page.getByLabel('Monitored strategy', { exact: true }).selectOption(JSON.stringify([strategy.strategy_id, strategy.version]));
  const review = await (await reviewResponse).json();
  assert.equal(review.status, 'BREACHED');
  assert.equal(review.journal_ids[0], workspace.journal[0].id);
  assert.equal(Number(review.maximum_drawdown_amount), -Number(workspace.journal[0].net_pnl));
  await page.getByText('STRATEGY AUTO-DISABLED — PAPER_DEGRADATION_BREACHED', { exact: true }).waitFor();
  assert.equal(await page.getByRole('button', { name: 'Reset reviewed degradation latch', exact: true }).isDisabled(), true);
  assert.deepEqual(errors, []);
  process.stdout.write('DEGRADATION_VERIFIED\n');
} finally {
  await browser.close();
}
