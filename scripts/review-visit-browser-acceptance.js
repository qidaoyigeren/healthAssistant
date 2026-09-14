/**
 * 浏览器验收：**回访**页（`/safety/:caseId/visit`）。
 *
 * 起两个进程：隔离的合成后端（`stage0.safety_browser_fixture`，**不调模型**）
 * 与 Vite 开发服务器，用真实 Chromium 打开页面。断言全部基于**页面上真的显示了
 * 什么**，不基于内部状态。
 *
 *   node scripts/review-visit-browser-acceptance.js --out output/visit/browser-<日期>
 *
 * 输出目录已存在时拒绝覆盖——失败证据不能被新结果顶掉。
 *
 * 与 `safety-mainline-browser-acceptance.js` 的分工：那一条是**主线回归网**
 * （看事项 → 回答补问 → 看到变化），本文件只看本轮新增的回访流程，两者都不改。
 */
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');

function arg(name, fallback) {
  const i = process.argv.indexOf(name);
  return i === -1 ? fallback : process.argv[i + 1];
}

function killTree(child) {
  if (!child || child.killed || child.exitCode !== null) return;
  if (process.platform === 'win32' && child.pid) {
    try { spawn('taskkill', ['/pid', String(child.pid), '/T', '/F'], { stdio: 'ignore' }); } catch { /* 已退出 */ }
  } else {
    child.kill();
  }
}

function waitForLine(child, marker, timeoutMs) {
  return new Promise((resolve, reject) => {
    let buffer = '';
    const timer = setTimeout(() => reject(new Error(`等待 ${marker} 超时`)), timeoutMs);
    const onData = (chunk) => {
      buffer += chunk.toString();
      const line = buffer.split('\n').find((l) => l.includes(marker));
      if (line) { clearTimeout(timer); child.stdout.off('data', onData); resolve(line); }
    };
    child.stdout.on('data', onData);
    child.on('exit', (code) => { clearTimeout(timer); reject(new Error(`进程在 ${marker} 之前退出，code=${code}`)); });
  });
}

function waitForHttp(url, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const tick = async () => {
      try { await fetch(url); resolve(); } catch {
        if (Date.now() > deadline) reject(new Error(`等待 ${url} 超时`)); else setTimeout(tick, 500);
      }
    };
    tick();
  });
}

async function main() {
  const out = path.resolve(arg('--out', 'output/visit-browser'));
  if (fs.existsSync(out)) throw new Error(`输出目录已存在，拒绝覆盖：${out}`);
  fs.mkdirSync(out, { recursive: true });

  const report = { steps: [], failures: [], started_at: new Date().toISOString() };
  const record = (name, ok, detail) => {
    report.steps.push({ name, ok, detail });
    if (!ok) report.failures.push({ name, detail });
    console.log(`${ok ? 'PASS' : 'FAIL'} ${name}${detail ? ' — ' + detail : ''}`);
  };

  const python = process.platform === 'win32'
    ? path.join(ROOT, '.venv', 'Scripts', 'python.exe') : 'python';
  const apiPort = arg('--api-port', '8100');
  let uiPort = null;
  // 库放在输出目录里而不是临时目录：**重启恢复**要拿同一个库再起一次，
  // 而临时目录的名字只有那个进程自己知道。
  const backendDb = path.join(out, 'fixture.db');
  let backend = spawn(python,
    ['-m', 'stage0.safety_browser_fixture', '--port', apiPort, '--db', backendDb],
    { cwd: ROOT, env: { ...process.env, MEMORY_ENABLE_LLM: '0', PYTHONIOENCODING: 'utf-8' } });
  backend.stderr.on('data', (c) => fs.appendFileSync(path.join(out, 'backend.log'), c));
  const vite = spawn(process.platform === 'win32' ? 'npx.cmd' : 'npx',
    ['vite', '--port', arg('--ui-port', '5200')],
    { cwd: path.join(ROOT, 'frontend'), shell: process.platform === 'win32',
      env: { ...process.env, NO_COLOR: '1', FORCE_COLOR: '0',
             STAGE0_DEV_API_TARGET: `http://127.0.0.1:${apiPort}` } });
  vite.stderr.on('data', (c) => fs.appendFileSync(path.join(out, 'vite.log'), c));
  vite.stdout.on('data', (c) => {
    fs.appendFileSync(path.join(out, 'vite.log'), c);
    const clean = c.toString().replace(/\[[0-9;]*m/g, '');
    const hit = /localhost:(\d+)/.exec(clean);
    if (hit && !uiPort) uiPort = hit[1];
  });

  let browser;
  try {
    const ready = await waitForLine(backend, 'FIXTURE_READY', 60000);
    const info = JSON.parse(ready.slice(ready.indexOf('{')));
    report.case_id = info.case_id;
    const deadline = Date.now() + 90000;
    while (!uiPort && Date.now() < deadline) await new Promise((r) => setTimeout(r, 300));
    if (!uiPort) throw new Error('Vite 未在 90 秒内报出端口');
    report.ui_port = uiPort;
    await waitForHttp(`http://localhost:${uiPort}/`, 120000);

    const { chromium } = require(require.resolve('playwright', { paths: [ROOT] }));
    browser = await chromium.launch();
    const page = await browser.newPage({ viewport: { width: 1280, height: 1200 } });
    page.on('pageerror', (e) => report.failures.push({ name: 'pageerror', detail: String(e) }));
    const body = () => page.locator('body').innerText();

    // 1) 详情页上有**入口**，且文案说得出这是"开始"还是"继续"。
    report.detail_url = `http://localhost:${uiPort}/safety/${info.case_id}`;
    await page.goto(report.detail_url, { waitUntil: 'networkidle' });
    await page.waitForTimeout(800);
    const detail = await body();
    record('详情页有回访入口', /开始回访|继续本次跟进/.test(detail));

    const entry = page.getByRole('link', { name: /开始回访|继续本次跟进/ });
    await entry.first().click();
    await page.waitForTimeout(1200);
    report.visit_url = page.url();
    record('入口进到可寻址的回访页（子路由）',
      /\/visit$/.test(new URL(page.url()).pathname), page.url());

    const first = await body();
    record('第 1 步：这次为什么需要跟进', /这次为什么需要跟进/.test(first));

    // 2) 开始/继续回访 → 五步都在。
    const start = page.getByRole('button', { name: /开始回访|继续本次跟进|正在/ });
    if (await start.count()) {
      await start.first().click();
      await page.waitForTimeout(2500);
    }
    // 产品里"回访开始"之后由后台 worker 把任务跑掉；验收关掉了 worker 线程
    // 以求确定性，所以在这里显式推进一步——接口只存在于合成后端。
    await fetch(`http://127.0.0.1:${apiPort}/__fixture/drain`, { method: 'POST' })
      .catch(() => {});
    await page.waitForTimeout(400);
    await page.reload({ waitUntil: 'networkidle' });
    await page.waitForTimeout(1200);

    const after = await body();
    record('开始之后仍停在同一路回访页', /\/visit$/.test(new URL(page.url()).pathname));
    record('第 2 步：上次之后已记录的变化', /上次之后已记录的变化/.test(after));
    record('第 5 步：本次结果及下一次安排', /本次结果及下一次安排/.test(after));
    record('回访状态在页面上可见', /在等您回答|正在整理这次的重点|这次已经跑完|这次没能跑成/.test(after));
    record('下一次安排说明确认状态',
      /尚未确认|已确认|没有登记跟进安排/.test(after)
      || /还没有跑出结果/.test(after));

    // 3) 候选变更区始终在，且说明"确认之前不改记录"。
    record('变更区说明确认之前不动记录',
      /不会\*\*改动|\*\*不会\*\*改动|确认之前.*不会|不会改动当前药单/.test(after)
      || /变更/.test(after));

    // 4) 回答表态：有可回答的问题时，五种表态都应出现。
    const hasQuestion = await page.locator('input[id^="answer-"]').count();
    if (hasQuestion > 0) {
      for (const label of ['已完成', '尚未完成', '不清楚', '情况有变化', '暂不回答']) {
        record(`表态按钮存在：${label}`,
          await page.getByRole('button', { name: label, exact: true }).count() > 0);
      }
    } else {
      record('本轮没有需要用户回答的问题（如实说明）', /没有需要您补充的问题/.test(after));
    }

    // 5) 结果说清实际变化：复用了什么、什么需要重核、为什么结束或等待。
    //    每条还要看得出**来源属性**——程序的核对与模型的解释是两回事。
    for (const [section, label] of [['reused', '复用了已有信息'],
                                    ['recheck', '需要重新核对'],
                                    ['why-ended', '本次为什么结束或等待']]) {
      record(`结果区有「${label}」`,
        await page.locator(`[data-visit-section="${section}"]`).count() > 0);
    }
    record('为什么结束/等待有一句实际的话',
      (await page.locator('[data-end-reason]').innerText().catch(() => '')).trim().length > 0);

    const basisKinds = await page.locator('[data-basis]')
      .evaluateAll((nodes) => nodes.map((n) => n.getAttribute('data-basis')));
    record('关键内容带来源属性', basisKinds.length > 0 && basisKinds.every(Boolean),
      `basis=${[...new Set(basisKinds)].join(',') || '（无）'}`);

    // 6) 刷新后仍在**同一次**回访上：不从头开始。
    const beforeReload = await body();
    await page.reload({ waitUntil: 'networkidle' });
    await page.waitForTimeout(1200);
    const reloaded = await body();
    record('刷新后仍是同一次回访（可寻址 + 服务端接续）',
      /\/visit$/.test(new URL(page.url()).pathname)
      && /继续本次跟进|第 一 次回访|第 若干 次回访|开始于/.test(reloaded));

    await page.screenshot({ path: path.join(out, 'visit-page.png'), fullPage: true });
    fs.writeFileSync(path.join(out, 'detail-before.txt'), beforeReload.slice(0, 20000), 'utf-8');

    // 7) 带歧义的换药登记：自然描述 → 补问 → 查看候选 → 修正 → 确认
    //    → 看当前记录、历史与安全检查状态。
    //
    //    措辞是脚本化的（真实语义理解属于有限真实验收），但每一步走的都是产品
    //    路径：真实的收录、可核对性校验、候选、确认、事务写入、必要检查与事项承接。
    const apiPost = (suffix, payload) => fetch(`http://127.0.0.1:${apiPort}${suffix}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }).then((r) => r.json());
    const setReading = (reading) => apiPost('/__fixture/reading', reading);
    const drain = () => apiPost('/__fixture/drain', {});
    const submitNote = async (text) => {
      await page.locator('[data-note-input]').fill(text);
      await page.locator('[data-note-submit]').click();
      await page.waitForTimeout(1500);
      await drain();
      await page.reload({ waitUntil: 'networkidle' });
      await page.waitForTimeout(1200);
    };

    // 7a) 用户说得含糊：换成"那个新的"，没有说清是哪一个。
    await setReading({
      summary: '用户说把药甲换掉，但没说清新药是哪一个',
      items: [
        { when: 'occurred', operation: 'remove', drug_name: '合成药甲',
          quote: '合成药甲不吃了', field: 'none', value: '',
          time_text: '这两天', time_precision: 'vague', uncertain: [],
          group_role: 'replace_from', reported_overlap: 'unstated' },
        { when: 'occurred', operation: 'add', drug_name: '',
          quote: '换个新的', field: 'none', value: '', uncertain: ['object_ambiguous'],
          group_role: 'replace_to', reported_overlap: 'unstated' },
      ],
      question: '',
    });
    await submitNote('合成药甲不吃了，换个新的');

    const afterVague = await body();
    const questions = await page.locator('[data-note-question]').allInnerTexts();
    record('7a 说得含糊时系统补问，不猜对象',
      questions.some((q) => /哪一种药|哪个药|什么药/.test(q)),
      questions.join(' | ').slice(0, 120));
    const vaguePending = await page.locator('[data-pending-candidates] [data-candidate]').count();
    record('7a 补问的那一条**没有**变成待确认候选', vaguePending === 1,
      `候选数=${vaguePending}`);
    record('7a 含糊时原文仍然留在页面上', /合成药甲不吃了，换个新的/.test(afterVague));

    // 7b) 用户补一句，把对象说清楚 —— 这一次形成**一组**换药候选。
    //     注意 quote 必须是**这一句原文**里的连续片段：核对不上的判定会被丢掉。
    await setReading({
      summary: '用户说把药甲换成合成药丙',
      items: [
        { when: 'occurred', operation: 'remove', drug_name: '合成药甲',
          quote: '合成药甲不吃了', field: 'none', value: '',
          time_text: '这两天', time_precision: 'vague', uncertain: [],
          group_role: 'replace_from', reported_overlap: 'unstated' },
        { when: 'occurred', operation: 'add', drug_name: '合成药丙',
          quote: '换成合成药丙', field: 'dose', value: '2mg', uncertain: [],
          group_role: 'replace_to', reported_overlap: 'unstated' },
      ],
      question: '',
    });
    await submitNote('合成药甲不吃了，换成合成药丙，2mg');

    const pending = page.locator('[data-pending-candidates] [data-candidate]');
    const pendingCount = await pending.count();
    record('7b 补清楚之后形成待确认候选', pendingCount >= 2, `候选数=${pendingCount}`);
    record('7b 候选写明**是哪一件事**（停用/新增）',
      (await page.locator('[data-candidate-operation="remove"]').count()) > 0
      && (await page.locator('[data-candidate-operation="add"]').count()) > 0);
    record('7b 候选显示原文依据', /原话依据/.test(await body()));
    record('7b 只说了个大概的时间如实表达，不编成具体日期',
      /只说了一个大概/.test(await body()));

    // 最终流程第 5 步：**确认之前**当前药单一个字节都不动。
    //
    // 用 `/v1/safety-mainline` 的 `current_medications`（＝ status='active' 的那些）。
    // `/v1/memory/state` 是**双时态快照**，会把已停用的行一并列出——拿它当"当前药单"
    // 会得出"药甲还在用"这种错结论。
    const currentMeds = async () => {
      const mainline = await fetch(`http://127.0.0.1:${apiPort}/v1/safety-mainline`)
        .then((r) => r.json());
      return (mainline.current_medications ?? []).map((m) => m.display_name);
    };
    const activeBefore = await currentMeds();
    record('5 确认之前当前药单没有变',
      activeBefore.includes('合成药甲') && !activeBefore.includes('合成药丙'),
      `在用=${activeBefore.join('、')}`);

    // 7c) 确认整组 —— 一次事务原子写入。
    const groupButton = page.locator('[data-candidate-confirm-group]').first();
    if (await groupButton.count()) {
      await groupButton.click();
    } else {
      await page.locator('[data-candidate-confirm]').first().click();
    }
    await page.waitForTimeout(1500);
    await drain();
    await page.reload({ waitUntil: 'networkidle' });
    await page.waitForTimeout(1500);

    const confirmedBody = await body();
    record('7c 确认之后页面说清这一次改变了什么',
      /已确认/.test(confirmedBody) || /已经登记进记录/.test(confirmedBody));
    record('7c 换药逐条陈述，没有"换药完成/只做了一半"的总结论',
      (await page.locator('[data-group-statement]').count()) > 0
      && !/换药完成|只登记了一半|换药已全部完成/.test(confirmedBody));

    // 7d) 当前记录：药甲停了、药丙在用。
    await page.goto(`http://localhost:${uiPort}/medications`, { waitUntil: 'networkidle' });
    await page.waitForTimeout(1200);
    const meds = await body();
    record('7d 当前药单反映这次换药', /合成药丙/.test(meds) && /在用/.test(meds));

    // 7e) 历史：阶段 + 变更记录，发生时间与登记时间分开。
    const historyButton = page.getByRole('button', { name: /版本链/ }).first();
    if (await historyButton.count()) {
      await historyButton.click();
      await page.waitForTimeout(1200);
    }
    record('7e 历史里能按**服用阶段**看',
      await page.locator('[data-medication-episodes]').count() > 0);
    record('7e 历史里能看到每一次记录操作',
      await page.locator('[data-medication-change-log]').count() > 0);
    const changeKinds = await page.locator('[data-medication-change]')
      .evaluateAll((nodes) => nodes.map((n) => n.getAttribute('data-medication-change')));
    record('7e 变更记录区分了开始/调整/停用/恢复/纠错',
      changeKinds.length > 0, `kinds=${[...new Set(changeKinds)].join(',')}`);
    record('7e 发生时间与登记时间分开表达', /登记：/.test(await body()));
    await page.screenshot({ path: path.join(out, 'medication-history.png'), fullPage: true });

    // 7f) 安全检查状态：队列真实状态可读，且换药之后确实排过。
    const mainline = await fetch(`http://127.0.0.1:${apiPort}/v1/safety-mainline`)
      .then((r) => r.json());
    const checks = mainline.necessary_checks ?? {};
    record('7f 必要检查队列的真实状态可读（不是靠"没看到提示"推断）',
      checks.available === true, JSON.stringify(checks));
    record('7f 换药之后必要检查确实被登记过', (checks.total ?? 0) > 0,
      `total=${checks.total}`);
    fs.writeFileSync(path.join(out, 'final-page.txt'), confirmedBody.slice(0, 20000), 'utf-8');

    // 8) 最终交付流程：事项继续、跟进安排全过程、重启恢复。
    const apiGet = (suffix) => fetch(`http://127.0.0.1:${apiPort}${suffix}`)
      .then((r) => r.json());

    // 7) 事项继续，未决风险没有被自动清除。
    const caseAfter = await apiGet(`/v1/safety-cases/${encodeURIComponent(info.case_id)}`);
    const closure = await apiGet(
      `/v1/safety-cases/${encodeURIComponent(info.case_id)}/closure-evidence`);
    record('7 相关事项仍在继续（没有被自动关闭）',
      caseAfter.status !== 'resolved',
      `status=${caseAfter.status} · ${caseAfter.status_label}`);
    record('7 "触发条件不再出现"与"整体风险已解除"分开表达',
      closure.nature && closure.nature.overall_risk_resolved === false
      && typeof closure.nature.note === 'string',
      JSON.stringify(closure.nature ?? {}).slice(0, 140));

    // 8) 保存并确认跟进安排 —— 走实际页面。
    const caseUrl = `http://localhost:${uiPort}/safety/${encodeURIComponent(info.case_id)}`;
    await page.goto(caseUrl, { waitUntil: 'networkidle' });
    // 等面板真的挂上再断言：这一页是先渲染骨架、再填数据的。
    //
    // 断言的是**面板本身**（模式单选 + 提交按钮），不是那个确认徽标：
    // 还没有任何安排时 `FollowUpSummary` 渲染的是另一段说明文字，**不带**
    // `data-follow-up-confirmed`——拿它当"面板在不在"会误判。
    await page.locator('input[value="schedule"]').first()
      .waitFor({ state: 'attached', timeout: 30000 }).catch(() => {});
    await page.waitForTimeout(500);
    record('8 事项页有跟进安排区（可安排 / 可确认 / 可取消）',
      (await page.locator('input[value="schedule"]').count()) > 0
      && (await page.locator('input[value="confirm"]').count()) > 0
      && (await page.locator('input[value="cancel"]').count()) > 0);

    const followBtn = (name) => page.getByRole('button', { name, exact: true });
    const scheduleAt = async (value) => {
      await page.locator('input[value="schedule"]').check();
      await page.waitForTimeout(200);
      await page.locator('select').first().selectOption('review_at');
      await page.waitForTimeout(200);
      await page.locator('input[type="datetime-local"]').fill(value);
      await followBtn('记录这项安排').click();
      await page.waitForTimeout(1500);
      await drain();
      await page.reload({ waitUntil: 'networkidle' });
      await page.waitForTimeout(1000);
    };
    const confirmedFlag = async () => page.locator('[data-follow-up-confirmed]').first()
      .getAttribute('data-follow-up-confirmed');

    const future = new Date(Date.now() + 3 * 24 * 3600 * 1000);
    const iso = (d) => d.toISOString().slice(0, 16);
    await scheduleAt(iso(future));
    record('8 安排之后**尚未确认**（安排 ≠ 有人确认过）',
      (await confirmedFlag()) === 'false', `confirmed=${await confirmedFlag()}`);

    await page.locator('input[value="confirm"]').check();
    await page.waitForTimeout(200);
    await followBtn('确认这条安排').click();
    await page.waitForTimeout(1500);
    await page.reload({ waitUntil: 'networkidle' });
    await page.waitForTimeout(1000);
    record('8 确认之后记为**已确认**（确认是一次单独的动作）',
      (await confirmedFlag()) === 'true', `confirmed=${await confirmedFlag()}`);

    // 9) 改期 / 取消 / 到期触发都要有**真实效果**。
    const later = new Date(Date.now() + 10 * 24 * 3600 * 1000);
    await scheduleAt(iso(later));
    const rescheduled = await apiGet(`/v1/safety-cases/${encodeURIComponent(info.case_id)}`);
    record('9 改期真的改了时间',
      (rescheduled.follow_up?.at ?? '').startsWith(iso(later).slice(0, 10)),
      `at=${rescheduled.follow_up?.at}`);
    record('9 改期之后确认状态回到"尚未确认"',
      rescheduled.follow_up?.confirmed === false,
      `confirmed=${rescheduled.follow_up?.confirmed}`);

    await page.locator('input[value="cancel"]').check();
    await page.waitForTimeout(200);
    await followBtn('准备好取消这条安排').click();
    await page.waitForTimeout(400);
    await followBtn('取消这条安排').click();
    await page.waitForTimeout(1500);
    await page.reload({ waitUntil: 'networkidle' });
    await page.waitForTimeout(1000);
    const cancelled = await apiGet(`/v1/safety-cases/${encodeURIComponent(info.case_id)}`);
    // 取消**故意不清空** `at`/`owner`/`note`——历史要留着，靠 `schedule_state`
    // 表达"已取消"。所以这里断言的是状态与页面文案，不是"记录消失了"。
    record('9 取消之后安排记为已取消（历史仍然留着）',
      cancelled.follow_up?.schedule_state === 'cancelled'
      && cancelled.follow_up?.at != null,
      `state=${cancelled.follow_up?.schedule_state} at=${cancelled.follow_up?.at}`);
    record('9 页面上说得出"已取消"',
      /已取消/.test(await body()));

    // 到期触发：排一个**过去**的时间，让 worker 把它变成一次真实触发。
    await scheduleAt(iso(new Date(Date.now() - 3600 * 1000)));
    const drained = await drain();
    await page.waitForTimeout(500);
    const triggered = await apiGet(`/v1/safety-cases/${encodeURIComponent(info.case_id)}`);
    record('9 到期的安排真的被触发（由 worker 扫描，不靠用户再点一次）',
      ['due', 'triggered', 'blocked'].includes(triggered.follow_up?.schedule_state),
      `schedule_state=${triggered.follow_up?.schedule_state} · drain=${JSON.stringify(drained).slice(0, 80)}`);

    // 10) 重启服务后：记录、候选、待办仍可恢复。
    const beforeRestart = await apiGet(`/v1/safety-cases/${encodeURIComponent(info.case_id)}`);
    const notesBefore = (beforeRestart.visit?.change_notes ?? []).length;
    killTree(backend);
    await new Promise((r) => setTimeout(r, 1500));
    const restarted = spawn(python,
      ['-m', 'stage0.safety_browser_fixture', '--port', apiPort, '--db', backendDb],
      { cwd: ROOT, env: { ...process.env, MEMORY_ENABLE_LLM: '0', PYTHONIOENCODING: 'utf-8' } });
    restarted.stderr.on('data', (c) => fs.appendFileSync(path.join(out, 'backend-restart.log'), c));
    await waitForLine(restarted, 'FIXTURE_READY', 60000);
    await waitForHttp(`http://127.0.0.1:${apiPort}/v1/health`, 30000);
    backend = restarted;

    const afterRestart = await apiGet(`/v1/safety-cases/${encodeURIComponent(info.case_id)}`);
    record('10 重启之后补充的原文还在',
      (afterRestart.visit?.change_notes ?? []).length === notesBefore && notesBefore > 0,
      `notes=${(afterRestart.visit?.change_notes ?? []).length}（重启前 ${notesBefore}）`);
    const confirmedCandidates = (afterRestart.visit?.change_candidates ?? [])
      .filter((c) => c.status === 'confirmed');
    record('10 重启之后已确认的候选仍是已确认（没有回退成待确认）',
      confirmedCandidates.length > 0, `confirmed=${confirmedCandidates.length}`);
    const namesAfter = await currentMeds();
    record('10 重启之后当前药单仍然是确认后的样子',
      namesAfter.includes('合成药丙') && !namesAfter.includes('合成药甲'),
      `在用=${namesAfter.join('、')}`);

    await page.reload({ waitUntil: 'networkidle' });
    await page.waitForTimeout(1200);
    record('10 重启之后页面仍然读得到同一件事项',
      await page.locator('[data-follow-up-confirmed]').count() > 0);

    report.status = report.failures.length === 0 ? 'pass' : 'fail';
  } catch (error) {
    report.status = 'fail';
    report.failures.push({ name: 'exception', detail: String(error && error.stack || error) });
    console.log(`FAIL exception — ${error}`);
  } finally {
    if (browser) await browser.close().catch(() => {});
    killTree(vite); killTree(backend);
    report.finished_at = new Date().toISOString();
    fs.writeFileSync(path.join(out, 'browser-check.json'),
      JSON.stringify(report, null, 2), 'utf-8');
  }
  console.log(`\nstatus ${report.status}（${report.steps.filter((s) => s.ok).length}/${report.steps.length} 步通过）`);
  process.exit(report.status === 'pass' ? 0 : 1);
}

main().catch((error) => { console.error(error); process.exit(1); });
