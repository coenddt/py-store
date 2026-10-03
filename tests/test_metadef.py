"""meta-store 定义持久化与版本化单测：A1（落库幂等）/ A2（版本化与回滚）+ 纯函数锚。

parity 锚：纯函数输出与 nodejs-store 侧同输入同输出（对拍脚本 + metadef.js 守护）。
运行：$env:LOCAL_CORE='1'; $env:PYTHONPATH='py-store/src'; python -m pytest py-store/tests/test_metadef.py -q
"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from py_store import metadef as md
from py_store.metadef import MetaDefError


def _register_test_schemas():
    """conftest 清场后恢复内建定义表（幂等自举）"""
    md.ensure_builtins()


# ─── 纯函数（双端逐字节等价锚） ────────────────────────────────

def test_same_defn_key_order_insensitive():
    assert md.same_defn({'a': 1, 'b': 2}, {'b': 2, 'a': 1})
    assert not md.same_defn({'a': 1}, {'a': 2})
    assert md.same_defn({'x': [1, {'k': 'v'}]}, {'x': [1, {'k': 'v'}]})


def test_next_version():
    assert md.next_version([]) == 1
    assert md.next_version([{'version': 1}, {'version': 3}]) == 4


def test_build_def_row_shape_and_key_order():
    row = md.build_def_row({'name': 'Item'}, {'tenant': 't1', 'env': 'dev', 'actor': 'u1'}, 2)
    assert row == {'tenant': 't1', 'env': 'dev', 'name': 'Item', 'version': 2,
                   'defn': {'name': 'Item'}, 'status': 'active', 'createdBy': 'u1'}
    # 键序与 node buildDefRow 一致（对拍逐字节）
    assert list(row) == ['tenant', 'env', 'name', 'version', 'defn', 'status', 'createdBy']


def test_build_def_row_actor_defaults_empty():
    row = md.build_def_row({'name': 'Item'}, {'tenant': 't', 'env': 'e'}, 1)
    assert row['createdBy'] == ''


# ─── A1 / A2：真实 sqlite 落库闭环 ─────────────────────────────

def _run_in_sqlite(body):
    """建 sqlite 内存库 → 建 __schemaDef 表 → init → 执行业务协程

    进程级连接映射是全局单例：用前快照、用后还原，避免把「已关闭的库」留给后续测试文件
    （对齐 conftest 的注册表隔离思路）。
    """

    async def _main():
        import aiosqlite

        from py_store import datasource as _ds
        from py_store import ddl as ddl_mod
        from py_store import executors, init, store
        md.ensure_builtins()
        prev = dict(_ds._connections)
        db = await aiosqlite.connect(':memory:')
        ddl = str(ddl_mod.generate('sqlite', ['__schemaDef']))
        for stmt in ddl.split('\n\n'):
            if stmt.strip():
                await db.execute(stmt.strip())
        await db.commit()
        await init({'default': executors.create_connection('sqlite', db)})
        try:
            return await body(store)
        finally:
            await db.close()
            _ds._connections = prev

    return asyncio.new_event_loop().run_until_complete(_main())


def test_a1_persist_one_row_and_idempotent():
    """A1：register 后 __schemaDef 出现 1 行（五要素）；同名同形重复不新增行"""

    async def _run(store):
        defn = {'name': 'Item', 'fields': {'_id': {'type': 'string'}, 'title': {'type': 'string'}}}
        r1 = await md.persist_def(store, defn, {'tenant': 't1', 'env': 'dev', 'actor': 'u1'})
        # 五要素 (tenant, env, name, version, defn)
        assert r1['tenant'] == 't1' and r1['env'] == 'dev' and r1['name'] == 'Item'
        assert r1['version'] == 1 and r1['defn'] == defn
        assert r1['status'] == 'active' and r1['createdBy'] == 'u1'
        rows = await md.list_defs(store, {'tenant': 't1', 'env': 'dev', 'name': 'Item'})
        assert len(rows) == 1
        # 同名同形重复注册 → 返回原行、不新增
        r2 = await md.persist_def(store, defn, {'tenant': 't1', 'env': 'dev', 'actor': 'u2'})
        assert r2['_id'] == r1['_id'] and r2['version'] == 1
        rows2 = await md.list_defs(store, {'tenant': 't1', 'env': 'dev', 'name': 'Item'})
        assert len(rows2) == 1

    _run_in_sqlite(_run)


def test_a2_versioning_history_and_rollback():
    """A2：变更 defn → 新增 version+1；可列全部历史；rollbackTo(v) 后生效 defn = 历史 v"""

    async def _run(store):
        v1 = {'name': 'Item', 'fields': {'_id': {'type': 'string'}, 'title': {'type': 'string'}}}
        v2 = {'name': 'Item', 'fields': {'_id': {'type': 'string'}, 'title': {'type': 'string'},
                                         'price': {'type': 'number'}}}
        r1 = await md.persist_def(store, v1, {'tenant': 't1', 'env': 'dev'})
        r2 = await md.persist_def(store, v2, {'tenant': 't1', 'env': 'dev'})
        assert r1['version'] == 1 and r2['version'] == 2
        hist = await md.list_defs(store, {'tenant': 't1', 'env': 'dev', 'name': 'Item'})
        assert [r['version'] for r in hist] == [2, 1]  # version desc
        # loadDefs：各 name 最新 active
        latest = await md.load_defs(store, {'tenant': 't1', 'env': 'dev'})
        assert len(latest) == 1 and latest[0]['version'] == 2
        # 回滚到 v1 → 重新 register → 生效 defn = v1
        rb = await md.rollback_to(store, {'tenant': 't1', 'env': 'dev', 'name': 'Item', 'version': 1})
        assert rb['version'] == 1
        assert store.get('Item')['fields'] == v1['fields']
        assert 'price' not in store.get('Item')['fields']

    _run_in_sqlite(_run)


def test_rollback_missing_version_raises():
    async def _run(store):
        with pytest.raises(MetaDefError):
            await md.rollback_to(store, {'tenant': 't1', 'env': 'dev', 'name': 'Ghost', 'version': 9})

    _run_in_sqlite(_run)


def test_persist_missing_name_raises():
    async def _run(store):
        with pytest.raises(MetaDefError):
            await md.persist_def(store, {'fields': {}}, {'tenant': 't1', 'env': 'dev'})

    _run_in_sqlite(_run)
