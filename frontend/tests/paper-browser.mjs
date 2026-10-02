import assert from 'node:assert/strict';
import { createInterface } from 'node:readline';
import { readFile } from 'node:fs/promises';
import { chromium } from 'playwright-core';

const input = createInterface({ input: process.stdin });
const proceed = new Promise(resolve => input.once('line', resolve));
const browser = await chromium.launch({ channel: process.env.ATS_BROWSER_CHANNEL || 'msedge', headless: true });
try {
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto(process.argv[2]);
  await page.getByLabel('Username', { exact: true }).fill('owner');
  await page.getByLabel('Password', { exact: true }).fill(process.env.ATS_TEST_OWNER_PASSWORD);
  const entryResponse = page.waitForResponse(response => response.url().endsWith('/api/v1/workspace'));
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  const entry = await (await entryResponse).json();
  assert.equal(entry.trading_mode, 'PAPER');
  assert.equal(entry.positions.length, 1);
  assert.equal(entry.positions[0].net_quantity, 111);
  assert.equal(entry.orders.length, 1);
  assert.equal(entry.fills.length, 1);
  await page.getByRole('button', { name: 'Positions', exact: true }).click();
  await page.getByRole('cell', { name: entry.positions[0].id, exact: true }).waitFor();
  assert.match(await page.locator('main').innerText(), /RECENT_SOFTWARE_CHECK/);
  await page.getByRole('button', { name: 'Orders', exact: true }).click();
  assert.equal(entry.orders[0].status, 'EXECUTED');
  assert.match(await page.locator('main').innerText(), /EXECUTED/);
  process.stdout.write('ENTRY_VERIFIED\n');
  assert.equal(await proceed, 'EXIT_READY');
  const exitPath = process.env.ATS_TEST_EXIT_PATH;
  if (exitPath !== 'target') {
    await page.getByRole('button', { name: 'Risk', exact: true }).click();
    await page.getByRole('combobox', { name: /^Emergency action/ }).selectOption('FLATTEN');
    await page.getByLabel('Emergency reason', { exact: true }).fill('Owner requests isolated browser emergency flatten');
    const confirmation = page.getByLabel('Type FLATTEN PAPER', { exact: true });
    const execute = page.getByRole('button', { name: 'Execute authenticated emergency action', exact: true });
    await confirmation.fill('incorrect');
    assert.equal(await execute.isDisabled(), true);
    await confirmation.fill('FLATTEN PAPER');
    const actionResponse = page.waitForResponse(response => response.url().endsWith('/api/v1/emergency') && response.request().method() === 'POST');
    await execute.click();
    const response = await actionResponse;
    assert.equal(response.status(), 200);
    const result = await response.json();
    assert.equal(result.entries_blocked, true);
    assert.equal(result.execution, 'INSPECT_ORDER_AND_POSITION_OUTCOMES');
    assert.equal(result.outcomes[0].status, exitPath === 'stale_emergency' ? 'OPEN' : 'EXECUTED');
    assert.equal(result.outcomes[0].remaining_quantity, exitPath === 'stale_emergency' ? '111' : '0');
    if (exitPath === 'emergency') {
      const repeated = page.waitForResponse(response => response.url().endsWith('/api/v1/emergency') && response.request().method() === 'POST');
      await execute.click();
      assert.deepEqual((await (await repeated).json()).outcomes, []);
    }
  }
  const exitResponse = page.waitForResponse(response => response.url().endsWith('/api/v1/workspace'));
  await page.getByRole('button', { name: 'Refresh', exact: true }).click();
  const exited = await (await exitResponse).json();
  if (exitPath === 'stale_emergency') {
    assert.equal(exited.fills.length, 1);
    assert.equal(exited.journal.length, 0);
    assert.equal(exited.positions[0].net_quantity, 111);
    assert.equal(exited.new_entries_allowed, false);
    assert.match(await page.locator('main').innerText(), /ENTRIES BLOCKED/);
  } else {
    assert.equal(exited.orders.length, 2);
    assert.equal(exited.fills.length, 2);
    assert.equal(exited.journal.length, 1);
    assert.equal(Number(exited.journal[0].gross_pnl), 888);
    assert.equal(Number(exited.journal[0].charges), 31.44);
    assert.equal(Number(exited.journal[0].net_pnl), 856.56);
    assert.equal(Number(exited.account.gross_exposure), 0);
    await page.getByRole('button', { name: 'Journal', exact: true }).click();
    await page.getByRole('cell', { name: exited.journal[0].id, exact: true }).waitFor();
    assert.match(await page.locator('main').innerText(), /856\.56/);
  }
  assert.ok(exited.notifications.length > 0);
  if (exitPath === 'target') {
    const journalId = exited.journal[0].id;
    await page.getByRole('button', { name: `Inspect ${journalId}`, exact: true }).click();
    const lineage = page.getByRole('region', { name: 'Journal lineage', exact: true });
    await lineage.getByText('Lineage: AVAILABLE; integrity: AUDIT_BOUND; mode: PAPER', { exact: true }).waitFor();
    await lineage.getByLabel('Journal note', { exact: true }).fill('Browser reviewed the actual costed PAPER lifecycle');
    await lineage.getByLabel('Journal tags (comma separated)', { exact: true }).fill('browser-reviewed, paper');
    await lineage.getByLabel('Journal annotation reason', { exact: true }).fill('Owner inspected proposal, risk, orders and fills');
    await lineage.getByRole('button', { name: 'Add audited annotation', exact: true }).click();
    await page.getByText('Annotation recorded and audited. Economics unchanged.', { exact: true }).waitFor();
    await lineage.getByText('Browser reviewed the actual costed PAPER lifecycle', { exact: true }).waitFor();
    const downloaded = page.waitForEvent('download');
    await page.getByRole('button', { name: 'Export journal JSON', exact: true }).click();
    const download = await downloaded;
    assert.equal(download.suggestedFilename(), 'journal.json');
    const exported = JSON.parse(await readFile(await download.path(), 'utf8'));
    assert.equal(exported.entries[0].id, journalId);
    assert.equal(Number(exported.entries[0].net_pnl), 856.56);
    assert.equal(exported.entries[0].annotations.length, 1);
    await lineage.getByText('Append contextual correction', { exact: true }).click();
    await lineage.getByLabel('Corrected plan adherence', { exact: true }).selectOption('FOLLOWED');
    await lineage.getByLabel('Journal correction reason', { exact: true }).fill('Owner assessed the recorded target-exit policy');
    const correctionResponse = page.waitForResponse(response => response.url().endsWith(`/journal/${journalId}/corrections`) && response.request().method() === 'POST');
    await lineage.getByRole('button', { name: 'Create audited journal version', exact: true }).click();
    const corrected = await (await correctionResponse).json();
    assert.equal(corrected.entry.version, 2);
    assert.equal(Number(corrected.entry.net_pnl), 856.56);
    await lineage.getByText(/Journal version 2: OWNER_CONTEXT_CORRECTION/).waitFor();
    await lineage.getByRole('button', { name: 'Inspect original journal', exact: true }).click();
    await lineage.getByText(/Journal version 1: ORIGINAL/).waitFor();
    await lineage.getByText('Browser reviewed the actual costed PAPER lifecycle', { exact: true }).waitFor();
    await page.getByRole('button', { name: 'Strategies', exact: true }).click();
    const evidence = page.getByRole('region', { name: 'Strategy evidence', exact: true });
    await evidence.getByLabel('Evidence strategy', { exact: true }).selectOption(JSON.stringify(['closed-candle-breakout', '1']));
    await evidence.getByText(/^Blockers: PAPER_POLICY_UNAVAILABLE/).waitFor();
    await evidence.getByLabel('Strategy evidence reason', { exact: true }).fill('Isolated browser PAPER evidence policy fixture');
    await evidence.getByText('Declare versioned PAPER evidence policy', { exact: true }).click();
    await evidence.getByLabel('PAPER evidence policy JSON', { exact: true }).fill(JSON.stringify({ version: 1, minimum_sessions: 1, minimum_trades: 1, minimum_session_coverage_fraction: '1', sample_interval_seconds: 60, maximum_sample_gap_seconds: 90 }));
    await evidence.getByRole('button', { name: 'Publish evidence policy', exact: true }).click();
    await evidence.getByText(/Audited policy:.*LIVE remains disabled/).waitFor();
    await evidence.getByRole('button', { name: 'Record strategy evidence review', exact: true }).click();
    await evidence.getByText(/Audited review:.*LIVE remains disabled/).waitFor();
    assert.match(await evidence.innerText(), /Eligible sessions: 0; eligible trades: 0/);
  }
  await page.getByRole('button', { name: 'Audit', exact: true }).click();
  assert.ok(exited.audit.length > 0);
  await page.getByLabel('Decision, proposal, trade or audit ID').fill(exited.audit[0].id);
  await page.getByRole('button', { name: 'Inspect ID', exact: true }).click();
  await page.getByRole('heading', { name: `Trail ${exited.audit[0].id}`, exact: true }).waitFor();
  await page.getByRole('button', { name: 'Dashboard', exact: true }).click();
  assert.match(await page.locator('main').innerText(), /UNVERIFIED/);
  assert.deepEqual(errors, []);
  process.stdout.write('EXIT_VERIFIED\n');
} finally {
  input.close();
  await browser.close();
}
