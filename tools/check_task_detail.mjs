#!/usr/bin/env node
// 任务详情（事件流）几何断言（批次 U3）：N 个任务 × M 个宽度，断言
//   document.documentElement.scrollWidth == clientWidth（页面无横向滚动）
//   .task-chat.scrollWidth == clientWidth（事件流容器无横向滚动）
// 并把超宽元素、`{` 残片、空白事件、原始 JSON 行打出来（异常清单用）。
// 每个宽度只加载一次页面，靠点树节点切任务（手机上先「← 返回」）。
// 用法：
//   node tools/check_task_detail.mjs --base http://127.0.0.1:6815 \
//        --password walk-u3d-1234 --shots E:/事项/远程/.tmp/u3d [--tasks id1,id2] [--widths 320,390,768,1440]
// 自带 90 秒看门狗；退出码：有任何一处几何断言不过 = 1。
import fs from 'node:fs';
import { spawn } from 'node:child_process';

const args = process.argv.slice(2);
function opt(name, dflt) {
    const i = args.indexOf('--' + name);
    return i >= 0 && args[i + 1] ? args[i + 1] : dflt;
}
const BASE = opt('base', 'http://127.0.0.1:6815');
const PASSWORD = opt('password', 'walk-u3d-1234');
const SHOTS = opt('shots', '');
const CDP_PORT = Number(opt('cdp', 9374));
const WIDTHS = (opt('widths', '320,390,768,1440')).split(',').map(Number);
const ONLY = opt('tasks', '');
const PROFILE = opt('profile', 'E:/事项/远程/.tmp/u3d/edge-profile-geo');
const EDGE = 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const WATCHDOG_MS = Number(opt('watchdog', 90000));
const watchdog = setTimeout(() => {
    console.log('WATCHDOG-TIMEOUT ' + WATCHDOG_MS + 'ms');
    process.exit(4);
}, WATCHDOG_MS);

const edge = spawn(EDGE, ['--headless=new', '--remote-debugging-port=' + CDP_PORT,
    '--user-data-dir=' + PROFILE, '--no-first-run', '--no-default-browser-check',
    '--window-size=1440,900', 'about:blank'], {detached: true, stdio: 'ignore'});
edge.unref();
let wsUrl = null;
for (let i = 0; i < 20; i++) {
    try {
        const list = await (await fetch(`http://127.0.0.1:${CDP_PORT}/json`)).json();
        const page = list.find(t => t.type === 'page');
        if (page) { wsUrl = page.webSocketDebuggerUrl; break; }
    } catch (e) { /* 还没起来 */ }
    await sleep(400);
}
if (!wsUrl) { console.log('FATAL: Edge CDP 没起来'); process.exit(2); }
const ws = new WebSocket(wsUrl);
await new Promise((res, rej) => {
    ws.onopen = res;
    ws.onerror = () => rej(new Error('ws error'));
    setTimeout(() => rej(new Error('ws open timeout')), 8000);
});
let msgId = 0;
const pending = new Map();
const consoleErrors = [];
ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) {
        const p = pending.get(m.id);
        pending.delete(m.id);
        m.error ? p.rej(new Error(JSON.stringify(m.error))) : p.res(m.result);
        return;
    }
    if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') {
        consoleErrors.push('console.error: ' + (m.params.args || [])
            .map(a => a.value !== undefined ? a.value : a.description).join(' ').slice(0, 200));
    }
    if (m.method === 'Runtime.exceptionThrown') {
        consoleErrors.push('exception: ' + JSON.stringify(m.params.exceptionDetails).slice(0, 240));
    }
    if (m.method === 'Log.entryAdded' && m.params.entry.level === 'error') {
        consoleErrors.push('log: ' + m.params.entry.text.slice(0, 200));
    }
};
const cdp = (method, params = {}) => new Promise((res, rej) => {
    const i = ++msgId;
    pending.set(i, {res, rej});
    ws.send(JSON.stringify({id: i, method, params}));
    setTimeout(() => { if (pending.has(i)) { pending.delete(i); rej(new Error('timeout ' + method)); } }, 15000);
});
const evaluate = async (expression) => {
    const r = await cdp('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true});
    if (r.exceptionDetails) { throw new Error('eval failed: ' + JSON.stringify(r.exceptionDetails).slice(0, 300)); }
    return r.result.value;
};
const shot = async (name) => {
    const r = await cdp('Page.captureScreenshot', {format: 'png'});
    fs.writeFileSync(SHOTS + '/' + name, Buffer.from(r.data, 'base64'));
};
async function waitFor(expr, what, tries = 25) {
    for (let i = 0; i < tries; i++) {
        try { if (await evaluate(expr)) { return true; } } catch (e) { /* 页面还在跳 */ }
        await sleep(250);
    }
    console.log('WAIT-FAIL: ' + what);
    return false;
}

let cookie = '';
{
    const body = new URLSearchParams({password: PASSWORD});
    const r = await fetch(BASE + '/login', {method: 'POST', body, redirect: 'manual'});
    cookie = (r.headers.get('set-cookie') || '').split(';')[0];
    if (r.status !== 302) { console.log('FATAL: 登录失败 ' + r.status); process.exit(2); }
}
await cdp('Page.enable');
await cdp('Runtime.enable');
await cdp('Log.enable');
await cdp('Network.enable');
await cdp('Network.setCookie', {name: 'session', value: cookie.split('=')[1],
    domain: '127.0.0.1', path: '/'});

// 手机上详情是全屏的：切任务前先返回列表
async function backIfFullscreen() {
    await evaluate(`(function () {
        var btn = document.querySelector('.cmd-detail-back');
        if (btn && getComputedStyle(btn).display !== 'none') { btn.click(); }
        return true; })()`);
}

// 等事件流渲染稳：计数两次相等，或 1.2s 封顶
async function settle() {
    let last = -1;
    for (let i = 0; i < 6; i += 1) {
        await sleep(200);
        const n = await evaluate(`(function () {
            return document.querySelectorAll('.task-event').length; })()`);
        if (n === last && n > 0) { return; }
        last = n;
    }
}

const MEASURE = `(function () {
  var de = document.documentElement;
  var chat = document.querySelector('.task-chat');
  var bad = [];
  document.querySelectorAll('.task-chat *, .task-detail *').forEach(function (el) {
      var r = el.getBoundingClientRect();
      if (r.width > de.clientWidth + 0.5 || r.right > de.clientWidth + 0.5) {
          bad.push((el.className && typeof el.className === 'string'
              ? '.' + String(el.className).trim().split(/\\\\s+/)[0] : el.tagName)
              + ' w=' + Math.round(r.width) + ' right=' + Math.round(r.right));
      }
  });
  var tools = Array.prototype.slice.call(document.querySelectorAll('.ev-tool-text'))
      .map(function (c) { return c.textContent.trim(); });
  var frag = tools.filter(function (t) { return t === '{' || t === '[' || t === ''; });
  var loading = tools.filter(function (t) { return t === '参数加载中…'; });
  var rawJson = tools.filter(function (t) {
      return t.indexOf('{') === 0 && t.indexOf('":') > 0;
  });
  var logs = Array.prototype.slice.call(document.querySelectorAll('.ev-log'));
  var blank = logs.filter(function (el) { return !el.textContent.trim(); });
  var results = Array.prototype.slice.call(document.querySelectorAll('.ev-result'));
  var openResults = results.filter(function (d) { return String(d.className).indexOf('open') >= 0; });
  var pres = Array.prototype.slice.call(document.querySelectorAll('.ev-result pre'));
  var preOverflow = pres.filter(function (p) { return p.scrollWidth > p.clientWidth + 0.5; }).length;
  var statusRuns = document.querySelectorAll('.ev-status-run').length;
  var autoLines = document.querySelectorAll('.ev-auto').length;
  return JSON.stringify({
      docSW: de.scrollWidth, docCW: de.clientWidth,
      chatSW: chat ? chat.scrollWidth : null, chatCW: chat ? chat.clientWidth : null,
      events: document.querySelectorAll('.task-event').length,
      statusRuns: statusRuns, autoLines: autoLines,
      toolCalls: tools.length, fragments: frag.length, loading: loading.length, rawJson: rawJson.length,
      logLines: logs.length, blankEvents: blank.length,
      results: results.length, openResults: openResults.length,
      preHorizontalOverflow: preOverflow,
      offenders: bad.slice(0, 6),
      sampleRaw: rawJson.slice(0, 2).map(function (s) { return s.slice(0, 60); }),
  });
})()`;

let failures = 0;
for (const w of WIDTHS) {
    await cdp('Emulation.setDeviceMetricsOverride',
        {width: w, height: 900, deviceScaleFactor: 1, mobile: w < 900});
    await cdp('Page.navigate', {url: BASE + '/tasks'});
    await waitFor(`(function () { return document.querySelectorAll('.cmd-node').length >= 2; })()`,
                  '树渲染 @ ' + w);
    // 首屏可能整棵树是折叠的（本地没存过展开状态、没有在跑/待办后代）：
    // 先把所有箭头点开，让全部任务节点进 DOM，后面的收集与几何断言才覆盖得到
    for (let round = 0; round < 4; round += 1) {
        const opened = await evaluate(`(function () {
            var carets = Array.prototype.slice.call(document.querySelectorAll('.cmd-node'))
                .filter(function (n) { return String(n.className).indexOf('open') < 0; })
                .map(function (n) { return n.querySelector('button.cmd-caret'); })
                .filter(function (c) { return c && c.offsetParent !== null; });
            if (!carets.length) { return false; }
            carets.forEach(function (c) { c.click(); });
            return true; })()`);
        if (!opened) { break; }
        await sleep(300);
    }
    // 已关闭任务默认只露最近 5 条：点「更多」把裁剪掉的也挂上，8 个任务都覆盖到
    for (let i = 0; i < 3; i += 1) {
        const clicked = await evaluate(`(function () {
            var btn = Array.prototype.slice.call(document.querySelectorAll('.cmd-more'))
                .filter(function (b) { return b.offsetParent !== null; })[0];
            if (btn) { btn.click(); return true; }
            return false; })()`);
        if (!clicked) { break; }
        await sleep(600);
    }
    // 任务清单从 DOM 收集（点过「更多」之后才是全量；API 的树仍是裁剪后的）
    let ids = await evaluate(`(function () {
        return Array.prototype.slice.call(
            document.querySelectorAll('.cmd-node[data-key^="task:"]'))
            .map(function (n) { return n.getAttribute('data-key').slice(5); }); })()`);
    if (ONLY) { ids = ids.filter(id => ONLY.split(',').includes(id)); }
    ids = ids.slice(0, 8);
    if (!ids.length) { console.log('FATAL: 树上没有任务 @ ' + w); process.exit(2); }
    console.log('W' + w + ' tasks(' + ids.length + '): ' + ids.join(' '));
    for (const id of ids) {
        await backIfFullscreen();
        // 树上点这个任务（祖先折叠着就先全部展开）
        for (let round = 0; round < 2; round += 1) {
            const found = await evaluate(`(function () {
                return !!document.querySelector('.cmd-node[data-key="task:${id}"]'); })()`);
            if (found) { break; }
            await evaluate(`(function () {
                Array.prototype.slice.call(document.querySelectorAll('.cmd-node')).forEach(function (n) {
                    var c = n.querySelector('button.cmd-caret');
                    if (c && String(n.className).indexOf('open') < 0) { c.click(); }
                }); return true; })()`);
            await sleep(200);
        }
        await evaluate(`(function () {
            var n = document.querySelector('.cmd-node[data-key="task:${id}"]');
            if (n) { n.querySelector('.cmd-node-main').click(); }
            return !!n; })()`);
        await waitFor(`(function () {
            return document.querySelectorAll('.task-event').length > 0
                || !!document.querySelector('.task-detail'); })()`, '详情 ' + id, 12);
        await settle();
        const m = JSON.parse(await evaluate(MEASURE));
        m.id = id;
        m.width = w;
        const bad = [];
        if (m.docSW !== m.docCW) { bad.push('页面横向滚动 ' + m.docSW + '>' + m.docCW); }
        if (m.chatSW !== m.chatCW) { bad.push('事件流横向滚动 ' + m.chatSW + '>' + m.chatCW); }
        if (m.fragments) { bad.push('`{` 残片 ' + m.fragments + ' 行'); }
        if (m.rawJson) { bad.push('原始 JSON ' + m.rawJson + ' 处'); }
        if (m.blankEvents) { bad.push('空白事件 ' + m.blankEvents + ' 处'); }
        if (m.preHorizontalOverflow) { bad.push('结果 pre 横向溢出 ' + m.preHorizontalOverflow + ' 处'); }
        if (m.offenders.length) { bad.push('超宽: ' + m.offenders.join(' | ')); }
        if (bad.length) { failures += 1; }
        console.log((bad.length ? 'FAIL ' : 'ok   ') + w + ' ' + id
            + ' events=' + m.events + ' statusRuns=' + m.statusRuns
            + ' auto=' + m.autoLines + ' results=' + m.results + '/' + m.openResults
            + (bad.length ? ' :: ' + bad.join(' ; ') : ''));
        if (SHOTS) {
            fs.mkdirSync(SHOTS, {recursive: true});
            await shot('detail-' + id + '-' + w + '.png');
        }
    }
}
console.log('CONSOLE-ERRORS ' + JSON.stringify(consoleErrors));
console.log(failures ? 'RESULT: ' + failures + ' 处不过' : 'RESULT: 全部通过');
if (consoleErrors.length) { failures += 1; }
clearTimeout(watchdog);
ws.close();
process.exit(failures ? 1 : 0);
