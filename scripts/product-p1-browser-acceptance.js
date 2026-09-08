// Product P1 browser acceptance (invoke via playwright-cli run-code).
// Five-step scenario: 打开提醒 → 查看原文 → 查看事实 → 更正事实 → 查看影响。
// Requires stage0.product_p1_browser_fixture (127.0.0.1:8000) + Vite (5173).
async (page) => {
  const origin = page.url().split('/').slice(0, 3).join('/');
  const checks = {};
  const errors = [];
  const serverErrors = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('response', response => {
    if (response.status() >= 500) serverErrors.push({ url: response.url(), status: response.status() });
  });
  const verify = (name, condition) => {
    checks[name] = Boolean(condition);
    if (!condition) throw new Error(`Acceptance failed: ${name}`);
  };
  const waitForApi = async (path, predicate, timeoutMs = 30000) => {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      try {
        const response = await page.request.get(`${origin}${path}`);
        if (response.ok()) {
          const body = await response.json();
          if (predicate(body)) return body;
        }
      } catch { /* backend not up yet */ }
      await page.waitForTimeout(300);
    }
    throw new Error(`waitForApi timed out: ${path}`);
  };

  // Vite proxies /v1; never run a mutating scenario against an unmarked server.
  const identity = await page.request.get(`${origin}/v1/acceptance/product-fixture`);
  verify('isolated_synthetic_fixture', identity.ok() && (await identity.json()).fixture === 'product-p1-synthetic');
  await waitForApi('/v1/alert-records?status=current', body => body.items.length > 0);

  // ---- 步骤 1: 打开提醒 -------------------------------------------------
  await page.goto(`${origin}/alerts`);
  await page.getByText('氨氯地平', { exact: false }).first().waitFor({ timeout: 15000 });
  await page.getByRole('button', { name: /查看详情与替代链/ }).first().click();
  await page.getByText('解释:这条结论由什么支撑').waitFor({ timeout: 10000 });
  checks.alert_detail_explanation_visible = true;

  // ---- 步骤 2: 查看原文(证据抽屉 + 精确高亮)------------------------------
  await page.getByRole('button', { name: /查看证据原文/ }).first().click();
  await page.getByTestId('evidence-highlight').waitFor({ timeout: 10000 });
  const highlighted = await page.getByTestId('evidence-highlight').textContent();
  verify('evidence_original_rendered_with_exact_highlight',
    typeof highlighted === 'string' && highlighted.includes('克拉霉素'));
  await page.getByText('未知(来源未记录版本)', { exact: true }).waitFor();
  checks.unknown_source_version_not_detector_name = true;
  await page.screenshot({ path: 'output/playwright/product-p1-evidence-drawer.png', fullPage: true });
  await page.keyboard.press('Escape');

  // ---- 步骤 3: 查看事实(关联记录 → 记录详情)------------------------------
  await page.getByRole('button', { name: /memory:medication:/ }).first().click();
  await page.getByRole('heading', { name: /记录详情/ }).waitFor({ timeout: 10000 });
  checks.fact_detail_visible = true;
  await page.screenshot({ path: 'output/playwright/product-p1-fact-detail.png', fullPage: true });
  await page.keyboard.press('Escape');

  // ---- 步骤 4: 更正事实(用药页剂量更正表单)------------------------------
  await page.goto(`${origin}/medications`);
  await page.getByRole('tab', { name: '记录剂量/用法变更' }).click();
  // 从当前药单选择氨氯地平(回填旧值)
  const select = page.locator('select').first();
  const optionTexts = await select.locator('option').allTextContents();
  const target = optionTexts.find(t => t.includes('氨氯地平'));
  verify('amlodipine_listed_for_selection', Boolean(target));
  await select.selectOption({ index: optionTexts.indexOf(target) });
  const doseBox = page.getByPlaceholder('如:5mg');
  await doseBox.fill('10mg');
  await page.getByRole('button', { name: '记录剂量变更' }).click();
  await page.getByText('记录完成', { exact: true }).first().waitFor({ timeout: 30000 });
  checks.dose_change_committed = true;

  // ---- 步骤 5: 查看影响(变更影响卡片)------------------------------------
  const impactResponse = page.waitForResponse(r => r.url().includes('/v1/change-impact?') && r.status() === 200);
  await page.getByTestId('change-impact-card').getByRole('button').first().click();
  const actualImpact = await (await impactResponse).json();
  verify('impact_is_run_attributed', actualImpact.attribution === 'run_audit' && !!actualImpact.run_id);
  verify('impact_has_real_invalidations', actualImpact.affected_conclusions.length > 0);
  verify('impact_count_matches_details', actualImpact.summary.affected_conclusions === actualImpact.affected_conclusions.length);
  await page.getByTestId('change-impact-card').getByText(/受影响结论/).first().waitFor({ timeout: 15000 });
  const impactText = await page.getByTestId('change-impact-card').textContent();
  verify('change_impact_card_renders', impactText.length > 0);
  await page.screenshot({ path: 'output/playwright/product-p1-change-impact.png', fullPage: true });

  await page.reload();
  await page.getByRole('heading', { name: '用药记录', exact: true }).waitFor();
  const reread = await page.request.get(`${origin}/v1/change-impact?run_id=${encodeURIComponent(actualImpact.run_id)}`);
  const restored = await reread.json();
  verify('impact_survives_reload', JSON.stringify(restored.changed_facts) === JSON.stringify(actualImpact.changed_facts));

  verify('no_react_pageerrors', errors.length === 0);
  verify('no_http_500', serverErrors.length === 0);
  return { checks, errors, serverErrors, origin, run_id: actualImpact.run_id, generated_at: new Date().toISOString() };
}
