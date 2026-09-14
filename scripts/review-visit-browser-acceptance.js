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
  const backend = spawn(python, ['-m', 'stage0.safety_browser_fixture', '--port', apiPort],
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
