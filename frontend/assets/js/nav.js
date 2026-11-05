// 全局导航配置 — 唯一来源。
//
// 背景: 导航原先在两处各写一份 —— app-shell.js 的 PRIMARY/MORE 与 shell.js 的
// VIEWS/NAV/TABS。两份已经漂移 (新壳有 tasks, 旧壳没有), 而 principles.md
// 原则 2 要求"全局只有一个导航面", 已否决的设计里也明确列了"复制动作表"。
//
// 约定:
//   views   视图定义 (href 可缺省 —— session 是本地/SSH 切换视图, 无固定地址)
//   primary 壳底部/顶栏的主项
//   more    "更多"里出现的视图
// 插件导航不走这里: 由服务端经 window.__WP_PLUGIN_NAV__ 注入, 两个壳各自追加。
window.WP_NAV = {
    views: {
        home: { href: '/', label: '控制台', icon: 'grid' },
        tasks: { href: '/tasks', label: '任务', icon: 'task' },
        files: { href: '/files', label: '文件', icon: 'folder' },
        session: { label: '会话', icon: 'terminal' },
        screen: { href: '/screen', label: '屏幕', icon: 'screen' },
        command: { href: '/command', label: '命令', icon: 'play' },
        settings: { href: '/settings', label: '设置', icon: 'settings' },
    },
    primary: ['home', 'files', 'session'],
    more: ['tasks', 'screen', 'command', 'settings'],
};
