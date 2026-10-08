"""Schema 触发器真库 e2e（02 步骤 8；镜像 nodejs-store/tests/triggers.test.js）

覆盖总纲 A2/A3/A4/A5/A6/A8/A9 + 回调式触发。全程只走 store 统一入口：
register(triggers) → insert/update → core 规划（plan 附 triggers）→ Host 触发链
执行器（占位符/命中判定/去重）→ run_atomic 单源真事务 / 跨源 non_atomic_write 声明。
真库 = aiosqlite 内存库。

运行：LOCAL_CORE=1 python -m pytest tests/test_triggers.py -q
"""

import asyncio

import aiosqlite
import pytest

from py_store import executors, feedback, init, permission, store
from py_store import schema as _sc
from py_store.crud.exec import ProfileViolation
from py_store.crud.triggers import resolve_trigger_placeholders

SRC_A = 'trg_a'
SRC_B = 'trg_b'

_DDL = """
CREATE TABLE t_order (_id TEXT PRIMARY KEY, amount REAL, status TEXT, note TEXT, __present TEXT);
CREATE TABLE t_order_deleted (_id TEXT PRIMARY KEY, amount REAL, status TEXT, note TEXT,
    deleted_at INTEGER, __present TEXT);
CREATE TABLE t_audit (_id TEXT PRIMARY KEY, kind TEXT, ref TEXT, amount REAL, __present TEXT);
CREATE TABLE t_audit_deleted (_id TEXT PRIMARY KEY, kind TEXT, ref TEXT, amount REAL,
    deleted_at INTEGER, __present TEXT);
CREATE TABLE t_boom (_id TEXT PRIMARY KEY, __present TEXT);
CREATE TABLE t_boom_deleted (_id TEXT PRIMARY KEY, deleted_at INTEGER, __present TEXT);
CREATE TABLE t_ghost (_id TEXT PRIMARY KEY, __present TEXT);
CREATE TABLE t_ghost_deleted (_id TEXT PRIMARY KEY, deleted_at INTEGER, __present TEXT);
CREATE TABLE t_cmd_fail (_id TEXT PRIMARY KEY, amount REAL, __present TEXT);
CREATE TABLE t_cmd_fail_deleted (_id TEXT PRIMARY KEY, amount REAL, deleted_at INTEGER,
    __present TEXT);
CREATE TABLE t_x_order (_id TEXT PRIMARY KEY, amount REAL, __present TEXT);
CREATE TABLE t_x_order_deleted (_id TEXT PRIMARY KEY, amount REAL, deleted_at INTEGER,
    __present TEXT);
CREATE TABLE t_x_audit (_id TEXT PRIMARY KEY, ref TEXT, __present TEXT);
CREATE TABLE t_x_audit_deleted (_id TEXT PRIMARY KEY, ref TEXT, deleted_at INTEGER,
    __present TEXT);
"""


def _register_test_schemas():
    """测试 schema 注册（conftest 模块夹具重放；triggers 契约见总纲 T 字段表）"""
    _sc.register({
        'name': 'TOrder', 'collection': 'tOrder', 'idPrefix': 'o_', 'timestamps': False,
        'datasource': SRC_A,
        'fields': {'amount': {'type': 'number'}, 'status': {'type': 'string'},
                   'note': {'type': 'string'}},
        'triggers': {
            'insert': [
                # A8：aud_cmd 重复同名 —— 同事务 (name, _id) 去重，只执行一次
                # （若不去重，第二次同 _id insertOne 主键冲突 → 整体失败）
                {'name': 'aud_cmd', 'into': 'TAudit', 'op': 'insert',
                 'data': {'_id': '{{root._id}}', 'kind': 'cmd', 'ref': '{{root._id}}',
                          'amount': '{{root.amount}}'}},
                {'name': 'aud_cmd', 'into': 'TAudit', 'op': 'insert',
                 'data': {'_id': '{{root._id}}', 'kind': 'cmd', 'ref': '{{root._id}}',
                          'amount': '{{root.amount}}'}},
                {'name': 'aud_cb', 'fnRef': 'audRecorder', 'args': {'ref': '{{root._id}}'}},
            ],
            'update': [
                {'name': 'aud_upd', 'onFields': ['amount'], 'into': 'TAudit', 'op': 'insert',
                 'data': {'_id': '{{now}}', 'kind': 'upd', 'ref': '{{root._id}}',
                          'amount': '{{root.amount}}'}},
            ],
            'remove': [
                # 命令式 op:"remove"：删 t_audit 中 ref = 被删文档 _id 的行
                {'name': 'aud_del', 'into': 'TAudit', 'op': 'remove',
                 'condition': {'ref': '{{before._id}}'}},
                # 回调式：before 占位符（被删文档删除前值）
                {'name': 'del_cb', 'fnRef': 'delRecorder',
                 'args': {'id': '{{before._id}}', 'amount': '{{before.amount}}'}},
            ],
        },
    })
    _sc.register({
        'name': 'TAudit', 'collection': 'tAudit', 'idPrefix': 'a_', 'timestamps': False,
        'datasource': SRC_A,
        'fields': {'kind': {'type': 'string'}, 'ref': {'type': 'string'},
                   'amount': {'type': 'number'}},
    })
    _sc.register({
        'name': 'TBoom', 'collection': 'tBoom', 'idPrefix': 'b_', 'timestamps': False,
        'datasource': SRC_A, 'fields': {},
        'triggers': {
            'insert': [{'name': 'boom', 'fnRef': 'boom'}],
            # remove 触发回调失败 → 主删除整体回滚
            'remove': [{'name': 'boomDel', 'fnRef': 'boom'}],
        },
    })
    _sc.register({
        'name': 'TGhost', 'collection': 'tGhost', 'idPrefix': 'g_', 'timestamps': False,
        'datasource': SRC_A, 'fields': {},
        'triggers': {'insert': [{'name': 'ghost', 'fnRef': 'ghost'}]},
    })
    _sc.register({
        'name': 'TCmdFail', 'collection': 'tCmdFail', 'idPrefix': 'cf_', 'timestamps': False,
        'datasource': SRC_A, 'fields': {'amount': {'type': 'number'}},
        # A5：命令式触发写失败（data._id 固定字面量，与预置行冲突）
        'triggers': {
            'insert': [{'name': 'cf_aud', 'into': 'TAudit', 'op': 'insert',
                        'data': {'_id': 'fixed-collision', 'kind': 'cf', 'ref': '{{root._id}}'}}],
        },
    })
    _sc.register({
        'name': 'TXOrder', 'collection': 'tXOrder', 'idPrefix': 'x_', 'timestamps': False,
        'datasource': SRC_A, 'fields': {'amount': {'type': 'number'}},
        # A6：跨源触发链（目标在 SRC_B）
        'triggers': {
            'insert': [{'name': 'x_aud', 'into': 'TXAudit', 'op': 'insert',
                        'data': {'_id': '{{root._id}}', 'ref': '{{root._id}}'}}],
        },
    })
    _sc.register({
        'name': 'TXAudit', 'collection': 'tXAudit', 'idPrefix': 'xa_', 'timestamps': False,
        'datasource': SRC_B, 'fields': {'ref': {'type': 'string'}},
    })


# ─── 回调实现注入（启动期一次；运行期计数在用例内自管） ─────────

cb_calls = []
del_calls = []


async def _aud_recorder(args, ctx, host):
    cb_calls.append(args)
    await host['store'].insert('TAudit', {'kind': 'cb', 'ref': args['ref'], 'amount': 0})


async def _del_recorder(args, ctx, host):
    del_calls.append(args)


async def _boom(args, ctx, host):
    raise RuntimeError('boom-err')


store.set_trigger_fn('audRecorder', _aud_recorder)
store.set_trigger_fn('delRecorder', _del_recorder)
store.set_trigger_fn('boom', _boom)


def _run(coro):
    return asyncio.run(coro)


async def _setup():
    """环境复位（node beforeEach 镜像）：sink / ctx / 档位 / 双源内存库"""
    events.clear()
    cb_calls.clear()
    del_calls.clear()
    feedback.set_sink(events.append)
    permission.set_context(None)
    store.set_profile('standard')
    db_a = await aiosqlite.connect(':memory:')
    db_b = await aiosqlite.connect(':memory:')
    db_a.row_factory = aiosqlite.Row
    db_b.row_factory = aiosqlite.Row
    await db_a.executescript(_DDL)
    await db_b.executescript(_DDL)
    await init({
        SRC_A: executors.create_connection('sqlite', db_a),
        SRC_B: executors.create_connection('sqlite', db_b),
    })
    return db_a, db_b


async def _fetchall(db, sql):
    cur = await db.execute(sql)
    rows = await cur.fetchall()
    await cur.close()
    return rows


async def _count(db, sql):
    rows = await _fetchall(db, sql)
    return rows[0][0]


events = []


# ─── 用例 ────────────────────────────────────────────────────

def test_a2_a3_insert_command_and_callback():
    """A2/A3 insert 触发链：命令式 + 回调式依序执行，占位符整值替换"""

    async def scenario():
        db_a, _ = await _setup()
        doc = await store.insert('TOrder', {'amount': 100, 'status': 'new', 'note': ''})
        rows = await _fetchall(db_a, 'SELECT _id, kind, ref, amount FROM t_audit ORDER BY kind')
        await db_a.close()
        return doc, rows

    doc, rows = _run(scenario())
    # aud_cmd（与重复同名去重后一次）+ aud_cb
    assert [r['kind'] for r in rows] == ['cb', 'cmd']
    cmd = next(r for r in rows if r['kind'] == 'cmd')
    assert cmd['_id'] == doc['_id'], '{{root._id}} 整值替换'
    assert cmd['ref'] == doc['_id']
    assert cmd['amount'] == 100, '{{root.amount}} 整值替换'
    assert len(cb_calls) == 1, '回调执行一次'
    assert cb_calls[0]['ref'] == doc['_id'], '回调 args 占位符替换'


def test_a3_a4_update_on_fields_hit_and_miss():
    """A3/A4 update onFields 命中才触发；未命中（值未变/字段不在 onFields）不触发"""

    async def scenario():
        db_a, _ = await _setup()
        doc = await store.insert('TOrder', {'amount': 100, 'status': 'new', 'note': ''})

        upd_count = lambda: _count(  # noqa: E731
            db_a, "SELECT COUNT(*) FROM t_audit WHERE kind = 'upd'")
        # 未命中：字段不在 onFields
        await store.update('TOrder', {'_id': doc['_id']}, {'note': 'x'})
        n_miss_field = await upd_count()
        # 未命中：值未变（no-op 抑制）
        await store.update('TOrder', {'_id': doc['_id']}, {'amount': 100})
        n_miss_value = await upd_count()
        # 命中：amount 真变
        updated = await store.update('TOrder', {'_id': doc['_id']}, {'amount': 200})
        rows = await _fetchall(db_a, "SELECT ref, amount FROM t_audit WHERE kind = 'upd'")
        await db_a.close()
        return n_miss_field, n_miss_value, updated, rows, doc

    n_miss_field, n_miss_value, updated, rows, doc = _run(scenario())
    assert n_miss_field == 0, 'onFields 外的字段变化不触发'
    assert n_miss_value == 0, 'onFields 字段值未变不触发'
    assert updated['amount'] == 200
    assert len(rows) == 1
    assert rows[0]['ref'] == doc['_id'], '{{root._id}}'
    assert rows[0]['amount'] == 200, '{{root.amount}} = 主写后的新值'


def test_a5_rollback_callback_failure():
    """A5 单源回滚：触发回调失败 → 主写整体回滚"""

    async def scenario():
        db_a, _ = await _setup()
        with pytest.raises(RuntimeError, match='boom-err'):
            await store.insert('TBoom', {})
        n = await _count(db_a, 'SELECT COUNT(*) FROM t_boom')
        await db_a.close()
        return n

    assert _run(scenario()) == 0, '主写已回滚'


def test_a5_rollback_command_failure():
    """A5 单源回滚：命令式触发写失败 → 主写整体回滚"""

    async def scenario():
        db_a, _ = await _setup()
        # 预置同 _id 审计行 → 触发 insertOne 主键冲突 → 触发链失败 → 主写回滚
        await db_a.execute(
            "INSERT INTO t_audit (_id, kind, ref, amount) VALUES"
            " ('fixed-collision', 'seed', '', 0)")
        await db_a.commit()
        with pytest.raises(Exception, match='UNIQUE constraint failed'):
            await store.insert('TCmdFail', {'amount': 1})
        n = await _count(db_a, 'SELECT COUNT(*) FROM t_cmd_fail')
        await db_a.close()
        return n

    assert _run(scenario()) == 0, '主写已回滚'


def test_a6_cross_source_non_atomic():
    """A6 跨源触发链：发 non_atomic_write（含涉及源）并顺序执行落库"""

    async def scenario():
        db_a, db_b = await _setup()
        doc = await store.insert('TXOrder', {'amount': 1})
        rows = await _fetchall(db_b, f'SELECT COUNT(*) FROM t_x_audit WHERE ref = {doc["_id"]!r}')
        await db_a.close()
        await db_b.close()
        return rows[0][0]

    n = _run(scenario())
    listed = [e for e in events if e.get('type') == 'non_atomic_write']
    assert len(listed) == 1, '恰发一次 non_atomic_write 声明'
    assert listed[0]['sources'] == sorted([SRC_A, SRC_B]), '声明含全部涉及源'
    assert n == 1, '跨源触发写已落库（顺序执行，非原子）'


def test_a6_session_cross_source_fail_closed():
    """A6 会话内跨源触发 fail-closed：NonAtomicWriteError 且全部回滚"""

    async def scenario():
        from py_store import NonAtomicWriteError
        db_a, db_b = await _setup()
        try:
            async with store.session() as s:
                await s.insert('TXOrder', {'amount': 1})
        except NonAtomicWriteError:
            pass
        else:
            raise AssertionError('应抛 NonAtomicWriteError')
        n_order = await _count(db_a, 'SELECT COUNT(*) FROM t_x_order')
        n_audit = await _count(db_b, 'SELECT COUNT(*) FROM t_x_audit')
        await db_a.close()
        await db_b.close()
        return n_order, n_audit

    n_order, n_audit = _run(scenario())
    assert n_order == 0, '主写已回滚'
    assert n_audit == 0, '触发写已回滚'


def test_a8_same_transaction_dedupe():
    """A8 同事务去重：同名触发只执行一次（若不去重将主键冲突整体失败）"""

    async def scenario():
        db_a, _ = await _setup()
        doc = await store.insert('TOrder', {'amount': 7, 'status': 'new', 'note': ''})
        n = await _count(
            db_a, f"SELECT COUNT(*) FROM t_audit WHERE kind = 'cmd' AND _id = {doc['_id']!r}")
        await db_a.close()
        return n

    assert _run(scenario()) == 1, '同名 (aud_cmd) 触发去重后只执行一次'


def test_a9_text2query_rejects_triggers():
    """A9 text2query 档：触发器显式拒绝（ProfileViolation + profile_blocked 反馈）"""

    async def scenario():
        await _setup()
        store.set_profile('text2query')
        with pytest.raises(ProfileViolation):
            await store.insert('TOrder', {'amount': 1, 'status': 'new', 'note': ''})

    _run(scenario())
    blocked = [e for e in events if e.get('type') == 'profile_blocked']
    assert len(blocked) == 1, '发 profile_blocked 反馈事件'


def test_trigger_fn_missing_start_and_runtime():
    """回调缺实现：启动期 assert_trigger_fns_covered 与运行期均显式报 ERR_TRIGGER_FN_MISSING"""
    with pytest.raises(RuntimeError, match='ERR_TRIGGER_FN_MISSING'):
        store.assert_trigger_fns_covered(
            [{'name': 'X', 'triggers': {'insert': [{'fnRef': 'ghost'}]}}])

    async def scenario():
        await _setup()
        await store.insert('TGhost', {})

    with pytest.raises(RuntimeError, match='ERR_TRIGGER_FN_MISSING'):
        _run(scenario())


def test_placeholder_embed_is_err():
    """占位符内嵌拼接：显式报 ERR_TRIGGER_PLACEHOLDER（禁静默漂移）"""
    scope = {'root': {'_id': 'x'}, 'before': None, 'now': 1}
    with pytest.raises(ValueError, match='ERR_TRIGGER_PLACEHOLDER'):
        resolve_trigger_placeholders('order-{{root._id}}', scope)
    with pytest.raises(ValueError, match='ERR_TRIGGER_PLACEHOLDER'):
        resolve_trigger_placeholders('a{{now}}b', scope)
    # 引用缺失字段 = 整值替换语义（取值缺失 → None），不抛
    assert resolve_trigger_placeholders('{{root.missing}}', scope) is None


# ─── remove 触发链（总纲 A5） ─────────────────────────────────

def test_a5_remove_command_and_callback():
    """A5 remove 触发链：命令式 op:"remove" + 回调式执行，before = 删除前文档"""

    async def scenario():
        db_a, _ = await _setup()
        doc = await store.insert('TOrder', {'amount': 100, 'status': 'new', 'note': ''})
        # 预置一条审计行（ref = 被删文档 _id）供命令式 remove 圈定删除
        await db_a.execute(
            "INSERT INTO t_audit (_id, kind, ref, amount) VALUES ('a_seed', 'seed', ?, 5)",
            (doc['_id'],))
        await db_a.commit()
        ret = await store.remove('TOrder', {'_id': doc['_id']})
        n_order = await _count(db_a, f"SELECT COUNT(*) FROM t_order WHERE _id = {doc['_id']!r}")
        n_arch = await _count(
            db_a, f"SELECT COUNT(*) FROM t_order_deleted WHERE _id = {doc['_id']!r}")
        n_audit = await _count(db_a, f"SELECT COUNT(*) FROM t_audit WHERE ref = {doc['_id']!r}")
        await db_a.close()
        return ret, n_order, n_arch, n_audit, doc

    ret, n_order, n_arch, n_audit, doc = _run(scenario())
    assert ret['deletedCount'] == 1
    assert ret['archivedCount'] == 1
    assert n_order == 0, '主表已删'
    assert n_arch == 1, '归档表有被删文档'
    assert n_audit == 0, '命令式 op:"remove" 触发删目标行'
    assert len(del_calls) == 1, 'remove 回调执行一次'
    assert del_calls[0]['id'] == doc['_id'], '{{before._id}}'
    assert del_calls[0]['amount'] == 100, '{{before.amount}} = 删除前值'


def test_a5_remove_zero_hit_no_trigger():
    """A5 remove 0 命中：不触发（与 update 0 行命中语义一致）"""

    async def scenario():
        db_a, _ = await _setup()
        ret = await store.remove('TOrder', {'_id': 'no_such_id'})
        await db_a.close()
        return ret

    ret = _run(scenario())
    assert ret['deletedCount'] == 0
    assert ret['archivedCount'] == 0
    assert len(del_calls) == 0, '未删到不触发'


def test_a5_remove_rollback_callback_failure():
    """A5 单源回滚：remove 触发回调失败 → 主删除整体回滚"""

    async def scenario():
        db_a, _ = await _setup()
        # 直接预置行（TBoom 的 insert 触发器本身抛错，不能走 store.insert）
        await db_a.execute("INSERT INTO t_boom (_id) VALUES ('b_seed')")
        await db_a.commit()
        with pytest.raises(RuntimeError, match='boom-err'):
            await store.remove('TBoom', {'_id': 'b_seed'})
        n = await _count(db_a, "SELECT COUNT(*) FROM t_boom WHERE _id = 'b_seed'")
        await db_a.close()
        return n

    assert _run(scenario()) == 1, '主删除已回滚'


def test_a5_remove_without_triggers_unchanged():
    """A5 未声明 remove 触发器：删+归档行为与改动前一致（零回归）"""

    async def scenario():
        db_a, _ = await _setup()
        doc = await store.insert('TAudit', {'kind': 'plain', 'ref': 'r1', 'amount': 1})
        ret = await store.remove('TAudit', {'_id': doc['_id']})
        n_live = await _count(db_a, f"SELECT COUNT(*) FROM t_audit WHERE _id = {doc['_id']!r}")
        n_arch = await _count(
            db_a, f"SELECT COUNT(*) FROM t_audit_deleted WHERE _id = {doc['_id']!r}")
        await db_a.close()
        return ret, n_live, n_arch

    ret, n_live, n_arch = _run(scenario())
    assert ret['deletedCount'] == 1
    assert ret['archivedCount'] == 1
    assert n_live == 0
    assert n_arch == 1
