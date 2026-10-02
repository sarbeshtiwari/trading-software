import assert from 'node:assert/strict';
import { chromium } from 'playwright-core';

const browser = await chromium.launch({ channel: process.env.ATS_BROWSER_CHANNEL || 'msedge', headless: true });
try {
  const page = await browser.newPage();
  await page.goto(process.argv[2]);
  await page.getByLabel('Username', { exact: true }).fill('owner');
  await page.getByLabel('Password', { exact: true }).fill(process.env.ATS_TEST_OWNER_PASSWORD);
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await page.getByRole('button', { name: 'Monitoring', exact: true }).click();
  const panel = page.getByRole('region', { name: 'PAPER reconciliation review', exact: true });
  await panel.getByRole('button', { name: `Review ${process.argv[3]}`, exact: true }).click();
  const submit = panel.getByRole('button', { name: 'Record authenticated discrepancy resolution' });
  assert.equal(await submit.isDisabled(), true);
  await panel.getByLabel('Discrepancy review reason').fill('Owner reviewed restored fixture accounting');
  await panel.getByLabel('Type RESOLVE PAPER DISCREPANCY').fill('RESOLVE PAPER DISCREPANCY');
  await submit.click();
  await panel.getByRole('status').filter({ hasText: 'Resolution recorded' }).waitFor();
  await panel.getByLabel('Show resolved discrepancies').check();
  await panel.getByRole('heading', { name: process.argv[3], exact: true }).waitFor();
  assert.match(await panel.innerText(), /REVIEWED/);
  assert.match(await panel.innerText(), /does not arm trading/);
  console.log('RECONCILIATION_REVIEW_BROWSER_VERIFIED');
} finally { await browser.close(); }
