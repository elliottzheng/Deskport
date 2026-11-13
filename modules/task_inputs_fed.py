# -*- coding: utf-8 -*-
"""跨机附件投递的联邦成员通道：成员 Deskport 的文件写端点。

用与文件页联邦转发同一套头（`X-Deskport-Token` + `X-Deskport-From`），
受两层开关约束（错误信息必须说清是哪一层）:

    中枢   remotes[].filesWrite   没开 → 直接 403，不发请求；
    成员   config.federationRoots 没配 → 先按 federation-info 的
           writeRootsConfigured 拒绝，再按成员的 403 原文兜底。

联邦不转发永久删除，失败清理走 `recycle-delete`（成员机回收站）；
上传同名自动 `(2)`，不覆盖。
"""
import io
import os
from urllib.parse import quote

import requests

import config
from modules import federation_write, fsutil
from modules.task_inputs_base import DeliverError, memberError

HTTP_TIMEOUT = 30


class FedTarget:
    kind = 'federation'

    def __init__(self, member):
        self.member = member
        self.name = member.get('name') or member.get('id') or ''
        self.created = []      # [(path, isDir)] 本次落的顶层条目
        self._cache = {}
        self._info = None
        self.session = requests.Session()

    # ---- HTTP ----
    def _call(self, method, path, timeout=HTTP_TIMEOUT, fromHeader=True, **kw):
        headers = {'X-Deskport-Token': self.member.get('token') or ''}
        if fromHeader:
            headers['X-Deskport-From'] = quote(config.getBrandName(), safe='')
            headers['X-Deskport-Actor'] = quote('task-inputs', safe='')
        try:
            resp = self.session.request(method, self.member['base'] + path,
                                        headers=headers, timeout=timeout, **kw)
        except requests.RequestException as e:
            raise DeliverError('执行点 %s 不可达: %s'
                               % (self.name, str(e)[:200]), 502)
        try:
            data = resp.json() if resp.content else {}
        except ValueError:
            data = {}
        return resp.status_code, (data if isinstance(data, dict) else {})

    def info(self):
        """成员声明：maxUploadMB 与根白名单是否已配（只问一次）。"""
        if self._info is None:
            # 能力声明是只读端点，必须不带 X-Deskport-From（成员端联邦
            # 白名单不含它）；与 files_federation._memberUploadLimitMB 同口径。
            status, data = self._call('GET', '/api/files/federation-info',
                                      timeout=6, fromHeader=False)
            self._info = data if status == 200 else {}
        return self._info

    def join(self, *parts):
        return os.path.join(*[str(p) for p in parts if str(p)])

    # ---- 远端探测 ----
    def _names(self, directory):
        """目录下一层名字（小写集合）；目录不存在返回 None。按目录缓存。"""
        key = os.path.normcase(os.path.normpath(directory))
        if key in self._cache:
            return self._cache[key]
        status, data = self._call('GET', '/api/files',
                                  params={'path': directory, 'hidden': '1'})
        if status == 200:
            names = {str(e.get('name') or '').lower()
                     for e in data.get('entries') or []}
        elif status == 404:
            names = None
        else:
            raise memberError(status, data, directory)
        self._cache[key] = names
        return names

    def isdir(self, path):
        return self._names(path) is not None

    def exists(self, path):
        parent, name = os.path.split(path)
        names = self._names(parent or '/')
        return names is not None and name.lower() in names

    # ---- 写 ----
    def _mkdir(self, parent, name):
        status, data = self._call('POST', '/api/files/new-folder',
                                  json={'path': parent, 'name': name})
        if status == 200:
            self._forget(parent)
            return True
        if status == 409:
            return False
        raise memberError(status, data, self.join(parent, name))

    def _forget(self, directory):
        self._cache.pop(os.path.normcase(os.path.normpath(directory)), None)

    def ensureDir(self, path):
        path = os.path.normpath(path)
        if self.exists(path):
            return False
        parent = os.path.dirname(path)
        name = os.path.basename(path)
        if not parent or not name:
            raise DeliverError('执行点上的路径不合法: %s' % path)
        if not self.exists(parent):
            raise DeliverError('执行点 %s 的工作目录不存在: %s'
                               % (self.name, parent))
        return self._mkdir(parent, name)

    def _unique(self, directory, name, isDir):
        return fsutil.uniquePath(directory, name, isDir=isDir, start=2, sep='',
                                 pathmod=os.path, exists=self.exists)

    def mkdirUnique(self, name, directory, track=True):
        target = self._unique(directory, name, True)
        parent, base = os.path.split(target)
        self._mkdir(parent, base)
        if track:
            self.created.append((target, True))
        return target

    def _upload(self, name, directory, payload, track):
        target = self._unique(directory, name, False)
        parent, base = os.path.split(target)
        status, data = self._call('POST', '/api/files/upload',
                                  data={'path': parent},
                                  files={'file': (base, payload)})
        if status != 200:
            raise memberError(status, data, target)
        path = str(data.get('path') or self.join(parent, base))
        self._forget(parent)
        if track:
            self.created.append((path, False))
        return path

    def putFile(self, localPath, name, directory, track=False):
        with open(localPath, 'rb') as fh:
            return self._upload(name, directory, fh, track)

    def putBytes(self, raw, name, directory, track=False):
        return self._upload(name, directory, io.BytesIO(raw), track)

    def cleanup(self, base, made):
        """联邦没有永久删除：把本次落的条目移进成员机回收站。"""
        paths = [base] if made.get('task') else [p for p, _d in self.created]
        if not paths:
            return
        try:
            self._call('POST', '/api/files/recycle-delete',
                       json={'paths': paths})
        except DeliverError:
            pass

    def maxMB(self):
        local = federation_write.uploadLimitMB()
        try:
            memberMB = int(self.info().get('maxUploadMB') or 0)
        except (TypeError, ValueError):
            memberMB = 0
        return min(local, memberMB) if memberMB > 0 else local

    def close(self):
        try:
            self.session.close()
        except Exception:
            pass
