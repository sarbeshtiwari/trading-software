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
  await page.getByRole('button', { name: 'primary', exact: true }).click();
  await page.getByLabel('Article JSON', { exact: true }).fill(process.env.ATS_TEST_NEWS_ARTICLE);
  await page.getByRole('button', { name: 'Import article observation', exact: true }).click();
  await page.getByRole('button', { name: 'Read story grouping', exact: true }).click();
  await page.getByRole('button', { name: 'Read saved interpretation', exact: true }).click();
  await page.getByText(/NEWS INTERPRETATION — GROUNDED_UNVERIFIED/).waitFor();
  await page.getByLabel('Research instrument ID', { exact: true }).fill('ins-test');
  const admitted = page.waitForResponse(result => result.request().method() === 'POST' && result.url().endsWith('/research'));
  await page.getByRole('button', { name: 'Evaluate quotation admission', exact: true }).click();
  const response = await admitted;
  assert.equal(response.status(), 200);
  const receipt = await response.json();
  assert.equal(receipt.status, 'SOURCE_POLICY_ADMITTED');
  assert.equal(receipt.snapshot.sentiment_score, null);
  assert.equal(receipt.snapshot.standalone_trigger_allowed, false);
  await page.getByText(`SOURCE_POLICY_ADMITTED; evidence ${receipt.source_id}; audit ${receipt.audit_id}; known at ${receipt.known_at}.`, { exact: true }).waitFor();
  await page.getByRole('button', { name: 'Load sentiment policy', exact: true }).click();
  await page.getByText(/UNAVAILABLE OR DISABLED/).waitFor();
  await page.getByLabel('Advisory sentiment policy JSON', { exact: true }).fill(JSON.stringify({
    enabled: true, accept_uncalibrated_model_scores: true,
    aggregation: { tier_weights: ['1', '0.8', '0.5', '0'], half_life_seconds: 60, max_age_seconds: 180, minimum_confidence: '0.5' },
  }));
  await page.getByLabel('Sentiment policy reason', { exact: true }).fill('Explicit synthetic browser test advisory policy');
  await page.getByRole('button', { name: 'Save sentiment policy', exact: true }).click();
  await page.getByText(/ENABLED — ADVISORY ONLY/).waitFor();
  await page.getByLabel('Sentiment instrument ID', { exact: true }).fill('ins-test');
  await page.getByLabel('Sentiment data origin', { exact: true }).selectOption('SYNTHETIC');
  const scoring = page.waitForResponse(result => result.url().includes('/news/sentiment/ins-test'));
  await page.getByRole('button', { name: 'Resolve advisory sentiment', exact: true }).click();
  const scored = await (await scoring).json();
  assert.equal(scored.status, 'AVAILABLE');
  assert.equal(scored.result.score, '1');
  assert.equal(scored.confidence_basis, 'MODEL_SELF_REPORT_UNCALIBRATED');
  assert.equal(scored.standalone_trigger_allowed, false);
  assert.deepEqual(scored.admission_ids, [receipt.source_id]);
  await page.getByText(/Score: 1\./).waitFor();
  assert.deepEqual(errors, []);
  process.stdout.write('NEWS_ADMISSION_BROWSER_VERIFIED\n');
} finally {
  await browser.close();
}
