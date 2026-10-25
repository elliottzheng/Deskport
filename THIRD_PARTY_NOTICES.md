# 第三方组件与许可证

本项目自身以 MIT 许可证发布。以下组件由各自作者以各自许可证提供，完整文本见对应项目主页或 `uv.lock`/`frontend/assets/vendor/` 内文件头。

## 前端（已 vendored，无运行时 CDN）

| 组件 | 许可证 | 说明 |
|---|---|---|
| Vue 3（`vue.global.prod.js`） | MIT | 含运行时模板编译器 |
| xterm.js | MIT | 终端渲染 |
| xterm-addon-fit / xterm-addon-search | MIT | xterm 插件 |
| xterm.css | MIT | xterm 样式 |

## Python 依赖

| 包 | 许可证（以上游为准） |
|---|---|
| Flask / Werkzeug / Jinja2 | BSD-3-Clause |
| flask-sock / simple-websocket | MIT |
| psutil | BSD-3-Clause |
| Pillow | HPND |
| pynput | LGPL-3.0 |
| pywinpty | MIT |
| requests / urllib3 | Apache-2.0 |
| paramiko | LGPL-2.1 |
| pytest / pyflakes（仅开发） | MIT |

发布二进制或修改分发时，请按各许可证要求保留版权与许可证文本。本文件不构成法律意见。

## 上游项目

| 项目 | 许可证 | 说明 |
|---|---|---|
| [mc-lhz/RemoteConnecter](https://github.com/mc-lhz/RemoteConnecter) | 上游未附许可证 | 本项目受其启发并大量借鉴其代码。上游作者已说明其仓库可无限制使用；原代码版权仍归其作者。 |
