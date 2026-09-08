// Invoke via playwright-cli -s=harness-final run-code --filename <this file>.
// Requires a fresh harness_browser_fixture and Vite on localhost.
async (page) => {
  const checks = {};
  const errors = [];
  const serverErrors = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('response', response => {
    if (response.status() >= 500) serverErrors.push({ url: response.url(), status: response.status() });
  });
  const engine = () => page.evaluate(async () => {
    const url = performance.getEntriesByType('resource').find(e => e.name.includes('/src/api/submissions.ts')).name;
    const { submissions } = await import(url);
    return { stable: submissions.snapshot() === submissions.snapshot(), tasks: submissions.snapshot() };
  });
  const verify = (name, condition) => {
    checks[name] = Boolean(condition);
    if (!condition) throw new Error(`Acceptance failed: ${name}`);
  };
  const waitEngine = async (predicate) => {
    const deadline = Date.now() + 20000;
    while (Date.now() < deadline) {
      const snapshot = await engine();
      if (predicate(snapshot.tasks)) return snapshot;
      await page.waitForTimeout(100);
    }
    throw new Error(`engine condition timed out: ${JSON.stringify(await engine())}`);
  };
  await page.context().setOffline(false);
  const initial = await (await page.request.get('http://127.0.0.1:8000/acceptance/counts')).json();
  verify('isolated_synthetic_database', initial.medication_effects === 0 && initial.runs.length <= 1);
  if (initial.runs.length === 0) {
    await page.evaluate(() => sessionStorage.removeItem('mcp.activeSubmissions.v1'));
    await page.goto('http://127.0.0.1:5173/medications');
    await page.getByRole('textbox', { name: '药名(商品名或通用名)', exact: true }).fill('氨氯地平');
    await page.getByRole('button', { name: '记录新增用药', exact: true }).click();
  }
  await page.getByRole('button', { name: '取消任务', exact: true }).waitFor();
  await page.reload();
  await page.getByRole('button', { name: '取消任务', exact: true }).waitFor();
  const before = await engine();
  const task = before.tasks.find(t => ['queued', 'processing'].includes(t.status));
  verify('stable_external_store_snapshot', before.stable);
  verify('pending_task_restored_after_reload', Boolean(task?.key && task?.runId));
  await waitEngine(tasks => tasks.some(t => t.progress.length > 0));
  const progress = (await engine()).tasks.find(t => t.key === task.key).progress;
  verify('progress_replayed_without_duplicate_ids', new Set(progress.map(e => e.event_id)).size === progress.length);
  await page.screenshot({ path: 'output/playwright/harness-progress.png', fullPage: true });
  await page.context().setOffline(true);
  await page.getByText('状态确认中', { exact: true }).waitFor({ timeout: 15000 });
  verify('network_loss_is_unknown', (await engine()).tasks.find(t => t.key === task.key).status === 'unknown');
  await page.context().setOffline(false);
  await page.reload();
  await page.getByRole('button', { name: '取消任务', exact: true }).waitFor({ timeout: 15000 });
  const recovered = (await engine()).tasks.find(t => t.key === task.key);
  verify('reconnect_preserves_event_and_run_identity', recovered?.runId === task.runId);
  await page.getByRole('button', { name: '取消任务', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: '取消任务', exact: true }).click();
  await waitEngine(tasks => tasks.some(t => ['requested', 'cancelled'].includes(t.cancelState)));
  await page.request.post('http://127.0.0.1:8000/acceptance/release');
  await waitEngine(tasks => tasks.some(t => t.status === 'committed'
    && t.result?.run_status === 'cancelled' && t.cancelState === 'cancelled'));
  const final = (await engine()).tasks.find(t => t.key === task.key);
  if (final.result?.run_status !== 'cancelled' || final.cancelState !== 'cancelled') {
    throw new Error(`cancel state did not converge: ${JSON.stringify(final)}`);
  }
  verify('server_confirms_cancelled', true);
  const response = await page.request.get('http://127.0.0.1:8000/acceptance/counts');
  const counts = await response.json();
  verify('refresh_and_reconnect_do_not_resubmit', counts.runs.length === 1);
  verify('cancel_before_write_has_no_medication_effect', counts.medication_effects === 0);
  await page.getByText('已取消', { exact: true }).waitFor();
  await page.screenshot({ path: 'output/playwright/harness-cancelled.png', fullPage: true });
  await page.getByRole('textbox', { name: '药名(商品名或通用名)', exact: true }).fill('氨氯地平');
  await page.getByRole('button', { name: '记录新增用药', exact: true }).click();
  await waitEngine(tasks => tasks.some(t => t.status === 'committed' && t.result?.run_status === 'succeeded'));
  const success = (await engine()).tasks.find(t => t.result?.run_status === 'succeeded');
  verify('successful_graph_returns_operation_outcomes', success.result.operation_outcomes.some(o => o.outcome === 'add'));
  const after = await (await page.request.get('http://127.0.0.1:8000/acceptance/counts')).json();
  verify('one_new_submission_one_medication_effect', after.runs.length === 2 && after.medication_effects === 1);
  await page.getByText('成分:氨氯地平', { exact: true }).waitFor();
  verify('structured_ingredient_has_readable_name', !(await page.locator('body').innerText()).includes('[object Object]'));
  await page.screenshot({ path: 'output/playwright/harness-completed.png', fullPage: true });
  verify('no_react_runtime_errors', errors.length === 0);
  verify('no_http_server_errors', serverErrors.length === 0);
  return { status: 'pass', checks, counts: after, final: { key: final.key, runId: final.runId,
    status: final.status, cancelState: final.cancelState, run_status: final.result.run_status } };
}
