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
  await page.getByRole('button', { name: 'Market / F&O', exact: true }).click();
  const panel = page.getByRole('region', { name: 'Stored market observations', exact: true });
  await panel.getByLabel('Chart data origin', { exact: true }).selectOption('SYNTHETIC');
  await panel.getByLabel('Chart instrument', { exact: true }).selectOption(process.argv[3]);
  await panel.getByText('SYNTHETIC: RECORDED', { exact: true }).waitFor();
  const watchlist = panel.getByRole('region', { name: 'Stored quote watchlist', exact: true });
  await watchlist.getByRole('button', { name: 'Watch selected instrument' }).click();
  await watchlist.getByRole('cell', { name: 'RECORDED', exact: true }).waitFor();
  await watchlist.getByRole('cell', { name: 'WebSocket', exact: true }).waitFor();
  assert.equal(Number(await watchlist.locator('tbody tr td').nth(3).innerText()), 100);
  await panel.locator('[aria-label="Stored candlestick chart"] canvas').first().waitFor();
  await panel.getByText('Stored candle values', { exact: true }).click();
  const candleTable = panel.locator('details').filter({ has: page.getByText('Stored candle values', { exact: true }) });
  assert.equal(await candleTable.locator('tbody tr').count(), 20);
  const lastAverage = await candleTable.locator('tbody tr').last().locator('td').nth(3).innerText();
  assert.equal(Number(lastAverage), 109.5);
  assert.equal(await candleTable.getByRole('cell', { name: 'UNAVAILABLE', exact: true }).count(), 19);
  await panel.getByLabel('Chart interval', { exact: true }).selectOption('5');
  await panel.getByText('SYNTHETIC: UNAVAILABLE', { exact: true }).waitFor();
  assert.equal(await panel.locator('canvas').count(), 0);
  const fundamentals = panel.getByRole('region', { name: 'Instrument fundamentals', exact: true });
  await fundamentals.getByRole('button', { name: 'Inspect source browser-fixture', exact: true }).click();
  await fundamentals.getByRole('cell', { name: '12.5', exact: true }).waitFor();
  assert.ok((await fundamentals.innerText()).includes('source: browser-fixture'));
  await fundamentals.getByLabel('Fundamentals source').fill('missing-source');
  await fundamentals.getByRole('button', { name: 'Inspect fundamentals' }).click();
  await fundamentals.getByText(/Fundamental record: UNAVAILABLE/).waitFor();
  assert.equal(await fundamentals.getByRole('cell', { name: '12.5', exact: true }).count(), 0);
  assert.deepEqual(errors, []);
  console.log('MARKET_CHART_VERIFIED');
} finally {
  await browser.close();
}
