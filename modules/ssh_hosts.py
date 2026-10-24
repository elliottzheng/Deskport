# -*- coding: utf-8 -*-
"""SSH 主机与连接：主机/凭据存储、known_hosts TOFU、连接注册表、主机 API。"""
import base64
import io
import json
import secrets
import threading
import time
from hashlib import sha256

import paramiko
from flask import jsonify, request

import config
from modules.ssh_base import blueprint, log, HostKeyChangedError, CONNECT_TIMEOUT


def _knownHostsPath():
    return config.statePath('ssh_known_hosts.json')


# ---- 连接注册表 ----
class HostRepository:
    """主机/凭据仓储：JSON 文件读写，原子写由 state 统一负责。"""

    def __init__(self, path=None):
        self.path = path
        self.store = {'hosts': []}
        self.loaded = False

    def filePath(self):
        return self.path or config.statePath('ssh_hosts.json')

    def load(self):
        if self.loaded:
            return
        self.loaded = True
        try:
            with open(self.filePath(), encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get('hosts'), list):
                self.store = data
        except (OSError, ValueError):
            pass

    def save(self):
        from modules import state
        state.atomicWriteJson(self.filePath(), self.store)

    def find(self, hostId):
        self.load()
        return next((h for h in self.store.get('hosts', []) if h['id'] == hostId), None)

    def all(self):
        self.load()
        return list(self.store.get('hosts', []))

    def upsert(self, entry):
        self.load()
        existing = self.find(entry['id'])
        if existing is None:
            self.store['hosts'].append(entry)
        else:
            self.store['hosts'][self.store['hosts'].index(existing)] = entry

    def remove(self, hostId):
        self.load()
        self.store['hosts'] = [h for h in self.store['hosts'] if h['id'] != hostId]


class ConnectionRegistry:
    """hostId -> paramiko SSHClient；只负责连接生命周期。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.connections = {}

    def get(self, hostId):
        with self.lock:
            return self.connections.get(hostId)

    def put(self, hostId, client):
        with self.lock:
            self.connections[hostId] = client

    def pop(self, hostId):
        with self.lock:
            return self.connections.pop(hostId, None)

    def drop(self, hostId):
        client = self.pop(hostId)
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


repo = HostRepository()
registry = ConnectionRegistry()

# ---- 主机存储 ----
def _loadStore():
    repo.load()


def _saveStore():
    repo.save()


def _getStore():
    return repo.store


def _loadKey(keyPem, passphrase):
    """解析私钥 PEM (按 Ed25519/ECDSA/RSA 依次尝试)。"""
    buf = io.StringIO(keyPem)
    lastError = None
    for cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
        try:
            return cls.from_private_key(buf, password=passphrase)
        except paramiko.SSHException as e:
            lastError = e
            buf.seek(0)
    raise paramiko.SSHException('无法解析私钥: ' + str(lastError))


def _getConnection(hostEntry):
    hostId = hostEntry['id']
    client = registry.get(hostId)
    if client is not None:
        transport = client.get_transport()
        if transport is not None and transport.is_active():
            return client
        registry.pop(hostId)

    # known_hosts TOFU: 先建立 transport 读取服务器主机密钥指纹
    transport = paramiko.Transport((hostEntry['host'],
                                     int(hostEntry.get('port') or 22)))
    transport.start_client(timeout=CONNECT_TIMEOUT)
    hostKey = transport.get_remote_server_key()
    fingerprint = _fingerprint(hostKey)
    stored = _knownFingerprint(hostEntry['host'], int(hostEntry.get('port') or 22))
    if stored is None:
        # 首次连接: 自动信任 (TOFU); 预检端点供前端展示确认
        _saveKnownFingerprint(hostEntry['host'], int(hostEntry.get('port') or 22),
                              fingerprint)
    elif stored != fingerprint:
        transport.close()
        raise HostKeyChangedError('主机密钥已变更! 指纹: ' + fingerprint +
                                  ' (原: ' + stored + ')。确认无中间人攻击后可重新信任。')

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client._transport = transport
    username = hostEntry['user']
    if hostEntry.get('authType') == 'key':
        transport.auth_publickey(username, _loadKey(hostEntry.get('privateKey'),
                                                    hostEntry.get('passphrase')))
    else:
        transport.auth_password(username, hostEntry.get('password'))
    registry.put(hostId, client)
    log.info('ssh connect host=%s user=%s@%s:%s fp=%s', hostId,
             username, hostEntry['host'], hostEntry.get('port'), fingerprint[:20])
    return client


def dropConnection(hostId):
    registry.drop(hostId)


def _fingerprint(key):
    digest = sha256(key.get_fingerprint()).digest()
    return 'SHA256:' + base64.b64encode(digest).decode().rstrip('=')


def _knownHosts():
    try:
        with open(_knownHostsPath(), 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _knownFingerprint(host, port):
    entry = _knownHosts().get(host + ':' + str(port))
    if isinstance(entry, dict):
        return entry.get('fingerprint')
    return entry


def _saveKnownFingerprint(host, port, fingerprint):
    data = _knownHosts()
    data[host + ':' + str(port)] = {'fingerprint': fingerprint,
                                    'addedAt': time.time()}
    from modules import state
    state.atomicWriteJson(_knownHostsPath(), data)


@blueprint.route('/api/ssh/hostkey/check')
def hostkeyCheck():
    """TOFU 预检: 读取服务器主机密钥指纹, 与已存指纹比对。"""
    hostId = request.args.get('hostId')
    hostEntry = repo.find(hostId)
    if hostEntry is None:
        return jsonify({'error': '主机不存在'}), 404
    host = hostEntry['host']
    port = int(hostEntry.get('port') or 22)
    try:
        transport = paramiko.Transport((host, port))
        transport.start_client(timeout=CONNECT_TIMEOUT)
        hostKey = transport.get_remote_server_key()
        fingerprint = _fingerprint(hostKey)
        transport.close()
    except Exception as e:
        return jsonify({'error': '无法连接: ' + str(e)}), 502
    stored = _knownFingerprint(host, port)
    if stored is None:
        status = 'new'
    elif stored == fingerprint:
        status = 'known'
    else:
        status = 'changed'
    return jsonify({'status': status, 'fingerprint': fingerprint, 'stored': stored,
                    'host': host, 'port': port})


@blueprint.route('/api/ssh/hostkey/trust', methods=['POST'])
def hostkeyTrust():
    data = request.json or {}
    host = data.get('host')
    port = int(data.get('port') or 22)
    fingerprint = data.get('fingerprint')
    if not host or not fingerprint:
        return jsonify({'error': '缺少 host 或 fingerprint'}), 400
    _saveKnownFingerprint(host, port, fingerprint)
    return jsonify({'ok': True})

# ---- 主机 CRUD ----
@blueprint.route('/api/ssh/hosts')
def listHosts():
    hosts = []
    for h in repo.all():
        hosts.append({k: h.get(k) for k in
                      ('id', 'name', 'host', 'port', 'user', 'authType',
                       'group', 'favorite', 'notes', 'createdAt')})
    hosts.sort(key=lambda h: (not h.get('favorite'), h['name'].lower()))
    return jsonify({'hosts': hosts})


@blueprint.route('/api/ssh/hosts', methods=['POST'])
def saveHost():
    data = request.json or {}
    name = (data.get('name') or '').strip()
    host = (data.get('host') or '').strip()
    if not name or not host:
        return jsonify({'error': '名称与主机地址必填'}), 400
    entry = {'id': data.get('id') or secrets.token_hex(8),
             'name': name, 'host': host,
             'port': int(data.get('port') or 22),
             'user': (data.get('user') or 'root').strip(),
             'authType': data.get('authType') or 'password',
             'group': (data.get('group') or '').strip(),
             'favorite': bool(data.get('favorite')),
             'notes': (data.get('notes') or '').strip(),
             'createdAt': data.get('createdAt') or time.time()}
    existing = repo.find(entry['id'])
    if existing is not None:
        entry['createdAt'] = existing.get('createdAt') or entry['createdAt']
    repo.upsert(entry)
    # 秘密字段: 提供了才更新 (编辑时不传=保留原值); 明文存储 (与 config.json 同级信任)
    for plainKey in ('password', 'privateKey', 'passphrase'):
        plain = data.get(plainKey)
        if plain:
            entry[plainKey] = plain
        elif existing is not None and existing.get(plainKey):
            entry[plainKey] = existing.get(plainKey)
    _saveStore()
    return jsonify({'ok': True, 'id': entry['id']})


@blueprint.route('/api/ssh/hosts/<hostId>', methods=['DELETE'])
def deleteHost(hostId):
    dropConnection(hostId)
    repo.remove(hostId)
    _saveStore()
    return jsonify({'ok': True})

# ---- 连接管理 ----
@blueprint.route('/api/ssh/connect', methods=['POST'])
def connectHost():
    hostId = (request.json or {}).get('hostId')
    hostEntry = repo.find(hostId)
    if hostEntry is None:
        return jsonify({'error': '主机不存在'}), 404
    try:
        _getConnection(hostEntry)
        return jsonify({'ok': True})
    except paramiko.AuthenticationException:
        return jsonify({'error': '认证失败', 'authRetry': True}), 401
    except HostKeyChangedError as e:
        return jsonify({'error': str(e), 'hostKeyChanged': True}), 409
    except Exception as e:
        return jsonify({'error': str(e)}), 502


@blueprint.route('/api/ssh/disconnect', methods=['POST'])
def disconnectHost():
    hostId = (request.json or {}).get('hostId')
    # SSH 托管在 sessiond 时, 让守护进程结束会话并断开连接 (内部契约见 internal_api)
    if config.getSessiondUrl('ssh'):
        try:
            from modules import internal_api
            internal_api.disconnectSsh(hostId)
        except Exception:
            pass
    dropConnection(hostId)
    return jsonify({'ok': True})


def findHost(hostId):
    return repo.find(hostId)


def getConnection(hostEntry):
    return _getConnection(hostEntry)


def cacheConnection(hostId, client):
    registry.put(hostId, client)
