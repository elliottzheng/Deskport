// 终端共享常量与状态文案（term-client / session-ui / controls 共用）
window.TermEnv = (function () {
    'use strict';
    var PING_INTERVAL = 15000;
    var RECONNECT_BASE = 1000;
    var RECONNECT_MAX = 30000;
    var ACTIVE_SESSION_KEY = 'activeSessionId';
var DEVICE_REPLY = /^(?:\x1b\[[?>]?[0-9;]*c|\x1b\[[0-9;]*n|\x1b\][0-9]+;[^\x07\x1b]*(?:\x07|\x1b\\)|\x1bP[^\x1b]*\x1b\\|\x1b\[[?>]?[0-9;]*\$y)$/;

    function stateText(s) {
        if (s.attachedElsewhere && (s.state === 'detached' || s.state === 'connecting')) {
            return '使用中';   // 会话正被其他窗口/设备使用
        }
        switch (s.state) {
            case 'connecting': return '连接中';
            case 'connected': return '已连接';
            case 'reconnecting': return '重连中 ' + (s.retryCount || 0);
            case 'detached': return '已断开';
            case 'ended': return '已退出';
            default: return s.state || '-';
        }
    }
    return {
        PING_INTERVAL: PING_INTERVAL, RECONNECT_BASE: RECONNECT_BASE,
        RECONNECT_MAX: RECONNECT_MAX, ACTIVE_SESSION_KEY: ACTIVE_SESSION_KEY,
        DEVICE_REPLY: DEVICE_REPLY, stateText: stateText,
    };
})();
