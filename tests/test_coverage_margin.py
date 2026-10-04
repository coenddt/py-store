"""覆盖率余量：补真实分支断言（执行文档 §5 步骤 7）

优先缺口模块（本机 term-missing 实测）：executors/__init__.py、executors/sqlite.py、
introspect/sqlite.py、ddl.py（去重告警）、crud/exec.py。
全部零外部依赖（sqlite 真实驱动），不依赖 MySQL / PG / Mongo 服务。

运行：PYTHONPATH=src python -m pytest tests/test_coverage_margin.py -q
"""

import asyncio

import aiosqlite
import pytest

from py_store import core as core_mod
from py_store import datasource, ddl, feedback, introspect, permission, store, sync
from py_store import schema as _sc
from py_store.crud import exec as crud_exec
from py_store.crud import id as crud_id
from py_store.executors import _scalar, create_connection, shape_result
from py_store.executors import mongo as mongo_exec
from py_store.executors import mysql as mysql_exec
from py_store.executors import postgres as pg_exec
from py_store.executors import sqlite as sqlite_exec
from py_store.introspect import postgres as introspect_pg
from py_store.introspect import sqlite as introspect_sqlite


@pytest.fixture(autouse=True)
def _isolate():
    feedback.set_sink(None)
    yield
    datasource.set_connections({})
    feedback.set_sink(None)


def _run(coro):
    return asyncio.run(coro)


# ─── executors/__init__.py ───────────────────────────────────

def test_create_connection_unknown_backend():
    with pytest.raises(ValueError, match='未知 SQL 后端'):
        create_connection('oracle', None)


def test_scalar_variants():
    assert _scalar([]) == 0
    assert _scalar([{'c': '42'}]) == 42
    assert _scalar([{'c': 'nope'}]) == 'nope'
    assert _scalar([('raw',)]) == ('raw',)


def test_shape_result_unknown_kind_passthrough():
    env = {'docs': [1], 'rows': [[2]], 'affectedRows': 0}
    assert shape_result({'kind': 'weird'}, env) is env


# ─── executors/sqlite.py ─────────────────────────────────────

def test_sqlite_create_requires_connection():
    with pytest.raises(TypeError, match='aiosqlite'):
        sqlite_exec.create(None)


def test_sqlite_with_transaction_rolls_back(tmp_path):
    db_path = tmp_path / 'tx.db'

    async def scenario():
        db = await aiosqlite.connect(db_path)
        await db.execute('CREATE TABLE t (_id TEXT PRIMARY KEY)')
        await db.commit()
        desc = create_connection('sqlite', db)

        async def body(exec_on_tx, _tx=None):
            await exec_on_tx({'stmts': [{'text': "INSERT INTO t (_id) VALUES ('y')"}]})
            raise RuntimeError('boom')

        with pytest.raises(RuntimeError, match='boom'):
            await desc['with_transaction'](body)
        await db.close()

        db2 = await aiosqlite.connect(db_path)
        try:
            cur = await db2.execute('SELECT COUNT(*) FROM t')
            n = (await cur.fetchone())[0]
            await cur.close()
        finally:
            await db2.close()
        return n

    assert _run(scenario()) == 0


# ─── introspect/sqlite.py ────────────────────────────────────

def test_introspect_sqlite_requires_connection():
    with pytest.raises(TypeError, match='aiosqlite'):
        _run(introspect_sqlite.introspect(None))


def test_introspect_sqlite_missing_attached_db(tmp_path):
    db_path = tmp_path / 'intro.db'

    async def scenario():
        db = await aiosqlite.connect(db_path)
        try:
            with pytest.raises(RuntimeError, match='attached db 不存在'):
                await introspect_sqlite.introspect(db, {'database': 'nope'})
        finally:
            await db.close()

    _run(scenario())


def test_introspect_sqlite_collects_indexes(tmp_path):
    db_path = tmp_path / 'idx.db'

    async def scenario():
        db = await aiosqlite.connect(db_path)
        try:
            await db.execute('CREATE TABLE t (_id TEXT PRIMARY KEY, v TEXT)')
            await db.execute('CREATE INDEX idx_t_v ON t(v)')
            await db.commit()
            out = await introspect_sqlite.introspect(db)
        finally:
            await db.close()
        return out

    out = _run(scenario())
    names = [i['name'] for i in out['indexes']]
    assert 'idx_t_v' in names


# ─── ddl.py 表名去重告警 ─────────────────────────────────────

def test_ddl_generate_dedupe_emits_feedback():
    events = []
    feedback.set_sink(events.append)
    store.register({
        'name': 'CovX', 'collection': 'cov_x', 'idPrefix': 'cx',
        'fields': {'_id': {'type': 'string'}},
    })
    sql = ddl.generate('sqlite', ['CovX', 'CovX'])
    assert sql.count('CREATE TABLE "cov_x"') == 1
    assert any(e.get('code') == 'ddlDuplicateTable' for e in events), events


# ─── crud/exec.py 连接薄包装 ─────────────────────────────────

def test_crud_exec_connection_wrappers():
    crud_exec.set_connections({'default': 'sentinel'})
    assert crud_exec._get_db() == 'sentinel'
    assert crud_exec._get_db('default') == 'sentinel'


# ─── 原生核心加载器（生产禁从相邻仓库加载） ───────────────────

def test_core_dev_fallback_requires_flag_and_non_production(monkeypatch):
    monkeypatch.delenv('LOCAL_CORE', raising=False)
    monkeypatch.delenv('NODE_ENV', raising=False)
    assert core_mod._dev_fallback() is None, '未设 LOCAL_CORE 时禁从相邻仓库加载'

    monkeypatch.setenv('LOCAL_CORE', '1')
    monkeypatch.setenv('NODE_ENV', 'production')
    assert core_mod._dev_fallback() is None, 'NODE_ENV=production 时同样禁用'

    monkeypatch.delenv('NODE_ENV', raising=False)
    fallback = core_mod._dev_fallback()
    assert fallback is not None and not isinstance(fallback, dict), \
        'LOCAL_CORE=1 且非 production 时应从相邻 dist 加载'


def test_core_load_error_carries_actionable_hint(monkeypatch):
    class _Boom:
        @staticmethod
        def import_module(_name):
            raise ImportError('模拟原生模块缺失')

    monkeypatch.setattr(core_mod, 'importlib', _Boom)

    monkeypatch.delenv('LOCAL_CORE', raising=False)
    with pytest.raises(ImportError, match='无法加载 rust-store 原生核心'):
        core_mod._load()

    # LOCAL_CORE=1 时兜底自身失败 → 错误信息须带上兜底原因（不吞错）
    monkeypatch.setenv('LOCAL_CORE', '1')
    with pytest.raises(ImportError, match='无法加载 rust-store 原生核心'):
        core_mod._load()


# ─── 数据源路由守卫（未配置 / 执行器未接入） ─────────────────

def test_datasource_guards():
    datasource.set_connections(None)
    assert datasource._connections == {}, 'None 归一为空映射'
    with pytest.raises(RuntimeError, match='数据源未配置'):
        datasource.get_connection('nope')
    assert datasource._exec_of('not-a-mapping') is None

    _sc.register({
        'name': 'CovSrc', 'collection': 'cov_src', 'datasource': 'cov_src_a',
        'timestamps': False, 'fields': {'v': 'string'}, 'relations': {},
    })
    datasource.set_connections({'cov_src_a': 'sentinel', 'default': 'dflt'})
    assert datasource.connection_of_schema('CovSrc') == 'sentinel'
    assert datasource.route({'source': 'cov_src_a'}) == ('cov_src_a', 'sentinel')
    assert datasource.route({}) == ('default', 'dflt'), '命令未带 source 时回落 default'

    # SQL 源描述符缺 exec → 显式报错（不得静默执行空结果）
    with pytest.raises(RuntimeError, match='执行器未接入'):
        _run(datasource.exec_sql(
            'cov_pg', {'kind': 'postgres'},
            {'source': 'cov_src_a', 'collection': 'cov_src', 'kind': 'find',
             'filter': {}, 'projection': None}))


# ─── introspection / 执行器守卫与纯函数 ──────────────────────

def test_introspect_unknown_backend():
    with pytest.raises(ValueError, match='未知 introspection 后端'):
        _run(introspect.run('oracle', None))


def test_postgres_executor_and_introspect_helpers():
    assert pg_exec._affected(None) == 0
    assert pg_exec._affected('UPDATE 3') == 3
    assert pg_exec._affected('UPDATE n/a') == 0
    with pytest.raises(TypeError, match='asyncpg'):
        pg_exec.create(None)
    with pytest.raises(TypeError, match='asyncpg'):
        _run(introspect_pg.introspect(None))

    merged = introspect_pg._group_indexes([
        {'table': 't', 'name': 'ix', 'unique': 1, 'column': 'a'},
        {'table': 't', 'name': 'ix', 'unique': 1, 'column': 'b'},
        {'table': 't', 'name': 'iy', 'unique': 0, 'column': 'c'},
    ])
    assert merged == [
        {'table': 't', 'name': 'ix', 'columns': ['a', 'b'], 'unique': 1},
        {'table': 't', 'name': 'iy', 'columns': ['c'], 'unique': 0},
    ], merged


def test_mysql_executor_guards_and_single_connection_acquire():
    with pytest.raises(TypeError, match='asyncmy'):
        mysql_exec.create(None)

    async def scenario():
        conn = object()
        async with mysql_exec.acquire(conn) as got:
            return got is conn

    assert _run(scenario()) is True, '单连接（无 acquire）应直接复用'


def test_mysql_executor_acquire_coroutine_variant():
    """少数 asyncmy 版本 ``acquire()`` 返回协程（非异步上下文管理器）→ await 取连接并交回池"""

    class _Pool:
        def __init__(self):
            self.conn = object()
            self.released = []

        def acquire(self):
            async def _c():
                return self.conn
            return _c()

        async def release(self, conn):
            self.released.append(conn)

    pool = _Pool()

    async def scenario():
        async with mysql_exec.acquire(pool) as got:
            return got is pool.conn

    assert _run(scenario()) is True
    assert pool.released == [pool.conn], '协程形态取到的连接须交回池'


def test_mysql_introspect_requires_driver():
    from py_store.introspect import mysql as introspect_mysql

    with pytest.raises(TypeError, match='asyncmy'):
        _run(introspect_mysql.introspect(None))


def test_mongo_executor_unknown_kind_raises():
    with pytest.raises(RuntimeError, match='未支持的命令'):
        _run(mongo_exec.exec_mongo({'cov_src': None}, {'collection': 'cov_src', 'kind': 'weird'}))


# ─── DDL 生成边界（字符串简写类型 / 缺 _id） ─────────────────

def test_ddl_columns_string_shorthand_and_missing_id():
    cols = ddl._columns(
        {'name': 'CovShorthand', 'fields': {'_id': 'string', 'v': 'int'}, 'timestamps': False},
        'sqlite')
    assert cols[0][:2] == ('_id', 'TEXT') and cols[0][2] is True
    assert cols[1][:2] == ('v', 'INTEGER'), '字符串简写类型应被识别'

    with pytest.raises(ValueError, match='缺少 _id'):
        ddl._columns({'name': 'CovNoId', 'fields': {'v': {'type': 'int'}}}, 'sqlite')


# ─── 权限包装 + ID 供给 ──────────────────────────────────────

def test_permission_wrappers_and_internal_sync_return():
    _sc.register({
        'name': 'CovOwner', 'collection': 'cov_owner', 'timestamps': False,
        'fields': {'a': 'string'},
        'relations': {
            'kids': {'model': 'CovOwner', 'type': 'many', 'localField': '_id',
                     'foreignField': 'aId'},
        },
        'read': ['creator'],
    })
    assert permission.should_inject_owner_condition(
        'CovOwner', {'userId': 'u1', 'roles': ['user']}) is True
    # 清单化语义（设计 §11.5）：admin 不再默认豁免——creator-only 下同样注入 owner 条件；
    # 显式豁免后恢复「不注入」
    assert permission.should_inject_owner_condition(
        'CovOwner', {'userId': 'u1', 'roles': ['admin']}) is True
    permission.set_exempt_roles(['admin'])
    assert permission.should_inject_owner_condition(
        'CovOwner', {'userId': 'u1', 'roles': ['admin']}) is False
    permission.set_exempt_roles([])
    assert permission.get_readable_relations('CovOwner', None) is None, '无上下文不裁剪'
    assert _run(permission.run_as_internal(lambda: 42)) == 42, '同步返回值须原样返回'


def test_crud_id_helpers_and_one_relation_walk():
    assert crud_id._to_base36(0) == '0'
    assert crud_id._to_base36(36) == '10'
    assert crud_id._truthy(True) is True
    assert crud_id._truthy('') is False

    _sc.register({
        'name': 'CovPoolParent', 'collection': 'cov_pool_parent', 'timestamps': False,
        'fields': {'a': 'string'},
        'relations': {
            'child': {'model': 'CovPoolChild', 'type': 'one', 'localField': '_id',
                      'foreignField': 'parentId'},
        },
    })
    _sc.register({
        'name': 'CovPoolChild', 'collection': 'cov_pool_child', 'idPrefix': 'CPC',
        'timestamps': False, 'fields': {'parentId': 'string'}, 'relations': {},
    })
    pool = crud_id._new_id_pool('CovPoolParent', {'_id': 'p1', 'child': {'parentId': 'p1'}})
    assert len(pool) == 1, pool
    assert pool[0].startswith('CPC'), 'one 关系子节点缺 _id 时也须入池'


# ─── schema 同步编排（overlay / datasource / register_defs） ──

def test_sync_schema_overlay_and_register_flag(monkeypatch):
    async def _fake_run(_backend, _driver, _options=None):
        return {
            'tables': [{'name': 'cov_sync'}],
            'columns': [{'table': 'cov_sync', 'name': '_id', 'type': 'TEXT',
                         'notnull': 1, 'pk': 1}],
            'fks': [],
            'indexes': [],
        }

    monkeypatch.setattr(sync.introspect, 'run', _fake_run)
    overlay = [{'name': 'cov_sync', 'fields': {'ov': {'type': 'int'}}}]

    defs = _run(sync.sync_schema(
        'sqlite', None, overlay=overlay, datasource='cov_ds', database='cov_db',
        register_defs=False))
    assert defs[0]['datasource'] == 'cov_ds' and defs[0]['database'] == 'cov_db'
    assert 'ov' in defs[0]['fields'], 'overlay 字段应合入'
    assert _sc.has('cov_sync') is False, 'register_defs=False 时不得注册'

    _run(sync.sync_schema('sqlite', None, register_defs=True))
    assert _sc.has('cov_sync') is True, 'register_defs=True 时应注册进 core'


# ─── SQL 执行器：事务提交/回滚 + 单连接事务路径 ───────────────

class _MyCursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = None
        self.rowcount = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, _sql, _params=None):
        self.conn.exec_count += 1
        self.conn.executed.append(_sql)
        if self.conn.fail and self.conn.exec_count > self.conn.fail_after:
            raise RuntimeError('模拟语句失败')

    async def fetchall(self):
        return []


class _MyConn:
    def __init__(self, fail=False, fail_after=0):
        self.fail = fail
        # 第 fail_after+1 次 execute 起失败（with_transaction 的 BEGIN 占第 1 次）
        self.fail_after = fail_after
        self.exec_count = 0
        self.executed = []
        self.committed = 0
        self.rolled = 0

    def cursor(self, _cls=None):
        return _MyCursor(self)

    async def commit(self):
        self.committed += 1

    async def rollback(self):
        self.rolled += 1


_WRITE_PLAN = {'stmts': [{'text': 'UPDATE t SET v = ?', 'params': [1], 'isWrite': True}]}


def test_mysql_executor_commit_and_rollback():
    conn = _MyConn()
    desc = mysql_exec.create(conn)
    out = _run(desc['exec'](_WRITE_PLAN))
    assert out['affectedRows'] == 0 and conn.committed == 1, '非事务路径成功后须显式 commit'

    bad = _MyConn(fail=True)
    with pytest.raises(RuntimeError, match='模拟语句失败'):
        _run(mysql_exec.create(bad)['exec'](_WRITE_PLAN))
    assert bad.rolled == 1 and bad.committed == 0, '语句失败须 rollback 后上抛'


def test_mysql_executor_transaction_commit_and_rollback():
    async def body(exec_on_tx, _tx=None):
        return await exec_on_tx(_WRITE_PLAN)

    conn = _MyConn()
    out = _run(mysql_exec.create(conn)['with_transaction'](body))
    assert out['affectedRows'] == 0 and conn.committed == 1

    bad = _MyConn(fail=True, fail_after=1)
    with pytest.raises(RuntimeError, match='模拟语句失败'):
        _run(mysql_exec.create(bad)['with_transaction'](body))
    assert bad.rolled == 1 and bad.committed == 0, '事务体失败须整体回滚'


class _PgTx:
    """asyncpg ``Transaction`` 桩：显式 start/commit/rollback（另保留上下文管理器形态）"""

    def __init__(self):
        self.started = 0
        self.committed = 0
        self.rolled = 0

    async def start(self):
        self.started += 1
        return self

    async def commit(self):
        self.committed += 1

    async def rollback(self):
        self.rolled += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _PgConn:
    """单连接（无 acquire）形态的 asyncpg 桩：走 driver.transaction() 分支"""

    def __init__(self):
        self.executed = []

    def transaction(self):
        return _PgTx()

    async def fetch(self, _sql, *_params):
        return [{'v': 1}]

    async def execute(self, _sql, *_params):
        self.executed.append(_sql)
        return 'UPDATE 2'


def test_postgres_executor_single_connection_transaction():
    conn = _PgConn()
    desc = pg_exec.create(conn)
    assert _run(desc['exec'](_WRITE_PLAN))['affectedRows'] == 2

    async def body(exec_on_tx, _tx=None):
        return await exec_on_tx(_WRITE_PLAN)

    assert _run(desc['with_transaction'](body))['affectedRows'] == 2
    assert len(conn.executed) == 2, '单连接事务路径应复用同一连接（不另开池连接）'


# ─── 保存点原语：SAVEPOINT / RELEASE SAVEPOINT / ROLLBACK TO SAVEPOINT ──

def test_sqlite_savepoint_primitives(tmp_path):
    """真实 aiosqlite：SAVEPOINT / ROLLBACK TO / RELEASE 行为可观测"""
    db_path = tmp_path / 'sp.db'

    async def scenario():
        db = await aiosqlite.connect(db_path)
        await db.execute('CREATE TABLE sp_t (_id TEXT PRIMARY KEY)')
        await db.commit()
        desc = create_connection('sqlite', db)

        def ins(v):
            return {'stmts': [{'text': 'INSERT INTO sp_t (_id) VALUES (?)',
                               'params': [v], 'isWrite': True}]}

        tx = await desc['open_transaction']()
        await tx['exec'](ins('a'))
        await tx['savepoint']('sp_1')
        await tx['exec'](ins('b'))
        await tx['rollback_to_savepoint']('sp_1')
        await tx['release_savepoint']('sp_1')
        await tx['exec'](ins('c'))
        await tx['commit']()
        await tx['release']()
        await db.close()

        db2 = await aiosqlite.connect(db_path)
        try:
            cur = await db2.execute('SELECT _id FROM sp_t ORDER BY _id')
            rows = [r[0] for r in await cur.fetchall()]
            await cur.close()
        finally:
            await db2.close()
        return rows

    assert _run(scenario()) == ['a', 'c'], '回滚到保存点后 b 不可见，保存点之后的 c 保留'


def test_mysql_savepoint_primitives():
    conn = _MyConn()
    tx = _run(mysql_exec.create(conn)['open_transaction']())

    async def drive():
        await tx['savepoint']('sp_1')
        await tx['release_savepoint']('sp_1')
        await tx['rollback_to_savepoint']('sp_1')

    _run(drive())
    assert conn.executed == [
        'BEGIN', 'SAVEPOINT sp_1', 'RELEASE SAVEPOINT sp_1', 'ROLLBACK TO SAVEPOINT sp_1']


def test_postgres_savepoint_primitives():
    conn = _PgConn()
    tx = _run(pg_exec.create(conn)['open_transaction']())

    async def drive():
        await tx['savepoint']('sp_1')
        await tx['release_savepoint']('sp_1')
        await tx['rollback_to_savepoint']('sp_1')

    _run(drive())
    assert conn.executed == [
        'SAVEPOINT sp_1', 'RELEASE SAVEPOINT sp_1', 'ROLLBACK TO SAVEPOINT sp_1']


def test_with_transaction_passes_tx_handle_to_body(tmp_path):
    """with_transaction 向 body 追加第二参数（事务句柄，供上层读保存点原语）"""
    db_path = tmp_path / 'sp_handle.db'

    async def scenario():
        db = await aiosqlite.connect(db_path)
        desc = create_connection('sqlite', db)
        seen = {}

        async def body(exec_on_tx, tx=None):
            seen['tx'] = tx
            return await exec_on_tx({'stmts': [{'text': 'SELECT 1', 'params': []}]})

        out = await desc['with_transaction'](body)
        await db.close()
        return out, seen

    out, seen = _run(scenario())
    assert out is not None
    handle = seen['tx']
    assert callable(handle.get('savepoint')), '第二参数须为事务句柄（含保存点原语）'
    assert callable(handle.get('release_savepoint'))
    assert callable(handle.get('rollback_to_savepoint'))


# ─── 嵌套事务：同源内层并入外层（不新开事务） ─────────────────

def test_run_in_transaction_nested_same_source_merges():
    events = []
    feedback.set_sink(events.append)
    opened = []
    inner_ran = []

    async def with_transaction(body):
        opened.append(1)
        return await body(lambda _plan: None)

    datasource.set_connections({
        'cov_nested': {'kind': 'sqlite', 'exec': None, 'with_transaction': with_transaction},
    })

    async def scenario():
        async def inner():
            inner_ran.append('inner')

        async def outer():
            await datasource.run_in_transaction('cov_nested', inner)

        await datasource.run_in_transaction('cov_nested', outer)

    _run(scenario())
    assert opened == [1], '嵌套同源事务只应开启一次'
    assert inner_ran == ['inner'], '内层体须并入外层事务执行'
    warned = [e for e in events if e.get('code') == 'nestedSavepointUnsupported']
    assert len(warned) == 1, '无保存点原语时降级须告警，且同一源只告警一次'
