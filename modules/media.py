# -*- coding: utf-8 -*-
"""图片缩略图共享服务：files 与 inbox 共用，不反向依赖功能门面。"""
import hashlib
import os

import config

THUMB_SIZE = 240
IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp'}


class MediaError(Exception):
    def __init__(self, message, status=500):
        super().__init__(message)
        self.status = status


def thumbPath(path):
    """校验图片并生成/返回缓存路径；错误用 MediaError(status) 表达。"""
    if not path or not os.path.isfile(path):
        raise MediaError('文件不存在', 404)
    if os.path.splitext(path)[1].lower() not in IMAGE_EXTS:
        raise MediaError('非图片文件', 415)
    cacheDir = os.path.join(config.stateDir(), 'thumbs')
    os.makedirs(cacheDir, exist_ok=True)
    keyHash = hashlib.sha1(os.path.abspath(path).encode('utf-8')).hexdigest()[:16]
    key = '%s.%d.%d.png' % (keyHash, int(os.path.getmtime(path)), THUMB_SIZE)
    cached = os.path.join(cacheDir, key)
    if os.path.isfile(cached):
        return cached
    try:
        from PIL import Image
        with Image.open(path) as im:
            im.thumbnail((THUMB_SIZE, THUMB_SIZE))
            im.convert('RGB').save(cached, 'PNG')
    except ImportError as e:
        raise MediaError('Pillow 未安装，无法生成缩略图: %s' % e, 500) from e
    except Exception as e:
        raise MediaError('缩略图生成失败: %s' % e, 500) from e
    return cached
