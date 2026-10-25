# 插件系统

Deskport 支持两种插件，同一套加载器：

- **原生插件**：声明 `api: 1`，注册到 `/p/<id>`，可以出现在导航里，页面进壳（iframe）。
- **legacy 插件**：`mc-lhz/RemoteConnecter` 的插件，`plugin.json` 只有 `id` / `entry` / `lib` 也能直接加载，保持原来的根路径（`/process`、`/code`）。

## 目录

| 位置 | 用途 |
|---|---|
| `<仓库>/plugins/` | 随项目分发的内置插件 |
| `%APPDATA%\<stateDirName>\plugins\` | 用户插件；同 id 覆盖内置 |

`stateDirName` 在 `config.json` 里，默认 `web-platform`，dev 实例通常设成 `web-platform-dev`。改完插件需要重启 Deskport（暂不支持热加载）。

## 原生清单

```json
{
  "id": "process",
  "name": "进程管理",
  "version": "1.0.0",
  "api": 1,
  "entry": "process_bp.py",
  "nav": [{ "label": "进程", "icon": "grid", "path": "" }],
  "openWith": ["docx", "xlsx"]
}
```

字段说明：

- `id`：`^[a-z][a-z0-9_-]{1,31}$`，不能和内置路由重名；
- `api`：插件 API 版本，当前只支持 `1`；缺省按 legacy 处理；
- `nav`：可空。`path` 相对于 `/p/<id>`，留空表示插件首页；
- `openWith`：扩展名列表，供文件管理器“打开方式”使用（数据已通过 `/api/plugins` 暴露，文件管理 UI 尚未接入）；
- `lib`：私有依赖目录，legacy 插件常用；原生插件建议把依赖写进 `pyproject.toml`。

## 代码约定

入口模块里：

- 任何以 `_bp` 结尾、或名为 `bp` / `blueprint` 的 `Blueprint` 会被自动注册；
- 模块级 `sock = flask_sock.Sock()` 会在全部插件注册完成后统一 `init_app`；
- 模板和静态目录由 Blueprint 自己声明：

```python
from flask import Blueprint

blueprint = Blueprint(
    'process_bp', __name__,
    template_folder='templates',
    static_folder='static',
    static_url_path='/process/static',
)


@blueprint.route('/')
def page():
    return '<h1>process</h1>'
```

原生插件不要写死 `/p/<id>` 前缀，路由写相对路径；宿主注册时会加前缀。

## 导航

- 原生插件：来自 manifest 的 `nav`；
- legacy 插件：自动生成——扫 `app.url_map` 里该蓝图最短的静态页面路由，名称按 `manifest.name` → 模板 `<title>`（去掉 ` - RemoteConnecter`，过滤 `Document` 等）→ 模块 docstring 首行 → id 依次回退。

自动结果写到 `%APPDATA%\<stateDirName>\plugins\nav.auto.json`，不要手改。想覆盖时新建同目录的 `nav.override.json`：

```json
{
  "process": { "label": "进程", "icon": "grid", "order": 10 },
  "proxy": { "hidden": true }
}
```

图标名来自 `frontend/assets/js/icons.js`；未知图标回退为 `plug`。

## /api/plugins

登录后可访问，返回当前插件与发现错误：

```json
{
  "api": 1,
  "plugins": [
    { "id": "process", "legacy": false, "ok": true, "nav": [...], "openWith": ["docx"] }
  ],
  "errors": [{ "dir": "...", "error": "id 非法" }]
}
```

## 认证与信任

- 插件路由和宿主共用同一个登录守卫（`auth.py` 的 `before_request`）；
- 插件 API 未登录返回 `401 JSON`，页面返回 302 到登录页；
- 插件在 Deskport 进程内、以同权限执行。**插件 = 可信代码**：不要在公网实例上装来路不明的插件。

## 导入 legacy 插件

以 `RemoteConnecter/plugins/process` 为例：

1. 把整个 `process/` 目录复制到 `%APPDATA%\web-platform-dev\plugins\process\`（或 prod 的 `web-platform`）；
2. 重启 Deskport；
3. 打开 `/api/plugins` 确认 `ok: true`，导航里会出现“进程”；
4. 页面地址保持原样：`/process`。

注意：

- legacy 插件页面是整页 HTML，会以**新标签页**打开，不进入 Deskport 的壳内导航（避免双顶栏）；
- 路由写在根路径，和宿主冲突时宿主优先；装之前看一眼 `/api/plugins` 的 `ok` 与日志；
- 模板同名时先注册的赢；`lib/` 会进入 `sys.path`，多个插件带同名依赖时按加载顺序生效；
- 轮询型插件注意开销：`process` 在 300+ 进程的机器上单次列表约 4–5 秒，页面每 3 秒刷新一次，Prod 不建议常开；
- 社区重型插件：[RemoteVSCode](https://github.com/mc-lhz/RemoteVSCode)（Monaco 代码工作台，自带前端资源）与 [RemoteOffice](https://github.com/mc-lhz/RemoteOffice)（OnlyOffice 系在线文档）。**Deskport 仓库不分发这两个插件本体，只做兼容加载**；`office` 另需约 530MB 的 `office-dist`（`RC_OFFICE_DIST` 或插件目录下 `office-dist/`），许可证与资源均归上游。

## 已知限制

- 不支持热加载，新增/删除插件需重启；
- legacy 插件没有命名空间，路由/模板冲突只能人工审查；
- 原生插件当前只支持“一个页面 + API”，跨模块联动（如文件管理“打开方式”真正调起插件）是下一步；
- 插件依赖 Python 3.13；从 3.9 迁过来的插件需要自己验证。

## 测试

`tests/test_plugins.py` 覆盖：legacy 根路径、原生前缀、坏清单、导入失败、注册失败隔离、导航生成与覆盖、Sock 初始化、未登录 401。
