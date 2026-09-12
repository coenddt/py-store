"""
命令执行（唯一 IO 边界） + 占位符替换 + core 调用包装

全部纯逻辑（GQL 解析、权限、命令规划、结果后处理）都在 Rust core；
本模块只做 Host 三件事里最底层的一件：把 core 产出的 Command JSON
路由到对应数据源连接并执行。不确定性输入由本层供给（now 时钟、newId 随机 ID）。

路由规则见 ``..datasource``：按命令的 ``collection`` 找 schema 绑定的数据源，
Mongo 走原生驱动，SQL 走 ``translate → exec``。对齐 ``nodejs-store/src/crud/exec.js``。
"""

import re
import time

from .. import datasource as _datasource
from ..executors.mongo import exec_mongo as _exec_mongo
from ..permission import PermissionError, get_context
from ..schema import get as _schema_get

_PHASE1_IDS = re.compile(r'^\{\{phase1\.ids\}\}$')
_STEP_PH = re.compile(r'^\{\{step\.(\d+)\._id\}\}$')

# core 权限类错误消息 → PermissionError（消息与 core 常量保持一致）
_PERMISSION_MSGS = frozenset(['无访问权限', '无写入权限', '无删除权限', '无批量写入权限'])


def set_db(db):
    """单库简写：等价于 ``set_connections({'default': db})``"""
    _datasource.set_connections({_datasource.DEFAULT_SOURCE: db})


def set_connections(connections):
    """设置数据源连接映射（见 ``..datasource.set_connections``）"""
    _datasource.set_connections(connections)


def _get_db(source=None):
    """取指定数据源连接（缺省 ``default``）"""
    return _datasource.get_connection(source or _datasource.DEFAULT_SOURCE)


def _now_for(schema_name):
    """按 schema 的 timestamps 单位产出当前时间戳（'s' → 秒，其余/未启用 → 毫秒）"""
    unit = _schema_get(schema_name).get('timestampUnit')
    return int(time.time()) if unit == 's' else int(time.time() * 1000)


def _ctx():
    return get_context()


def _call(fn):
    """绑定层调用包装：权限类错误映射为 PermissionError"""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        if str(e) in _PERMISSION_MSGS:
            raise PermissionError(str(e)) from e
        raise


# ─── 命令执行（唯一 IO 边界） ────────────────────────────────

async def _exec_on(source, cmd):
    """在指定数据源上执行命令（Mongo 走原生驱动，SQL 走 translate → exec）"""
    connection = _datasource.get_connection(source)
    if _datasource.is_sql(connection):
        return await _datasource.exec_sql(source, connection, cmd)
    return await _exec_mongo(connection, cmd)


async def _exec(cmd):
    """Command JSON → 按 collection 绑定路由到 Mongo 原生 / SQL 翻译执行"""
    return await _exec_on(_datasource.source_of_collection(cmd['collection']), cmd)


def _substitute(value, resolver):
    """深度替换命令中的占位符（命中 resolver 返回非字符串时替换）"""
    if isinstance(value, str):
        return resolver(value)
    if isinstance(value, list):
        return [_substitute(v, resolver) for v in value]
    if isinstance(value, dict):
        return {k: _substitute(v, resolver) for k, v in value.items()}
    return value


def resolve_placeholders(command, ids=None, steps=None):
    """Host 契约：把命令中的占位符替换为执行结果

      - ``{{phase1.ids}}``   → 两阶段查询第一步取回的 id 数组（整值替换）
      - ``{{step.<N>._id}}`` → mutation 第 N 步执行结果的 _id

    未命中的占位符原样保留（便于定位 core 与 Host 的契约漂移）。
    Node 侧 ``nodejs-store/src/crud.js`` 的 ``resolvePlaceholders`` 为同语义实现，
    两侧共测 ``rust-store/fixtures/host/placeholders.json``。
    """
    steps = steps or []

    def resolver(s):
        if _PHASE1_IDS.match(s):
            return ids if ids is not None else s
        m = _STEP_PH.match(s)
        if m:
            idx = int(m.group(1))
            if idx < len(steps):
                return steps[idx]
        return s

    return _substitute(command, resolver)
