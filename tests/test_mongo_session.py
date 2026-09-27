"""Mongo session 事务用例（执行总纲 A1–A8）

零外部服务路径：假 Mongo client/db（记录 session 与事务调用）+ 假 SQL 执行器；
真实路径：本机单节点副本集 rs0（独立库 mongo_store_e2e_tx）验证事务真提交 / 真回滚；
非 rs 环境（CI standalone）自动 skip。

运行：PYTHONPATH=src LOCAL_CORE=1 python -m pytest tests/test_mongo_session.py -q
对齐 nodejs-store/tests/mongo-session.test.js
"""

import asyncio

import pytest

from py_store import feedback, store
from py_store import datasource as _ds
from py_store import schema as _sc
from py_store.crud.exec import run_atomic
from py_store.executors.mongo import exec_mongo, open_transaction


# ─── 假 Mongo 驱动（能力 + session 记录） ────────────────────

class _RecCursor:
    async def to_list(self, length=None):
        return []


class _RecColl:
    def __init__(self, rec, name):
        self._rec = rec
        self._name = name

    def _hit(self, op, session):
        self._rec['ops'].append((self._name, op, session))

    def find(self, f=None, projection=None, session=None):
        self._hit('find', session)
        return _RecCursor()

    async def aggregate(self, pipeline=None, session=None):
        self._hit('aggregate', session)
        return _RecCursor()

    async def count_documents(self, f=None, session=None):
        self._hit('countDocuments', session)
        return 0

    async def find_one(self, f=None, projection=None, session=None):
        self._hit('findOne', session)
        return None

    async def insert_one(self, doc, session=None):
        self._hit('insertOne', session)
        return doc

    async def insert_many(self, docs, session=None):
        self._hit('insertMany', session)
        return {'insertedCount': len(docs)}

    async def replace_one(self, f, doc, upsert=False, session=None):
        self._hit('replaceOne', session)
        return {'modifiedCount': 1}

    async def find_one_and_update(self, f, u, **kw):
        self._hit('findOneAndUpdate', kw.get('session'))
        return None

    async def update_many(self, f, u, session=None):
        self._hit('updateMany', session)
        return {'modifiedCount': 1}

    async def delete_many(self, f, session=None):
        self._hit('deleteMany', session)
        return {'deletedCount': 1}


class _FakeSession:
    """PyMongo async 语义：start_session/start_transaction 由 client 侧编排，
    ``start_transaction`` 为协程（与真实 AsyncClientSession 一致）。"""

    def __init__(self, rec):
        self._rec = rec

    async def start_transaction(self, *a, **kw):
        self._rec['tx'].append('start')

    async def commit_transaction(self):
        self._rec['tx'].append('commit')

    async def abort_transaction(self):
        self._rec['tx'].append('abort')

    async def end_session(self):
        self._rec['tx'].append('end')


class _FakeAdmin:
    def __init__(self, hello, counter):
        self._hello = hello
        self._counter = counter

    async def command(self, _name):
        self._counter['hello'] += 1
        if isinstance(self._hello, BaseException):
            raise self._hello
        return self._hello


class _FakeMongoClient:
    """client 桩：admin.command('hello') 决定能力；start_session 记录

    ``start_session`` 为**同步**方法（对齐 PyMongo async 实测语义）。
    """

    def __init__(self, hello, rec):
        self._counter = {'hello': 0}
        self.admin = _FakeAdmin(hello, self._counter)
        self._rec = rec

    def start_session(self):
        s = _FakeSession(self._rec)
        self._rec['sessions'].append(s)
        return s


class _FakeMongoDb:
    """db 桩：``client`` 属性供探测；``__getitem__`` 取集合"""

    def __init__(self, client, rec):
        self.client = client
        self._rec = rec

    def __getitem__(self, name):
        return _RecColl(self._rec, name)


def _make_db(hello=None, transactable=True):
    rec = {'ops': [], 'tx': [], 'sessions': [], 'hello': 0}
    hello = hello if hello is not None else ({'setName': 'rs0'} if transactable else {'ok': 1})
    client = _FakeMongoClient(hello, rec)
    return _FakeMongoDb(client, rec), rec, client


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _isolate():
    feedback.set_sink(None)
    yield
    _ds.set_connections({})
    _ds._mongo_tx_cap.clear()
    feedback.set_sink(None)


def _register_mongo(name, source, collection=None):
    _sc.register({
        'name': name, 'collection': collection or name.lower(),
        'timestamps': False, 'idPrefix': 'mx',
        'fields': {'v': {'type': 'string'}}, 'relations': {}, 'datasource': source,
    })


# ─── A1 能力探测四态 ─────────────────────────────────────────

def test_capability_four_states():
    case_repl, _, _ = _make_db({'setName': 'rs0'})
    case_shard, _, _ = _make_db({'msg': 'isdbgrid'})
    case_alone, _, _ = _make_db({'ok': 1})
    case_err, _, _ = _make_db(RuntimeError('boom'))
    assert _run(_ds.mongo_transactable(case_repl)) is True
    assert _run(_ds.mongo_transactable(case_shard)) is True
    assert _run(_ds.mongo_transactable(case_alone)) is False
    assert _run(_ds.mongo_transactable(case_err)) is None


# ─── A2 探测按 client 缓存；失败不缓存 ───────────────────────

def test_capability_cached_per_client():
    db, _, client = _make_db({'setName': 'rs0'})
    _run(_ds.mongo_transactable(db))
    _run(_ds.mongo_transactable(db))
    assert client._counter['hello'] == 1, '同一 client 只探测一次'
    bad, _, bad_client = _make_db(RuntimeError('x'))
    assert _run(_ds.mongo_transactable(bad)) is None
    assert _run(_ds.mongo_transactable(bad)) is None
    assert bad_client._counter['hello'] == 2, '探测失败不写缓存（重探）'


# ─── A6 事务原语：start/commit/abort/end，幂等，无保存点 ─────

def test_open_transaction_primitives():
    db, rec, _ = _make_db({'setName': 'rs0'})

    async def scenario():
        tx = await open_transaction(db)
        assert 'savepoint' not in tx and 'release_savepoint' not in tx
        await tx['commit']()
        await tx['commit']()          # 幂等
        await tx['release']()
        return tx

    tx = _run(scenario())
    assert rec['tx'] == ['start', 'commit', 'end']
    assert 'session' in tx


# ─── A5 exec_mongo 全 9 kind 透传 session ───────────────────

def test_exec_mongo_passes_session_all_kinds():
    db, rec, _ = _make_db({'setName': 'rs0'})
    sentinel = object()
    cmds = [
        {'collection': 'c', 'kind': 'find', 'filter': {}},
        {'collection': 'c', 'kind': 'aggregate', 'pipeline': []},
        {'collection': 'c', 'kind': 'countDocuments', 'filter': {}},
        {'collection': 'c', 'kind': 'findOne', 'filter': {}},
        {'collection': 'c', 'kind': 'insertOne', 'doc': {'_id': '1'}},
        {'collection': 'c', 'kind': 'insertMany', 'docs': [{'_id': '1'}]},
        {'collection': 'c', 'kind': 'insertMany', 'docs': [{'_id': '1'}], 'upsertById': True},
        {'collection': 'c', 'kind': 'findOneAndUpdate', 'filter': {}, 'update': {}},
        {'collection': 'c', 'kind': 'updateMany', 'filter': {}, 'update': {}},
        {'collection': 'c', 'kind': 'deleteMany', 'filter': {}},
    ]
    for cmd in cmds:
        _run(exec_mongo(db, dict(cmd), session=sentinel))
    assert rec['ops'], '应有操作记录'
    assert all(op[2] is sentinel for op in rec['ops']), '每个操作都携带 session'


def test_exec_mongo_without_session_no_kwarg():
    db, rec, _ = _make_db({'setName': 'rs0'})
    _run(exec_mongo(db, {'collection': 'c', 'kind': 'insertOne', 'doc': {'_id': '1'}}))
    assert rec['ops'][0][2] is None, '零回归：无 session 时不传'


# ─── A3 standalone：会话内声明降级、命令仍执行 ──────────────

def test_session_standalone_declares_and_runs():
    db, rec, _ = _make_db({'ok': 1})   # standalone
    _register_mongo('MxAlone', 'mx_alone')
    _ds.set_connections({'mx_alone': db})
    events = []
    feedback.set_sink(events.append)

    async def scenario():
        async with store.session() as s:
            await s.insert('MxAlone', {'v': '1'})
            await s.insert('MxAlone', {'v': '2'})

    _run(scenario())
    warned = [e for e in events if e.get('code') == 'mongoTransactionUnsupported']
    assert len(warned) == 1, '同一源只声明一次'
    assert warned[0]['deployment'] == 'standalone'
    assert not [e for e in events if e.get('type') == 'session_not_atomic']
    assert rec['sessions'] == [], 'standalone 不开 session'
    assert [op[1] for op in rec['ops']] == ['insertOne', 'insertOne'], '命令按原样执行'


# ─── A4 unknown：声明 deployment=unknown ────────────────────

def test_session_unknown_deployment_declares():
    db, _, _ = _make_db(RuntimeError('probe-fail'))
    _register_mongo('MxUnknown', 'mx_unknown')
    _ds.set_connections({'mx_unknown': db})
    events = []
    feedback.set_sink(events.append)

    async def scenario():
        async with store.session() as s:
            await s.insert('MxUnknown', {'v': '1'})

    _run(scenario())
    warned = [e for e in events if e.get('code') == 'mongoTransactionUnsupported']
    assert len(warned) == 1 and warned[0]['deployment'] == 'unknown'


# ─── A7 单 Mongo 源 run_atomic 包事务（可事务桩） ───────────

def test_run_atomic_wraps_mongo_transaction():
    db, rec, _ = _make_db({'setName': 'rs0'})
    _register_mongo('MxTx', 'mx_tx')
    _ds.set_connections({'mx_tx': db})

    async def scenario():
        async def body():
            await store.insert('MxTx', {'v': '1'})
            return 'ok'
        return await run_atomic({'mx_tx'}, body)

    out = _run(scenario())
    assert out == 'ok'
    assert rec['tx'] == ['start', 'commit', 'end']
    assert rec['ops'][0][2] is not None, '写入携带 session'


def test_run_atomic_mongo_failure_aborts():
    db, rec, _ = _make_db({'setName': 'rs0'})
    _register_mongo('MxTxFail', 'mx_tx_fail')
    _ds.set_connections({'mx_tx_fail': db})

    async def scenario():
        async def body():
            await store.insert('MxTxFail', {'v': '1'})
            raise RuntimeError('boom')
        return await run_atomic({'mx_tx_fail'}, body)

    with pytest.raises(RuntimeError, match='boom'):
        _run(scenario())
    assert rec['tx'] == ['start', 'abort', 'end']


# ─── A8 嵌套 Mongo 走保存点降级；跨源写 fail-closed ─────────

def test_nested_mongo_uses_savepoint_degradation():
    db, rec, _ = _make_db({'setName': 'rs0'})
    _register_mongo('MxNest', 'mx_nest')
    _ds.set_connections({'mx_nest': db})
    events = []
    feedback.set_sink(events.append)

    async def scenario():
        async with store.session() as s:
            await s.insert('MxNest', {'v': '1'})
            async with store.session() as inner:
                await inner.insert('MxNest', {'v': '2'})

    _run(scenario())
    assert [e for e in events if e.get('code') == 'nestedSavepointUnsupported'], \
        'Mongo 无保存点 → 既有降级声明'
    assert not [e for e in events if e.get('code') == 'mongoTransactionUnsupported'], \
        '可事务的 Mongo 不应发「不支持」声明'
    assert rec['tx'].count('start') == 1, '嵌套会话共用外层事务'


# ─── A10 真实本机 rs0：事务真提交 / 真回滚 ──────────────────

MONGO_URI = 'mongodb://127.0.0.1:27017/mongo_store_e2e_tx'


def _run_real(scenario):
    """单一事件循环内：连接 → 探测 → 非 rs / 不可达 skip → 执行 scenario(client, db)

    AsyncMongoClient 与创建它的事件循环绑定，故探测与用例必须在**同一** ``asyncio.run`` 内。
    """
    pytest.importorskip('pymongo')
    from pymongo import AsyncMongoClient

    async def _main():
        client = AsyncMongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
        try:
            hello = await client.admin.command('hello')
        except BaseException:
            await client.close()
            pytest.skip('本机 MongoDB 不可达（127.0.0.1:27017）')
        if not hello.get('setName'):
            await client.close()
            pytest.skip('本机 Mongo 非副本集（如 CI standalone）→ 跳过真实事务用例')
        db = client['mongo_store_e2e_tx']
        try:
            return await scenario(client, db)
        finally:
            await client.close()

    return asyncio.run(_main())


def test_real_rs0_session_commits():
    async def scenario(_client, db):
        await db['mx_real'].drop()
        _register_mongo('MxReal', 'mx_real', collection='mx_real')
        _ds.set_connections({'mx_real': db})
        async with store.session() as s:
            await s.insert('MxReal', {'v': 'a'})
            await s.insert('MxReal', {'v': 'b'})
        n = await db['mx_real'].count_documents({})
        await db['mx_real'].drop()
        return n

    assert _run_real(scenario) == 2, 'rs0 会话事务提交后全部可见'


def test_real_rs0_session_aborts_on_error():
    async def scenario(_client, db):
        await db['mx_real_b'].drop()
        _register_mongo('MxRealB', 'mx_real', collection='mx_real_b')
        _ds.set_connections({'mx_real': db})
        try:
            async with store.session() as s:
                await s.insert('MxRealB', {'v': 'a'})
                raise RuntimeError('boom')
        except RuntimeError:
            pass
        n = await db['mx_real_b'].count_documents({})
        await db['mx_real_b'].drop()
        return n

    assert _run_real(scenario) == 0, 'rs0 会话事务异常 → abort，全部不可见'
