# -*- coding: utf-8 -*-
"""批次 G2 段 1：工作项存储与项目配置——持久化规矩（PS1/PS2）、缺省项目、
校验拒绝。夹具同 test_worklines：statePath 指到 tmp，不起真 kimi。
"""
import json

import pytest
from types import SimpleNamespace

import config as configMod
from modules import workitem_projects, workitem_store
from modules import workitem_state as ws

PASSWORD = 'pw-123456'


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfgfile = tmp_path / 'config.json'
    cfgfile.write_text(json.dumps({'password': PASSWORD}), encoding='utf-8')

    def statePathInTmp(filename):
        target = tmp_path / 'state' / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        return str(target)

    monkeypatch.setattr(configMod, 'CONFIG_PATH', str(cfgfile))
    monkeypatch.setattr(configMod, 'statePath', statePathInTmp)
    configMod._cache.update(mtime=None, data=None)
    monkeypatch.setattr(workitem_store, 'storePath',
                        lambda: str(tmp_path / 'state' / 'workitems.json'))
    monkeypatch.setattr(workitem_store, 'repo',
                        workitem_store.WorkitemRepository(
                            str(tmp_path / 'state' / 'workitems.json')))
    monkeypatch.setattr(workitem_projects, 'storePath',
                        lambda: str(tmp_path / 'state' / 'projects.json'))
    monkeypatch.setattr(workitem_projects, 'repo',
                        workitem_projects.ProjectRepository(
                            str(tmp_path / 'state' / 'projects.json')))
    return SimpleNamespace(tmp=tmp_path, monkeypatch=monkeypatch)


def makeRepo(env):
    return workitem_store.repo


# ---------------------------------------------------------------- 工作项存储

def test_create_and_reload(env):
    repo = makeRepo(env)
    wi = repo.create({'title': '存储', 'projectId': 'deskport',
                      'implModel': 'm1', 'verifyModel': 'm2',
                      'footprint': ['modules/*.py']})
    assert wi['id'].startswith('wi-')
    assert wi['state'] == ws.QUEUED and wi['attempt'] == 0
    assert wi['maxAttempts'] == 3
    assert wi['verifyTasks'] == [] and wi['implTask'] == ''
    assert wi['integration'] == {'commit': '', 'gates': '', 'tests': '', 'log': ''}
    # 新仓储实例（重启恢复）：从盘上读回同一份
    fresh = workitem_store.WorkitemRepository(repo.store_path)
    assert fresh.get(wi['id'])['title'] == '存储'


def test_create_rejects_bad_id(env):
    with pytest.raises(ValueError):
        makeRepo(env).create({'id': 'WI-BAD', 'title': 'x'})


def test_update_whitelist(env):
    repo = makeRepo(env)
    wi = repo.create({'title': 'x', 'projectId': 'p', 'implModel': 'a',
                      'verifyModel': 'b', 'footprint': ['f']})
    repo.update(wi['id'], {'state': ws.IMPLEMENTING, 'implTask': 'abcd1234',
                           'evil': 'nope', 'id': 'hacked'})
    got = repo.get(wi['id'])
    assert got['state'] == ws.IMPLEMENTING and got['implTask'] == 'abcd1234'
    assert 'evil' not in got and got['id'] == wi['id']


def test_taskid_shape_guard():
    assert workitem_store.normalizeTaskId('abcd1234') == 'abcd1234'
    assert workitem_store.normalizeTaskId('wi-123456') == ''
    assert workitem_store.normalizeTaskId('') == ''
    assert workitem_store.normalizeTaskId(None) == ''


def test_integration_log_roundtrip(env):
    repo = makeRepo(env)
    wi = repo.create({'title': 'x', 'projectId': 'p', 'implModel': 'a',
                      'verifyModel': 'b', 'footprint': ['f']})
    path = workitem_store.appendLog(wi['id'], 'merge ok\n')
    assert path and path.endswith('.log')
    workitem_store.appendLog(wi['id'], 'gates pass')
    assert 'merge ok' in workitem_store.readLog(wi['id'])
    assert 'gates pass' in workitem_store.readLog(wi['id'])
    repo.remove(wi['id'])
    assert workitem_store.readLog(wi['id']) == ''


# ---------------------------------------------------------------- 项目配置

def test_default_project_seeded(env):
    projects = workitem_projects.repo.list()
    assert [p['id'] for p in projects] == ['deskport']
    proj = projects[0]
    assert proj['repoPath'] == workitem_projects.DESKPORT_REPO
    assert proj['mainBranch'] == 'main'
    assert '{repo}' in proj['gatesCmd'] and '{tmp}' in proj['testCmd']
    assert proj['setupNotes'] and proj['policy']['allowPaths']


def test_default_project_seeded_only_once(env):
    workitem_projects.repo.load()
    fresh = workitem_projects.ProjectRepository(
        workitem_projects.repo.store_path)
    assert [p['id'] for p in fresh.list()] == ['deskport']


def test_project_validate_ok(tmp_path):
    repo = tmp_path / 'repo'
    (repo / '.git').mkdir(parents=True)
    clean, errors = workitem_projects.validate({
        'id': 'other', 'name': '别的仓库', 'repoPath': str(repo),
        'worktreeRoot': str(tmp_path / 'wt'), 'mainBranch': 'trunk',
        'gatesCmd': 'make gates', 'testCmd': 'make test',
        'setupNotes': '边界', 'fallbackModels': ['m3'],
        'policy': {'default': 'allow', 'allowPaths': [str(repo)]}})
    assert errors == {}, errors
    assert clean['mainBranch'] == 'trunk' and clean['node'] == 'local'
    assert clean['policy']['default'] == 'allow'
    assert clean['policy']['allowBackground'] is False
    # 不给 policy：归一成空对象（= 逐项审批，安全默认）
    clean, errors = workitem_projects.validate({
        'id': 'other', 'name': 'n', 'repoPath': str(repo),
        'worktreeRoot': str(tmp_path / 'wt')})
    assert errors == {} and clean['policy'] == {}


def test_project_validate_rejects(tmp_path):
    repo = tmp_path / 'repo'
    (repo / '.git').mkdir(parents=True)
    _c, errors = workitem_projects.validate({
        'id': 'BAD', 'name': '', 'repoPath': str(tmp_path / 'nope'),
        'worktreeRoot': ''})
    assert 'id' in errors and 'name' in errors
    assert 'repoPath' in errors and 'worktreeRoot' in errors
    notRepo = tmp_path / 'plain'
    notRepo.mkdir()
    _c, errors = workitem_projects.validate({
        'id': 'ok', 'name': 'n', 'repoPath': str(notRepo),
        'worktreeRoot': str(tmp_path / 'wt')})
    assert '不是 git 仓库' in errors['repoPath']
    _c, errors = workitem_projects.validate({
        'id': 'ok', 'name': 'n', 'repoPath': str(repo),
        'worktreeRoot': str(tmp_path / 'wt'), 'node': 'node-1'})
    assert 'node' in errors          # v1：平台直执行只支持本机
    _c, errors = workitem_projects.validate({
        'id': 'ok', 'name': 'n', 'repoPath': str(repo),
        'worktreeRoot': str(tmp_path / 'wt'), 'policy': {'default': 'maybe'}})
    assert 'policy' in errors


def test_project_put_get_remove(env, tmp_path):
    repo = tmp_path / 'repo'
    (repo / '.git').mkdir(parents=True)
    clean, errors = workitem_projects.validate({
        'id': 'other', 'name': '别的仓库', 'repoPath': str(repo),
        'worktreeRoot': str(tmp_path / 'wt')})
    assert errors == {}
    workitem_projects.repo.put(clean)
    assert workitem_projects.repo.get('other')['name'] == '别的仓库'
    workitem_projects.repo.remove('other')
    assert workitem_projects.repo.get('other') is None
    with pytest.raises(KeyError):
        workitem_projects.repo.remove('other')


def test_state_dir_label_in_log(env):
    """集成日志走 ensureWritable 闸门（L6）：只读进程下追加返回空串不炸。"""
    wi = workitem_store.repo.create({'title': 'x', 'projectId': 'p',
                                     'implModel': 'a', 'verifyModel': 'b',
                                     'footprint': ['f']})
    from modules import state_owner
    orig = state_owner.ensureWritable
    state_owner.ensureWritable = lambda what: (_ for _ in ()).throw(
        state_owner.StateDirReadOnly('x', '1'))
    try:
        assert workitem_store.appendLog(wi['id'], 'x') == ''
    finally:
        state_owner.ensureWritable = orig
