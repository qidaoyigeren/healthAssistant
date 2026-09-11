async (page) => {
  const origin = page.url().split('/').slice(0,3).join('/');
  const identity = await (await page.request.get(`${origin}/v1/acceptance/live-identity`)).json();
  if (!identity.isolated || !identity.live_provider || identity.response_substitutes.length) throw new Error('Not live isolated backend');
  await page.setViewportSize({width:1400,height:1000});
  await page.goto(`${origin}/assistant`);
  await page.getByLabel('输入问题').fill('请检索现有药品说明书，解释氨氯地平与克拉霉素合用的相互作用依据，引用原文并说明证据缺口。不要提出调整剂量或停药建议。');
  const submitted = page.waitForResponse(r => r.url().endsWith('/v1/events') && r.request().method()==='POST');
  const started = Date.now();
  await page.getByRole('button',{name:'发送',exact:true}).click();
  const response = await submitted;
  const accepted = await response.json();
  const input = response.request().postDataJSON();
  let outcome;
  for (let i=0; i<150; i++) {
    outcome = await (await page.request.get(`${origin}${accepted.status_url}`)).json();
    if (['committed','failed'].includes(outcome.status)) break;
    await page.waitForTimeout(2000);
  }
  const result = outcome.response || {};
  await page.waitForTimeout(2500);
  await page.screenshot({path:'output/playwright/live-product-question.png',fullPage:true});
  const traceResponse = await page.request.get(`${origin}/v1/sessions/${input.session_id}/turns/${accepted.run_id}/trace`);
  const trace = await traceResponse.json();
  const citations = [];
  for (const ref of result.audit_trail?.source_refs || []) {
    if (ref.evidence_id) {
      const source = await page.request.get(`${origin}/v1/evidence/${ref.evidence_id}`);
      citations.push({evidence_id:ref.evidence_id,status:source.status(),body:await source.json()});
    }
  }
  return {track:'live_provider_browser_question',seconds:(Date.now()-started)/1000,
    checks:{accepted:response.status()===202,committed:outcome.status==='committed',
      succeeded:result.run_status==='succeeded',has_citations:citations.length>0,
      citations_readable:citations.length>0 && citations.every(c=>c.status===200),
      model_answer_delivered:result.audit_trail?.response_source==='llm'},
    accepted,outcome,trace,citations,generated_at:new Date().toISOString()};
}
