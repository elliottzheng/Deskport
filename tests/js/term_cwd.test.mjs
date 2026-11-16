// 快捷 CD 栏纯函数单测（反馈 dad400ea 最近访问 / d9377f0f 文件即下载）。
// 跑法：node --test tests/js/（check_frontend 门禁含这一步）
import { test } from 'node:test';
import assert from 'node:assert/strict';
import ui from '../../frontend/assets/js/term-session-ui.js';

const { trimRecent, cdCommand, classifyProbes, baseName, RECENT_CAP } = ui;

// ---------------------------------------------------------------- 最近访问裁剪

test('trimRecent: 按 path 去重留最前、封顶 10、坏条目剔除', () => {
    const many = [];
    for (let i = 1; i <= 14; i++) { many.push({ path: 'D:\\dir' + i, machine: '' }); }
    const out = trimRecent(many, RECENT_CAP);
    assert.equal(out.length, RECENT_CAP);
    assert.deepEqual(out.map(x => x.path),
                     ['D:\\dir1', 'D:\\dir2', 'D:\\dir3', 'D:\\dir4', 'D:\\dir5',
                      'D:\\dir6', 'D:\\dir7', 'D:\\dir8', 'D:\\dir9', 'D:\\dir10']);
    // 重复路径只留最前一条（ newest-first 语义）
    assert.deepEqual(trimRecent([{ path: '/a' }, { path: '/b' }, { path: '/a' }], 10)
                     .map(x => x.path), ['/a', '/b']);
    // 裸字符串、非对象、空 path、空白都容错
    assert.deepEqual(trimRecent(['/x', { path: '/y' }, null, 42, { path: '  ' }, { path: '/x' }], 10),
                     [{ path: '/x', machine: '' }, { path: '/y', machine: '' }]);
    assert.deepEqual(trimRecent(null, 10), []);
    assert.deepEqual(trimRecent('nope', 10), []);
    // 自定义 cap 也生效
    assert.deepEqual(trimRecent([{ path: '/a' }, { path: '/b' }], 1).map(x => x.path), ['/a']);
});

test('trimRecent: machine 跟随条目，缺省空串', () => {
    assert.deepEqual(trimRecent([{ path: '/r', machine: 'm2' }], 10),
                     [{ path: '/r', machine: 'm2' }]);
    assert.deepEqual(trimRecent([{ path: '/r' }], 10), [{ path: '/r', machine: '' }]);
});

// ---------------------------------------------------------------- cd 命令构造

test('cdCommand: 引号包裹、去尾部分隔符；根路径与空值放弃', () => {
    assert.equal(cdCommand('E:\\事项\\默认工作空间'), 'cd "E:\\事项\\默认工作空间"');
    assert.equal(cdCommand('D:\\a b\\c'), 'cd "D:\\a b\\c"');          // 含空格
    assert.equal(cdCommand('  /var/log/  '), 'cd "/var/log"');
    assert.equal(cdCommand('E:\\dir\\'), 'cd "E:\\dir"');
    assert.equal(cdCommand('E:\\'), 'cd "E:"');                        // 盘根保留盘符
    assert.equal(cdCommand('/'), '');                                  // posix 根剥离后为空
    assert.equal(cdCommand(''), '');
    assert.equal(cdCommand(null), '');
});

// ---------------------------------------------------------------- 三态判定

test('classifyProbes: 目录 200→dir；文件 200→file；都不是 200→missing', () => {
    assert.equal(classifyProbes(200, 404), 'dir');
    assert.equal(classifyProbes(200, 200), 'dir');       // 目录探测先判
    assert.equal(classifyProbes(404, 200), 'file');
    assert.equal(classifyProbes(404, 404), 'missing');
    assert.equal(classifyProbes(500, 404), 'missing');
    assert.equal(classifyProbes(0, 404), 'missing');
});

// ---------------------------------------------------------------- 展示名

test('baseName: 兼容盘符/正斜杠/尾分隔符/空值', () => {
    assert.equal(baseName('E:\\a\\b.md'), 'b.md');
    assert.equal(baseName('/var/log/'), 'log');
    assert.equal(baseName('C:\\'), 'C:');
    assert.equal(baseName(''), '');
    assert.equal(baseName(null), '');
});
