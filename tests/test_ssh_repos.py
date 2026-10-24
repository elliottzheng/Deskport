# -*- coding: utf-8 -*-
"""SSH 主机仓储与连接注册表。"""
from modules.ssh_hosts import ConnectionRegistry, HostRepository


def test_host_repository_roundtrip(tmp_path):
    path = tmp_path / 'hosts.json'
    repo = HostRepository(str(path))
    repo.upsert({'id': 'h1', 'name': 'a', 'host': '127.0.0.1'})
    repo.save()

    repo2 = HostRepository(str(path))
    repo2.load()
    assert repo2.find('h1')['name'] == 'a'
    assert len(repo2.all()) == 1
    repo2.remove('h1')
    assert repo2.find('h1') is None


def test_connection_registry_put_and_drop():
    class Client:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    reg = ConnectionRegistry()
    c = Client()
    reg.put('h1', c)
    assert reg.get('h1') is c
    reg.drop('h1')
    assert reg.get('h1') is None
    assert c.closed is True
