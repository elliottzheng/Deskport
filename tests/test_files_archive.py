# -*- coding: utf-8 -*-
"""文件归档纯函数回归：流式 ZIP 的 sink 必须满足 zipfile 的写协议。"""
import pathlib
import shutil
import zipfile

from modules import files_archive as archive

BASE = pathlib.Path(__file__).resolve().parent.parent / '.run' / 'pytest_zip'


class CollectSink:
    """同步收集 ZIP 字节，验证 write 返回长度、flush/close 协议。"""

    def __init__(self):
        self.buf = bytearray()
        self.error = None

    def write(self, data):
        self.buf.extend(data)
        return len(data)

    def flush(self):
        pass

    def close(self):
        pass


def test_stream_zip_roundtrip():
    shutil.rmtree(BASE, ignore_errors=True)
    BASE.mkdir(parents=True, exist_ok=True)
    try:
        src = BASE / 'a.txt'
        src.write_text('hello', encoding='utf-8')
        sink = CollectSink()
        archive._buildZip(sink, [str(src)])
        assert sink.error is None
        data = bytes(sink.buf)
        assert data[:2] == b'PK'

        target = BASE / 'out.zip'
        target.write_bytes(data)
        with zipfile.ZipFile(target) as zf:
            assert zf.namelist() == ['a.txt']
            assert zf.read('a.txt') == b'hello'
    finally:
        shutil.rmtree(BASE, ignore_errors=True)

def test_streaming_zip_job(tmp_path):
    src = tmp_path / 'b.txt'
    src.write_text('world', encoding='utf-8')
    job = archive.StreamingZipJob([str(src)])
    job.start()
    data = b''.join(job.chunks())
    job.close()
    assert data[:2] == b'PK'
    target = tmp_path / 'job.zip'
    target.write_bytes(data)
    with zipfile.ZipFile(target) as zf:
        assert zf.read('b.txt') == b'world'
