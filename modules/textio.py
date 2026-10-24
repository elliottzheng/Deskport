# -*- coding: utf-8 -*-
"""文本编解码策略：读时探测编码，写时保留原编码。

文件管理与 SFTP 共用同一套规则，避免"GBK 读进来、UTF-8 写回去"的乱码。
latin-1 作为无损兜底（任何字节都能解码），含 NUL 时标记为有损、前端只读。
"""
SUPPORTED_ENCODINGS = ('utf-8', 'gbk', 'latin-1')


def decodeSmart(raw):
    """返回 (text, encoding, lossy)。lossy 表示含 NUL，通常是二进制。"""
    lossy = b'\x00' in raw[:8192]
    for encoding in SUPPORTED_ENCODINGS:
        try:
            return raw.decode(encoding), encoding, lossy
        except UnicodeDecodeError:
            continue
    return raw.decode('utf-8', 'replace'), 'utf-8', True


def encodeUsing(text, encoding):
    """按指定编码写回；编码名非法或字符不可表示时抛 ValueError。"""
    encoding = (encoding or 'utf-8').lower()
    if encoding not in SUPPORTED_ENCODINGS:
        raise ValueError('不支持的编码: %s' % encoding)
    try:
        return text.encode(encoding)
    except UnicodeEncodeError as e:
        raise ValueError('文本包含 %s 无法表示的字符: %s' % (encoding, e)) from e
