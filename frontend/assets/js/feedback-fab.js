// FAB 落位与避让（从 feedback-panel.js 拆出，批次 FB-5）：反馈悬浮按钮放哪、什么时候让位。
// 三件事：
//   1. M4 存量：页面出现底部工具条（文件多选、收件箱操作栏、终端虚拟键、控制台 FAB）时
//      抬高 72px；全屏浮层打开时收起；
//   2. FB-5 桌面避让：探测 FAB 是否压住可交互元素（设置页目录行、表单提交钮等，
//      含壳里 iframe 内的元素），压住就上移两档，躲不开就藏起；
//   3. FB-5 滚动让位：桌面滚动时 FAB 隐藏（滚停复现并重新探测）；壳里页面装在
//      iframe，滚动发生在 iframe 文档内，给每个同源 iframe 的 window 也挂一份。
// 移动端（≤640px）维持 M4 口径，不参与避让与滚动隐藏。
// 页面侧挂到 window.WPFeedbackPanel.fab（沿用既有入口，不为 FAB 新增 window 全局，
// 与 tasks-tree.js 挂 WPTasksOverview.tree 同一约定）。
(function () {
    'use strict';

    var scrollTimer = null;

    function isMobileWidth() {
        return !!(window.matchMedia && window.matchMedia('(max-width: 640px)').matches);
    }

    function place() {
        // 控制键条的容器是 #control-keys（旧选择器 .term-vkeys 根本不存在，等于没抬过——
        // 手机反馈「反馈按钮挡住了到底部的按钮」就是它压在控制键条上）
        var raise = document.querySelector('.bottombar:not([hidden]), .inbox-actionbar:not([hidden]), #control-keys:not(.hidden), .ctl-fab');
        // 壳（/app）里终端与 SSH 是 iframe：控制键条在 iframe 里面，外层选择器看不见它，
        // 但浮按钮画在壳文档上、永远盖在 iframe 之上，所以按"当前视图"判断
        var frame = document.querySelector('iframe');
        var src = frame ? (frame.getAttribute('src') || '') : '';
        if (/\/terminal|\/ssh/.test(src)) { raise = raise || document.body; }
        document.body.classList.toggle('fb-raise', !!raise);
        // 全屏浮层（移动端 SFTP 面板铺满整屏）打开时直接收起：那时抬起来也会压在
        // 浮层底部的操作行（新建/上传/删除）上，用户报过"挡住到底部的按钮"
        var sftp = document.getElementById('sftp-panel');
        var covered = !!(sftp && sftp.offsetParent !== null && getComputedStyle(sftp).display !== 'none')
            || !!document.querySelector('.more-sheet:not([hidden])');
        document.body.classList.toggle('fb-hide', covered);
        // FB-5 桌面避让：FAB 压在可交互元素（设置页目录行、表单提交钮等）上时就地
        // 上移两档，还躲不开就藏起；滚动中另有 fb-scroll 整体隐藏（见 onScroll）
        var lift = 0;
        if (!isMobileWidth() && !covered) {
            for (var k = 0; k <= 2; k++) {
                document.body.classList.toggle('fb-lift1', k === 1);
                document.body.classList.toggle('fb-lift2', k === 2);
                if (!fabBlocked()) { lift = k; break; }
                lift = -1;
            }
        }
        document.body.classList.toggle('fb-lift1', lift === 1);
        document.body.classList.toggle('fb-lift2', lift === 2);
        document.body.classList.toggle('fb-avoid', lift < 0);
    }

    function fabBlocked() {
        var fab = document.querySelector('.fb-fab');
        if (!fab) { return false; }
        // 注意不能用 offsetParent 判可见：fixed 元素的 offsetParent 恒为 null
        var cs = getComputedStyle(fab);
        if (cs.display === 'none' || cs.visibility === 'hidden') { return false; }
        var r = fab.getBoundingClientRect();
        if (r.width < 1 || r.bottom < 0 || r.top > (window.innerHeight || 0)) { return false; }
        var pts = [[r.left + r.width / 2, r.top + r.height / 2], [r.right - 10, r.top + 10],
                   [r.left + 10, r.bottom - 10]];
        for (var i = 0; i < pts.length; i++) { if (interactiveAt(pts[i][0], pts[i][1])) { return true; } }
        return false;
    }

    // 该点上最顶层的非反馈元素是否可交互；命中 iframe 时钻进去看（壳里页面装在 iframe
    // 里，外层只摸得到 iframe 元素本身，不钻进去等于没避开）
    function interactiveAt(x, y) {
        var stack = document.elementsFromPoint ? document.elementsFromPoint(x, y) : [];
        for (var i = 0; i < stack.length; i++) {
            var node = stack[i];
            if (!node || node.nodeType !== 1) { continue; }
            if (node.closest && node.closest('.fb-root')) { continue; }
            if (node.tagName === 'IFRAME') {
                var doc = null;
                try { doc = node.contentDocument; } catch (e) { /* 跨源 */ }
                if (doc && doc.body) {
                    var r = node.getBoundingClientRect();
                    var hit = doc.elementFromPoint(x - r.left, y - r.top);
                    if (hit) { return isInteractive(hit); }
                }
                continue;   // 读不到：看它下面一层
            }
            return isInteractive(node);
        }
        return false;
    }

    function isInteractive(node) {
        if (!node || node.nodeType !== 1) { return false; }
        var tag = node.tagName.toLowerCase();
        if (/^(button|a|input|select|textarea|summary|label|option)$/.test(tag)) { return true; }
        var role = node.getAttribute && node.getAttribute('role');
        if (role && /^(button|link|checkbox|switch|tab)$/.test(role)) { return true; }
        if (node.isContentEditable) { return true; }
        var view = node.ownerDocument && node.ownerDocument.defaultView;
        // Vue 的 @click 行/卡片不带语义标签，鼠标样式（pointer）就是判定
        return !!(view && view.getComputedStyle(node).cursor === 'pointer');
    }

    // FB-5：桌面滚动时 FAB 让位（滚停复现并重新探测避让）；移动端 M4 口径不参与
    function onScroll() {
        if (isMobileWidth()) { return; }
        document.body.classList.add('fb-scroll');
        if (scrollTimer) { clearTimeout(scrollTimer); }
        scrollTimer = setTimeout(function () {
            scrollTimer = null;
            document.body.classList.remove('fb-scroll');
            place();
        }, 180);
    }

    function hookFrame(frame) {
        if (!frame) { return; }
        var doc = null;
        try { doc = frame.contentDocument; } catch (e) { return; }
        var win = doc ? doc.defaultView : null;
        if (!doc || !win) { return; }
        // 记 Document 而不是布尔或 window：iframe 导航必然换 Document，但浏览器不
        // 一定换 Window 对象（实测同内核下 reload 复用 Window、只换 Document 并清掉
        // Window 上的监听器）——按 window 判重会把补挂漏掉，滚动让位从此失效
        if (frame.__wpFbDoc === doc) { return; }
        frame.__wpFbDoc = doc;
        win.addEventListener('scroll', onScroll, { passive: true });
    }

    function hookFrames() {
        var frames = document.querySelectorAll('iframe');
        for (var i = 0; i < frames.length; i++) { hookFrame(frames[i]); }
    }

    // 壳的 iframe 按需创建：给容器挂 capture 的 load（iframe 的 load 不冒泡但能被
    // 祖先捕获），任意框就绪后补挂 scroll + 重排避让
    function watch() {
        hookFrames();
        var host = document.getElementById('shell-frames');
        if (host) {
            host.addEventListener('load', function () {
                hookFrames();
                setTimeout(place, 250);
            }, true);
        }
    }

    var api = { place: place, onScroll: onScroll, watch: watch };
    if (typeof window !== 'undefined' && window.WPFeedbackPanel) {
        window.WPFeedbackPanel.fab = api;
    }
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = api;
    }
})();
