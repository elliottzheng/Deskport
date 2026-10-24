// 壳与 iframe 的消息契约：只允许引用这里的常量，避免字符串拼错。
window.WP_MSG = Object.freeze({
    V: 1,
    SRC: { SHELL: 'wp-shell', EMBED: 'wp-embed' },
    SHELL: {
        VIEW_ACTIVE: 'view-active',
        ACTIVATE_SESSION: 'activate-session',
        OPEN_TERMINAL: 'open-terminal',
    },
    EMBED: {
        TITLE: 'title',
        NAV: 'nav',
        FILES_PATH: 'files-path',
    },
});
