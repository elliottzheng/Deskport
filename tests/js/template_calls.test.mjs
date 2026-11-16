// 模板 ↔ Vue 实例接线门禁（批次 S 返工防回归）。
// 背景：settings.html 模板裸调 bindable(...)，但 settings-leads.js 的 mixin
// 没把它暴露成实例方法——盘上有 ≥1 个主管时设置页渲染抛错整页全白；
// tasks.js 则是 createApp 返回值没接住的同类接线事故。纯函数单测与结构
// 门禁都抓不到这类"模板调了、实例上没有"的问题，这里补一道机械防线：
// 抽出 settings.html / tasks.html 模板（{{ }} 与 v-*/:*/@* 属性值）里的
// 函数调用名，断言每个都能在对应 JS 的 methods/computed（含各 mixin）里
// 找到定义。
// 局限：只查函数调用，不查属性/数据引用（那需要解析作用域，保守不做）；
// 内置函数走小名单白名单（BUILTINS）。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const ROOT = new URL('../../', import.meta.url);
const read = (rel) => readFileSync(new URL(rel, ROOT), 'utf8');

// 保守白名单：模板表达式里可能出现的 JS 内置（当前两个页面其实一个都没用到，
// 带上只为将来加模板时不误红）。
const BUILTINS = new Set([
    'Number', 'String', 'Boolean', 'Array', 'Object', 'JSON', 'Date',
    'parseInt', 'parseFloat', 'isNaN', 'isFinite',
    'encodeURIComponent', 'decodeURIComponent',
]);

const JS_KEYWORDS = new Set([
    'if', 'for', 'while', 'switch', 'catch', 'return', 'typeof', 'instanceof',
    'delete', 'function', 'new', 'await', 'void', 'in', 'of', 'do', 'else',
    'try', 'finally', 'throw', 'case', 'default', 'with', 'class', 'const',
    'let', 'var', 'super', 'this', 'import', 'export', 'extends', 'async',
]);

// 页面 → HTML 模板 + 该页面加载的全部 JS（顺序与 HTML 里 <script> 一致：
// 家族 mixin 文件在前、主文件在后）。
const PAGES = [
    {
        name: 'settings',
        html: 'frontend/settings.html',
        js: ['frontend/assets/js/settings-leads.js',
             'frontend/assets/js/settings-capability.js',
             'frontend/assets/js/settings-feedback.js',
             'frontend/assets/js/settings.js'],
    },
    {
        name: 'tasks',
        html: 'frontend/tasks.html',
        js: ['frontend/assets/js/tasks-leads.js',
             'frontend/assets/js/settings-leads.js',
             'frontend/assets/js/tasks-result.js',
             'frontend/assets/js/tasks-overview.js',
             'frontend/assets/js/tasks-tree.js',
             'frontend/assets/js/tasks-events.js',
             'frontend/assets/js/workitems.js',
             'frontend/assets/js/tasks.js'],
    },
];

// 去掉表达式里的字符串字面量，避免字符串内容里的 "name(" 造成误报。
function stripStrings(expr) {
    return expr.replace(/'(?:\\.|[^'\\])*'/g, "''")
               .replace(/"(?:\\.|[^"\\])*"/g, '""');
}

// {{ }} 插值 + v-*/:*/@* 属性值里的裸调用 name(（排除 .name( 与关键字）。
function templateCalls(html) {
    const exprs = [];
    for (const m of html.matchAll(/\{\{([\s\S]*?)\}\}/g)) {
        exprs.push(m[1]);
    }
    const attr = /(?:^|\s)([:@]?[A-Za-z_][\w.:@-]*)\s*=\s*(?:"([^"]*)"|'([^']*)')/g;
    for (const m of html.matchAll(attr)) {
        if (/^([:@]|v-)/.test(m[1])) { exprs.push(m[2] !== undefined ? m[2] : m[3]); }
    }
    const out = new Set();
    for (const raw of exprs) {
        for (const m of stripStrings(raw).matchAll(/(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(/g)) {
            if (!JS_KEYWORDS.has(m[1])) { out.add(m[1]); }
        }
    }
    return out;
}

// 在 vm 沙箱里按浏览器顺序加载页面 JS，收集 createApp 选项与各 mixin 的
// methods/computed 键名——即模板表达式真正能看到的实例成员。
function instanceMembers(jsFiles) {
    const seen = { options: [], mixins: [] };
    const fakeApp = {
        mixin(m) { seen.mixins.push(m); return fakeApp; },
        mount() { return fakeApp; },
        use() { return fakeApp; },
        component() { return fakeApp; },
        directive() { return fakeApp; },
        config: { globalProperties: {} },
    };
    const sandbox = {
        Vue: { createApp(opts) { seen.options.push(opts); return fakeApp; } },
    };
    // 浏览器里 window 的属性就是全局变量（页面 JS 靠 window.WPxxx 挂全局）；
    // 沙箱里让 window 指向全局本身，复刻这个行为。
    sandbox.window = sandbox;
    const ctx = vm.createContext(sandbox);
    for (const rel of jsFiles) {
        vm.runInContext(read(rel), ctx, { filename: rel });
    }
    const out = new Set();
    for (const obj of [...seen.options, ...seen.mixins]) {
        for (const group of ['methods', 'computed']) {
            if (obj && obj[group]) {
                for (const key of Object.keys(obj[group])) { out.add(key); }
            }
        }
    }
    return out;
}

for (const page of PAGES) {
    test(`${page.name}.html 模板调用都能在实例 methods/computed 里找到`, () => {
        const calls = templateCalls(read(page.html));
        const defined = instanceMembers(page.js);
        const missing = [...calls].filter(n => !defined.has(n) && !BUILTINS.has(n)).sort();
        assert.deepEqual(missing, [],
            `${page.name}.html 模板调用了实例上不存在的函数: ${missing.join(', ')}`);
    });
}
