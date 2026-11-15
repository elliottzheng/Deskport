// 文件页手机宽度几何复测（批次 M2 配套工具）。
// 无头浏览器（ego-browser）移动模拟下量 documentElement.scrollWidth/clientWidth，
// 四宽度 × 文件页各状态，输出 PASS/FAIL 表并存截图。
//
// 跑法（需先起一个临时实例，端口/密码与下面 BASE/PASSWORD 一致）：
//     ego-browser nodejs < tools/files-mobile-geometry.mjs
// 改 BASE / OUT / DEEP 三个常量适配你的环境；DEEP 指一个 6 层左右深的目录。
// 判据：每个状态 ok:true（scrollWidth == clientWidth）且 offenders 为空。
// 依赖：ego-browser（任务空间 m2-files-mobile-overflow 可复用）。
const UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1';
const BASE = 'http://127.0.0.1:6808';
const PASSWORD = 'm2test';
const OUT = 'E:/事项/远程/.tmp/m2';
const DEEP = 'E:\\事项\\远程\\.tmp\\m2\\files\\项目-智慧校园二期第一阶段设计方案与里程碑计划\\高二年段数学学科期中质量分析数据与图表汇总\\2026-2027学年第一学期第四次阶段测验原始答题卡\\客观题评分细则与双评仲裁过程记录存档目录\\高二年段各班各题得分明细与进退步统计分析总表\\移动端界面走查问题清单与整改验收记录附件存档';
const SHALLOW = DEEP.split('\\').slice(0, 6).join('\\');
const FWD = DEEP.replace(/\\/g, '/');
const WIDTHS = [320, 375, 390, 430];

const HOOK = `window.__errs = [];
window.addEventListener('error', (e) => window.__errs.push('error: ' + (e.message || '').slice(0,120)));
const _ce = console.error;
console.error = function (...a) { window.__errs.push('console.error: ' + a.map((x) => { try { return (x && x.message) || String(x); } catch (e) { return '?'; } }).join(' ').slice(0,140)); _ce.apply(console, a); };`;

async function setMobile(page, w, h) {
  await page.cdp('Emulation.setDeviceMetricsOverride',
    { width: w, height: h, deviceScaleFactor: 3, mobile: true, screenWidth: w, screenHeight: h });
  await page.cdp('Emulation.setTouchEmulationEnabled', { enabled: true, maxTouchPoints: 5 });
  await page.cdp('Emulation.setUserAgentOverride', { userAgent: UA, platform: 'mobile' });
}

const MEASURE_FM = () => {
  const f = [...document.querySelectorAll('iframe')].find((x) => { try { return x.contentDocument && x.contentDocument.querySelector('.fm-layout'); } catch (e) { return false; } });
  if (!f) return { error: 'no fm iframe' };
  const doc = f.contentDocument;
  const de = doc.documentElement;
  const out = { docSW: de.scrollWidth, docCW: de.clientWidth, ok: de.scrollWidth === de.clientWidth,
    innerW: f.contentWindow.innerWidth, errs: (f.contentWindow.__errs || []).slice(0, 3), offenders: [] };
  const vw = f.contentWindow.innerWidth;
  doc.querySelectorAll('*').forEach((el) => {
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return;
    const cs = getComputedStyle(el);
    if (cs.position === 'fixed' || cs.position === 'absolute') return;
    if (r.right <= vw + 1) return;
    let p = el.parentElement, clipped = false;
    while (p && p !== de) {
      const pc = getComputedStyle(p);
      if (pc.overflow !== 'visible' || pc.overflowX !== 'visible') { clipped = true; break; }
      p = p.parentElement;
    }
    if (!clipped) out.offenders.push({ cls: (el.className || '').toString().slice(0, 40), right: Math.round(r.right) });
  });
  out.offenders = out.offenders.slice(0, 5);
  const name = doc.querySelector('.fm-row .fm-name');
  if (name) out.name = { w: Math.round(name.getBoundingClientRect().width), ellipsis: name.scrollWidth > name.clientWidth + 1 };
  const segs = [...doc.querySelectorAll('.crumb-seg')];
  out.crumbs = segs.map((el) => (el.textContent || '').trim().slice(0, 12));
  return out;
};

async function waitFm(page, timeout = 20000) {
  await page.waitForFunction(() => {
    const f = [...document.querySelectorAll('iframe')].find((x) => { try { return x.contentDocument && x.contentDocument.querySelector('.crumb-wrap'); } catch (e) { return false; } });
    return !!f;
  }, undefined, { timeout });
  await page.waitForTimeout(400);
}
async function gotoApp(page) {
  await page.goto(BASE + '/app?view=files', { waitUntil: 'domcontentloaded' });
  if ((await page.url()).includes('/login')) {
    await page.waitForTimeout(600);
    await page.fill('input[type="password"]', PASSWORD);
    await page.click('button[type="submit"], .btn-primary');
    await page.waitForTimeout(1600);
  }
  await waitFm(page);
}
async function loadIframe(page, qs) {
  await page.evaluate((q) => {
    const f = [...document.querySelectorAll('iframe')].find((x) => { try { return x.contentDocument && x.contentDocument.querySelector('.fm-layout'); } catch (e) { return false; } });
    if (f) { f.dataset.loaded = ''; f.src = f.src.split('?')[0] + '?embed=1' + (q || ''); }
  }, qs || '');
  await waitFm(page);
}
async function gotoPath(page, path) {
  await page.evaluate((p) => { try { localStorage.setItem('fmLastLoc', ''); localStorage.setItem('fmLastPath', p); } catch (e) {} }, path);
  await loadIframe(page, '');
}

const task = await taskSpace('m2-files-mobile-overflow');
const page = task.page('p2');
const results = [];
async function measure(tag, shot) {
  const m = await page.evaluate(MEASURE_FM);
  results.push({ tag, ...m });
  console.log(tag, JSON.stringify(m));
  if (shot) await page.screenshot({ path: OUT + '/' + shot });
  return m;
}

await page.cdp('Page.addScriptToEvaluateOnNewDocument', { source: HOOK });
await setMobile(page, 390, 844);
await gotoApp(page);

for (const w of WIDTHS) {
  await setMobile(page, w, 844);
  await gotoApp(page);
  await gotoPath(page, SHALLOW);
  await measure('SHALLOW-' + w);
  await gotoPath(page, DEEP);
  await measure('DEEP-' + w, '文件-' + w + '-01-深层.png');
  await loadIframe(page, '&loc=inbox');
  await measure('INBOX-' + w, '文件-' + w + '-03-收件箱.png');
  await gotoPath(page, DEEP);
  await page.fill('.search-input', '问题清单');
  await page.press('.search-input', 'Enter');
  await page.waitForTimeout(1500);
  await measure('SEARCH-' + w, '文件-' + w + '-04-搜索.png');
  await gotoPath(page, DEEP);
  await page.click('.fm-nav-toggle');
  await page.waitForTimeout(700);
  await measure('DRAWER-' + w, '文件-' + w + '-05-抽屉.png');
}

const bad = results.filter((r) => !r.ok || (r.offenders && r.offenders.length) || (r.errs && r.errs.length));
console.log('\n==== 汇总 ====');
for (const r of results) {
  console.log((r.ok ? 'PASS' : 'FAIL') + '  ' + r.tag.padEnd(14) + ' scrollWidth=' + r.docSW + ' clientWidth=' + r.docCW
    + (r.offenders && r.offenders.length ? ' offenders=' + JSON.stringify(r.offenders) : '')
    + (r.errs && r.errs.length ? ' errs=' + JSON.stringify(r.errs) : ''));
}
console.log(bad.length ? 'RESULT: FAIL (' + bad.length + ')' : 'RESULT: ALL PASS (' + results.length + ' 状态)');
