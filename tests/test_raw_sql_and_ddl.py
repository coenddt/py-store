"""Host 原生 SQL（execute_raw / transaction）与 DDL 生成（ddl.generate）测试

依据执行文档：doc/execution/2026/09/处理中-Host原生SQL与DDL生成-执行.md §4.8
运行：PYTHONPATH=src python -m pytest tests/test_raw_sql_and_ddl.py
JS 侧对拍：nodejs-store/tests/raw-sql-ddl.test.js
"""

import asyncio

import pytest

from py_store import datasource, feedback, store


@pytest.fixture(autouse=True)
def _isolate_globals():
    """隔离全局连接映射与反馈 sink（测试间互不影响）"""
    datasource.set_connections({})
    feedback.set_sink(None)
    yield
    datasource.set_connections({})
    feedback.set_sink(None)


def _run(coro):
    return asyncio.run(coro)


class _FakeMongo:
    """非 Mapping → 被判定为 Mongo 源"""


# ─── ① execute_raw ──────────────────────────────────────────

def test_execute_raw_builds_plan_and_returns_rows():
    captured = {}

    async def fake_exec(plan):
        captured['plan'] = plan
        return {'docs': None, 'rows': [{'a': 1}], 'affectedRows': 0}

    datasource.set_connections({'db': {'kind': 'sqlite', 'exec': fake_exec}})
    out = _run(store.execute_raw('db', 'SELECT 1', [7]))

    assert captured['plan'] == {'stmts': [{'text': 'SELECT 1', 'params': [7], 'isWrite': False}]}
    assert out == {'rows': [{'a': 1}], 'affectedRows': 0}


def test_execute_raw_is_write_flag_and_affected_rows():
    captured = {}

    async def fake_exec(plan):
        captured['plan'] = plan
        return {'docs': None, 'rows': None, 'affectedRows': 3}

    datasource.set_connections({'db': {'kind': 'mysql', 'exec': fake_exec}})
    out = _run(store.execute_raw('db', 'UPDATE t SET a = ?', [1], True))

    assert captured['plan']['stmts'][0]['isWrite'] is True
    assert out == {'rows': None, 'affectedRows': 3}


def test_execute_raw_mongo_source_raises():
    datasource.set_connections({'db': _FakeMongo()})
    with pytest.raises(store.RawSqlError):
        _run(store.execute_raw('db', 'SELECT 1'))


def test_execute_raw_missing_executor_raises():
    datasource.set_connections({'db': {'kind': 'sqlite'}})
    with pytest.raises(store.RawSqlError):
        _run(store.execute_raw('db', 'SELECT 1'))


# ─── ①′ execute_raw 命名档与写推断（core raw_stmt_compile 编译链）───

def _capture_exec(captured):
    async def fake_exec(plan):
        captured['plan'] = plan
        return {'docs': None, 'rows': [], 'affectedRows': 0}
    return fake_exec


def test_execute_raw_named_params_mysql():
    captured = {}
    datasource.set_connections({'db': {'kind': 'mysql', 'exec': _capture_exec(captured)}})
    _run(store.execute_raw('db', 'SELECT * FROM t WHERE a = :x', {'x': 7}))
    assert captured['plan']['stmts'][0]['text'] == 'SELECT * FROM t WHERE a = ?'
    assert captured['plan']['stmts'][0]['params'] == [7]
    assert captured['plan']['stmts'][0]['isWrite'] is False


def test_execute_raw_named_params_pg_reorder():
    captured = {}
    datasource.set_connections({'db': {'kind': 'postgres', 'exec': _capture_exec(captured)}})
    _run(store.execute_raw('db', 'SELECT :b, :a', {'a': 1, 'b': 2}))
    assert captured['plan']['stmts'][0]['text'] == 'SELECT $1, $2'
    assert captured['plan']['stmts'][0]['params'] == [2, 1]


def test_execute_raw_named_reuse_and_cast():
    captured = {}
    datasource.set_connections({'db': {'kind': 'postgres', 'exec': _capture_exec(captured)}})
    _run(store.execute_raw('db', 'SELECT :x::text OR b = :x', {'x': 'v'}))
    # 同名复用各占一个占位符；:: cast 不误判为参数
    assert captured['plan']['stmts'][0]['text'] == 'SELECT $1::text OR b = $2'
    assert captured['plan']['stmts'][0]['params'] == ['v', 'v']


def test_execute_raw_quoted_and_comment_colons_kept():
    captured = {}
    datasource.set_connections({'db': {'kind': 'mysql', 'exec': _capture_exec(captured)}})
    sql = "SELECT ':' -- :hint\nFROM t WHERE a = :x"
    _run(store.execute_raw('db', sql, {'x': 1}))
    # 字符串字面量与行注释内的冒号保持原样，仅真实 :x 被编译
    assert captured['plan']['stmts'][0]['text'] == "SELECT ':' -- :hint\nFROM t WHERE a = ?"
    assert captured['plan']['stmts'][0]['params'] == [1]


def test_execute_raw_write_inferred_from_insert():
    captured = {}
    datasource.set_connections({'db': {'kind': 'mysql', 'exec': _capture_exec(captured)}})
    _run(store.execute_raw('db', 'INSERT INTO t VALUES (1)'))
    assert captured['plan']['stmts'][0]['isWrite'] is True


def test_execute_raw_unknown_first_word_treated_as_write():
    captured = {}
    datasource.set_connections({'db': {'kind': 'postgres', 'exec': _capture_exec(captured)}})
    # 未知首词按写（R7 默认安全方向，非 Err）
    _run(store.execute_raw('db', 'VACUUM ANALYZE t'))
    assert captured['plan']['stmts'][0]['isWrite'] is True


def test_execute_raw_named_missing_param_raises():
    datasource.set_connections({'db': {'kind': 'mysql', 'exec': _capture_exec({})}})
    with pytest.raises(store.RawSqlError) as ei:
        _run(store.execute_raw('db', 'SELECT * FROM t WHERE a = :x', {}))
    assert '未在 params 中提供' in str(ei.value)


def test_execute_raw_named_unused_param_raises():
    datasource.set_connections({'db': {'kind': 'mysql', 'exec': _capture_exec({})}})
    with pytest.raises(store.RawSqlError) as ei:
        _run(store.execute_raw('db', 'SELECT * FROM t WHERE a = :x', {'x': 1, 'y': 2}))
    assert '未使用的命名参数' in str(ei.value)


def test_execute_raw_scalar_params_raises():
    datasource.set_connections({'db': {'kind': 'mysql', 'exec': _capture_exec({})}})
    with pytest.raises(store.RawSqlError) as ei:
        _run(store.execute_raw('db', 'SELECT 1', 7))
    assert '仅支持 list/tuple（位置档）或 dict（命名档）' in str(ei.value)


def test_transaction_uses_tx_connection_and_rolls_back():
    state = {'committed': 0, 'rolled_back': 0}

    async def run_stmts(plan):
        return {'docs': None, 'rows': [{'id': 1}], 'affectedRows': 0}

    async def with_transaction(body):
        try:
            out = await body(run_stmts)
            state['committed'] += 1
            return out
        except BaseException:
            state['rolled_back'] += 1
            raise

    datasource.set_connections(
        {'db': {'kind': 'sqlite', 'exec': run_stmts, 'with_transaction': with_transaction}})

    async def body():
        # 事务内 execute_raw 经 connection_for 落到事务连接（tx_desc.exec）
        out = await store.execute_raw('db', 'SELECT * FROM t WHERE id = ? FOR UPDATE', [1])
        assert out == {'rows': [{'id': 1}], 'affectedRows': 0}
        raise RuntimeError('boom')

    with pytest.raises(RuntimeError):
        _run(store.transaction('db', body))

    assert state['rolled_back'] == 1
    assert state['committed'] == 0


def test_transaction_without_with_transaction_runs_plainly():
    async def fake_exec(plan):
        return {'docs': None, 'rows': [], 'affectedRows': 0}

    datasource.set_connections({'db': {'kind': 'sqlite', 'exec': fake_exec}})
    events = []
    feedback.set_sink(events.append)

    async def body():
        return 'ok'

    assert _run(store.transaction('db', body)) == 'ok'
    warned = [e for e in events if e.get('type') == 'transaction_not_atomic']
    assert len(warned) == 1, '缺 with_transaction 的事务作用域必须显式声明，不静默'
    assert warned[0]['code'] == 'transactionNotAtomic'
    assert warned[0]['source'] == 'db'
    assert warned[0]['kind'] == 'sqlite'


# ─── ①″ execute_native（Mongo 原生聚合逃生口，与 js 侧对拍）───

class _FakeMongoDb:
    """带 get_collection → Database 形态（execute_native 判定为合法 Mongo 源）"""

    def get_collection(self, name):
        return None  # 不会被真正调用（exec_mongo 被替换）


def test_execute_native_builds_aggregate_command(monkeypatch):
    captured = {}

    async def fake_exec_mongo(db, cmd, session=None):
        captured['db'] = db
        captured['cmd'] = cmd
        captured['session'] = session
        return [{'n': 1}]

    monkeypatch.setattr(datasource.executors.mongo, 'exec_mongo', fake_exec_mongo)
    db = _FakeMongoDb()
    datasource.set_connections({'db': db})
    out = _run(store.execute_native(
        'db', 'orders', [{'$match': {'a': 1}}], {'allowDiskUse': True}))

    assert captured['db'] is db
    assert captured['cmd'] == {'kind': 'aggregate', 'collection': 'orders',
                               'pipeline': [{'$match': {'a': 1}}],
                               'options': {'allowDiskUse': True}}
    assert captured['session'] is None
    assert out == {'rows': [{'n': 1}]}


def test_execute_native_pipeline_none_defaults_empty(monkeypatch):
    captured = {}

    async def fake_exec_mongo(db, cmd, session=None):
        captured['cmd'] = cmd
        return []

    monkeypatch.setattr(datasource.executors.mongo, 'exec_mongo', fake_exec_mongo)
    datasource.set_connections({'db': _FakeMongoDb()})
    _run(store.execute_native('db', 't'))
    assert captured['cmd']['pipeline'] == []
    assert captured['cmd']['options'] == {}


def test_execute_native_tx_view_session_injected(monkeypatch):
    captured = {}

    async def fake_exec_mongo(db, cmd, session=None):
        captured['session'] = session
        return []

    async def fake_resolve(source, is_write=False):
        return {'kind': 'mongo', 'conn': _FakeMongoDb(), 'session': 'SENTINEL', 'tx': {}}

    monkeypatch.setattr(datasource, 'resolve_connection', fake_resolve)
    monkeypatch.setattr(datasource.executors.mongo, 'exec_mongo', fake_exec_mongo)
    _run(store.execute_native('db', 't', []))
    # 事务视图 session 强制注入执行器
    assert captured['session'] == 'SENTINEL'


def test_execute_native_options_session_cannot_override_tx(monkeypatch):
    captured = {}

    async def fake_exec_mongo(db, cmd, session=None):
        captured['session'] = session
        captured['options'] = cmd['options']
        return []

    async def fake_resolve(source, is_write=False):
        return {'kind': 'mongo', 'conn': _FakeMongoDb(), 'session': 'SENTINEL', 'tx': {}}

    monkeypatch.setattr(datasource, 'resolve_connection', fake_resolve)
    monkeypatch.setattr(datasource.executors.mongo, 'exec_mongo', fake_exec_mongo)
    _run(store.execute_native('db', 't', [], {'session': 'USER_SESSION'}))
    # session 由事务接管：用户 options.session 不可覆盖（executors _opts 内 session 强制注入）
    assert captured['session'] == 'SENTINEL'
    assert captured['options'] == {'session': 'USER_SESSION'}


def test_execute_native_sql_source_raises():
    datasource.set_connections({'db': {'kind': 'mysql', 'exec': _capture_exec({})}})
    with pytest.raises(store.NativeCommandError) as ei:
        _run(store.execute_native('db', 't', []))
    assert '是 SQL 源' in str(ei.value)
    assert 'execute_raw' in str(ei.value)


def test_execute_native_non_mongo_source_raises():
    datasource.set_connections({'db': _FakeMongo()})
    with pytest.raises(store.NativeCommandError) as ei:
        _run(store.execute_native('db', 't', []))
    assert '不是 Mongo 源' in str(ei.value)


def test_exec_mongo_aggregate_forwards_options():
    captured = {}

    class _FakeCursor:
        async def to_list(self, length=None):
            return [{'n': 1}]

    class _FakeColl:
        async def aggregate(self, pipeline, **kw):
            captured['pipeline'] = pipeline
            captured['kw'] = kw
            return _FakeCursor()

    class _FakeDriverDb:
        def __getitem__(self, name):
            captured['collection'] = name
            return _FakeColl()

    out = _run(datasource.executors.mongo.exec_mongo(
        _FakeDriverDb(),
        {'kind': 'aggregate', 'collection': 'orders',
         'pipeline': [{'$match': {'a': 1}}], 'options': {'allowDiskUse': True}}))

    assert captured['collection'] == 'orders'
    assert captured['pipeline'] == [{'$match': {'a': 1}}]
    assert captured['kw'] == {'allowDiskUse': True}
    assert out == [{'n': 1}]


def test_exec_mongo_aggregate_without_options_unchanged():
    """既有 GQL 路径 cmd 无 options 键 → 调用形态不变（零回归）"""
    captured = {}

    class _FakeCursor:
        async def to_list(self, length=None):
            return []

    class _FakeColl:
        async def aggregate(self, pipeline, **kw):
            captured['kw'] = kw
            return _FakeCursor()

    class _FakeDriverDb:
        def __getitem__(self, name):
            return _FakeColl()

    _run(datasource.executors.mongo.exec_mongo(
        _FakeDriverDb(), {'kind': 'aggregate', 'collection': 't', 'pipeline': []}))
    assert captured['kw'] == {}


# ─── ② ddl.generate ─────────────────────────────────────────

def test_ddl_present_and_archive_includes_json_columns():
    store.register({
        'name': 'DdlProbe', 'collection': 'ddl_probes', 'idPrefix': 'd',
        'timestamps': False,
        'fields': {
            '_id': {'type': 'string'},
            'title': {'type': 'string'},
            'nested': {'type': 'object', 'fields': {'x': {'type': 'string'}}},
            'tags': {'type': 'array'},
        },
    })
    sql = store.generate_ddl('mysql', ['DdlProbe', 'DdlProbeDeleted'])

    assert 'CREATE TABLE `ddl_probes`' in sql
    assert 'CREATE TABLE `ddl_probes_deleted`' in sql
    assert '`__present` VARCHAR(255)' in sql
    assert '`deletedAt` BIGINT' in sql
    assert 'PRIMARY KEY (`_id`)' in sql
    # object / array 建 JSON 列（同 core field_column_ref::Json）
    assert '`nested` JSON' in sql
    assert '`tags` JSON' in sql


def test_ddl_timestamps_and_no_index():
    store.register({
        'name': 'DdlTs', 'collection': 'ddl_ts', 'idPrefix': 't', 'timestamps': True,
        'indexes': [{'keys': {'name': 1}}],
        'fields': {'_id': {'type': 'string'}, 'name': {'type': 'string'}},
    })
    sql = store.generate_ddl('postgres', ['DdlTs'])

    assert '"createdAt" BIGINT' in sql
    assert '"updatedAt" BIGINT' in sql
    assert '"__present" TEXT' in sql
    # 阶段3：schema.indexes 落地为 CREATE INDEX（原「不建索引」铁律已放开）
    assert 'CREATE INDEX "idx_ddl_ts_name" ON "ddl_ts" ("name" ASC)' in sql


def test_ddl_backend_types():
    store.register({
        'name': 'DdlTypes', 'collection': 'ddl_types', 'idPrefix': 'y', 'timestamps': False,
        'fields': {
            '_id': {'type': 'string'},
            's': {'type': 'string'},
            'i': {'type': 'int'},
            'f': {'type': 'float'},
            'ok': {'type': 'bool'},
            'at': {'type': 'datetime'},
        },
    })
    my = store.generate_ddl('mysql', ['DdlTypes'])
    pg = store.generate_ddl('postgres', ['DdlTypes'])
    lite = store.generate_ddl('sqlite', ['DdlTypes'])

    assert '`s` VARCHAR(255)' in my and '`f` DOUBLE' in my and '`ok` TINYINT(1)' in my
    assert '`at` BIGINT' in my
    assert '"s" TEXT' in pg and '"f" DOUBLE PRECISION' in pg and '"ok" BOOLEAN' in pg
    assert '"s" TEXT' in lite and '"f" REAL' in lite and '"ok" INTEGER' in lite
    assert '"at" INTEGER' in lite


def test_ddl_json_column_types_per_backend():
    store.register({
        'name': 'DdlJson', 'collection': 'ddl_json', 'idPrefix': 'j', 'timestamps': False,
        'fields': {
            '_id': {'type': 'string'},
            'obj': {'type': 'object'},
            'arr': {'type': 'array'},
        },
    })
    my = store.generate_ddl('mysql', ['DdlJson'])
    pg = store.generate_ddl('postgres', ['DdlJson'])
    lite = store.generate_ddl('sqlite', ['DdlJson'])

    assert '`obj` JSON' in my and '`arr` JSON' in my
    assert '"obj" jsonb' in pg and '"arr" jsonb' in pg
    assert '"obj" TEXT' in lite and '"arr" TEXT' in lite


def test_ddl_unknown_backend_raises():
    with pytest.raises(ValueError):
        store.generate_ddl('oracle', ['X'])


def test_ddl_unknown_field_type_raises():
    store.register({
        'name': 'DdlBad', 'collection': 'ddl_bad', 'idPrefix': 'b', 'timestamps': False,
        'fields': {'_id': {'type': 'string'}, 'weird': {'type': 'weird'}},
    })
    with pytest.raises(ValueError):
        store.generate_ddl('mysql', ['DdlBad'])


def test_ddl_present_overflow_emits_feedback():
    events = []
    feedback.set_sink(events.append)
    fields = {'_id': {'type': 'string'}}
    for i in range(40):
        fields[f'verylongfieldname{i:02d}'] = {'type': 'string'}
    store.register({
        'name': 'DdlWide', 'collection': 'ddl_wide', 'idPrefix': 'w', 'timestamps': False,
        'fields': fields,
    })
    store.generate_ddl('mysql', ['DdlWide'])

    assert any(e.get('code') == 'ddlPresentOverflow' for e in events), events
