// SFTP 路径记忆与快速访问 — SSH 页 SFTP 面板与文件页"上传到服务器"共用
// 数据结构: { [hostId]: { last: '/path', pins: ['/path', ...] } }
window.SftpMemory = (function () {
    'use strict';
    var KEY = 'wp.sftp.memory';

    function read() {
        try {
            var raw = JSON.parse(localStorage.getItem(KEY) || '{}');
            return raw && typeof raw === 'object' ? raw : {};
        } catch (err) { return {}; }
    }

    function write(all) {
        try { localStorage.setItem(KEY, JSON.stringify(all)); } catch (err) { /* quota */ }
    }

    function entry(hostId) {
        var all = read();
        var e = all[hostId];
        if (!e || typeof e !== 'object') { e = {}; }
        if (typeof e.last !== 'string') { e.last = ''; }
        if (!Array.isArray(e.pins)) { e.pins = []; }
        return e;
    }

    function save(hostId, e) {
        if (!hostId) { return; }
        var all = read();
        all[hostId] = e;
        write(all);
    }

    function getLast(hostId) {
        return hostId ? entry(hostId).last : '';
    }

    function getHome(hostId) {
        return hostId ? (entry(hostId).home || '') : '';
    }

    function setHome(hostId, path) {
        if (!hostId || !path) { return; }
        var e = entry(hostId);
        e.home = path;
        save(hostId, e);
    }

    function setLast(hostId, path) {
        if (!hostId || !path) { return; }
        var e = entry(hostId);
        e.last = path;
        save(hostId, e);
    }

    function getPins(hostId) {
        return hostId ? entry(hostId).pins.slice() : [];
    }

    function isPinned(hostId, path) {
        return !!hostId && entry(hostId).pins.indexOf(path) >= 0;
    }

    function pin(hostId, path) {
        if (!hostId || !path) { return []; }
        var e = entry(hostId);
        if (e.pins.indexOf(path) < 0) { e.pins.push(path); }
        e.last = path;
        save(hostId, e);
        return e.pins.slice();
    }

    function unpin(hostId, path) {
        if (!hostId) { return []; }
        var e = entry(hostId);
        e.pins = e.pins.filter(function (p) { return p !== path; });
        save(hostId, e);
        return e.pins.slice();
    }

    return {
        getLast: getLast, setLast: setLast,
        getHome: getHome, setHome: setHome,
        getPins: getPins, isPinned: isPinned, pin: pin, unpin: unpin,
    };
})();