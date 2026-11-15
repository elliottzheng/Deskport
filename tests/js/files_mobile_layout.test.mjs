// 文件页手机宽度几何不变量（批次 M2）静态断言。
// 跑法：node --test tests/js/（tools/check_frontend.py 门禁含这一步）。
// 说明：门禁跑在无浏览器的环境里，只能静态守住「不让宽度溢出」的 CSS 结构；
//       真机几何复测（scrollWidth/clientWidth）跑 tools/files-mobile-geometry.mjs。
//
// 背景（M2 根因）：移动端骨架里 .fm-layout 漏了 align-items:stretch，
// column-flex 下 .fm-main 按 fit-content 取值，被面包屑/工具栏的 min-content
// 顶到视口外，body:has(.fm-layout){overflow:hidden} 直接裁掉右侧。
// 这些断言守住院子里的这几条结构性规则不被改回去。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const read = (rel) => readFileSync(path.join(root, rel), 'utf-8');
// M2 起手机宽度规则放 files-mobile.css（files.css 登记「存量只减不增」），合并后断言
const css = read('frontend/assets/css/files.css') + '\n' + read('frontend/assets/css/files-mobile.css');
const html = read('frontend/files.html');
const core = read('frontend/assets/js/files-methods-core.js');
const app = read('frontend/assets/js/files.js');

const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

function mediaBlocks(text, prop, value) {
    const blocks = [];
    const re = new RegExp('@media\\s*\\(\\s*' + prop + ':\\s*' + value + 'px\\s*\\)\\s*\\{', 'g');
    let m;
    while ((m = re.exec(text))) {
        let i = m.index + m[0].length;
        let depth = 1;
        while (i < text.length && depth > 0) {
            if (text[i] === '{') depth += 1;
            else if (text[i] === '}') depth -= 1;
            i += 1;
        }
        blocks.push(text.slice(m.index, i));
    }
    return blocks;
}
const mobile = mediaBlocks(css, 'max-width', 640).join('\n');
const desktop = mediaBlocks(css, 'min-width', 641).join('\n');

function ruleBodies(block, selector) {
    const re = new RegExp(esc(selector) + '\\s*\\{([^}]*)\\}', 'g');
    const out = [];
    let m;
    while ((m = re.exec(block))) out.push(m[1]);
    return out;
}
function declRe(decl) {
    const idx = decl.indexOf(':');
    const prop = decl.slice(0, idx).trim();
    const value = decl.slice(idx + 1).trim();
    return new RegExp(esc(prop) + '\\s*:\\s*' + esc(value).replace(/\s+/g, '\\s+'));
}
function has(block, selector, decl) {
    const bodies = ruleBodies(block, selector);
    assert.ok(bodies.length > 0, '缺少规则 ' + selector);
    const re = declRe(decl);
    assert.ok(bodies.some((b) => re.test(b)), selector + ' 缺少声明 ' + decl);
}

test('移动端骨架: 主区严格等于容器宽度, 不吃面包屑的 min-content', () => {
    has(mobile, '.fm-layout', 'align-items: stretch');
    has(mobile, '.fm-layout', 'min-width: 0');
    has(mobile, '.fm-main', 'min-width: 0');
    has(mobile, '.fm-main', 'max-width: 100%');
    has(css, '.fm-addressbar', 'min-width: 0');
});

test('面包屑: 独立一行可横向滚动, 窄屏折叠段不顶宽', () => {
    has(css, '.crumb-wrap', 'overflow-x: auto');
    has(css, '.crumb-wrap', 'min-width: 0');
    has(mobile, '.fm-addressbar .crumb-wrap', 'flex-basis: 100%');
    has(mobile, '.fm-addressbar .search-input', 'min-width: 0');
    has(mobile, '.crumb-seg.is-current', 'text-overflow: ellipsis');
    has(mobile, '.crumb-seg.is-current', 'flex-shrink: 1');
    has(mobile, '.crumb-seg.crumb-more', 'min-width: 44px');
});

test('「…」上级跳转弹层: 受视口宽度约束, 不溢出', () => {
    has(css, '.crumb-jump', 'max-width: calc(100vw - 24px)');
    has(css, '.crumb-jump', 'overflow-y: auto');
    has(css, '.crumb-jump-mask', 'position: fixed');
});

test('工具栏/状态栏/多选栏: 换行消化, 不撑宽页面', () => {
    has(css, '.fm-toolbar', 'flex-wrap: wrap');
    has(css, '.fm-toolbar', 'min-width: 0');
    has(css, '.fm-toolbar .btn', 'min-width: 0');
    has(css, '.statusbar', 'flex-wrap: wrap');
    has(mobile, '.bottombar', 'flex-wrap: wrap');
    has(mobile, '.bottombar .btn', 'min-width: 0');
});

test('模板与脚本: 折叠渲染 + 跳转闭环', () => {
    assert.match(html, /class="crumb-seg crumb-more"/, '缺少「…」折叠按钮');
    assert.match(html, /class="crumb-jump"/, '缺少上级跳转弹层');
    assert.match(html, /@click="jumpToCrumb\(c\)"/, '弹层项未接跳转');
    assert.match(html, /narrow && crumbs\.length > 3/, '缺少窄屏折叠条件');
    assert.match(app, /narrow: window\.matchMedia\('\(max-width: 640px\)'\)/, '缺少窄屏响应式标志');
    assert.match(app, /currentPath\(\) \{ this\.crumbJumpOpen = false; \}/, '切路径未收起弹层');
    assert.match(core, /jumpToCrumb\(c\) \{[\s\S]*navigate\(c\.path\)/, 'jumpToCrumb 未走 navigate');
});

test('桌面端骨架保持不变 (stretch 原样, 不出现移动端补丁)', () => {
    has(desktop, '.fm-layout', 'align-items: stretch');
    assert.doesNotMatch(desktop, /crumb-jump/, '桌面 CSS 不应出现跳转弹层规则');
});
