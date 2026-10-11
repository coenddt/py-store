"""进程内内存数据源（memory）py 宿主单测 —— A1 / A3

镜像 ``nodejs-store/tests/memory-store.test.js``；覆盖执行文档 01 §5 步骤 10：
  - A1 ``connect({'database': …})['kind'] == 'memory'``、``handle_for`` 可调用，并经**真实 CRUD**
       （``init`` + ``store.insert`` / ``store.query``）证明该 kind 端到端可用；
  - A3 同一载体两个不同 ``database``、同名 ``collection`` **互不可见**（消除静默串源）。

运行：PYTHONPATH=src LOCAL_CORE=1 python -m pytest tests/test_memory.py -q
"""

import asyncio

from py_store import datasource as _ds
from py_store import init, store
from py_store import schema as _sc
from py_store.memory import MEMORY_KIND, connect


def _run(coro):
    return asyncio.run(coro)


def _register_test_schemas():
    """conftest 模块夹具重放（清场后恢复本模块 schema）"""
    _sc.register({
        'name': 'MemA1User', 'collection': 'mem_a1_users', 'idPrefix': 'ma1_', 'timestamps': False,
        'fields': {'name': {'type': 'string'}}, 'relations': {},
    })
    _sc.register({
        'name': 'MemA3A', 'collection': 'mem_a3_items', 'idPrefix': 'a3a_', 'timestamps': False,
        'database': 'db_a', 'fields': {'name': {'type': 'string'}}, 'relations': {},
    })
    _sc.register({
        'name': 'MemA3B', 'collection': 'mem_a3_items', 'idPrefix': 'a3b_', 'timestamps': False,
        'database': 'db_b', 'fields': {'name': {'type': 'string'}}, 'relations': {},
    })


# ─── A1：memory kind 可用（描述符 + 真实 CRUD） ───────────────

def test_memory_a1_descriptor():
    conn = connect({'database': 'sales_db'})
    assert MEMORY_KIND == 'memory'
    assert conn['kind'] == 'memory'
    assert conn['database'] == 'sales_db'
    assert callable(conn['handle_for'])
    assert callable(conn['open_transaction'])
    assert callable(conn['with_transaction'])


def test_memory_a1_real_crud():
    try:
        _run(init(connect({'store': {}})))

        async def _main():
            u = await store.insert('MemA1User', {'name': 'Ada'})
            assert str(u['_id']).startswith('ma1_'), '应生成 idPrefix 前缀 _id'
            rows = await store.query('MemA1User{ _id name }')
            assert rows == [{'_id': u['_id'], 'name': 'Ada'}]
            assert await store.count('MemA1User', {}) == 1

        _run(_main())
    finally:
        _ds.set_connections({})


# ─── 事务：快照隔离（对齐 local 载体 test_facade_open_transaction_snapshot） ───

def test_memory_open_transaction_snapshot():
    # 预置「同前缀但跨库分区」键（'txdb/nested/deep'）：验证 io.load 分区守卫（不串源）
    part: dict = {'txdb/nested/deep': [{'_id': 'x'}]}
    conn = connect({'database': 'txdb', 'store': part})

    async def _main():
        # commit：tx 内写先入内存快照，commit 才整体写回分区
        tx = await conn['open_transaction']()
        assert tx['session']['snapshot']() == {}, '跨库分区（nested/deep）不属 txdb 库'
        await tx['handle']['users'].insert_one({'_id': 'u1'})
        assert len(tx['session']['snapshot']()['users']) == 1, 'tx 内快照含新写'
        assert 'txdb/users' not in part, 'commit 前分区不可见（快照隔离）'
        await tx['commit']()
        await tx['release']()
        assert len(part['txdb/users']) == 1, 'commit 后写回分区'

        # 幂等守卫：重复 commit 不重复写回
        guard = await conn['open_transaction']()
        await guard['commit']()
        await guard['commit']()
        await guard['release']()

        # rollback：丢弃快照
        tx2 = await conn['open_transaction']()
        await tx2['handle']['users'].insert_one({'_id': 'u2'})
        await tx2['rollback']()
        await tx2['release']()
        assert len(part['txdb/users']) == 1, 'rollback 丢弃快照'

        # with_transaction 便捷包装（成功路径自动 commit）
        async def _body(_s, t):
            await t['handle']['users'].insert_one({'_id': 'u3'})
            return 'ok'

        out = await conn['with_transaction'](_body)
        assert out == 'ok'
        assert len(part['txdb/users']) == 2, 'with_transaction 提交生效'

        # with_transaction 异常路径：body 抛错 → rollback 后原样上抛（不写回）
        async def _boom(_s, t):
            await t['handle']['users'].insert_one({'_id': 'u5'})
            raise ValueError('boom')

        try:
            await conn['with_transaction'](_boom)
            raise AssertionError('应原样上抛 body 异常')
        except ValueError as e:
            assert str(e) == 'boom'
        assert all(d['_id'] != 'u5' for d in part['txdb/users']), '异常路径回滚（u5 未写回）'
        return out

    assert _run(_main()) == 'ok'


# ─── A3：database 维键空间隔离 ──────────────────────────────

def test_memory_a3_same_collection_distinct_database():
    try:
        # 注入独立分区 dict，隔离进程内默认分区
        _run(init(connect({'store': {}})))

        async def _main():
            await store.insert('MemA3A', {'name': 'fromA'})
            await store.insert('MemA3B', {'name': 'fromB'})

            a = await store.query('MemA3A{ _id name }')
            b = await store.query('MemA3B{ _id name }')

            assert len(a) == 1, 'db_a 分区只含自身文档'
            assert len(b) == 1, 'db_b 分区只含自身文档'
            assert a[0]['name'] == 'fromA'
            assert b[0]['name'] == 'fromB'
            # 同名 collection、不同 database 互不串源
            assert a != b

        _run(_main())
    finally:
        _ds.set_connections({})
