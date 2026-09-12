"""ID 供给（Host 随机源） —— 与 core `needs_new_id` 语义对齐

随机段使用 secrets 强随机源 8 位 base36（约 41 bit 熵）：random 仅 4 位
（36^4 ≈ 168 万组合），insert_many 同毫秒批量生成时碰撞概率不可忽略（CWE-338）。
"""

import secrets
import string
import time

from ..schema import get as _get_schema

_ID_CHARS = 'abcdefghijklmnopqrstuvwxyz0123456789'
_BASE36_DIGITS = string.digits + string.ascii_lowercase


def _to_base36(n):
    if n == 0:
        return '0'
    out = []
    while n:
        n, r = divmod(n, 36)
        out.append(_BASE36_DIGITS[r])
    return ''.join(reversed(out))


def _generate_id(schema):
    """按 schema.idPrefix 生成唯一 ID（时间戳36进制 + secrets 随机8位）"""
    ts = _to_base36(int(time.time() * 1000)).upper()
    rnd = ''.join(secrets.choice(_ID_CHARS).upper() for _ in range(8))
    return schema['idPrefix'] + ts + rnd


def _truthy(v):
    """对齐 core `is_truthy`（字符串仅判空，不 trim）"""
    if v is None or v is False:
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return len(v) > 0
    return True


def _new_id_pool(schema_name, data):
    """预生成 mutation 的 ID 池：按数据树逐节点判断是否需要新 _id

    （与 core `needs_new_id` 一致：无有效 _id 且 schema 配了 idPrefix），
    保证游标消费顺序与节点顺序对齐（父子 schema 前缀不同也能取对 ID）。
    """
    pool = []

    def walk(name, node):
        s = _get_schema(name)
        if not _truthy((node or {}).get('_id')) and s['idPrefix']:
            pool.append(_generate_id(s))
        for key, val in (node or {}).items():
            rel = (s.get('relations') or {}).get(key)
            if not rel or val is None:
                continue
            if isinstance(val, list):
                for child in val:
                    if child is not None:
                        walk(rel.get('model'), child)
            else:
                walk(rel.get('model'), val)

    walk(schema_name, data)
    return pool
