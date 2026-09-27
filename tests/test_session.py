"""Session（工作单元）与原子边界用例（执行文档 05 §4.2）

逐条覆盖总纲 A1–A9，全部基于**可观测副作用**（open_transaction / commit / rollback /
release 调用计数、连接标识、异常类型、反馈事件），不依赖实现细节。
零外部服务：假 SQL 执行器 + 真实 aiosqlite 文件库。

运行：``PYTHONPATH=src LOCAL_CORE=1 python -m pytest tests/test_session.py -q``
对齐 ``nodejs-store/tests/session.test.js``（镜像 #1–#10）。
"""

import asyncio

import aiosqlite
import pytest

from py_store import (
    NonAtomicWriteError,
    datasource,
    executors,
    feedback,
    init,
    permission,
    store,
)
from py_store import schema as _sc
from py_store.crud import write as write_mod
from py_store.crud.exec import run_atomic

# ─── 假 SQL 执行器工厂（doc 4.1） ─────────────────────────────

def make_fake_sql_executor(kind='sqlite', savepoints=True):
    """返回 ``(descriptor, state)``

    descriptor = ``{'kind', 'exec', 'with_transaction', 'open_transaction'}``
    state 计数与连接标识供用例断言：

      - open_transaction() → 新建连接标识（'conn-1' …）并 opened += 1；
      - commit / rollback / release 幂等；
      - exec(plan) 记录当前连接标识；``state['fail_on_write']`` 为真且 plan 含写语句时抛错；
      - ``savepoints=False`` 时句柄不带保存点原语（验证嵌套作用域降级）。
    """
    state = {
        'opened': 0, 'committed': 0, 'rolled_back': 0, 'released': 0,
        'tx_conns': [], 'exec_conns': [], 'rows': [],
        'fail_on_write': False,
        'savepoints': [], 'released_sps': [], 'rolled_to_sps': [],
    }
    seq = {'n': 0}

    async def _run_on(conn_id, plan):
        state['exec_conns'].append(conn_id)
        stmts = (plan or {}).get('stmts') or []
        if state['fail_on_write'] and any(s.get('isWrite') for s in stmts):
            raise RuntimeError('模拟写失败')
        return {'docs': [], 'rows': [], 'affectedRows': 1}

    async def base_exec(plan):
        return await _run_on('base', plan)

    async def open_transaction():
        seq['n'] += 1
        state['opened'] += 1
        conn_id = 'conn-%d' % seq['n']
        state['tx_conns'].append(conn_id)
        done = {'commit': False, 'rollback': False, 'release': False}

        async def commit():
            if done['commit']:
                return
            done['commit'] = True
            state['committed'] += 1

        async def rollback():
            if done['rollback']:
                return
            done['rollback'] = True
            state['rolled_back'] += 1

        async def release():
            if done['release']:
                return
            done['release'] = True
            state['released'] += 1

        async def tx_exec(plan):
            return await _run_on(conn_id, plan)

        tx = {'exec': tx_exec, 'commit': commit, 'rollback': rollback, 'release': release}
        if savepoints:
            async def savepoint(name):
                state['savepoints'].append((conn_id, name))

            async def release_savepoint(name):
                state['released_sps'].append((conn_id, name))

            async def rollback_to_savepoint(name):
                state['rolled_to_sps'].append((conn_id, name))

            tx.update({'savepoint': savepoint,
                       'release_savepoint': release_savepoint,
                       'rollback_to_savepoint': rollback_to_savepoint})
        return tx

    async def with_transaction(body):
        tx = await open_transaction()
        try:
            out = await body(tx['exec'], tx)
        except BaseException:
            await tx['rollback']()
            raise
        else:
            await tx['commit']()
            return out
        finally:
            await tx['release']()

    descriptor = {
        'kind': kind,
        'exec': base_exec,
        'with_transaction': with_transaction,
        'open_transaction': open_transaction,
    }
    return descriptor, state


# ─── Mongo 桩驱动（#10） ─────────────────────────────────────

class _MemCursor:
    def __init__(self, docs):
        self._docs = docs

    async def to_list(self, length=None):
        return list(self._docs)


class _MemColl:
    def __init__(self, docs):
        self._docs = docs or []

    def find(self, _filter=None, _projection=None):
        return _MemCursor(self._docs)

    async def aggregate(self, _pipeline=None):
        return _MemCursor(self._docs)

    async def count_documents(self, _filter=None):
        return len(self._docs)

    async def find_one(self, _filter=None, _projection=None):
        return dict(self._docs[0]) if self._docs else None


class _FakeDb:
    """Mongo db 实例形态（``__getitem__`` 取集合；无 ``get_database``）"""

    def __init__(self, colls=None):
        self.colls = colls or {}

    def __getitem__(self, name):
        return _MemColl(self.colls.get(name))


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _isolate():
    feedback.set_sink(None)
    permission.set_context(None)
    yield
    datasource.set_connections({})
    feedback.set_sink(None)
    permission.set_context(None)


def _register_sql(schema_name, source, id_prefix=''):
    _sc.register({
        'name': schema_name, 'collection': schema_name.lower(), 'idPrefix': id_prefix,
        'timestamps': False, 'fields': {'v': {'type': 'string'}}, 'relations': {},
        'datasource': source,
    })


# ─── #1 成功提交（A8 单一路径 / A1 单源） ────────────────────

def test_session_commit_on_success():
    desc, state = make_fake_sql_executor()
    _register_sql('SessA', 'sess_a', 'SA')
    datasource.set_connections({'sess_a': desc})

    async def scenario():
        async with store.session() as s:
            await s.insert('SessA', {'v': '1'})
            await s.insert('SessA', {'v': '2'})

    _run(scenario())
    assert state['opened'] == 1
    assert state['committed'] == 1
    assert state['rolled_back'] == 0
    assert state['released'] == 1


# ─── #2 异常回滚（A2） ───────────────────────────────────────

def test_session_rollback_on_error():
    desc, state = make_fake_sql_executor()
    _register_sql('SessA', 'sess_a', 'SA')
    datasource.set_connections({'sess_a': desc})

    async def scenario():
        async with store.session() as s:
            await s.insert('SessA', {'v': '1'})
            raise RuntimeError('boom')

    with pytest.raises(RuntimeError, match='boom'):
        _run(scenario())
    assert state['rolled_back'] == 1
    assert state['committed'] == 0
    assert state['released'] == 1


# ─── #3 惰性开事务：空会话不占连接（A3） ─────────────────────

def test_session_lazy_open_no_commands():
    desc, state = make_fake_sql_executor()
    _register_sql('SessA', 'sess_a', 'SA')
    datasource.set_connections({'sess_a': desc})

    async def scenario():
        async with store.session():
            pass

    _run(scenario())
    assert state['opened'] == 0


# ─── #4 跨源写 fail-closed（A1 正向） ────────────────────────

def test_session_cross_source_write_fails_closed():
    desc_a, state_a = make_fake_sql_executor()
    desc_b, state_b = make_fake_sql_executor()
    _register_sql('SessA', 'sess_a', 'SA')
    _register_sql('SessB', 'sess_b', 'SB')
    datasource.set_connections({'sess_a': desc_a, 'sess_b': desc_b})

    async def scenario():
        async with store.session() as s:
            await s.insert('SessA', {'v': 'a'})
            await s.insert('SessB', {'v': 'b'})

    with pytest.raises(NonAtomicWriteError) as ei:
        _run(scenario())
    assert ei.value.sources == ['sess_a', 'sess_b']
    assert state_a['rolled_back'] == 1 and state_a['committed'] == 0
    assert state_b['rolled_back'] == 1 and state_b['committed'] == 0


# ─── #5 跨源读不拦截（A1 反例） ──────────────────────────────

def test_session_cross_source_read_ok():
    desc_a, state_a = make_fake_sql_executor()
    desc_b, state_b = make_fake_sql_executor()
    _register_sql('SessA', 'sess_a', 'SA')
    _register_sql('SessB', 'sess_b', 'SB')
    datasource.set_connections({'sess_a': desc_a, 'sess_b': desc_b})

    async def scenario():
        async with store.session() as s:
            await s.insert('SessA', {'v': 'a'})
            await s.count('SessB')

    _run(scenario())
    assert state_a['committed'] == 1 and state_a['rolled_back'] == 0
    assert state_b['committed'] == 1 and state_b['rolled_back'] == 0


# ─── #6 缺 open_transaction 的 SQL 源：降级不静默（A9） ──────

def test_session_not_atomic_warning():
    desc, state = make_fake_sql_executor()
    del desc['open_transaction']  # 降级形态：无显式事务原语
    _register_sql('SessC', 'sess_c')
    datasource.set_connections({'sess_c': desc})

    events = []
    feedback.set_sink(events.append)

    async def scenario():
        async with store.session() as s:
            await s.execute_raw('sess_c', 'SELECT 1', [], is_write=False)
            await s.execute_raw('sess_c', 'SELECT 2', [], is_write=False)

    _run(scenario())
    warned = [e for e in events if e.get('type') == 'session_not_atomic']
    assert len(warned) == 1, '同一源第二次命令不再告警'
    assert warned[0]['code'] == 'sessionNotAtomic'
    assert warned[0]['source'] == 'sess_c'
    assert state['opened'] == 0


# ─── #7 execute_raw 落会话连接（A4） ─────────────────────────

def test_session_execute_raw_on_session_conn():
    desc, state = make_fake_sql_executor()
    _register_sql('SessA', 'sess_a', 'SA')
    datasource.set_connections({'sess_a': desc})

    async def scenario():
        async with store.session() as s:
            await s.execute_raw('sess_a', 'SELECT 1', [], is_write=True)

    _run(scenario())
    assert state['tx_conns'] and state['exec_conns'] == state['tx_conns'], \
        'execute_raw 必须落在会话事务连接上'


# ─── #8 会话内 store.transaction 并网（A5） ──────────────────

def test_transaction_inside_session_merges():
    desc, state = make_fake_sql_executor()
    _register_sql('SessA', 'sess_a', 'SA')
    datasource.set_connections({'sess_a': desc})
    inner_ran = []

    async def scenario():
        async def inner():
            inner_ran.append('inner')
            await store.insert('SessA', {'v': 't'})

        async with store.session() as s:
            await s.insert('SessA', {'v': '1'})
            await store.transaction('sess_a', inner)

    _run(scenario())
    assert inner_ran == ['inner']
    assert state['opened'] == 1, '会话内 transaction 不得另开事务'


# ─── #9 update 探针与写同事务（A7） ──────────────────────────

def test_update_probe_and_write_same_tx(monkeypatch):
    desc, state = make_fake_sql_executor()
    state['fail_on_write'] = True
    _register_sql('SessProbe', 'sess_probe', 'SP')
    datasource.set_connections({'sess_probe': desc})

    # 借用真实 core 产出规范命令，再以规划桩注入「首次 needsProbe → 重入 command」
    plan = write_mod._core.plan_update(
        'SessProbe', {'_id': 'p1'}, {'v': 'b'}, None, 0,
        {'internal': True}, None, None, None)
    write_cmd = plan['command']
    probe_cmd = {
        'source': write_cmd['source'], 'collection': write_cmd['collection'],
        'kind': 'findOne', 'filter': write_cmd['filter'],
        'projection': {'_id': 1, 'createdBy': 1},
    }

    class _ProbeStub:
        def __init__(self):
            self.calls = 0

        def plan_update(self, *_a, **_kw):
            self.calls += 1
            if self.calls == 1:
                return {'needsProbe': probe_cmd}
            return {'command': write_cmd}

        @staticmethod
        def apply_write_defaults(_name, result):
            return result

    monkeypatch.setattr(write_mod, '_core', _ProbeStub())

    async def scenario():
        return await store.update('SessProbe', {'_id': 'p1'}, {'v': 'b'})

    with pytest.raises(RuntimeError, match='模拟写失败'):
        _run(scenario())
    assert len(state['exec_conns']) == 2, '探针 + 写两次执行'
    assert state['exec_conns'][0] == state['exec_conns'][1], '探针与写必须同连接'
    assert state['exec_conns'][0] == state['tx_conns'][0], '必须落在事务连接上'
    assert state['committed'] == 0 and state['rolled_back'] == 1


# ─── #10 Mongo 源直通（不告警） ──────────────────────────────

def test_session_mongo_passthrough():
    db = _FakeDb({'sess_mongo': [{'_id': 'm1', 'v': 'x'}]})
    _sc.register({
        'name': 'SessMongo', 'collection': 'sess_mongo', 'timestamps': False,
        'fields': {'v': {'type': 'string'}}, 'relations': {}, 'datasource': 'sess_mongo',
    })
    datasource.set_connections({'sess_mongo': db})

    events = []
    feedback.set_sink(events.append)

    async def scenario():
        async with store.session() as s:
            return await s.query('SessMongo{_id, v}')

    got = _run(scenario())
    assert [d['_id'] for d in got] == ['m1']
    assert not [e for e in events if e.get('type') == 'session_not_atomic'], \
        'Mongo 源不属「缺原语的 SQL 源」，不得告警'


# ─── #11 真实 SQLite：提交可见 / 回滚不可见（A1/A2） ─────────

def test_real_sqlite_session_commit_rollback(tmp_path):
    db_path = tmp_path / 'sess.db'

    async def scenario():
        db = await aiosqlite.connect(db_path)
        db2 = await aiosqlite.connect(db_path)
        await db.execute('CREATE TABLE sess_real (_id TEXT PRIMARY KEY, v TEXT, __present TEXT)')
        await db.commit()
        _sc.register({
            'name': 'SessReal', 'collection': 'sess_real', 'idPrefix': 'SR',
            'timestamps': False, 'fields': {'v': {'type': 'string'}}, 'relations': {},
            'datasource': 'sess_real',
        })
        await init({'sess_real': executors.create_connection('sqlite', db)})

        async def count():
            cur = await db2.execute('SELECT COUNT(*) FROM sess_real')
            try:
                return (await cur.fetchone())[0]
            finally:
                await cur.close()

        async with store.session() as s:
            await s.insert('SessReal', {'v': 'keep'})
        after_commit = await count()

        async def failing():
            async with store.session() as s:
                await s.insert('SessReal', {'v': 'drop'})
                raise RuntimeError('boom')

        with pytest.raises(RuntimeError, match='boom'):
            await failing()
        after_rollback = await count()

        await db.close()
        await db2.close()
        return after_commit, after_rollback

    after_commit, after_rollback = _run(scenario())
    assert after_commit == 1, '会话提交后新连接应可见数据'
    assert after_rollback == 1, '会话异常回滚后数据不可见'


# ─── #12 非会话多源写：程序化声明 nonAtomic（B1） ────────────

def test_non_atomic_write_multi_source_emits():
    events = []
    feedback.set_sink(events.append)
    ran = []

    async def scenario():
        async def body():
            ran.append(True)
            return 'ok'
        return await run_atomic({'sess_a', 'sess_b'}, body)

    out = _run(scenario())
    assert out == 'ok' and ran == [True], '多源仍按顺序原样执行（不阻断）'
    na = [e for e in events if e.get('code') == 'nonAtomic']
    assert len(na) == 1, '多源写恰声明一次'
    assert na[0]['type'] == 'non_atomic_write'
    assert na[0]['layer'] == 'crud'
    assert na[0]['sources'] == ['sess_a', 'sess_b']


# ─── #13 非会话单源写：包事务且不声明 nonAtomic（零回归） ─────

def test_single_source_write_wraps_transaction_without_emit():
    desc, state = make_fake_sql_executor()
    datasource.set_connections({'sess_a': desc})
    events = []
    feedback.set_sink(events.append)

    async def scenario():
        async def body():
            return 42
        return await run_atomic({'sess_a'}, body)

    out = _run(scenario())
    assert out == 42
    assert state['opened'] == 1 and state['committed'] == 1, '单源须包事务'
    assert [e for e in events if e.get('code') == 'nonAtomic'] == []


# ─── #14 同源嵌套 transaction：内层失败只回滚内层、外层提交 ───

def test_nested_transaction_inner_failure_rolls_back_to_savepoint():
    desc, state = make_fake_sql_executor()
    datasource.set_connections({'sess_a': desc})

    async def scenario():
        async def inner():
            raise RuntimeError('inner-boom')

        async def outer():
            try:
                await store.transaction('sess_a', inner)
            except RuntimeError as exc:
                assert str(exc) == 'inner-boom'

        await store.transaction('sess_a', outer)

    _run(scenario())
    assert state['opened'] == 1, '嵌套同源事务只开一次事务'
    assert [n for _, n in state['savepoints']] == ['sp_1']
    assert [n for _, n in state['rolled_to_sps']] == ['sp_1']
    assert [n for _, n in state['released_sps']] == ['sp_1']
    assert state['committed'] == 1 and state['rolled_back'] == 0, '外层捕获后仍整体提交'


# ─── #15 同源嵌套 transaction：内层成功只 RELEASE ────────────

def test_nested_transaction_inner_success_releases_savepoint():
    desc, state = make_fake_sql_executor()
    datasource.set_connections({'sess_a': desc})

    async def scenario():
        async def inner():
            return 'inner-ok'

        async def outer():
            return await store.transaction('sess_a', inner)

        return await store.transaction('sess_a', outer)

    assert _run(scenario()) == 'inner-ok'
    assert state['opened'] == 1
    assert [n for _, n in state['savepoints']] == ['sp_1']
    assert [n for _, n in state['rolled_to_sps']] == []
    assert [n for _, n in state['released_sps']] == ['sp_1']
    assert state['committed'] == 1 and state['rolled_back'] == 0


# ─── #16 句柄无保存点原语：降级并入外层 + 同源只告警一次 ─────

def test_nested_transaction_without_savepoint_degrades():
    desc, state = make_fake_sql_executor(savepoints=False)
    datasource.set_connections({'sess_a': desc})
    events = []
    feedback.set_sink(events.append)
    ran = []

    async def scenario():
        async def inner():
            ran.append('inner')
            raise RuntimeError('inner-boom')

        async def outer():
            for _ in range(2):
                try:
                    await store.transaction('sess_a', inner)
                except RuntimeError:
                    ran.append('caught')

        await store.transaction('sess_a', outer)

    _run(scenario())
    assert ran == ['inner', 'caught', 'inner', 'caught'], '降级：异常上抛由外层自行处理'
    assert state['opened'] == 1
    assert state['savepoints'] == [] and state['released_sps'] == []
    warned = [e for e in events if e.get('code') == 'nestedSavepointUnsupported']
    assert len(warned) == 1, '同一源（同一外层作用域）只告警一次'
    assert warned[0]['type'] == 'nested_savepoint_unsupported'
    assert warned[0]['source'] == 'sess_a'


# ─── #17 真实 SQLite：嵌套 transaction 内层回滚、外层提交 ─────

def test_real_sqlite_nested_transaction_inner_rollback(tmp_path):
    db_path = tmp_path / 'nest.db'

    async def scenario():
        db = await aiosqlite.connect(db_path)
        db2 = await aiosqlite.connect(db_path)
        await db.execute('CREATE TABLE nest_t (_id TEXT PRIMARY KEY, v TEXT)')
        await db.commit()
        datasource.set_connections(
            {'nest_real': executors.create_connection('sqlite', db)})

        async def ins(v):
            await store.execute_raw(
                'nest_real', 'INSERT INTO nest_t (_id, v) VALUES (?, ?)', [v, v], is_write=True)

        async def outer():
            await ins('keep')

            async def inner():
                await ins('drop')
                raise RuntimeError('inner-boom')

            try:
                await store.transaction('nest_real', inner)
            except RuntimeError:
                pass
            await ins('outer')

        await store.transaction('nest_real', outer)

        cur = await db2.execute('SELECT _id FROM nest_t ORDER BY _id')
        rows = [r[0] for r in await cur.fetchall()]
        await cur.close()
        await db.close()
        await db2.close()
        return rows

    assert _run(scenario()) == ['keep', 'outer'], '内层写入回滚，外层写入提交'


# ─── #18 真实 SQLite：嵌套 session 内层回滚、外层提交 ─────────

def test_real_sqlite_nested_session_inner_rollback(tmp_path):
    db_path = tmp_path / 'nest_sess.db'

    async def scenario():
        db = await aiosqlite.connect(db_path)
        db2 = await aiosqlite.connect(db_path)
        await db.execute('CREATE TABLE sess_t (_id TEXT PRIMARY KEY, v TEXT)')
        await db.commit()
        datasource.set_connections({'nest_sess': executors.create_connection('sqlite', db)})

        async def ins(v):
            await store.execute_raw(
                'nest_sess', 'INSERT INTO sess_t (_id, v) VALUES (?, ?)', [v, v], is_write=True)

        async with store.session():
            await ins('keep')

            async def inner_body():
                async with store.session():
                    await ins('inner')
                    raise RuntimeError('inner-boom')

            try:
                await inner_body()
            except RuntimeError:
                pass
            await ins('outer')

        cur = await db2.execute('SELECT _id FROM sess_t ORDER BY _id')
        rows = [r[0] for r in await cur.fetchall()]
        await cur.close()
        await db.close()
        await db2.close()
        return rows

    assert _run(scenario()) == ['keep', 'outer'], '内层会话写入回滚，外层写入提交'


# ─── #19 嵌套 session：首次写开保存点、退出释放、共用外层事务 ──

def test_nested_session_opens_savepoint_on_write():
    desc, state = make_fake_sql_executor()
    _register_sql('SessA', 'sess_a', 'SA')
    datasource.set_connections({'sess_a': desc})

    async def scenario():
        async with store.session() as outer:
            await outer.insert('SessA', {'v': '1'})
            async with store.session() as inner:
                await inner.insert('SessA', {'v': '2'})

    _run(scenario())
    assert state['opened'] == 1, '嵌套会话共用外层事务连接'
    assert [n for _, n in state['savepoints']] == ['sp_1'], '内层作用域首次写才开保存点'
    assert [n for _, n in state['released_sps']] == ['sp_1']
    assert state['rolled_back'] == 0 and state['committed'] == 1


# ─── #20 会话内 transaction：作为嵌套作用域（失败只回滚本层） ──

def test_transaction_inside_session_child_scope_rolls_back():
    desc, state = make_fake_sql_executor()
    _register_sql('SessA', 'sess_a', 'SA')
    datasource.set_connections({'sess_a': desc})

    async def scenario():
        async with store.session() as s:
            await s.insert('SessA', {'v': '1'})

            async def inner():
                await store.insert('SessA', {'v': '2'})
                raise RuntimeError('inner-boom')

            try:
                await store.transaction('sess_a', inner)
            except RuntimeError:
                pass
            await s.insert('SessA', {'v': '3'})

    _run(scenario())
    assert state['opened'] == 1, '会话内 transaction 不另开事务'
    assert [n for _, n in state['savepoints']] == ['sp_1']
    assert [n for _, n in state['rolled_to_sps']] == ['sp_1']
    assert [n for _, n in state['released_sps']] == ['sp_1']
    assert state['rolled_back'] == 0 and state['committed'] == 1
