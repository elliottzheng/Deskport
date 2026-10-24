// SSH 主机列表的唯一数据源 + 订阅通知（Observer）。
window.SSHHostStore = (function () {
    'use strict';
    let hosts = [];
    const subscribers = [];

    function notify() {
        subscribers.slice().forEach(function (fn) {
            try { fn(hosts); } catch (err) { console.error('host store subscriber:', err); }
        });
    }

    return {
        get hosts() { return hosts; },
        setHosts: function (list) {
            hosts = Array.isArray(list) ? list : [];
            notify();
        },
        subscribe: function (fn) {
            if (typeof fn !== 'function') { return function () {}; }
            subscribers.push(fn);
            return function () {
                const i = subscribers.indexOf(fn);
                if (i >= 0) { subscribers.splice(i, 1); }
            };
        },
    };
})();
