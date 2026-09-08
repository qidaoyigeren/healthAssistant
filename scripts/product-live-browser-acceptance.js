// Live provider acceptance: real default backend tools; synthetic inputs, isolated database.
async (page) => {
  const origin = page.url().split('/').slice(0, 3).join('/');
  const checks = {}, errors = [], serverErrors = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('response', r => { if (r.status() >= 500) serverErrors.push(r.url()); });
  const require = (okay, message) => { if (!okay) throw new Error(message); };
  const identity = await (await page.request.get(`${origin}/v1/acceptance/live-identity`)).json();
  require(identity.isolated === true && identity.live_provider === true && identity.response_substitutes.length === 0, 'must use isolated live provider host');
  checks.live_host_without_substitutes = true;
  await page.setViewportSize({width:1500,height:1000});
  await page.goto(`${origin}/materials`);
  await page.getByLabel('选择用药表图片或 PDF').setInputFiles('docs/product-upgrade/p5/samples/clear-table.png');
  const imported = page.waitForResponse(r => r.url().includes('/parse-jobs/') && r.request().method() === 'POST');
  await page.getByRole('button', {name:'上传并识别用药表'}).click();
  const parsed = await (await imported).json();
  require(parsed.status === 'completed' && parsed.parser_output.parser_version.includes('rapidocr'), 'real OCR did not complete');
  checks.real_ocr_import = true;
  await page.getByRole('complementary', {name:'材料原件'}).waitFor();
  await page.locator('article').first().getByRole('button',{name:'定位药名',exact:true}).click();
  await page.getByLabel('选中字段原文位置').waitFor();
  checks.real_bbox = true;
  require(await page.locator('article').first().getByRole('button',{name:'确认记录内容'}).isDisabled(), 'OCR must require review');
  checks.unreviewed_ocr_blocked = true;
  await page.screenshot({path:'output/playwright/live-product-p5-ocr-source.png',fullPage:true});
  const first = page.locator('article').first();
  await first.getByRole('button',{name:'补充或更正信息'}).click();
  await first.getByLabel('剂量数字',{exact:true}).fill('5.0');
  await first.getByLabel('已对照原件核实所有识别字段与患者归属').check();
  await first.getByRole('button',{name:'保存补充信息'}).click();
  await first.getByRole('button',{name:'确认记录内容'}).click();
  await page.getByText('已确认记录',{exact:true}).waitFor();
  await page.reload();
  await page.getByText('已确认记录',{exact:true}).waitFor();
  checks.partial_confirmation_survives_reload = true;
  let caseData = await (await page.request.get(`${origin}/v1/reconciliations/${parsed.case_id}`)).json();
  require(caseData.items[0].candidate.original_fields.dose === '5' && caseData.items[0].candidate.fields.dose === '5.0', 'raw/corrected provenance lost');
  require(!!caseData.items[0].receipt_id && !!caseData.items[0].safety_check, 'domain receipt or safety check missing');
  checks.original_and_correction_preserved = true;
  checks.domain_receipt_and_safety_run = true;
  // Finish OCR case, keeping the drug absent from this material.
  const aspirin = page.locator('article').filter({hasText:'阿司匹林'}).first();
  await aspirin.getByRole('button',{name:'补充或更正信息'}).click();
  await aspirin.getByLabel('已对照原件核实所有识别字段与患者归属').check();
  await aspirin.getByRole('button',{name:'保存补充信息'}).click();
  await aspirin.getByRole('button',{name:'确认记录内容'}).click();
  await page.getByText('已确认记录',{exact:true}).nth(1).waitFor();
  const omitted = page.locator('article').filter({hasText:'材料未列出'});
  while (await omitted.getByRole('button',{name:'保留现有记录'}).count()) {
    await omitted.getByRole('button',{name:'保留现有记录'}).first().click();
    await page.waitForResponse(r => r.url().includes('/v1/reconciliations/') && r.request().method() === 'GET');
  }
  await page.getByRole('status').waitFor();
  checks.not_listed_keeps_authority = true;
  // A separate material has a genuinely missing name; create a durable task.
  const csv = 'name,dose,unit,schedule,date,subject\n,500,mg,每日一次,2026-09-08,local-demo\n';
  await page.getByLabel('药单 CSV 内容').fill(csv);
  const csvResponse = page.waitForResponse(r => r.url().endsWith('/v1/materials/csv') && r.request().method() === 'POST');
  await page.getByRole('button',{name:'导入并预览差异'}).click();
  const missingCase = await (await csvResponse).json();
  require(missingCase.items[0].kind === 'unresolved', 'missing name should wait');
  checks.missing_name_waits = true;
  await page.goto(`${origin}/tasks`);
  await page.getByRole('combobox',{name:'选择材料',exact:true}).selectOption(missingCase.case_id);
  await page.getByRole('button',{name:'保存待办'}).click();
  await page.getByText('等待补充',{exact:true}).waitFor();
  await page.reload();
  await page.getByText('等待补充',{exact:true}).waitFor();
  checks.task_survives_new_session = true;
  await page.screenshot({path:'output/playwright/live-product-p3-waiting-task.png',fullPage:true});
  await page.getByRole('link',{name:'打开材料补充信息'}).first().click();
  const missing = page.locator('article').first();
  await missing.getByRole('button',{name:'补充或更正信息'}).click();
  await missing.getByLabel('药名',{exact:true}).fill('二甲双胍');
  await missing.getByRole('button',{name:'保存补充信息'}).click();
  await missing.getByRole('button',{name:'确认记录内容'}).click();
  await page.getByText('已确认记录',{exact:true}).waitFor();
  let buttons = page.getByRole('button',{name:'保留现有记录',exact:true});
  while (await buttons.count()) {
    await buttons.first().click();
    await page.waitForResponse(r => r.url().includes('/v1/reconciliations/') && r.request().method() === 'GET');
  }
  checks.supplement_resumes_same_case = true;
  const completed = await (await page.request.get(`${origin}/v1/reconciliations/${missingCase.case_id}`)).json();
  require(completed.status === 'completed', 'case incomplete');
  const safetyKey = completed.items[0].safety_check.event_key;
  for (let i=0;i<150;i++) {
    const response = await page.request.get(`${origin}/v1/events/${safetyKey}`);
    const status = await response.json();
    if (status.status === 'committed') { checks.background_safety_check_committed = true; break; }
    await page.waitForTimeout(2000);
  }
  require(checks.background_safety_check_committed, 'safety job did not commit');
  await page.goto(`${origin}/tasks`);
  await page.getByRole('region',{name:'照护待办列表'}).locator('article').first().waitFor();
  if (await page.getByRole('button',{name:'继续处理',exact:true}).count()) await page.getByRole('button',{name:'继续处理',exact:true}).first().click();
  await page.getByText('已完成',{exact:true}).first().waitFor();
  checks.task_code_verified_completion = true;
  const tasks = await (await page.request.get(`${origin}/v1/care-tasks`)).json();
  const task = tasks.items.find(t => t.case_id === missingCase.case_id);
  require(task.budget.spent > 1 && task.runs.some(r => r.runner === 'reconciliation-supplement-v1'), 'task-level budget and event association missing');
  require(task.resource_budget.tokens_reserved > 0 && task.resource_budget.tokens_reserved <= task.resource_budget.token_limit && task.resource_budget.calls_reserved <= task.resource_budget.call_limit, 'task-level resource reservations missing');
  checks.cumulative_task_budget = true;
  await page.getByRole('button',{name:'生成就诊摘要'}).click();
  await page.locator('details').last().locator('summary').click();
  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('link',{name:'下载 HTML'}).last().click();
  const download = await downloadPromise;
  await download.saveAs('output/playwright/live-product-visit-summary.html');
  checks.html_summary_download = true;
  const summaries = await (await page.request.get(`${origin}/v1/visit-summaries`)).json();
  require(summaries.items[0].markdown.includes('二甲双胍') && summaries.items[0].source_refs.length > 0, 'summary missing meds or sources');
  checks.summary_has_patient_sources = true;
  await page.screenshot({path:'output/playwright/live-product-p3-visit-summary.png',fullPage:true});
  await page.setViewportSize({width:390,height:844});
  await page.goto(`${origin}/tasks`);
  require(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 2), 'mobile page overflows');
  checks.mobile_no_horizontal_overflow = true;
  await page.screenshot({path:'output/playwright/live-product-p6-mobile.png',fullPage:true});
  checks.no_react_errors = errors.length === 0;
  checks.no_server_errors = serverErrors.length === 0;
  return {status:Object.values(checks).every(Boolean)?'pass':'fail',checks,errors,serverErrors,case_id:missingCase.case_id,task_id:task.id,generated_at:new Date().toISOString()};
}
