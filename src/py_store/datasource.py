"""
数据源路由（多后端）

core 产出的 Command 只带 ``collection``（见 rust-store/core 的 Command 契约），
Host 依据 schema 镜像的 ``collection → datasource`` 绑定，把每条命令路由到对应连接：
  - Mongo 源：直接交原生驱动（``db[collection]``）
  - SQL 源（mysql / postgres / sqlite）：先经 core ``dialect_translate`` 翻译为
    SQL 语句序列，再交该连接的 ``exec`` 执行器

数据源名缺省为 ``default``；``init`` 传入单个 Mongo db 实例时自动归一为
``{default: db}``，保证既有单库调用零变更。对齐 ``nodejs-store/src/datasource.js``。
"""

from collections.abc import Mapping

from . import executors
from .schema import core as _core
from .schema import get as _get_schema
from .schema import list as _list_schemas

DEFAULT_SOURCE = 'default'

_connections = {}


def _normalize(connections):
    """归一化连接映射：单个 Mongo db 实例 → ``{default: db}``"""
    if connections is None:
        return {}
    if isinstance(connections, Mapping):
        return dict(connections)
    return {DEFAULT_SOURCE: connections}


def set_connections(connections):
    """设置数据源连接映射（Mongo 传 db 实例；SQL 传 ``{'kind', 'exec'}`` 描述符）"""
    global _connections
    _connections = _normalize(connections)


def get_connection(source):
    """取指定数据源连接（未配置即报错）"""
    conn = _connections.get(source)
    if conn is None:
        raise RuntimeError(
            f'数据源未配置: {source}（请检查 init(connections) 与 schema 的 datasource 绑定）')
    return conn


def source_of_schema(name):
    """schema 声明的数据源名（缺省 ``default``）"""
    return _get_schema(name).get('datasource') or DEFAULT_SOURCE


def source_of_collection(collection):
    """collection → 数据源名（schema 镜像反查；未命中回落 ``default``）"""
    for name in _list_schemas():
        if _get_schema(name).get('collection') == collection:
            return source_of_schema(name)
    return DEFAULT_SOURCE


def connection_of_schema(name):
    """某 schema 所属数据源的连接（供索引创建等 Host 侧直连场景）"""
    return get_connection(source_of_schema(name))


def route(collection):
    """Command.collection → ``(source, connection)``"""
    source = source_of_collection(collection)
    return source, get_connection(source)


def _kind_of(connection):
    """SQL 源以 ``{'kind', 'exec'}`` 描述符（Mapping）承载，Mongo 源为驱动实例

    注意：**不可**用 ``getattr(连接, 'kind')`` 判定 —— PyMongo 的 ``Database`` 实现了
    ``__getattr__`` 兜底，``db.kind`` 会返回一个名为 ``kind`` 的 Collection 而非报错，
    导致 Mongo 源被误判为 SQL 源。故只认 Mapping。
    """
    if isinstance(connection, Mapping):
        return connection.get('kind')
    return None


def is_sql(connection):
    """判定连接是否为 SQL 数据源描述符（Mongo 驱动实例一律视作 Mongo 源）"""
    return _kind_of(connection) is not None


def _exec_of(connection):
    if isinstance(connection, Mapping):
        return connection.get('exec')
    return None


async def exec_sql(source, connection, cmd):
    """
    SQL 路径：translate（core 纯逻辑）→ exec（连接执行器）→ 结果塑形

    执行器只做「绑定参数 + 执行 + restore_rows」，返回中立包络；此处依 command.kind
    塑形为 Mongo 驱动等价返回值（见 ``executors.shape_result``），使上层（crud/*）
    对 Mongo / SQL 两条路径无感。
    """
    exec_fn = _exec_of(connection)
    if not callable(exec_fn):
        raise RuntimeError(
            f'SQL 数据源 {source}({_kind_of(connection)}) 的执行器未接入（见执行文档 Phase 4）')
    plan = _core.dialect_translate(_kind_of(connection), cmd)
    # Host 兜底：core 标记了无法安全下推的组合（如 $lookup 子 $limit 每父 top-N）时，
    # 绝不执行「缺少该段」的 SQL（会静默返回错误结果），改为显式报错，由调用方降级重查。
    unsupported = plan.get('unsupported') or []
    if unsupported:
        codes = ', '.join(
            str(u.get('code')) if isinstance(u, Mapping) else str(u) for u in unsupported)
        raise RuntimeError(
            f"SQL 下推不支持（{_kind_of(connection)}）: {codes}；{' / '.join(plan.get('warnings') or [])}")
    out = await exec_fn(plan)
    return executors.shape_result(cmd, out)
