// 批注画布（feedback-canvas.js）视图数学与重绘纯函数单测。
// 跑法：node --test tests/js/（check_frontend 门禁含这一步）
import { test } from 'node:test';
import assert from 'node:assert/strict';
import canvasMod from '../../frontend/assets/js/feedback-canvas.js';

const {
    fitScale, scaleRange, clampScale, scaleAtCursor, clampPan,
    toImage, toCanvas, strokeWidth, zoomPercent, baseWidthOf, render
} = canvasMod;

test('fitScale: 整图缩进包裹层，取小的一边；缺尺寸按 1', () => {
    assert.equal(fitScale(1920, 1080, 1040, 480), 480 / 1080);
    assert.equal(fitScale(800, 600, 1040, 480), 0.8);
    assert.equal(fitScale(0, 0, 100, 100), 1);
    assert.equal(fitScale(100, 100, 0, 0), 1);
});

test('scaleRange/clampScale: 下限适配值一半、上限 4× 与单边刹车', () => {
    const fit = 0.5;
    assert.deepEqual(scaleRange(fit, 1000, 800), { min: 0.25, max: 4 });
    assert.equal(clampScale(10, fit, 1000, 800), 4);
    assert.equal(clampScale(0.01, fit, 1000, 800), 0.25);
    assert.equal(clampScale(1, fit, 1000, 800), 1);
    // 图很宽时受单边 8192 刹车：8192/1920 ≈ 4.27 > 4，仍取 4
    assert.equal(clampScale(99, 0.5, 1920, 1080), 4);
    // 4000px 宽的图：4× 会到 16000 > 8192，刹车生效
    assert.equal(clampScale(99, 0.5, 4000, 3000), 8192 / 4000);
});

test('scaleAtCursor: 滚轮以光标为中心，光标下的图像点不动', () => {
    // 画布 1000px 宽（图 1000×500，scale=1），平移 (0,0)，光标在画布 (200,100)
    const before = { x: 0, y: 0 };
    const r = scaleAtCursor(1, 2, 200, 100, 0, 0, before.x, before.y, 0.5, 1000, 500);
    assert.equal(r.scale, 2);
    // 光标下的图像点 x=200 → 新画布坐标 400 → panX' = 200-400 = -200
    assert.equal(r.x, -200);
    assert.equal(r.y, -100);
    // 已平移时同样成立：pan=(50,20)，光标屏幕 (250,120) → 画布坐标 (200,100)
    const r2 = scaleAtCursor(1, 2, 250, 120, 0, 0, 50, 20, 0.5, 1000, 500);
    assert.equal(r2.x, 250 - 400);
    assert.equal(r2.y, 120 - 200);
});

test('scaleAtCursor: 超出范围被夹住（下限适配一半、上限 4×）', () => {
    const r = scaleAtCursor(1, 100, 10, 10, 0, 0, 0, 0, 0.5, 1000, 500);
    assert.equal(r.scale, 4);
    const r2 = scaleAtCursor(1, 0.001, 10, 10, 0, 0, 0, 0, 0.5, 1000, 500);
    assert.equal(r2.scale, 0.25);
});

test('clampPan: 画布不被整个拖飞；画布小于包裹层时可居中', () => {
    // 画布 2000×1000 在 1000×500 的包裹层里：x 最多拖到 1000-48=952（右侧留 48）
    let p = clampPan(5000, 5000, 2000, 1000, 1000, 500);
    assert.equal(p.x, 952);
    assert.equal(p.y, 452);
    p = clampPan(-5000, -5000, 2000, 1000, 1000, 500);
    assert.equal(p.x, 48 - 2000);   // 左侧至少露出 48
    // 小画布（200×100）在大包裹层里：区间含 0，居中值不被动
    p = clampPan(0, 0, 200, 100, 1000, 500);
    assert.equal(p.x, 0);
    assert.equal(p.y, 0);
    // 画布恰好盖住包裹层：仍可在 ±(wrap-2×margin) 内挪，重叠不少于 48
    p = clampPan(123, 456, 1000, 500, 1000, 500);
    assert.equal(p.x, 123);
    assert.equal(p.y, 452);   // 456 超出 [-452, 452]，夹住
    p = clampPan(9999, 9999, 1000, 500, 1000, 500);
    assert.equal(p.x, 952);
    assert.equal(p.y, 452);
});

test('toImage/toCanvas: 屏幕↔图像坐标按缩放比换算', () => {
    assert.deepEqual(toImage(300, 200, 100, 50, 2), [100, 75]);
    assert.deepEqual(toCanvas(100, 75, 2), [200, 150]);
    // 1:1 时一进一出恒等
    assert.deepEqual(toCanvas(...toImage(640, 360, 0, 0, 1), 1), [640, 360]);
});

test('strokeWidth: 粗细按缩放比补偿，1:1 时等于基粗', () => {
    assert.equal(strokeWidth(1, 5), 5);
    assert.equal(strokeWidth(2, 5), 10);
    assert.equal(strokeWidth(0.5, 5), 2.5);
    assert.equal(strokeWidth(0.01, 5), 1);      // 至少 1 画布像素
    assert.equal(strokeWidth(1, 0), 3);         // 缺省基粗兜底
    assert.equal(baseWidthOf(1920), 5);         // 与改前 canvas.width/400 口径一致
    assert.equal(baseWidthOf(0), 2);
});

test('zoomPercent: 100% 即 1:1', () => {
    assert.equal(zoomPercent(1), 100);
    assert.equal(zoomPercent(0.542), 54);
    assert.equal(zoomPercent(2.5), 250);
});

test('render: 坐标与线宽按 scale 补偿；打码走 fillRect', () => {
    const calls = [];
    const ctx = {
        setTransform: (...a) => calls.push(['setTransform', ...a]),
        clearRect: (...a) => calls.push(['clearRect', ...a]),
        drawImage: (...a) => calls.push(['drawImage', ...a]),
        save: () => calls.push(['save']),
        restore: () => calls.push(['restore']),
        beginPath: () => calls.push(['beginPath']),
        moveTo: (...a) => calls.push(['moveTo', ...a]),
        lineTo: (...a) => calls.push(['lineTo', ...a]),
        stroke: () => calls.push(['stroke']),
        strokeRect: (...a) => calls.push(['strokeRect', ...a]),
        fillRect: (...a) => calls.push(['fillRect', ...a]),
        set strokeStyle(v) { calls.push(['strokeStyle', v]); },
        set fillStyle(v) { calls.push(['fillStyle', v]); },
        set lineWidth(v) { calls.push(['lineWidth', v]); },
        set lineCap(v) { calls.push(['lineCap', v]); },
        set lineJoin(v) { calls.push(['lineJoin', v]); },
    };
    const ops = [
        { type: 'pen', w: 5, points: [[10, 20], [30, 40]] },
        { type: 'rect', w: 5, x1: 100, y1: 50, x2: 140, y2: 90 },
        { type: 'black', w: 5, x1: 0, y1: 0, x2: 60, y2: 20 },
    ];
    render(ctx, { width: 200, height: 100 }, ops, 2, 400, 200);
    assert.ok(calls.some(c => c[0] === 'drawImage' && c[4] === 400 && c[5] === 200));
    // 画笔：图像坐标 ×2，线宽 5×2
    assert.ok(calls.some(c => c[0] === 'moveTo' && c[1] === 20 && c[2] === 40));
    assert.ok(calls.some(c => c[0] === 'lineTo' && c[1] === 60 && c[2] === 80));
    assert.ok(calls.some(c => c[0] === 'lineWidth' && c[1] === 10));
    // 框选：strokeRect 起点与尺寸都 ×2
    assert.ok(calls.some(c => c[0] === 'strokeRect' && c[1] === 200 && c[2] === 100 && c[3] === 80 && c[4] === 80));
    // 打码：fillRect 且描边色为深灰
    assert.ok(calls.some(c => c[0] === 'fillRect' && c[1] === 0 && c[2] === 0 && c[3] === 120 && c[4] === 40));
    assert.ok(calls.some(c => c[0] === 'strokeStyle' && c[1] === '#111827'));
    // 1:1 时坐标原样、线宽=基粗
    calls.length = 0;
    render(ctx, { width: 200, height: 100 }, [{ type: 'pen', w: 5, points: [[10, 20], [30, 40]] }], 1, 200, 100);
    assert.ok(calls.some(c => c[0] === 'moveTo' && c[1] === 10 && c[2] === 20));
    assert.ok(calls.some(c => c[0] === 'lineWidth' && c[1] === 5));
});

test('render: 空 ops / 无底图不炸，单点笔画不描', () => {
    const calls = [];
    const ctx = {
        setTransform: () => calls.push(['setTransform']),
        clearRect: () => calls.push(['clearRect']),
        drawImage: () => calls.push(['drawImage']),
        save: () => {}, restore: () => {},
        beginPath: () => {}, moveTo: () => {}, lineTo: () => {},
        stroke: () => calls.push(['stroke']),
        strokeRect: () => {}, fillRect: () => {},
        strokeStyle: '', fillStyle: '', lineWidth: 0, lineCap: '', lineJoin: '',
    };
    render(ctx, null, [], 2, 100, 100);
    assert.equal(calls.filter(c => c[0] === 'stroke').length, 0);
    render(ctx, null, [{ type: 'pen', w: 3, points: [[1, 1]] }], 1, 100, 100);
    assert.equal(calls.filter(c => c[0] === 'stroke').length, 0);
});
