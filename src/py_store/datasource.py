"""
数据源路由（多后端）

core 产出的 Command 携带 ``source`` / ``namespace`` / ``collection`` 三元组
（见 rust-store/core 的 Command 契约），Host 只按 ``source`` 选连接、按 ``namespace``
定位连接内的库/schema：
  - Mongo 源：直接交原生驱动（``db[collection]``）
  - SQL 源（mysql / postgres / sqlite）：先经 core ``dialect_translate`` 翻译为
    SQL 语句序列，再交该连接的 ``exec`` 执行器

Mongo 连接支持两种形态（绝不猜，按命令的 namespace 严格校验）：
  - db 实例（PyMongo Database）：命令 ``namespace`` 必须为 None（db 实例无法跨库，
    非 None 显式报错）
  - MongoClient：命令 ``namespace`` 必须非 None（db 名）→ ``client.get_database(ns)``

数据源名缺省为 ``default``；``init`` 传入单个 Mongo db 实例/MongoClient 时自动归一为
``{default: 连接}``，保证既有单库调用零变更。对齐 ``nodejs-store/src/datasource.js``。
"""

from collections.abc import Mapping

from . import executors
from .feedback import emit as _emit_feedback
from .schema import core as _core
from .schema import get as _get_schema

DEFAULT_SOURCE = 'default'

_connections = {}


class PushdownUnsupportedError(RuntimeError):
    """SQL 下推遇到无法安全翻译的组合（core 标记 unsupported）

    显式报错而非静默执行「缺少该段」的 SQL（会返回错误结果）；
    自动反馈：触发原因见 message，修复指引见 feedback()。
    """

    def __init__(self, source, kind, codes, warnings):
        msg = f"SQL 下推不支持（{kind}）: {', '.join(codes)}；{' / '.join(warnings)}"
        super().__init__(msg)
        self.source = source
        self.kind = kind
        self.codes = codes
        self.warnings = warnings

    def feedback(self):
        """转统一反馈事件（与 feedback.emit 的事件形状一致）"""
        return {
            'type': 'sql_pushdown_unsupported',
            'code': 'pushdownUnsupported',
            'layer': 'dialect',
            'message': str(self),
            'hint': '改写查询避开该组合，或改用 Mongo 源执行该段取数',
            'source': self.source,
            'kind': self.kind,
        }


def _is_mongo_client(x):
    """PyMongo MongoClient 判别：有 ``get_database``（Database 只有 ``get_collection``）"""
    return hasattr(x, 'get_database') and not hasattr(x, 'get_collection')


def _normalize(connections):
    """归一化连接映射：单个 Mongo db 实例 / MongoClient → ``{default: 连接}``"""
    if connections is None:
        return {}
    if isinstance(connections, Mapping):
        return dict(connections)
    return {DEFAULT_SOURCE: connections}


def set_connections(connections):
    """设置数据源连接映射（Mongo 传 db 实例或 MongoClient；SQL 传 ``{'kind', 'exec'}`` 描述符）"""
    global _connections
    _connections = _normalize(connections)


def get_connection(source):
    """取指定数据源连接（未配置即报错）"""
    conn = _connections.get(source)
    if conn is None:
        raise RuntimeError(
            f'数据源未配置: {source}（请检查 init(connections) 与 schema 的 datasource 绑定）')
    return conn


def mongo_db(connection, source, namespace):
    """
    Mongo 源：按命令的 ``namespace`` 解析目标 db（两种形态，绝不猜）

      - db 实例（非 SQL 描述符的驱动实例，含鸭子类型 db）：namespace 必须为 None，
        非 None 显式报错；
      - MongoClient：namespace 必须非 None，返回 ``client.get_database(namespace)``；
      - 非 Mongo（SQL 描述符 Mapping）返回 None，由调用方走 SQL 路径。
    """
    if is_sql(connection):
        return None
    if _is_mongo_client(connection):
        if not namespace:
            raise RuntimeError(
                f'数据源 {source} 是 MongoClient，命令缺少 namespace'
                '（MongoClient 形态必须在 schema 声明 namespace 即 db 名）')
        return connection.get_database(namespace)
    if namespace:
        raise RuntimeError(
            f'数据源 {source} 是 Mongo db 实例，命令携带了 namespace="{namespace}"'
            '（db 实例不支持跨库；跨库请改传 MongoClient 并用 schema.namespace 声明库名）')
    return connection


def source_of_schema(name):
    """schema 声明的数据源名（缺省 ``default``）"""
    return _get_schema(name).get('datasource') or DEFAULT_SOURCE


def connection_of_schema(name):
    """某 schema 所属数据源的连接（供 Host 侧直连场景）"""
    return get_connection(source_of_schema(name))


def db_of_schema(name):
    """某 schema 的 Mongo db 句柄（按镜像的 datasource + namespace 解析；SQL 源返回 None）"""
    s = _get_schema(name)
    source = s.get('datasource') or DEFAULT_SOURCE
    return mongo_db(get_connection(source), source, s.get('namespace') or None)


def route(cmd):
    """Command.source → ``(source, connection)``（三元组中的 source 精确路由）"""
    source = cmd.get('source') or DEFAULT_SOURCE
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
    plan = _core.dialect_translate(_kind_of(connection), cmd)
    # Host 兜底：core 标记了无法安全下推的组合（如 $lookup 子 $limit 每父 top-N）时，
    # 绝不执行「缺少该段」的 SQL（会静默返回错误结果），改为显式报错，由调用方降级重查。
    # 先于执行器检查 —— 命令本身不可安全下推时，报下推不支持而非「执行器未接入」。
    unsupported = plan.get('unsupported') or []
    if unsupported:
        err = PushdownUnsupportedError(
            source,
            _kind_of(connection),
            [str(u.get('code')) if isinstance(u, Mapping) else str(u) for u in unsupported],
            [str(w) for w in (plan.get('warnings') or [])],
        )
        # 自动反馈：拦截即告警（无 sink 时打 stderr），禁止静默失守
        _emit_feedback(err.feedback())
        raise err
    exec_fn = _exec_of(connection)
    if not callable(exec_fn):
        raise RuntimeError(
            f'SQL 数据源 {source}({_kind_of(connection)}) 的执行器未接入（见执行文档 Phase 4）')
    out = await exec_fn(plan)
    return executors.shape_result(cmd, out)
