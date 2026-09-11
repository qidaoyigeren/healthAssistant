// A5 browser demo — the open-review walkthrough on the real UI + API.
//
// Flow: seed synthetic facts → start an open evidence review in the UI →
// gaps found → task waits (screenshot) → supplement the missing record
// through the task UI → incremental re-check → evidence read-back (drawer)
// → final report.  Completion is honest: `completed` only when every check
// passed; an insufficient bounded report is a retained FAILURE, not a pass.
//
// Run:  node scripts/agent-capability-demo.js   (API on :8000, Vite on :5173)
const { chromium } = require('playwright');

const API = process.env.DEMO_API || 'http://127.0.0.1:8000';
const UI = process.env.DEMO_UI || 'http://localhost:5173';

async function api(context, method, path, body) {
  const headers = { 'Content-Type': 'application/json' };
  if (method === 'POST') headers['Idempotency-Key'] = `demo-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  const response = await context.request.fetch(`${API}${path}`, { method, headers, data: body ? JSON.stringify(body) : undefined });
  return { status: response.status(), payload: await response.json().catch(() => null) };
}

(async () => {
  const steps = [];
  const step = (name, detail) => { steps.push({ name, ...detail }); console.log(`✓ ${name}`, JSON.stringify(detail ?? {})); };
  const browser = await chromium.launch({ headless: true,
    executablePath: process.env.DEMO_CHROME || undefined });
  const context = await browser.newContext({ viewport: { width: 1400, height: 1000 } });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));

  // Seed two medications (one dose deliberately lacks a unit).
  for (const [name, dose, schedule] of [['氨氯地平', '5', '每日一次'], ['克拉霉素', '250mg', '每日两次']]) {
    const accepted = await api(context, 'POST', '/v1/events', {
      event_type: 'medication_change', text: `添加${name} ${dose} ${schedule}`,
      payload: { medication: name, action: 'add', dose, schedule }, session_id: 'demo' });
    let outcome = null;
    for (let i = 0; i < 300; i++) {
      outcome = (await api(context, 'GET', accepted.payload.status_url)).payload;
      if (['committed', 'failed'].includes(outcome.status)) break;
      await page.waitForTimeout(200);
    }
    step(`seed_${name}`, { status: outcome.status });
  }

  // 1. Clean up leftover waiting tasks from previous demo runs so this run
  //    owns exactly one waiting card, then start the open review in the UI.
  for (const existing of (await api(context, 'GET', '/v1/care-tasks')).payload.items) {
    if (existing.goal_type === 'evidence_review' && !['completed', 'cancelled', 'failed'].includes(existing.status)) {
      await api(context, 'POST', `/v1/care-tasks/${existing.id}/resume`,
        { key: `demo-cleanup-${existing.revision}-${Date.now()}`, revision: existing.revision, action: 'cancel' });
    }
  }
  await page.goto(`${UI}/tasks`);
  await page.getByLabel('核查目标', { exact: false }).first().fill('核查当前用药剂量与相互作用证据');
  await page.getByRole('button', { name: '开始核查' }).click();
  await page.getByText('等待补充', { exact: false }).first().waitFor({ timeout: 60000 });
  step('review_started_and_waiting', {});

  let task = null;
  for (let i = 0; i < 50; i++) {
    const items = (await api(context, 'GET', '/v1/care-tasks')).payload.items;
    task = items.find((t) => t.goal_type === 'evidence_review' && t.status === 'waiting_input'
      && (t.missing_inputs || []).some((q) => (q.field || '').startsWith('dose_unit:')));
    if (task) break;
    await page.waitForTimeout(400);
  }
  if (!task) throw new Error('no waiting review with a dose-unit question');
  step('gaps_found', { questions: (task.missing_inputs || []).map((q) => q.field) });
  await page.screenshot({ path: 'output/a5-demo-1-waiting.png', fullPage: true });

  // 2. Partial report is readable before completion.
  const partial = await api(context, 'GET', `/v1/investigation-reports/${task.partial_report_refs[0]}`);
  step('partial_report_readable', { partial: partial.payload.partial });

  // 3. Supplement through the task UI (recorded as reported, not an approval).
  await page.getByLabel(/请补充/).first().fill('5mg');
  await page.getByRole('button', { name: '保存补充并继续核查' }).first().click();
  await page.waitForTimeout(8000);

  // 4. Poll to a terminal state (completed, or an honest failed partial report).
  let after = null;
  for (let i = 0; i < 120; i++) {
    await page.waitForTimeout(1000);
    const items = (await api(context, 'GET', '/v1/care-tasks')).payload.items;
    after = items.find((t) => t.id === task.id);
    if (['completed', 'failed', 'cancelled'].includes(after.status)) break;
  }
  step('incremental_recheck_finished', { status: after.status,
    termination: (after.investigation || {}).termination_reason,
    invalidations: (after.investigation || {}).invalidations,
    budget: after.budget });
  if (!['completed', 'failed'].includes(after.status)) throw new Error(`unexpected status ${after.status}`);
  if (after.status === 'failed' && !(after.partial_report_refs || []).length) throw new Error('failed task must retain its report');

  // 5. Evidence read-back through the UI drawer.
  const drawer = page.getByRole('button', { name: /回读证据/ }).first();
  if (await drawer.count()) {
    await drawer.click();
    await page.waitForTimeout(1500);
    step('evidence_read_back', {});
  }
  const report = await api(context, 'GET', `/v1/investigation-reports/${after.result_refs[0] ?? after.partial_report_refs[0]}`);
  step('final_report', { partial: report.payload.partial });
  await page.screenshot({ path: 'output/a5-demo-2-final.png', fullPage: true });

  if (errors.length) throw new Error(`page errors: ${errors.join('; ')}`);
  console.log(JSON.stringify({ track: 'a5-open-review-browser-demo', passed: true, steps }, null, 1));
  await browser.close();
})().catch((error) => { console.error('DEMO FAILED:', error.message); process.exit(1); });
