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

import contextvars
import weakref
from collections.abc import Mapping

from . import executors
from .feedback import emit as _emit_feedback
from .schema import core as _core
from .schema import get as _get_schema

DEFAULT_SOURCE = 'default'

_connections: dict = {}

# 事务作用域的连接覆盖：source → 事务描述符（见 run_in_transaction）
_tx_override: contextvars.ContextVar = contextvars.ContextVar(
    'py_store_tx_override', default=None)

# 当前会话：优先于 _tx_override（会话内部自持事务连接）
_current_session: contextvars.ContextVar = contextvars.ContextVar(
    'py_store_current_session', default=None)

# 写命令 kind（与 crud 侧 Command.kind 一致）
WRITE_KINDS = frozenset({'insertOne', 'insertMany', 'updateMany', 'findOneAndUpdate', 'deleteMany'})

# SQL 后端 kind 白名单（Mongo 驱动实例 / Mongo 事务视图一律非 SQL）
SQL_KINDS = frozenset({'mysql', 'postgres', 'sqlite'})

_MISSING = object()
# Mongo 事务能力缓存：client -> True/False（探测失败不写缓存，下次重探）
_mongo_tx_cap = weakref.WeakKeyDictionary()


def is_write_cmd(cmd):
    """命令是否为写命令（跨源写 fail-closed 判定用）"""
    return (cmd or {}).get('kind') in WRITE_KINDS


class NonAtomicWriteError(RuntimeError):
    """会话内写入了多个数据源：跨源写无法原子（fail-closed，绝不静默提交半截）"""

    def __init__(self, sources):
        self.sources = sorted(sources)
        super().__init__(
            '会话内写入了多个数据源（%s）：跨源写无法原子（Phase 1 未提供分布式事务）；'
            '请拆分为多个会话，或改用单一数据源' % ', '.join(self.sources))


def current_session():
    """当前生效会话（无则 None）"""
    return _current_session.get()


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


def has_connection(source):
    """数据源是否已在当前连接映射中（供索引创建等初始化辅助动作软跳过未配置源）"""
    return _connections.get(source) is not None


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


def _mongo_client_of(connection):
    """Mongo 连接的 client：db 实例取 ``.client``；MongoClient 返回自身；其余 None"""
    if _is_mongo_client(connection):
        return connection
    return getattr(connection, 'client', None)


async def mongo_transactable(connection):
    """探测 Mongo 部署是否支持多文档事务（四态，绝不猜；结果按 client 缓存）

      - True : 支持（replica set 的 ``setName``，或 sharded 的 ``msg == 'isdbgrid'``）
      - False: 不支持（standalone）
      - None : 探测失败 / 无法探测（unknown；不写缓存，下次重探）

    经 ``admin`` 库的 ``hello`` 命令探测（只读、幂等、驱动无关）。
    """
    client = _mongo_client_of(connection)
    if client is None:
        return None
    cached = _mongo_tx_cap.get(client, _MISSING)
    if cached is not _MISSING:
        return cached
    try:
        hello = await client.admin.command('hello')
    except BaseException:
        return None
    cap = bool(hello.get('setName')) or hello.get('msg') == 'isdbgrid'
    _mongo_tx_cap[client] = cap
    return cap


def is_mongo_source(source):
    """数据源名是否绑定 Mongo 源（非 SQL 描述符即 Mongo 驱动实例 / Mongo 事务视图）"""
    return not is_sql(get_connection(source))


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
    """判定连接是否为 SQL 数据源描述符

    仅 ``kind ∈ SQL_KINDS`` 才算 SQL —— Mongo 驱动实例（``kind`` 为 None）与
    Mongo 事务视图（``kind='mongo'``）一律非 SQL。
    """
    return _kind_of(connection) in SQL_KINDS


def is_sql_source(source):
    """数据源名是否绑定 SQL 源"""
    return is_sql(get_connection(source))


def connection_for(source):
    """当前生效连接：事务作用域内返回覆盖描述符，否则返回全局映射的连接
    （``crud.exec._exec_on`` 经此取连接，使事务内所有命令落到专用连接）"""
    override = _tx_override.get()
    if override and source in override:
        return override[source]
    return get_connection(source)


async def resolve_connection(source, is_write=False):
    """会话感知的连接解析：会话内返回事务覆盖，否则返回全局连接

    ``crud.exec._exec_on`` 与 ``execute_raw`` 共用此入口，保证会话内命令
    （含跨多次调用的 CRUD 与原生 SQL）落到同一事务连接。
    """
    session = _current_session.get()
    if session is not None:
        override = await session.conn_for(source, is_write)
        if override is not None:
            return override
    return connection_for(source)


def _warn_savepoint_unavailable(source):
    """嵌套作用域无保存点原语 → 降级并入外层（允许降级，禁止静默）"""
    _emit_feedback({
        'type': 'nested_savepoint_unsupported',
        'code': 'nestedSavepointUnsupported',
        'layer': 'datasource',
        'message': ('数据源 %s 的事务句柄未提供保存点原语：嵌套作用域并入外层'
                    '（该层失败将回滚整个外层事务）' % source),
        'hint': ('为执行器 open_transaction 句柄补 '
                 'savepoint / release_savepoint / rollback_to_savepoint'),
        'source': source,
    })


def _warn_savepoint_failed(source, name, exc):
    """错误路径保存点回滚 / 释放失败：发反馈，绝不掩盖原始错误"""
    _emit_feedback({
        'type': 'savepoint_failed',
        'code': 'savepointFailed',
        'layer': 'datasource',
        'message': '保存点回滚/释放失败（%s，数据源 %s）：%s' % (name, source, exc),
        'hint': '检查该数据源连接与事务状态；该嵌套作用域可能未能独立回滚',
        'source': source,
        'savepoint': name,
    })


def _warn_mongo_unsupported(source, deployment):
    """Mongo 部署不支持事务 → 降级按原样执行（允许降级，禁止静默）

    ``deployment``: ``'standalone'``（探测为不支持）| ``'unknown'``（探测失败/无法探测）。
    """
    _emit_feedback({
        'type': 'mongo_transaction_unsupported',
        'code': 'mongoTransactionUnsupported',
        'layer': 'datasource',
        'deployment': deployment,
        'message': ('数据源 %s 的 Mongo 部署不支持多文档事务（%s）：'
                    '本次调用按原样执行（非原子）' % (source, deployment)),
        'hint': '将 MongoDB 部署为 replica set 或 sharded cluster 以启用 session 事务；standalone 无此能力',
        'source': source,
    })


def _warn_transaction_not_atomic(source, kind):
    """事务作用域所在 SQL 执行器未实现 ``with_transaction`` → 按原样执行（允许降级，禁止静默）

    与 ``session_not_atomic``（会话路径）对称：同一类降级在两条入口（``store.transaction``
    与 ``store.session``）都必须显式声明，不留静默口子。
    """
    _emit_feedback({
        'type': 'transaction_not_atomic',
        'code': 'transactionNotAtomic',
        'layer': 'datasource',
        'message': ('数据源 %s(%s) 未实现 with_transaction：'
                    '事务作用域内命令按原样执行（非原子）' % (source, kind)),
        'hint': '为该执行器实现 with_transaction，或将写命令收敛到已支持事务的数据源',
        'source': source,
        'kind': kind,
    })


async def _rollback_savepoint(tx, source, name):
    """回滚到保存点并释放；任一失败发 savepoint_failed（不掩盖原始错误）"""
    errors = []
    for op in ('rollback_to_savepoint', 'release_savepoint'):
        try:
            await tx[op](name)
        except BaseException as exc:
            errors.append(exc)
    if errors:
        _warn_savepoint_failed(source, name, errors[0])


async def _nested_savepoint_scope(source, outer, fn):
    """同源嵌套事务作用域：在已持有的事务连接上开保存点

      - 外层句柄无 ``savepoint`` 原语 → 降级并入外层（同一外层作用域只告警一次）；
      - 成功 ``RELEASE``；失败 ``ROLLBACK TO`` + ``RELEASE`` 后原样上抛（外层可继续）。
    """
    tx = outer.get('tx')
    open_sp = tx.get('savepoint') if isinstance(tx, Mapping) else None
    if not callable(open_sp):
        if not outer.get('sp_warned'):
            outer['sp_warned'] = True
            _warn_savepoint_unavailable(source)
        return await fn()
    depth = int(outer.get('sp_depth') or 0) + 1
    outer['sp_depth'] = depth
    name = 'sp_%d' % depth
    await open_sp(name)          # 创建失败直接上抛（保存点不存在，无需回滚）
    try:
        out = await fn()
    except BaseException:
        await _rollback_savepoint(tx, source, name)
        raise
    else:
        await tx['release_savepoint'](name)
        return out
    finally:
        outer['sp_depth'] = depth - 1


async def run_in_transaction(source, fn):
    """事务作用域：在单个源上以「同连接 + 同事务」执行 fn 内的全部命令

      - 会话内调用：并入会话（事务边界由会话统一管理），不另开事务；
      - SQL 源且执行器实现 with_transaction：包事务；同源嵌套开 SAVEPOINT sp_<n>；
      - SQL 源且执行器**未**实现 with_transaction：按原样执行并发 ``transaction_not_atomic``
        （降级不静默，与 ``store.session`` 的 ``session_not_atomic`` 对称）；
      - Mongo 源：探测可事务（replica set / sharded）→ 包 session 事务；standalone / unknown
        → 发 ``mongo_transaction_unsupported`` 并按原样执行（绝不静默假装已事务化）；
      - Mongo 无保存点原语：同源嵌套走既有 ``nested_savepoint_unsupported`` 降级声明；
      - 事务体抛错统一 rollback 后原样上抛。
    """
    session = _current_session.get()
    if session is not None:
        # 会话内：事务边界由会话统一管理；本层作为嵌套作用域开保存点（失败只回滚本层）
        return await session.nested_scope(fn)
    conn = get_connection(source)
    parent = _tx_override.get() or {}

    if isinstance(conn, Mapping):
        # ── SQL 分支（事务作用域）──
        with_tx = conn.get('with_transaction')
        if not callable(with_tx):
            # 降级不静默：与 store.session 的 session_not_atomic 对称，显式声明本事务作用域未生效
            _warn_transaction_not_atomic(source, conn.get('kind'))
            return await fn()
        if source in parent:
            # 同源嵌套事务：在已持有的事务连接上开保存点（内层失败只回滚本层，外层可继续）
            return await _nested_savepoint_scope(source, parent[source], fn)
        tx_desc = {'kind': conn['kind'], 'exec': None, 'tx': None}

        async def _body(exec_on_tx, tx=None):
            tx_desc['exec'] = exec_on_tx
            tx_desc['tx'] = tx
            return await fn()

        token = _tx_override.set({**parent, source: tx_desc})
        try:
            return await with_tx(_body)
        finally:
            _tx_override.reset(token)

    # ── Mongo 分支 ──
    if source in parent:
        # 同源嵌套：Mongo 无保存点 → 复用降级声明（内层失败将回滚整个外层事务）
        return await _nested_savepoint_scope(source, parent[source], fn)
    cap = await mongo_transactable(conn)
    if cap is not True:
        _warn_mongo_unsupported(source, 'standalone' if cap is False else 'unknown')
        return await fn()
    tx = await executors.mongo.open_transaction(conn)
    view = {'kind': 'mongo', 'conn': conn, 'session': tx['session'], 'tx': tx}
    token = _tx_override.set({**parent, source: view})
    try:
        out = await fn()
    except BaseException:
        await tx['rollback']()
        raise
    else:
        await tx['commit']()
        return out
    finally:
        await tx['release']()
        _tx_override.reset(token)


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


class RawSqlError(RuntimeError):
    """原生 SQL 入口的显式错误（非 SQL 源 / 执行器未接入）"""


async def execute_raw(source, sql, params=None, is_write=False):
    """
    在指定 SQL 源上执行原生 SQL（Host 层逃生口，绕开 core 的 dialect_translate）

      - 事务 / 会话作用域内经 ``resolve_connection`` 落到事务专用连接 → 支持 SELECT ... FOR UPDATE；
      - 占位符沿用各后端原生风格（mysql/sqlite 用 ``?``，postgres 用 ``$1..$n``）；
      - 仅支持 SQL 源；Mongo 源显式报错（绝不静默）；
      - ``is_write=False`` 视为读（取行）；``True`` 视为写（取影响行数）；
      - 返回 ``{'rows': list|None, 'affectedRows': int}``。
    """
    conn = await resolve_connection(source, is_write=bool(is_write))
    if not is_sql(conn):
        raise RawSqlError(
            f'数据源 {source} 不是 SQL 源（原生 SQL 入口仅支持 mysql/postgres/sqlite）')
    exec_fn = _exec_of(conn)
    if not callable(exec_fn):
        raise RawSqlError(
            f'SQL 数据源 {source}({_kind_of(conn)}) 的执行器未接入')
    stmt = {'text': sql, 'params': list(params or []), 'isWrite': bool(is_write)}
    out = await exec_fn({'stmts': [stmt]})
    return {'rows': out.get('rows'), 'affectedRows': int(out.get('affectedRows') or 0)}


class Session:
    """显式会话（工作单元）

    - 惰性开事务：命令真正落到某 SQL 源时才 checkout 并 BEGIN（空会话不占连接）；
    - Mongo 源按探测结果事务化；不可事务（standalone/unknown）→ 直通 +
      `mongo_transaction_unsupported` 声明（绝不静默）；
    - 跨源写 fail-closed：≥2 个源发生写命令 → 退出时全部 rollback 并抛
      NonAtomicWriteError；
    - 嵌套会话 / 会话内 transaction：作为嵌套作用域在已有事务上开保存点
      （内层失败只回滚本层；SAVEPOINT 名字形如 sp_<n>）。
    """

    def __init__(self):
        self._views = {}     # source -> {'kind','exec','tx'} | Mongo 视图 | None（None=直通）
        self._txs = {}       # source -> 显式事务句柄
        self._opened = []    # 开启顺序
        self._wrote = set()  # 发生过写命令的 source
        self._warned = set()
        self._outer = None
        self._token = None
        self._scopes = []        # 嵌套作用域栈（仅最外层会话持有）
        self._sp_seq = 0         # 保存点命名序号（sp_<n>）
        self._sp_warned = set()  # 无保存点原语的告警去重
        self._scope = None       # 本内层会话对应的作用域（嵌套时）

    # ---------- 生命周期 ----------

    async def __aenter__(self):
        parent = _current_session.get()
        if parent is not None:
            self._outer = parent                 # 嵌套：生命周期交外层
            self._scope = parent.push_scope()    # 内层作用域在已有事务上开保存点
            return self
        self._token = _current_session.set(self)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._outer is not None:
            # 嵌套：只收束本层（失败回滚到本层保存点），生命周期仍交外层
            await self._outer.pop_scope(self._scope, rollback=exc_type is not None)
            return False
        try:
            if exc_type is not None:
                await self._finalize(commit=False)
                return False
            if len(self._wrote) > 1:
                await self._finalize(commit=False)
                raise NonAtomicWriteError(self._wrote)
            await self._finalize(commit=True)
            return False
        finally:
            _current_session.reset(self._token)

    async def _finalize(self, commit):
        errors = []
        if commit:
            for source in self._opened:
                try:
                    await self._txs[source]['commit']()
                except BaseException as exc:          # 提交失败：其余全部回滚
                    errors.append((source, exc))
                    for other in self._opened:
                        if other == source:
                            continue
                        try:
                            await self._txs[other]['rollback']()
                        except BaseException as exc2:
                            errors.append((other, exc2))
                    break
        else:
            for source in reversed(self._opened):
                try:
                    await self._txs[source]['rollback']()
                except BaseException as exc:
                    errors.append((source, exc))
        for source in self._opened:
            try:
                await self._txs[source]['release']()
            except BaseException as exc:
                errors.append((source, exc))
        self._views.clear()
        self._txs.clear()
        self._opened.clear()
        for source, exc in errors:
            self._warn_finalize_failure(source, commit, exc)
        if errors:
            raise errors[0][1]

    # ---------- 连接解析（供 crud.exec / execute_raw 调用） ----------

    async def conn_for(self, source, is_write=False):
        """命令落到该源时解析连接：事务视图 / None（直通，用原始连接）

        嵌套作用域内首次**写**某源时，在该事务连接上开保存点（惰性，只读不开）。
        """
        if is_write:
            self._wrote.add(source)
        if source not in self._views:
            self._views[source] = await self._open_view(source)
        view = self._views[source]
        if is_write and self._scopes:
            await self._scope_savepoints(source, view)
        return view

    async def _open_view(self, source):
        """解析并缓存该源的事务视图；返回 ``{'kind','exec','tx'}`` /
        ``{'kind':'mongo','conn','session','tx'}`` / None（直通）"""
        override = _tx_override.get()
        if override and source in override:
            ov = override[source]
            if ov.get('tx') is not None:
                # 外层事务（run_in_transaction / 外层会话）已绑定该源 → 复用，不新开事务、不告警
                return ov
        connection = get_connection(source)
        if isinstance(connection, Mapping):
            if callable(connection.get('open_transaction')):
                tx = await connection['open_transaction']()
                self._txs[source] = tx
                self._opened.append(source)
                return {'kind': connection['kind'], 'exec': tx['exec'], 'tx': tx}
            self._warn_not_atomic(source, connection.get('kind'))
            return None
        # Mongo 源（裸驱动实例）
        cap = await mongo_transactable(connection)
        if cap is True:
            tx = await executors.mongo.open_transaction(connection)
            self._txs[source] = tx
            self._opened.append(source)
            return {'kind': 'mongo', 'conn': connection, 'session': tx['session'], 'tx': tx}
        self._warn_mongo(source, cap)
        return None

    # ---------- 嵌套作用域（嵌套会话 / 会话内 transaction） ----------

    def push_scope(self):
        """进入嵌套作用域（内层 session / 会话内 transaction）"""
        scope = {'savepoints': {}, 'txs': {}}
        self._scopes.append(scope)
        return scope

    async def pop_scope(self, scope, rollback):
        """退出嵌套作用域：按成败回滚到保存点或释放（失败发反馈，不掩盖原异常）"""
        try:
            for source in reversed(list(scope['savepoints'])):
                name = scope['savepoints'][source]
                if name is None:
                    continue
                tx = scope['txs'][source]
                try:
                    if rollback:
                        await tx['rollback_to_savepoint'](name)
                    await tx['release_savepoint'](name)
                except BaseException as exc:
                    _warn_savepoint_failed(source, name, exc)
        finally:
            # 按身份（is）移出本层，避免同内容的空作用域按值误删外层
            self._scopes[:] = [s for s in self._scopes if s is not scope]

    async def nested_scope(self, fn):
        """会话内以嵌套作用域执行 fn（保存点隔离；失败只回滚本层）"""
        scope = self.push_scope()
        try:
            out = await fn()
        except BaseException:
            await self.pop_scope(scope, rollback=True)
            raise
        await self.pop_scope(scope, rollback=False)
        return out

    async def _scope_savepoints(self, source, view):
        """嵌套作用域首次写到某源时开保存点（惰性；句柄无原语 → 降级 + 告警一次）"""
        tx = view.get('tx') if isinstance(view, Mapping) else None
        for scope in self._scopes:
            if source in scope['savepoints']:
                continue
            if tx is None or not callable(tx.get('savepoint')):
                self._warn_scope_no_savepoint(source)
                scope['savepoints'][source] = None
                continue
            self._sp_seq += 1
            name = 'sp_%d' % self._sp_seq
            await tx['savepoint'](name)     # 创建失败直接上抛（可见错误）
            scope['savepoints'][source] = name
            scope['txs'][source] = tx

    def _warn_scope_no_savepoint(self, source):
        if source in self._sp_warned:
            return
        self._sp_warned.add(source)
        _warn_savepoint_unavailable(source)

    # ---------- 告警（自动反馈：允许降级、禁止静默） ----------

    def _warn_not_atomic(self, source, kind):
        if source in self._warned:
            return
        self._warned.add(source)
        _emit_feedback({
            'type': 'session_not_atomic',
            'code': 'sessionNotAtomic',
            'layer': 'datasource',
            'message': ('数据源 %s(%s) 未实现 open_transaction：'
                        '会话内该源命令按原样执行（非原子）' % (source, kind)),
            'hint': '为该执行器实现 open_transaction，或将该源的写命令移出会话',
            'source': source,
            'kind': kind,
        })

    def _warn_mongo(self, source, cap):
        """Mongo 部署不支持事务 → 降级声明（同源只声明一次）

        ``cap``: False → standalone；None → unknown（探测失败/无法探测）。
        """
        if source in self._warned:
            return
        self._warned.add(source)
        _warn_mongo_unsupported(source, 'standalone' if cap is False else 'unknown')

    def _warn_finalize_failure(self, source, commit, exc):
        _emit_feedback({
            'type': 'session_finalize_failed',
            'code': 'sessionFinalizeFailed',
            'layer': 'datasource',
            'message': ('会话收尾失败（%s，数据源 %s）：%s'
                        % ('commit' if commit else 'rollback', source, exc)),
            'hint': '检查该数据源连接状态；rollback 失败可能意味着连接已失效',
            'source': source,
        })

    # ---------- 会话 API（与 Store 同名同形，委托 crud） ----------

    async def query(self, *a, **kw):
        from . import crud
        return await crud.query(*a, **kw)

    async def query_one(self, *a, **kw):
        from . import crud
        return await crud.query_one(*a, **kw)

    async def query_with_count(self, *a, **kw):
        from . import crud
        return await crud.query_with_count(*a, **kw)

    async def insert(self, *a, **kw):
        from . import crud
        return await crud.insert(*a, **kw)

    async def insert_many(self, *a, **kw):
        from . import crud
        return await crud.insert_many(*a, **kw)

    async def update(self, *a, **kw):
        from . import crud
        return await crud.update(*a, **kw)

    async def update_many(self, *a, **kw):
        from . import crud
        return await crud.update_many(*a, **kw)

    async def upsert(self, *a, **kw):
        from . import crud
        return await crud.upsert(*a, **kw)

    async def remove(self, *a, **kw):
        from . import crud
        return await crud.remove(*a, **kw)

    async def exists(self, *a, **kw):
        from . import crud
        return await crud.exists(*a, **kw)

    async def count(self, *a, **kw):
        from . import crud
        return await crud.count(*a, **kw)

    async def mutation(self, *a, **kw):
        from . import crud
        return await crud.mutation(*a, **kw)

    async def execute_raw(self, *a, **kw):
        return await execute_raw(*a, **kw)
