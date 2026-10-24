# -*- coding: utf-8 -*-
"""fsutil：路径包含判断与唯一命名。"""
import posixpath

from modules import fsutil


def test_is_under_handles_prefix_and_case(tmp_path):
    root = tmp_path / 'root'
    child = root / 'a.txt'
    sibling = tmp_path / 'root2' / 'a.txt'
    root.mkdir()
    child.write_text('x', encoding='utf-8')
    sibling.parent.mkdir()
    sibling.write_text('x', encoding='utf-8')
    assert fsutil.isUnder(str(child), str(root))
    assert fsutil.isUnder(str(root), str(root))
    assert not fsutil.isUnder(str(sibling), str(root))
    assert fsutil.isUnder(str(child).upper(), str(root))


def test_unique_path_local(tmp_path):
    first = tmp_path / 'a.txt'
    first.write_text('x', encoding='utf-8')
    assert fsutil.uniquePath(str(tmp_path), 'a.txt') == str(tmp_path / 'a (1).txt')
    (tmp_path / 'a (1).txt').write_text('y', encoding='utf-8')
    assert fsutil.uniquePath(str(tmp_path), 'a.txt') == str(tmp_path / 'a (2).txt')


def test_unique_path_dir_and_remote(tmp_path):
    (tmp_path / 'v1.2').mkdir()
    assert fsutil.uniquePath(str(tmp_path), 'v1.2', isDir=True, start=2) == \
        str(tmp_path / 'v1.2 (2)')
    existing = {'/home/u/a.txt'}
    got = fsutil.uniquePath('/home/u', 'a.txt', pathmod=posixpath,
                            exists=lambda p: p in existing)
    assert got == '/home/u/a (1).txt'
