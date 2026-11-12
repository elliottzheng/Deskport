// 文件页共享常量与初始状态（必须在 files.js 与方法模块之前加载）
const TEXT_EXTS = ['txt', 'md', 'json', 'log', 'csv', 'xml', 'yml', 'yaml', 'ini', 'cfg', 'conf',
    'html', 'htm', 'css', 'js', 'mjs', 'py', 'java', 'c', 'cpp', 'h', 'cs', 'go', 'rs', 'sql', 'sh'];
const IMAGE_EXTS = ['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg', 'ico'];

const FM_VIEW_KEY = 'fmView';
const FM_HIDDEN_KEY = 'fmHidden';
const FM_NAV_KEY = 'fmNavCollapsed';
const FM_PATH_KEY = 'fmLastPath';
const FM_LOC_KEY = 'fmLastLoc';
const ILLEGAL = '\\/:*?"<>|';

function _initialLoc() {
    const params = new URLSearchParams(location.search);
    const urlLoc = params.get('loc') || '';
    const urlPath = params.get('path') || '';
    if (urlLoc) { return urlLoc; }
    if (urlPath) { return ''; }
    try { return localStorage.getItem(FM_LOC_KEY) || ''; } catch (err) { return ''; }
}

function _initialPath() {
    const params = new URLSearchParams(location.search);
    const urlPath = params.get('path') || '';
    if (urlPath) { return urlPath; }
    try { return localStorage.getItem(FM_PATH_KEY) || '/'; } catch (err) { return '/'; }
}

// 联邦成员浏览: ?machine=<成员id>, 只从 URL 取 (切成员即重置路径, 不入 localStorage)
function _initialMachine() {
    return (new URLSearchParams(location.search).get('machine') || '').trim();
}

