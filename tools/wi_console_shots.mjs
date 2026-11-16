#!/usr/bin/env node
// 指挥台「工作项」面板截图与几何断言（批次 G2 端到端）：
//   登录 → /tasks → 等工作项面板渲染 → 1440/390 两个宽度截图 +
//   断言无横向滚动（390 可读，规格 §3）。
// 用法：
//   node tools/wi_console_shots.mjs --base http://127.0.0.1:6824 \
//        --password e2e-pw-123456 --shots E:/事项/远程/.tmp/g2/e2e/shots \
//        [--widths 1440,390] [--expand]
// 自带 120 秒看门狗；退出码：几何断言不过 / 面板没渲染 = 1。
import fs from 'node:fs';
import { spawn } from 'node:child_process';

const args = process.argv.slice(2);
function opt(name, dflt) {
    const i = args.indexOf('--' + name);
    return i >= 0 && args[i + 1] ? args[i + 1] : dflt;
}
const BASE = opt('base', 'http://127.0.0.1:6824');
const PASSWORD = opt('password', 'e2e-pw-123456');
const SHOTS = opt('shots', '');
const WIDTHS = (opt('widths', '1440,390')).split(',').map(Number);
const EXPAND = args.includes('--expand');
const CDP_PORT = Number(opt('cdp', 9376));
const PROFILE = opt('profile', 'E:/事项/远程/.tmp/g2/e2e/edge-profile-wi');
const EDGE = 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const watchdog = setTimeout(() => {
    console.log('WATCHDOG-TIMEOUT');
    process.exit(4);
}, 120000);

fs.mkdirSync(SHOTS, {recursive: true});
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
const pageErrors = [];
ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) {
        const p = pending.get(m.id);
        pending.delete(m.id);
        m.error ? p.rej(new Error(JSON.stringify(m.error))) : p.res(m.result);
        return;
    }
    if (m.method === 'Runtime.exceptionThrown') {
        pageErrors.push('exception: ' + JSON.stringify(m.params.exceptionDetails).slice(0, 240));
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

let cookie = '';
{
    const body = new URLSearchParams({password: PASSWORD});
    const r = await fetch(BASE + '/login', {method: 'POST', body, redirect: 'manual'});
    cookie = (r.headers.get('set-cookie') || '').split(';')[0];
    if (r.status !== 302) { console.log('FATAL: 登录失败 ' + r.status); process.exit(2); }
}
await cdp('Page.enable');
await cdp('Runtime.enable');
await cdp('Network.enable');
await cdp('Network.setCookie', {name: 'session', value: cookie.split('=')[1],
    domain: '127.0.0.1', path: '/'});

await cdp('Page.navigate', {url: BASE + '/tasks'});
// 等工作项面板出现且有行（没有工作项时是空提示，也算渲染成功）
let rendered = false;
for (let i = 0; i < 40; i++) {
    await sleep(250);
    try {
        rendered = await evaluate(`(function () {
            var sec = document.querySelector('.cmd-workitems');
            return !!sec && (!!sec.querySelector('.cmd-wi') || !!sec.querySelector('.empty-tip'));
        })()`);
        if (rendered) { break; }
    } catch (e) { /* 页面还在跳 */ }
}
if (!rendered) { console.log('FATAL: 工作项面板没渲染'); process.exit(1); }
// 可选：展开第一个工作项（看名下任务）
if (EXPAND) {
    await evaluate(`(function () {
        var row = document.querySelector('.cmd-wi');
        if (row && !row.classList.contains('open')) {
            var caret = row.querySelector('.cmd-wi-caret');
            if (caret) { caret.click(); }
        }
        return true; })()`);
    await sleep(800);
}

let bad = 0;
for (const width of WIDTHS) {
    await cdp('Emulation.setDeviceMetricsOverride',
              {width: width, height: 900, deviceScaleFactor: 1, mobile: width < 720});
    await sleep(700);
    const geo = await evaluate(`(function () {
        var de = document.documentElement;
        var wide = [];
        if (de.scrollWidth > de.clientWidth + 1) {
            document.querySelectorAll('.cmd-workitems *').forEach(function (el) {
                if (el.scrollWidth > el.clientWidth + 1
                        && getComputedStyle(el).overflowX === 'visible') {
                    wide.push(el.className + ' ' + el.scrollWidth + '>' + el.clientWidth);
                }
            });
        }
        return {pageScroll: de.scrollWidth - de.clientWidth, wide: wide.slice(0, 5)};
    })()`);
    const shot = await cdp('Page.captureScreenshot', {format: 'png'});
    const name = 'console-workitems-' + width + '.png';
    fs.writeFileSync(SHOTS + '/' + name, Buffer.from(shot.data, 'base64'));
    const ok = geo.pageScroll <= 1;
    if (!ok) { bad++; }
    console.log('width=' + width + ' 横向溢出=' + geo.pageScroll + 'px '
                + (geo.wide.length ? '超宽元素: ' + geo.wide.join(' | ') : '')
                + ' → ' + name + (ok ? ' OK' : ' FAIL'));
}
if (pageErrors.length) { console.log('页面错误: ' + pageErrors.join(' ; ').slice(0, 400)); }
console.log(bad ? 'GEOMETRY-FAIL' : 'GEOMETRY-OK');
process.exit(bad ? 1 : 0);
