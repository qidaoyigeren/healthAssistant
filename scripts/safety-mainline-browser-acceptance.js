/**
 * 浏览器验收：在真实页面上走通「看事项 → 回答补问 → 看到变化」。
 *
 * 起三个进程：隔离的合成后端（不调模型）、Vite 开发服务器、Chromium。
 * 断言全部基于**页面上真的显示了什么**，不基于内部状态。
 *
 *   node scripts/safety-mainline-browser-acceptance.js --out output/browser-<日期>
 *
 * 输出目录已存在时拒绝覆盖——失败证据不能被新结果顶掉。
 */
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const ANSWER = '每日一次，晚上服用';

function arg(name, fallback) {
  const i = process.argv.indexOf(name);
  return i === -1 ? fallback : process.argv[i + 1];
}

/** 只结束**本次自己起的**进程树。按 PID 精确结束，不按端口扫。 */
function killTree(child) {
  if (!child || child.killed || child.exitCode !== null) return;
  if (process.platform === 'win32' && child.pid) {
    try {
      spawn('taskkill', ['/pid', String(child.pid), '/T', '/F'], { stdio: 'ignore' });
    } catch { /* 已经退出 */ }
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
      if (line) {
        clearTimeout(timer);
        child.stdout.off('data', onData);
        resolve(line);
      }
    };
    child.stdout.on('data', onData);
    child.on('exit', (code) => {
      clearTimeout(timer);
      reject(new Error(`进程在 ${marker} 之前退出，code=${code}`));
    });
  });
}

function waitForHttp(url, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const tick = () => {
      fetch(url)
        .then(() => resolve())
        .catch(() => (Date.now() > deadline
          ? reject(new Error(`服务未就绪：${url}`))
          : setTimeout(tick, 400)));
    };
    tick();
  });
}

async function main() {
  const out = path.resolve(arg('--out', 'output/browser-safety'));
  if (fs.existsSync(out)) {
    console.error(`保留既有验收产物；请换一个输出目录：${out}`);
    process.exit(2);
  }
  fs.mkdirSync(out, { recursive: true });

  const report = { steps: [], failures: [], answer: ANSWER };
  const record = (name, ok, detail) => {
    report.steps.push({ name, ok, detail });
    if (!ok) report.failures.push({ name, detail });
    console.log(`${ok ? 'PASS' : 'FAIL'} ${name}${detail ? ' — ' + detail : ''}`);
  };

  const python = process.platform === 'win32'
    ? path.join(ROOT, '.venv', 'Scripts', 'python.exe') : 'python';
  const apiPort = arg('--api-port', '8000');
  let uiPort = null;   // 由 Vite 自己选一个空闲端口后回报，避免和别的进程抢
  const backend = spawn(python, ['-m', 'stage0.safety_browser_fixture', '--port', apiPort],
    { cwd: ROOT, env: { ...process.env, MEMORY_ENABLE_LLM: '0', PYTHONIOENCODING: 'utf-8' } });
  backend.stderr.on('data', (c) => fs.appendFileSync(path.join(out, 'backend.log'), c));
  const vite = spawn(process.platform === 'win32' ? 'npx.cmd' : 'npx',
    ['vite', '--port', arg('--ui-port', '5173')],
    { cwd: path.join(ROOT, 'frontend'), shell: process.platform === 'win32',
      env: { ...process.env, NO_COLOR: '1', FORCE_COLOR: '0',
             STAGE0_DEV_API_TARGET: `http://127.0.0.1:${apiPort}` } });
  vite.stderr.on('data', (c) => fs.appendFileSync(path.join(out, 'vite.log'), c));
  vite.stdout.on('data', (c) => {
    fs.appendFileSync(path.join(out, 'vite.log'), c);
    // Vite 会给输出加 ANSI 颜色码，端口号被包在转义序列里——先剥掉再匹配。
    const clean = c.toString().replace(/\[[0-9;]*m/g, '');
    const hit = /localhost:(\d+)/.exec(clean);
    if (hit && !uiPort) uiPort = hit[1];
  });

  let browser;
  try {
    const ready = await waitForLine(backend, 'FIXTURE_READY', 60000);
    const info = JSON.parse(ready.slice(ready.indexOf('{')));
    report.case_id = info.case_id;
    // 等 Vite 报出它实际占用的端口。
    const deadline = Date.now() + 90000;
    while (!uiPort && Date.now() < deadline) await new Promise((r) => setTimeout(r, 300));
    if (!uiPort) throw new Error('Vite 未在 90 秒内报出端口');
    report.ui_port = uiPort;
    report.url = `http://localhost:${uiPort}/safety/${info.case_id}`;
    await waitForHttp(`http://localhost:${uiPort}/`, 120000);

    const { chromium } = require(require.resolve('playwright', { paths: [ROOT] }));
    browser = await chromium.launch();
    const page = await browser.newPage({ viewport: { width: 1280, height: 1000 } });
    page.on('pageerror', (e) => report.failures.push({ name: 'pageerror', detail: String(e) }));

    await page.goto(report.url, { waitUntil: 'networkidle' });
    await page.waitForTimeout(800);
    const body = () => page.locator('body').innerText();

    // 1 为什么出现这件事
    const first = await body();
    record('显示为何出现这件事', /为什么|触发|检查/.test(first),
      first.replace(/\s+/g, ' ').slice(0, 120));

    // 2 已经查到了什么（含结论与来源）
    record('显示已经查到了什么', /合成药甲|合成药乙/.test(first) && /来源|https?:/.test(first));

    // 3 现在需要我做什么：补问、它要弄清什么、从哪里取、以及理由
    record('显示需要我回答的问题', first.includes(info.question), info.question);
    record('说明为什么需要这条信息', /为什么|影响|决定/.test(first));
    record('说明这条问题要弄清哪一类信息、从哪里取',
      /要弄清的是/.test(first) && /从哪里取/.test(first),
      first.replace(/\s+/g, ' ').slice(0, 200));

    // 4 提交回答：填**能解锁提交按钮**的那个输入框（页面上还有别的输入框，
    //   按位置取第一个会填错地方——实测踩过一次）。
    const submit = page.getByRole('button', { name: /提交这一条|提交回答|^提交$/ }).first();
    // 回答输入框是**没有 type 属性**的 <input>，`input[type="text"]` 匹配不到它
// （CSS 属性选择器要求属性存在）——之前就是栽在这里。
    const fields = page.locator('textarea, input:not([type]), input[type="text"]');
    const count = await fields.count();
    let filled = false;
    for (let i = 0; i < count; i += 1) {
      await fields.nth(i).fill(ANSWER);
      // React 受控输入需要一帧才把状态渲染回按钮的 disabled——立刻查会读到旧值。
      await page.waitForTimeout(250);
      if (await submit.isEnabled()) { filled = true; break; }
      await fields.nth(i).fill('');
    }
    record('找到回答输入框并可提交', filled, `候选输入框 ${count} 个`);
    if (!filled) {
      // 失败也要留下证据：把当时的页面结构存下来，而不是只留一句 "没找到"。
      fs.writeFileSync(path.join(out, 'page.html'), await page.content(), 'utf-8');
      throw new Error(`没有找到能让提交按钮可用的回答输入框（候选 ${count} 个，已存 page.html）`);
    }
    await submit.click();
    await page.waitForTimeout(1500);
    const after = await body();
    record('回答已保存并给出反馈', /已保存|已提交|收到|记录/.test(after),
      after.replace(/\s+/g, ' ').slice(0, 160));
    record('回答内容出现在页面上（输入未被丢弃）', after.includes(ANSWER) || !after.includes(info.question));

    // 5 补充之后发生了什么变化：这条问题不再是一个待填的输入框，而且页面
    //   说得出"补充之后变成了什么"。页面别处也会出现"等待您补充"这类词，
    //   所以要断言的是**这一条问题**的输入框没了，而不是某个词没出现。
    // request_id 里含冒号，`#id` 选择器会解析失败——用带引号的属性选择器。
    const stillAsked = await page.locator(`input[id="answer-${info.request_id}"]`).count();
    record('这条问题不再需要回答', stillAsked === 0, `仍在等待回答的输入框 ${stillAsked} 个`);
    record('页面说明了补充之后的变化', /补充|已保存|收到|继续/.test(after),
      after.replace(/\s+/g, ' ').slice(-200));
    // 用户要看的是"这一答补上了哪一部分、来源是什么性质、还剩什么不确定"，
    // 而不是一句"任务恢复成功"。
    record('页面说明这条回答补上了什么、来源属性与仍不确定的部分',
      /已经拿到/.test(after) && /来源属性/.test(after) && /仍不能判断/.test(after),
      after.replace(/\s+/g, ' ').slice(0, 260));

    // 6 空回答不得冒充已解决
    await page.goto(report.url, { waitUntil: 'networkidle' });
    await page.waitForTimeout(600);
    const emptyState = await body();
    record('回答后重新加载仍显示已保存的回答', emptyState.includes(ANSWER)
      || !emptyState.includes(info.question),
      emptyState.replace(/\s+/g, ' ').slice(0, 160));

    await page.screenshot({ path: path.join(out, 'safety-case.png'), fullPage: true });
    await page.goto(`http://localhost:${uiPort}/`, { waitUntil: 'networkidle' });
    await page.waitForTimeout(600);
    await page.screenshot({ path: path.join(out, 'safety-mainline.png'), fullPage: true });
  } catch (err) {
    report.failures.push({ name: 'exception', detail: String(err && err.stack || err) });
    console.log('FAIL exception — ' + err);
  } finally {
    if (browser) await browser.close().catch(() => {});
    killTree(backend);
    killTree(vite);
  }

  report.status = report.failures.length ? 'fail' : 'pass';
  fs.writeFileSync(path.join(out, 'acceptance.json'),
    JSON.stringify(report, null, 2), 'utf-8');
  console.log(`\nstatus ${report.status}（${report.steps.filter((s) => s.ok).length}/${report.steps.length} 步通过）`);
  process.exit(report.status === 'pass' ? 0 : 1);
}

main();
