// mixin 级测试的 Vue 替身（批次 U2/U3 返工）：把 mixin 的 data/computed/
// methods 装进一个普通对象，$nextTick 同步执行、setTimeout/clearTimeout 与
// document.querySelectorAll 可按用例替换——不引 Vue 也能跑「点击 → 定位/
// 高亮/展开祖先」这类交互路径（纯函数单测覆盖不到的那层）。
// 局限：没有响应式依赖追踪，断言的是方法调用后的当即状态。
export function makeVm(mixin, props = {}) {
    const data = typeof mixin.data === 'function' ? mixin.data() : (mixin.data || {});
    const vm = Object.assign({}, data, props);
    for (const [name, fn] of Object.entries(mixin.computed || {})) {
        Object.defineProperty(vm, name, {
            get: () => fn.call(vm), enumerable: true, configurable: true,
        });
    }
    Object.assign(vm, mixin.methods || {});
    vm.$nextTick = (cb) => { cb(); return Promise.resolve(); };
    return vm;
}

// 收集 setTimeout 回调（不真的等）：返回 {fire, timers}
export function fakeTimers() {
    const timers = [];
    const realSet = globalThis.setTimeout;
    const realClear = globalThis.clearTimeout;
    globalThis.setTimeout = (fn) => { timers.push(fn); return timers.length; };
    globalThis.clearTimeout = () => {};
    return {
        timers,
        restore() {
            globalThis.setTimeout = realSet;
            globalThis.clearTimeout = realClear;
        },
        fire() {
            const fn = timers.shift();
            if (fn) { fn(); }
        },
    };
}

// document.querySelectorAll 替身：nodes 是 {key} 列表，scrollIntoView 被记录下来
export function fakeDocument(keys) {
    const scrolled = [];
    const nodes = keys.map((key) => ({
        getAttribute: (name) => (name === 'data-key' ? key : null),
        scrollIntoView: (opts) => { scrolled.push({key: key, opts: opts}); },
    }));
    const real = globalThis.document;
    globalThis.document = {
        querySelectorAll: (sel) => {
            if (sel === '.cmd-node') { return nodes; }
            return [];
        },
    };
    return {scrolled, restore: () => { globalThis.document = real; }};
}
