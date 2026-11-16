# -*- coding: utf-8 -*-
"""文本共享助手：编解码策略 + 自由文本口径。

编解码：读时探测编码，写时保留原编码。文件管理与 SFTP 共用同一套规则，
避免"GBK 读进来、UTF-8 写回去"的乱码。latin-1 作为无损兜底（任何字节都
能解码），含 NUL 时标记为有损、前端只读。

自由文本口径 cleanText：strip + 按上限截断、非字符串返回 None——信封校验
与服务端补写列共用（原来 feedback_envelope / feedback_store 各写一份）。
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


def cleanText(value, limit=None):
    """自由文本口径（v1 同款）：非字符串返回 None；strip 后按上限截断。

    信封校验的自由文本字段与服务端补写/兜底的展示列（submitted_at /
    feedback_id / page_id）走这里——客户端爱传什么都不能让落库路径 500，
    超长也不能无上限整条落库（规格 §4.1）。
    """
    if not isinstance(value, str):
        return None
    value = value.strip()
    if limit is not None:
        value = value[:limit]
    return value
