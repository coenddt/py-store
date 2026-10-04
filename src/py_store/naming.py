"""Mongo 物理名翻译与结果回映射（设计 §6 / 03 执行文档 §4.5）

唯一翻译算法在 Rust core::naming（经 core-py 透出 ``translate_name``）；本模块**只消费**，
禁止自研归一：
  - 正向 ``_to_mongo(cmd)``：逻辑命令 → 物理命令（collection / 键 / 字段引用 → camelCase）
  - 反向 ``_to_logical(result, cmd)``：驱动返回文档 → 逻辑文档（按逐 schema 逆表）

三条不变量（I1–I3）：
  - 契约保留键（键名本身，如 localField/foreignField/as/let）不翻译；
  - ``$`` 前缀操作符、``_id`` 保留物理主键、``__``/``^__`` 内部合成名不翻译；
  - 关系名不翻译（``$lookup.as`` 保持逻辑关系名），其**值**为数据字段名时翻译。

回映射**不可**由 camelCase 反推（物理形式不可逆），必须以命令 collection 定位 schema，
用 ``physical`` 对每个逻辑键算出物理键构成 ``physical → logical`` 逆表；关系子文档按关系
目标 schema 的逆表递归。对齐 ``nodejs-store/src/naming.js``。
"""

from .core import native as _native
from .schema import get as _schema_get
from .schema import list as _schema_list

_MONGO = 'mongodb'


def is_reserved(name):
    """保留物理名（I3）：``_id``、``__``/``^__`` 前缀内部合成名不翻译"""
    return isinstance(name, str) and (
        name == '_id' or name.startswith('__') or name.startswith('^__'))


def physical(logical):
    """逻辑名 → Mongo 物理名（camelCase）；保留名原样（单点：core::naming.translate_name）"""
    if not isinstance(logical, str) or logical == '':
        return logical
    return logical if is_reserved(logical) else _native.translate_name(logical, _MONGO)


# ─── 正向：逻辑命令 → 物理命令 ───────────────────────────────

def _tr_key(key, rels):
    """键名 / 点号路径翻译（关系名与内部别名不翻译；段级处理）"""
    if not isinstance(key, str) or key == '':
        return key
    if key.startswith('$'):
        return key
    if is_reserved(key):
        return key
    if '.' in key:
        return '.'.join(_tr_seg(seg, rels) for seg in key.split('.'))
    return key if key in rels else physical(key)


def _tr_seg(seg, rels):
    if is_reserved(seg):
        return seg
    return seg if seg in rels else physical(seg)


def _tr_ref(value, rels):
    """``$fieldRef`` 字符串值翻译（``$$var`` 系统变量保留）"""
    if not isinstance(value, str) or not value.startswith('$') or value.startswith('$$'):
        return value
    return '$' + _tr_key(value[1:], rels)


def _walk(node, rels, transform_refs):
    """深度键翻译。

    ``transform_refs=True`` 时同时翻译 ``$fieldRef`` 字符串值（pipeline / ``$expr`` 语境）；
    普通文档（filter/doc/projection）保持值原样，避免误译字面量。
    """
    if isinstance(node, list):
        return [_walk(n, rels, transform_refs) for n in node]
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k == '$literal':
                out[k] = v
                continue
            if k == '$expr':
                out[k] = _walk(v, rels, True)
                continue
            if k == '$lookup' and isinstance(v, dict):
                out[k] = _tr_lookup(v, rels)
                continue
            out[_tr_key(k, rels)] = _walk(v, rels, transform_refs)
        return out
    return _tr_ref(node, rels) if transform_refs else node


def _tr_lookup(obj, rels):
    """``$lookup`` 阶段：``from``=集合名（译）、``as``=关系名（不译）、
    ``localField``/``foreignField``=关系字段（译）"""
    out = {}
    for k, v in obj.items():
        if k == 'from':
            out[k] = physical(v) if isinstance(v, str) else v
        elif k in ('localField', 'foreignField'):
            out[k] = physical(v) if isinstance(v, str) else v
        elif k == 'as':
            out[k] = v
        elif k == 'let':
            m = {}
            for vk, vv in (v or {}).items():
                m[vk] = _walk(vv, rels, True)
            out[k] = m
        else:
            out[k] = _walk(v, rels, True)
    return out


def _collect_rels(info, pipeline):
    """收集关系名：schema 声明的 relations + pipeline 内全部 ``$lookup.as``"""
    rels = set((info or {}).get('relations') or {})
    _collect_as(pipeline, rels)
    return rels


def _collect_as(node, rels):
    if isinstance(node, list):
        for n in node:
            _collect_as(n, rels)
        return
    if not isinstance(node, dict):
        return
    for k, v in node.items():
        if k == '$lookup' and isinstance(v, dict) and isinstance(v.get('as'), str):
            rels.add(v['as'])
        _collect_as(v, rels)


def _to_mongo(cmd):
    """逻辑命令 → 物理命令。只翻译**数据标识符**：collection、filter/projection/update/doc/docs
    的键、pipeline 的键与 ``$fieldRef`` 值、``$lookup.from``/``localField``/``foreignField`` 的值。
    ``database`` / ``options`` 等非数据标识符原样透传。"""
    if not isinstance(cmd, dict):
        return cmd
    info = _info_for_collection(cmd.get('collection'))
    rels = _collect_rels(info, cmd.get('pipeline'))
    out = dict(cmd)
    if isinstance(cmd.get('collection'), str):
        out['collection'] = physical(cmd['collection'])
    for k in ('filter', 'projection', 'update', 'doc', 'docs'):
        if cmd.get(k) is not None:
            out[k] = _walk(cmd[k], rels, False)
    if cmd.get('pipeline') is not None:
        out['pipeline'] = _walk(cmd['pipeline'], rels, True)
    return out


# ─── 反向：物理文档 → 逻辑文档（逐 schema 逆表） ──────────────

_INV_CACHE = {}


def clear_cache():
    """清空逆表缓存（schema 注册表重建时同步，避免同名异形 schema 命中陈旧逆表）"""
    _INV_CACHE.clear()


def _inverse_table(info):
    """逐 schema 逆表：physical → logical（fields ∪ computes ∪ timestamps ∪ ``_id``）"""
    name = info.get('name')
    cached = _INV_CACHE.get(name)
    if cached is not None:
        return cached
    t = {'_id': '_id', 'createdAt': 'createdAt', 'updatedAt': 'updatedAt'}
    for f in (info.get('fields') or {}):
        t[physical(f)] = f
    for c in (info.get('computes') or {}):
        t[physical(c)] = c
    _INV_CACHE[name] = t
    return t


def _info_for_collection(collection):
    """由镜像按 collection 定位 schema（定义零落点：collection 逻辑名）"""
    if not isinstance(collection, str):
        return None
    for name in _schema_list():
        try:
            s = _schema_get(name)
        except KeyError:
            continue
        if s and s.get('collection') == collection:
            return s
    return None


def _info_for_name(name):
    if not isinstance(name, str) or name == '':
        return None
    try:
        return _schema_get(name)
    except KeyError:
        return None


def _map_doc(value, info):
    """递归回映射文档：关系子文档按目标 schema 逆表递归，其余标量保持不变"""
    if isinstance(value, list):
        return [_map_doc(v, info) for v in value]
    if not isinstance(value, dict):
        return value
    inv = _inverse_table(info)
    relations = info.get('relations') or {}
    out = {}
    for k, v in value.items():
        if k in relations:
            target = _info_for_name((relations[k] or {}).get('model'))
            out[k] = _map_doc(v, target) if target else v
        elif k in inv:
            out[inv[k]] = v
        else:
            out[k] = v
    return out


def _to_logical(result, cmd):
    """物理结果 → 逻辑结果。按命令 collection 定位 schema；未注册（无法建逆表）时原样返回
    （绝不臆测 camelCase 反推）。"""
    info = _info_for_collection((cmd or {}).get('collection'))
    if not info:
        return result
    return _map_doc(result, info)
