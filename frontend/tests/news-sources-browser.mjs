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
  await page.getByText('No configured sources. No news has been invented.', { exact: true }).waitFor();
  const configuration = {
    name: 'Isolated browser fixture', publisher_id: 'fixture-publisher', tier: 2, kind: 'rss',
    endpoint: 'https://fixture.example.test/feed', weight: '1', is_enabled: false,
  };
  await page.getByLabel('Source slug', { exact: true }).fill('browser-fixture');
  await page.getByLabel('Source configuration JSON', { exact: true }).fill(JSON.stringify(configuration));
  await page.getByLabel('Source configuration reason', { exact: true }).fill('Owner configures isolated browser source');
  const written = page.waitForResponse(response => response.request().method() === 'PUT' && response.url().endsWith('/news/sources/browser-fixture'));
  await page.getByRole('button', { name: 'Save source configuration', exact: true }).click();
  const response = await written;
  assert.equal(response.status(), 200);
  const record = await response.json();
  assert.equal(record.integrity, 'AUDITED');
  assert.equal(record.acquisition_status, 'SUPPORTED_ADAPTER');
  assert.equal(record.configuration.is_enabled, false);
  await page.getByText(`Configuration recorded: ${record.event_id}. Scheduler settings are unchanged.`, { exact: true }).waitFor();
  await page.reload();
  await page.getByRole('button', { name: 'Market / F&O', exact: true }).click();
  await page.getByRole('button', { name: 'browser-fixture', exact: true }).click();
  await page.getByText(`Expected audit version: ${record.event_id}. Existing audited records require their current version.`, { exact: true }).waitFor();
  const stored = JSON.parse(await page.getByLabel('Source configuration JSON', { exact: true }).inputValue());
  assert.equal(stored.publisher_id, configuration.publisher_id);
  assert.equal(stored.is_enabled, false);
  stored.is_enabled = true;
  await page.getByLabel('Source configuration JSON', { exact: true }).fill(JSON.stringify(stored));
  await page.getByLabel('Source configuration reason', { exact: true }).fill('Enable isolated article import test');
  const enabled = page.waitForResponse(result => result.request().method() === 'PUT' && result.url().endsWith('/news/sources/browser-fixture'));
  await page.getByRole('button', { name: 'Save source configuration', exact: true }).click();
  const enabledRecord = await (await enabled).json();
  await page.getByText(`Configuration recorded: ${enabledRecord.event_id}. Scheduler settings are unchanged.`, { exact: true }).waitFor();
  await page.getByLabel('Article JSON', { exact: true }).fill(JSON.stringify({
    url: 'https://fixture.example.test/article', title: 'Synthetic browser test',
    body: 'Synthetic fixture only, not market news.', published_at: '2025-01-01T00:00:00Z', data_origin: 'SYNTHETIC',
  }));
  const imported = page.waitForResponse(result => result.request().method() === 'POST' && result.url().endsWith('/news/sources/browser-fixture/articles'));
  await page.getByRole('button', { name: 'Import article observation', exact: true }).click();
  const importedResponse = await imported;
  assert.equal(importedResponse.status(), 200);
  const receipt = await importedResponse.json();
  const attributed = page.waitForResponse(result => result.request().method() === 'GET' && result.url().endsWith(`/news/articles/${receipt.article_id}/entities`));
  await page.getByRole('button', { name: 'Read instrument mentions', exact: true }).click();
  const attributionResponse = await attributed;
  assert.equal(attributionResponse.status(), 200);
  const attribution = await attributionResponse.json();
  assert.equal(attribution.attribution.meaning, 'MENTION_ONLY_NOT_TRADABILITY');
  assert.deepEqual(attribution.attribution.matches, []);
  await page.getByText('No unambiguous instrument mentions mapped.', { exact: true }).waitFor();
  const grouped = page.waitForResponse(result => result.request().method() === 'GET' && result.url().endsWith(`/news/articles/${receipt.article_id}/story`));
  await page.getByRole('button', { name: 'Read story grouping', exact: true }).click();
  const groupedResponse = await grouped;
  assert.equal(groupedResponse.status(), 200);
  const story = await groupedResponse.json();
  assert.equal(story.story.verification_status, 'UNVERIFIED');
  assert.equal(story.story.members[0].article_id, receipt.article_id);
  await page.getByText('Story group — UNVERIFIED', { exact: true }).waitFor();
  const assessed = page.waitForResponse(result => result.request().method() === 'GET' && result.url().endsWith(`/news/articles/${receipt.article_id}/conflicts`));
  await page.getByRole('button', { name: 'Read source contradictions', exact: true }).click();
  const assessmentResponse = await assessed;
  assert.equal(assessmentResponse.status(), 200);
  const assessment = await assessmentResponse.json();
  assert.equal(assessment.assessment.status, 'UNASSESSED');
  assert.equal(assessment.assessment.actionable, false);
  await page.getByText('NEWS CONFLICT ASSESSMENT — UNASSESSED', { exact: true }).waitFor();
  const interpreted = page.waitForResponse(result => result.request().method() === 'POST' && result.url().endsWith(`/news/articles/${receipt.article_id}/interpretation`));
  await page.getByRole('button', { name: 'Generate advisory interpretation', exact: true }).click();
  const interpretationResponse = await interpreted;
  assert.equal(interpretationResponse.status(), 200);
  const interpretation = await interpretationResponse.json();
  assert.equal(interpretation.result.status, 'UNAVAILABLE');
  assert.equal(interpretation.result.result, null);
  await page.getByText(/NEWS INTERPRETATION — UNAVAILABLE/).waitFor();
  await page.getByText(`OWNER_IMPORT — UNVERIFIED; article ${receipt.article_id}; audit ${receipt.audit_id}; observed ${receipt.observed_at}`, { exact: true }).waitFor();
  const acquired = page.waitForResponse(result => result.request().method() === 'POST' && result.url().endsWith('/news/sources/browser-fixture/poll'));
  await page.getByRole('button', { name: 'Fetch configured news source', exact: true }).click();
  const acquiredResponse = await acquired;
  assert.equal(acquiredResponse.status(), 200);
  const acquiredRecord = await acquiredResponse.json();
  assert.equal(acquiredRecord.status, 'ACQUIRED_UNVERIFIED');
  assert.equal(acquiredRecord.articles[0].acquisition, 'REMOTE_FEED');
  await page.getByText('NEWS ACQUISITION — ACQUIRED_UNVERIFIED', { exact: true }).waitFor();
  await page.reload();
  await page.getByRole('button', { name: 'Market / F&O', exact: true }).click();
  await page.getByRole('button', { name: 'browser-fixture', exact: true }).click();
  await page.getByRole('button', { name: 'Read news acquisition state', exact: true }).click();
  await page.getByText('NEWS ACQUISITION — ACQUIRED_UNVERIFIED', { exact: true }).waitFor();
  assert.deepEqual(errors, []);
  process.stdout.write('NEWS_SOURCE_CONFIGURATION_VERIFIED\n');
} finally {
  await browser.close();
}
