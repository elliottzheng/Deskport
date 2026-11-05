# -*- coding: utf-8 -*-
"""HostRepository：mtime 缓存语义与跨进程可见性。

背景: 主机 CRUD 走 web 进程, /api/ssh/ws 由 sessiond 进程处理, 两者是各自
的 HostRepository 实例。早期实现用一次性 loaded 标志, 导致 web 里新加的
主机对 sessiond 永远不可见 (要重启守护进程才好)。这里锁定 mtime 行为。
"""
import json

import pytest

from modules.ssh_hosts import HostRepository


@pytest.fixture()
def repo(tmp_path):
    return HostRepository(path=str(tmp_path / 'ssh_hosts.json'))


def _read(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def test_missing_file_reads_as_empty(repo):
    assert repo.all() == []
    assert repo.find('nope') is None


def test_upsert_find_remove_roundtrip(repo, tmp_path):
    repo.upsert({'id': 'a', 'name': 'A'})
    repo.save()
    assert [h['id'] for h in _read(repo.filePath())['hosts']] == ['a']
    assert repo.find('a')['name'] == 'A'

    repo.remove('a')
    repo.save()
    assert _read(repo.filePath())['hosts'] == []
    assert repo.find('a') is None


def test_external_write_is_visible(repo):
    """另一进程改文件后, 本实例下次读取必须看得到 (mtime 缓存而非一次性加载)。"""
    repo.upsert({'id': 'a', 'name': 'A'})
    repo.save()

    # 模拟另一个进程直接改文件
    with open(repo.filePath(), 'w', encoding='utf-8') as f:
        json.dump({'hosts': [{'id': 'a', 'name': 'A'}, {'id': 'b', 'name': 'B'}]}, f)

    assert repo.find('b') is not None, '外部新增的主机应可见'
    assert len(repo.all()) == 2


def test_corrupt_file_keeps_last_known(repo):
    """文件损坏时保留上次已知内容, 不清空 (与 federation 的 stale 策略一致)。"""
    repo.upsert({'id': 'a', 'name': 'A'})
    repo.save()
    with open(repo.filePath(), 'w', encoding='utf-8') as f:
        f.write('{ not json')
    assert repo.find('a') is not None


def test_save_records_mtime_so_no_redundant_read(repo):
    repo.upsert({'id': 'a', 'name': 'A'})
    repo.save()
    before = repo.mtime
    assert before is not None
    # 未改动文件时 load() 不应改变已记录的 mtime
    repo.load()
    assert repo.mtime == before


def test_concurrent_upsert_remove_keeps_file_valid(repo):
    import threading
    errors = []

    def worker(n):
        try:
            for i in range(20):
                repo.upsert({'id': 'w%d-%d' % (n, i), 'name': 'w'})
                repo.save()
                repo.remove('w%d-%d' % (n, i))
                repo.save()
        except Exception as e:      # pragma: no cover - 仅用于暴露竞态
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert _read(repo.filePath())['hosts'] == []
