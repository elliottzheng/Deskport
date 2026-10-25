# -*- coding: utf-8 -*-
"""插件系统：legacy 兼容、原生挂载、故障隔离、导航生成与认证语义。"""
import json

from flask import Flask

from modules import plugins

LEGACY_SRC = '''
from flask import Blueprint, jsonify, render_template

bp = Blueprint("{bp_name}", __name__, template_folder="templates",
               static_folder="static", static_url_path="/{pid}/static")


@bp.route("/{pid}")
def page():
    return render_template("page.html")


@bp.route("/{pid}/api/ping")
def ping():
    return jsonify(ok=True, plugin="{pid}")
'''

NATIVE_SRC = '''
from flask import Blueprint, jsonify

blueprint = Blueprint("native_bp_{pid}", __name__)


@blueprint.route("/api/ping")
def ping():
    return jsonify(ok=True, plugin="{pid}")
'''


def makePlugin(root, pid, source, manifest=None, templates=None):
    folder = root / pid
    folder.mkdir(parents=True, exist_ok=True)
    manifest = manifest or {"id": pid, "entry": "bp.py"}
    (folder / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    (folder / "bp.py").write_text(source, encoding="utf-8")
    if templates:
        tpl = folder / "templates"
        tpl.mkdir(exist_ok=True)
        for name, text in templates.items():
            (tpl / name).write_text(text, encoding="utf-8")
    return folder


def bareApp():
    app = Flask(__name__)
    app.config["TESTING"] = True
    return app


def loadOne(tmp_path, root):
    app = bareApp()
    result = plugins.load(app, roots=[root], navDir=tmp_path / "nav")
    return app, result


def test_legacy_plugin_loads_at_root(tmp_path):
    root = tmp_path / "plugins"
    root.mkdir()
    makePlugin(root, "demo", LEGACY_SRC.format(pid="demo", bp_name="demo_bp"),
               templates={"page.html": "<title>演示页 - RemoteConnecter</title>"})

    app, result = loadOne(tmp_path, root)
    record = result["plugins"][0]

    assert record["ok"] and record["legacy"]
    assert record["nav"][0]["href"] == "/demo"
    assert record["nav"][0]["label"] == "演示页"
    assert record["nav"][0]["external"] is True

    client = app.test_client()
    assert client.get("/demo").status_code == 200
    assert client.get("/demo/api/ping").get_json()["plugin"] == "demo"


def test_native_plugin_mounts_under_prefix(tmp_path):
    root = tmp_path / "plugins"
    root.mkdir()
    makePlugin(root, "native", NATIVE_SRC.format(pid="native"), manifest={
        "id": "native", "api": 1, "entry": "bp.py", "name": "原生",
        "nav": [{"label": "原生", "icon": "grid", "path": ""}],
    })

    app, result = loadOne(tmp_path, root)
    record = result["plugins"][0]

    assert record["ok"] and not record["legacy"]
    assert record["nav"][0] == {"id": "native", "label": "原生", "icon": "grid",
                                "href": "/p/native", "external": False}

    client = app.test_client()
    assert client.get("/p/native/api/ping").status_code == 200
    assert client.get("/native/api/ping").status_code == 404


def test_broken_manifest_is_reported_and_skipped(tmp_path):
    root = tmp_path / "plugins"
    root.mkdir()
    makePlugin(root, "good", LEGACY_SRC.format(pid="good", bp_name="good_bp"),
               templates={"page.html": "<title>好插件</title>"})
    bad = root / "bad"
    bad.mkdir()
    (bad / "plugin.json").write_text(json.dumps({"id": "Bad Id", "entry": "bp.py"}),
                                    encoding="utf-8")

    app, result = loadOne(tmp_path, root)
    assert [p["id"] for p in result["plugins"]] == ["good"]
    assert result["errors"] and "id" in result["errors"][0]["error"]
    assert app.test_client().get("/good").status_code == 200


def test_import_failure_does_not_block_others(tmp_path):
    root = tmp_path / "plugins"
    root.mkdir()
    makePlugin(root, "boom", "raise RuntimeError('boom')\n")
    makePlugin(root, "good", LEGACY_SRC.format(pid="good", bp_name="good_bp"),
               templates={"page.html": "<title>好插件</title>"})

    app, result = loadOne(tmp_path, root)
    byId = {p["id"]: p for p in result["plugins"]}
    assert byId["boom"]["ok"] is False and "导入失败" in byId["boom"]["error"]
    assert byId["good"]["ok"] is True
    assert app.test_client().get("/good").status_code == 200


def test_registration_failure_isolated(tmp_path):
    root = tmp_path / "plugins"
    root.mkdir()
    # 两个插件使用同一个蓝图名：第二个注册必然 ValueError（Flask 3.1）
    for pid in ("aaa", "bbb"):
        makePlugin(root, pid, LEGACY_SRC.format(pid=pid, bp_name="same_bp"),
                   templates={"page.html": "<title>%s</title>" % pid})

    app, result = loadOne(tmp_path, root)
    byId = {p["id"]: p for p in result["plugins"]}
    assert byId["aaa"]["ok"] is True
    assert byId["bbb"]["ok"] is False
    assert "注册失败" in byId["bbb"]["error"]
    assert app.test_client().get("/aaa").status_code == 200


def test_title_blocklist_and_doc_fallback(tmp_path):
    root = tmp_path / "plugins"
    root.mkdir()
    makePlugin(root, "plain", '"""Plain 插件 — 最小示例。"""\n' +
               LEGACY_SRC.format(pid="plain", bp_name="plain_bp"),
               templates={"page.html": "<title>Document</title>"})

    _app, result = loadOne(tmp_path, root)
    assert result["plugins"][0]["nav"][0]["label"] == "最小示例"


def test_nav_override_wins_and_auto_is_persisted(tmp_path):
    root = tmp_path / "plugins"
    root.mkdir()
    makePlugin(root, "demo", LEGACY_SRC.format(pid="demo", bp_name="demo_bp"),
               templates={"page.html": "<title>原名</title>"})
    navDir = tmp_path / "nav"
    navDir.mkdir()
    (navDir / "nav.override.json").write_text(
        json.dumps({"demo": {"label": "改名", "icon": "grid"}}), encoding="utf-8")

    app = bareApp()
    result = plugins.load(app, roots=[root], navDir=navDir)
    item = result["plugins"][0]["nav"][0]
    assert item["label"] == "改名" and item["icon"] == "grid"
    auto = json.loads((navDir / "nav.auto.json").read_text(encoding="utf-8"))
    assert auto["demo"][0]["label"] == "改名"


def test_unsupported_api_version_is_rejected(tmp_path):
    root = tmp_path / "plugins"
    root.mkdir()
    makePlugin(root, "future", NATIVE_SRC.format(pid="future"),
               manifest={"id": "future", "api": 99, "entry": "bp.py"})

    _app, result = loadOne(tmp_path, root)
    assert result["plugins"] == []
    assert "api" in result["errors"][0]["error"]


def test_plugin_sock_is_initialized_and_renamed(tmp_path):
    root = tmp_path / "plugins"
    root.mkdir()
    source = '''
from flask import Blueprint
from flask_sock import Sock

bp = Blueprint("{pid}_bp", __name__)
sock = Sock()


@sock.route("/{pid}/ws")
def ws(handler):
    handler.send("hi")
'''
    for pid in ("s1", "s2"):
        makePlugin(root, pid, source.format(pid=pid),
                   manifest={"id": pid, "entry": "bp.py"})

    app, result = loadOne(tmp_path, root)
    assert all(p["ok"] for p in result["plugins"])
    rules = {rule.rule for rule in app.url_map.iter_rules()}
    assert "/s1/ws" in rules and "/s2/ws" in rules


def test_plugin_api_returns_401_json_when_unauthenticated(tmp_path):
    from app import create_app

    root = tmp_path / "plugins"
    root.mkdir()
    makePlugin(root, "demo", LEGACY_SRC.format(pid="demo", bp_name="demo_bp"),
               templates={"page.html": "<title>演示</title>"})

    app, _sock = create_app(pluginRoots=[root], pluginNavDir=tmp_path / "nav")
    client = app.test_client()

    apiResp = client.get("/demo/api/ping")
    assert apiResp.status_code == 401 and apiResp.is_json

    pageResp = client.get("/demo")
    assert pageResp.status_code == 302 and "/login" in pageResp.headers["Location"]

    with client.session_transaction() as session:
        session["authenticated"] = True
    assert client.get("/demo/api/ping").status_code == 200


def test_api_plugins_lists_registry_and_errors(tmp_path):
    from app import create_app

    root = tmp_path / "plugins"
    root.mkdir()
    makePlugin(root, "demo", LEGACY_SRC.format(pid="demo", bp_name="demo_bp"),
               templates={"page.html": "<title>演示</title>"})
    (root / "broken").mkdir()
    (root / "broken" / "plugin.json").write_text("not json", encoding="utf-8")

    app, _sock = create_app(pluginRoots=[root], pluginNavDir=tmp_path / "nav")
    client = app.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True

    data = client.get("/api/plugins").get_json()
    assert data["api"] == plugins.API_VERSION
    assert [p["id"] for p in data["plugins"]] == ["demo"]
    assert data["errors"]
