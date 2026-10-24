# -*- coding: utf-8 -*-
"""文本编码策略：读时探测、写时保留，GBK 文件不被写成 UTF-8。"""
import pytest

from modules import textio


def test_decode_utf8_and_gbk():
    assert textio.decodeSmart('中文'.encode('utf-8')) == ('中文', 'utf-8', False)
    assert textio.decodeSmart('中文'.encode('gbk')) == ('中文', 'gbk', False)


def test_decode_binary_is_lossy():
    raw = b'\x00\x01\x02\xff'
    text, encoding, lossy = textio.decodeSmart(raw)
    assert encoding == 'latin-1'
    assert lossy is True
    assert text.encode('latin-1') == raw


def test_encode_using_roundtrip():
    assert textio.encodeUsing('中文', 'gbk').decode('gbk') == '中文'
    assert textio.encodeUsing('中文', 'utf-8').decode('utf-8') == '中文'


def test_encode_rejects_unknown_encoding():
    with pytest.raises(ValueError):
        textio.encodeUsing('x', 'cp936')
    with pytest.raises(ValueError):
        textio.encodeUsing('😀', 'gbk')


@pytest.fixture()
def client():
    from app import app
    app.config.update(TESTING=True)
    with app.test_client() as c:
        with c.session_transaction() as sess:
            sess['authenticated'] = True
        yield c


def test_read_write_preserves_gbk(client, tmp_path):
    path = tmp_path / 'legacy.txt'
    path.write_bytes('第一行\n'.encode('gbk'))

    resp = client.post('/api/files/read', json={'path': str(path)})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['encoding'] == 'gbk'
    assert data['content'] == '第一行\n'

    resp = client.post('/api/files/write', json={
        'path': str(path), 'content': '第二行\n', 'encoding': 'gbk'})
    assert resp.status_code == 200
    assert path.read_bytes().decode('gbk') == '第二行\n'


def test_write_rejects_unsupported_encoding(client, tmp_path):
    path = tmp_path / 'a.txt'
    path.write_text('x', encoding='utf-8')
    resp = client.post('/api/files/write', json={
        'path': str(path), 'content': 'y', 'encoding': 'cp936'})
    assert resp.status_code == 400


def test_drive_roots_are_absolute(client):
    resp = client.get('/api/files', query_string={'path': '/'})
    assert resp.status_code == 200
    for entry in resp.get_json()['entries']:
        assert entry['path'].endswith(':\\')
