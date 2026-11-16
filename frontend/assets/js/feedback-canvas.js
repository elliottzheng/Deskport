// 批注画布（FB-5）：桌面面板放大后的精批能力——滚轮缩放（以光标为中心）、
// 拖拽平移（移动工具/空格/右键）、复位；画笔与框选的粗细和坐标都按缩放比
// 补偿，缩到 1:1（canvas.width/截图宽 = 1）时 1 画布像素 = 1 截图像素。
//
// 视图数学抽成纯函数（fitScale/clampScale/scaleAtCursor/clampPan/toImage/
// toCanvas/strokeWidth/zoomPercent）经 module.exports 供 node 单测；
// render(ctx, image, ops, scale) 同样可测（传桩 ctx）。
// 页面侧挂到 window.WPFeedbackPanel.canvas（沿用既有入口，不为画布新增
// window 全局——ALLOWED_WINDOW 登记表在本批边界外，与 tasks-tree.js 挂
// WPTasksOverview.tree 同一约定），由 feedback-panel.js 的 init 装订。
//
// 坐标约定：ops 一律存「截图像素」坐标；画布备份尺寸 = 图宽 × scale，
// CSS 尺寸与备份尺寸 1:1，所以 1 画布像素在屏幕上也是 1 CSS 像素；
// scale = canvas.width / 截图宽，即验收口径里的缩放比。
(function () {
    'use strict';

    var MIN_REL = 0.5;       // 最小缩到适配值的一半
    var MAX_SCALE = 4;       // 最大 4×：画布像素 = 图宽×4，再大浏览器 canvas 吃不消
    var MAX_EDGE = 8192;     // 画布单边上限（浏览器 canvas 尺寸限制前刹车）
    var PAN_MARGIN = 48;     // 平移后画布与包裹层至少保持的重叠边距
    var WHEEL_STEP = 1.15;   // 每格滚轮的缩放倍率
    var BASE_W = 3;          // ops 缺省基粗（图像像素）
    var BASE_DIVISOR = 400;  // 笔画基粗（图像像素）= 图宽 / 400，与改前口径一致

    function clamp(value, lo, hi) {
        return value < lo ? lo : (value > hi ? hi : value);
    }

    // 适配缩放：整图缩进包裹层（取小的一边）
    function fitScale(imgW, imgH, wrapW, wrapH) {
        if (!(imgW > 0) || !(imgH > 0) || !(wrapW > 0) || !(wrapH > 0)) { return 1; }
        return Math.min(wrapW / imgW, wrapH / imgH);
    }

    // 缩放范围：适配值的一半 ~ 4×，同时受画布单边上限约束
    function scaleRange(fit, imgW, imgH) {
        var lo = Math.max(0.02, fit * MIN_REL);
        var hi = MAX_SCALE;
        if (imgW > 0) { hi = Math.min(hi, MAX_EDGE / imgW); }
        if (imgH > 0) { hi = Math.min(hi, MAX_EDGE / imgH); }
        if (hi < lo) { hi = lo; }
        return { min: lo, max: hi };
    }

    function clampScale(scale, fit, imgW, imgH) {
        var range = scaleRange(fit, imgW, imgH);
        return clamp(scale, range.min, range.max);
    }

    // 滚轮缩放（以光标为中心）：光标下的画布坐标 c = clientX - wrapLeft - panX，
    // 缩放后保持同一个图像点留在光标下 → panX' = clientX - wrapLeft - c × (s'/s)
    function scaleAtCursor(scale, factor, clientX, clientY, wrapLeft, wrapTop, panX, panY, fit, imgW, imgH) {
        var next = clampScale(scale * factor, fit, imgW, imgH);
        var k = next / scale;
        return {
            scale: next,
            x: clientX - wrapLeft - (clientX - wrapLeft - panX) * k,
            y: clientY - wrapTop - (clientY - wrapTop - panY) * k
        };
    }

    // 平移边界：画布始终与包裹层保持 ≥ margin 的重叠（不能整个拖飞）；
    // 画布比包裹层小时区间自然含中位，小图可居中
    function clampPan(x, y, canvasW, canvasH, wrapW, wrapH, margin) {
        var m = (margin == null) ? PAN_MARGIN : margin;
        return {
            x: clamp(x, m - canvasW, wrapW - m),
            y: clamp(y, m - canvasH, wrapH - m)
        };
    }

    // 屏幕坐标 → 截图像素坐标（按缩放比补偿）
    function toImage(clientX, clientY, canvasLeft, canvasTop, scale) {
        return [(clientX - canvasLeft) / scale, (clientY - canvasTop) / scale];
    }

    // 截图像素坐标 → 画布备份像素
    function toCanvas(ix, iy, scale) {
        return [ix * scale, iy * scale];
    }

    // 笔画粗细补偿：ops 里存的是图像像素基粗，渲染时 × 缩放比；
    // 1:1 时线宽（画布像素）= 基粗（图像像素），可精批
    function strokeWidth(scale, base) {
        return Math.max(1, (base || BASE_W) * scale);
    }

    // 缩放百分比：相对截图原始尺寸，100% 即 1:1
    function zoomPercent(scale) {
        return Math.round(scale * 100);
    }

    function baseWidthOf(imgW) {
        return Math.max(2, Math.round((imgW || 0) / BASE_DIVISOR));
    }

    // 重绘：底图铺满备份画布，ops（图像坐标）按 scale 换算成画布像素
    function render(ctx, image, ops, scale, width, height) {
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.clearRect(0, 0, width, height);
        if (image) { ctx.drawImage(image, 0, 0, width, height); }
        ops = ops || [];
        for (var i = 0; i < ops.length; i++) {
            var op = ops[i];
            ctx.save();
            ctx.strokeStyle = op.type === 'black' ? '#111827' : '#DC2626';
            ctx.fillStyle = '#111827';
            ctx.lineWidth = strokeWidth(scale, op.w);
            ctx.lineCap = 'round';
            ctx.lineJoin = 'round';
            if (op.type === 'pen' && op.points && op.points.length > 1) {
                ctx.beginPath();
                ctx.moveTo(op.points[0][0] * scale, op.points[0][1] * scale);
                for (var j = 1; j < op.points.length; j++) {
                    ctx.lineTo(op.points[j][0] * scale, op.points[j][1] * scale);
                }
                ctx.stroke();
            } else if (op.type === 'rect' || op.type === 'black') {
                var x = Math.min(op.x1, op.x2) * scale;
                var y = Math.min(op.y1, op.y2) * scale;
                var w = Math.abs(op.x2 - op.x1) * scale;
                var h = Math.abs(op.y2 - op.y1) * scale;
                if (op.type === 'black') { ctx.fillRect(x, y, w, h); } else { ctx.strokeRect(x, y, w, h); }
            }
            ctx.restore();
        }
    }

    // ---- 交互挂载：状态在本闭包里，页面只通过回调读写底图/ops/工具 ----
    // opts: {wrap, canvas, getBase, setBase, getOps, getTool, onView, onOps}
    function mount(opts) {
        var wrap = opts.wrap;
        var canvas = opts.canvas;
        var view = { scale: 1, x: 0, y: 0 };
        var fit = 1;
        var userZoomed = false;
        var drawing = null;
        var panning = null;
        var spaceDown = false;

        function imgSize() {
            var base = opts.getBase();
            return base ? { w: base.width || 0, h: base.height || 0 } : { w: 0, h: 0 };
        }

        function wrapSize() {
            return { w: wrap.clientWidth || 0, h: wrap.clientHeight || 0 };
        }

        function apply(repaint) {
            var size = imgSize();
            if (!size.w || !size.h) {
                canvas.hidden = true;
                if (opts.onView) { opts.onView(view, fit); }
                return;
            }
            canvas.hidden = false;
            var cw = Math.max(1, Math.round(size.w * view.scale));
            var ch = Math.max(1, Math.round(size.h * view.scale));
            if (canvas.width !== cw) { canvas.width = cw; }
            if (canvas.height !== ch) { canvas.height = ch; }
            canvas.style.width = cw + 'px';
            canvas.style.height = ch + 'px';
            canvas.style.transform = 'translate(' + Math.round(view.x) + 'px,' + Math.round(view.y) + 'px)';
            if (repaint !== false) { paint(); }
            if (opts.onView) { opts.onView(view, fit); }
        }

        function paint() {
            var ctx = canvas.getContext('2d');
            render(ctx, opts.getBase(), opts.getOps(), view.scale, canvas.width, canvas.height);
        }

        function fitView() {
            var size = imgSize();
            var ws = wrapSize();
            fit = fitScale(size.w, size.h, ws.w, ws.h);
            view.scale = fit;
            view.x = Math.round((ws.w - Math.round(size.w * fit)) / 2);
            view.y = Math.round((ws.h - Math.round(size.h * fit)) / 2);
            userZoomed = false;
            apply(true);
        }

        function onResize() {
            var size = imgSize();
            if (!size.w) { return; }
            if (!userZoomed) { fitView(); return; }
            var ws = wrapSize();
            var moved = clampPan(view.x, view.y,
                                 Math.round(size.w * view.scale), Math.round(size.h * view.scale),
                                 ws.w, ws.h, PAN_MARGIN);
            view.x = moved.x;
            view.y = moved.y;
            apply(false);
        }

        function onWheel(ev) {
            var base = opts.getBase();
            if (!base) { return; }
            ev.preventDefault();
            var rect = wrap.getBoundingClientRect();
            var size = imgSize();
            var factor = ev.deltaY < 0 ? WHEEL_STEP : 1 / WHEEL_STEP;
            // 这一步会跨过 1:1 时就吸附到 100%：精批的落点要能精确停在 1 画布像素 = 1 图像像素
            var raw = view.scale * factor;
            if ((view.scale < 1 && raw > 1) || (view.scale > 1 && raw < 1)) { raw = 1; }
            var next = scaleAtCursor(view.scale, raw / view.scale, ev.clientX, ev.clientY,
                                     rect.left, rect.top, view.x, view.y, fit, size.w, size.h);
            view.scale = next.scale;
            view.x = next.x;
            view.y = next.y;
            userZoomed = true;
            apply(true);
        }

        function panIntent(ev) {
            return opts.getTool() === 'move' || spaceDown || ev.button === 1 || ev.button === 2;
        }

        function imagePoint(ev) {
            var rect = canvas.getBoundingClientRect();
            var sx = canvas.width / (rect.width || 1);
            var sy = canvas.height / (rect.height || 1);
            var p = toImage(ev.clientX, ev.clientY, rect.left, rect.top, view.scale);
            // rect 已含缩放/平移（CSS 尺寸 = 备份尺寸），sx/sy 兜住四舍五入的误差
            return [p[0] * sx, p[1] * sy];
        }

        function onDown(ev) {
            if (!opts.getBase()) { return; }
            ev.preventDefault();
            if (panIntent(ev)) {
                panning = { x: ev.clientX, y: ev.clientY, ox: view.x, oy: view.y };
                wrap.classList.add('is-panning');
            } else {
                var p = imagePoint(ev);
                var type = opts.getTool();
                if (type === 'pen') {
                    drawing = { type: 'pen', w: baseWidthOf(imgSize().w), points: [p] };
                } else {
                    drawing = { type: type, w: baseWidthOf(imgSize().w),
                                x1: p[0], y1: p[1], x2: p[0], y2: p[1] };
                }
                opts.getOps().push(drawing);
                paint();
            }
            try {
                if (canvas.setPointerCapture) { canvas.setPointerCapture(ev.pointerId); }
            } catch (e) { /* 合成事件没有真实 pointerId，忽略 */ }
        }

        function onMove(ev) {
            if (panning) {
                var size = imgSize();
                var ws = wrapSize();
                var moved = clampPan(panning.ox + (ev.clientX - panning.x),
                                     panning.oy + (ev.clientY - panning.y),
                                     Math.round(size.w * view.scale), Math.round(size.h * view.scale),
                                     ws.w, ws.h, PAN_MARGIN);
                view.x = moved.x;
                view.y = moved.y;
                apply(false);   // 平移只挪位置，不用重绘
                return;
            }
            if (!drawing) { return; }
            var p = imagePoint(ev);
            if (drawing.type === 'pen') {
                drawing.points.push(p);
            } else {
                drawing.x2 = p[0];
                drawing.y2 = p[1];
            }
            paint();
        }

        function onUp() {
            if (panning || drawing) {
                panning = null;
                drawing = null;
                wrap.classList.remove('is-panning');
                if (opts.onOps) { opts.onOps(); }
            }
        }

        function onKeyDown(ev) {
            if (ev && (ev.code === 'Space' || ev.key === ' ')) {
                spaceDown = true;
                wrap.classList.add('is-pan');
            }
        }

        function onKeyUp(ev) {
            if (ev && (ev.code === 'Space' || ev.key === ' ')) {
                spaceDown = false;
                wrap.classList.remove('is-pan');
            }
        }

        canvas.addEventListener('wheel', onWheel, { passive: false });
        canvas.addEventListener('pointerdown', onDown);
        canvas.addEventListener('pointermove', onMove);
        canvas.addEventListener('pointerup', onUp);
        canvas.addEventListener('pointercancel', onUp);
        canvas.addEventListener('contextmenu', function (ev) { ev.preventDefault(); });
        window.addEventListener('keydown', onKeyDown);
        window.addEventListener('keyup', onKeyUp);
        window.addEventListener('blur', onKeyUp);
        if (typeof ResizeObserver !== 'undefined') {
            new ResizeObserver(onResize).observe(wrap);
        }

        return {
            paint: paint,
            setImage: function (img) {
                opts.setBase(img || null);
                if (img && (img.width || img.height)) { fitView(); } else { apply(false); }
            },
            reset: fitView,
            onResize: onResize,
            view: function () {
                var size = imgSize();
                return {
                    scale: view.scale, fit: fit, x: view.x, y: view.y,
                    zoom: zoomPercent(view.scale), userZoomed: userZoomed,
                    canvasW: canvas.width, canvasH: canvas.height,
                    imgW: size.w, imgH: size.h,
                    ratio: size.w ? canvas.width / size.w : 0
                };
            }
        };
    }

    var api = {
        fitScale: fitScale, scaleRange: scaleRange, clampScale: clampScale,
        scaleAtCursor: scaleAtCursor, clampPan: clampPan,
        toImage: toImage, toCanvas: toCanvas,
        strokeWidth: strokeWidth, zoomPercent: zoomPercent,
        baseWidthOf: baseWidthOf, render: render, mount: mount
    };
    if (typeof window !== 'undefined' && window.WPFeedbackPanel) {
        window.WPFeedbackPanel.canvas = api;
    }
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = api;
    }
})();
