# -*- coding: utf-8 -*-
"""跨机附件投递的 agent node 通道：经执行点的 SSH 主机走 SFTP。

执行点配置 `agentNodes[].via` 指向中枢已有的 SSH 主机（同一条隧道用的
那台机器），这里用与 agent_sftp 相同的客户端（`ssh_sftp_base._openSftp`）
把文件放到该机器的远端目录。没有 `via` 就没有可靠的文件通道，显式报错，
不猜路径、不假装成功。
"""
import posixpath
import stat as statMod

from modules import federation_write, fsutil
from modules import ssh_hosts as hosts
from modules.ssh_sftp_base import _openSftp
from modules.task_inputs_base import DeliverError


def viaHost(via):
    """SSH 主机解析：先 id 后 name（与 agent_tunnel 同规则）。"""
    entry = hosts.findHost(via)
    if entry is not None:
        return entry
    wanted = str(via or '').strip().lower()
    for item in hosts.repo.all():
        if str(item.get('name') or '').strip().lower() == wanted:
            return item
    return None


class SshTarget:
    kind = 'ssh'

    def __init__(self, entry):
        name = entry.get('name') or ''
        via = str(entry.get('via') or '').strip()
        if not via:
            raise DeliverError(
                '执行点 %s 未配置 SSH 主机（config.agentNodes[].via），'
                '无法投递附件' % name)
        host = viaHost(via)
        if host is None:
            raise DeliverError('执行点 %s 的 SSH 主机不存在: %s' % (name, via))
        try:
            self.sftp = _openSftp(host['id'])
        except Exception as e:
            raise DeliverError('连接执行点 %s 的 SSH 主机失败: %s'
                               % (name, str(e)[:200]), 502)
        self.created = []      # [(path, isDir)] 本次落的条目（只记顶层）

    def join(self, *parts):
        return posixpath.join(*[str(p) for p in parts if str(p)])

    def isdir(self, path):
        try:
            return statMod.S_ISDIR(self.sftp.stat(path).st_mode)
        except IOError:
            return False

    def exists(self, path):
        try:
            self.sftp.stat(path)
            return True
        except IOError:
            return False

    def ensureDir(self, path):
        path = posixpath.normpath(path)
        if path in ('', '.', '/'):
            return False
        created = False
        acc = '/' if path.startswith('/') else ''
        for part in [p for p in path.split('/') if p]:
            acc = (acc.rstrip('/') + '/' + part) if acc else part
            if self.exists(acc):
                continue
            self.sftp.mkdir(acc)
            created = True
        return created

    def _unique(self, directory, name, isDir):
        return fsutil.uniquePath(directory, name, isDir=isDir, start=2, sep='',
                                 pathmod=posixpath, exists=self.exists)

    def mkdirUnique(self, name, directory, track=True):
        target = self._unique(directory, name, True)
        self.sftp.mkdir(target)
        if track:
            self.created.append((target, True))
        return target

    def _write(self, target, source):
        tmp = target + '.deskport-part'
        try:
            if isinstance(source, bytes):
                with self.sftp.open(tmp, 'wb') as f:
                    f.write(source)
            else:
                self.sftp.put(source, tmp)
            try:
                self.sftp.posix_rename(tmp, target)
            except IOError:
                self.sftp.rename(tmp, target)
        except BaseException:
            self._remove(tmp)
            raise

    def putFile(self, localPath, name, directory, track=False):
        target = self._unique(directory, name, False)
        self._write(target, localPath)
        if track:
            self.created.append((target, False))
        return target

    def putBytes(self, raw, name, directory, track=False):
        target = self._unique(directory, name, False)
        self._write(target, raw)
        if track:
            self.created.append((target, False))
        return target

    def _remove(self, path):
        try:
            self.sftp.remove(path)
        except IOError:
            pass

    def _rmtree(self, path):
        try:
            attrs = self.sftp.listdir_attr(path)
        except IOError:
            self._remove(path)
            return
        for attr in attrs:
            child = path.rstrip('/') + '/' + attr.filename
            if statMod.S_ISDIR(attr.st_mode):
                self._rmtree(child)
            else:
                self._remove(child)
        try:
            self.sftp.rmdir(path)
        except IOError:
            pass

    def cleanup(self, base, made):
        """只清本次投的部分：任务目录是我们建的就整棵删，否则删本次落的条目。"""
        if made.get('task'):
            self._rmtree(base)
            if made.get('inputs'):
                try:
                    self.sftp.rmdir(posixpath.dirname(base))
                except IOError:
                    pass
            return
        for path, isDir in reversed(self.created):
            if isDir:
                self._rmtree(path)
            else:
                self._remove(path)

    def maxMB(self):
        return federation_write.uploadLimitMB()

    def close(self):
        try:
            self.sftp.close()
        except Exception:
            pass
