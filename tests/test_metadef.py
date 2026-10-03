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

def _run_in_sqlite(body, tables=('__schemaDef',)):
    """建 sqlite 内存库 → 建内建表（默认 __schemaDef）→ init → 执行业务协程

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
        ddl = str(ddl_mod.generate('sqlite', list(tables)))
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


def _wf_defn(name, gql='Item(){ _id }'):
    """最小合法 workflow defn（注册期白名单通过；gql 内容不参与注册期校验）"""
    return {'name': name, 'steps': [{'op': 'query', 'as': 'a', 'gql': gql}]}


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
        # 回滚到 v1（追加式）→ 以 v1 defn 追加新版本行；本进程 register → 生效 defn = v1
        rb = await md.rollback_to(store, {'tenant': 't1', 'env': 'dev', 'name': 'Item', 'version': 1})
        assert rb['version'] == 3            # 追加式：回滚 = 以 v1 defn 追加 v3（非原地改 v1）
        assert rb['defn'] == v1
        assert store.get('Item')['fields'] == v1['fields']
        assert 'price' not in store.get('Item')['fields']
        # 历史 append-only：目标行未被改写
        hist2 = await md.list_defs(store, {'tenant': 't1', 'env': 'dev', 'name': 'Item'})
        assert [r['version'] for r in hist2] == [3, 2, 1]
        # 跨进程闭环锚：网关 hydrate 读 load_defs → 现返回回滚后的最新行（defn=v1）
        latest2 = await md.load_defs(store, {'tenant': 't1', 'env': 'dev'})
        assert len(latest2) == 1 and latest2[0]['version'] == 3
        assert latest2[0]['defn'] == v1

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


# ─── D2：版本唯一性（行自然键 _id） ────────────────────────────

def test_def_id_natural_key_shape():
    assert md.def_id('t1', 'dev', 'Item', 2) == 't1\u001fdev\u001fItem\u001f2'
    assert md.def_id(None, None, 'Item', 1) == '\u001f\u001fItem\u001f1'


def test_d2_persist_writes_natural_id_and_version_unique():
    """D2：persist 落行带自然键 _id；同版本二次写入 → 存储层唯一键冲突（显式上抛）"""

    async def _run(store):
        defn = {'name': 'UniqItem', 'fields': {'_id': {'type': 'string'}}}
        r1 = await md.persist_def(store, defn, {'tenant': 't2', 'env': 'dev'})
        assert r1['_id'] == md.def_id('t2', 'dev', 'UniqItem', 1)
        # 并发写同版本的最小复现：绕过「读最新行 +1」直插同版本行
        dup = md.build_def_row(defn, {'tenant': 't2', 'env': 'dev'}, 1)
        dup['_id'] = md.def_id('t2', 'dev', 'UniqItem', 1)
        with pytest.raises(Exception) as ei:
            with md._internal_ctx():
                await store.insert('__schemaDef', dup)
        assert 'unique' in str(ei.value).lower()

    _run_in_sqlite(_run)


# ─── D1：从持久化定义重建注册表（发布闭环桥） ────────────────

def test_d1_restore_defs_rebuilds_registry():
    """D1：persist 只落库不注册；restore_defs 从库重建注册表（同版本幂等）"""

    async def _run(store):
        md._applied.clear()
        defn = {'name': 'RestoredItem',
                'fields': {'_id': {'type': 'string'}, 'title': {'type': 'string'}}}
        await md.persist_def(store, defn, {'tenant': 't3', 'env': 'dev'})
        assert not store.has('RestoredItem')  # 原缺口：落库后协议面不可见
        out = await md.restore_defs(store, {'tenant': 't3', 'env': 'dev'})
        assert out['applied'] == 1
        assert store.has('RestoredItem')      # 重建后即可见
        out2 = await md.restore_defs(store, {'tenant': 't3', 'env': 'dev'})
        assert out2['applied'] == 0           # 同版本幂等：不重复注册

    _run_in_sqlite(_run)


# ─── D21：回滚跨进程闭环（rollback → reload hydrate → 按历史 defn 装配） ────

def test_d21_rollback_visible_via_restore_defs():
    """D21：rollback 以历史 defn 追加新版本 → restore_defs（网关 hydrate 路径）按历史 defn 装配

    复现原缺口：若回滚只原地重注册、不落新行，load_defs 仍返回被回滚掉的旧版本，
    网关 hydrate 后协议面仍是旧版本 —— 跨进程回滚不闭环。追加式回滚后 load_defs 返回
    回滚后的新行（新自然键），restore_defs 必然 applied。
    """

    async def _run(store):
        md._applied.clear()
        v1 = {'name': 'RbItem', 'fields': {'_id': {'type': 'string'}, 'title': {'type': 'string'}}}
        v2 = {'name': 'RbItem', 'fields': {'_id': {'type': 'string'}, 'title': {'type': 'string'},
                                           'price': {'type': 'number'}}}
        o = {'tenant': 't5', 'env': 'dev'}
        await md.persist_def(store, v1, o)
        await md.restore_defs(store, o)                       # 网关首次 hydrate → v1
        await md.persist_def(store, v2, o)
        await md.restore_defs(store, o)                       # 网关再次 hydrate → v2
        assert 'price' in store.get('RbItem')['fields']
        # 控制面回滚到 v1
        await md.rollback_to(store, {'tenant': 't5', 'env': 'dev', 'name': 'RbItem', 'version': 1})
        # 网关新一次 reload：load_defs 应返回回滚后的最新行（defn=v1），新键 → applied
        latest = await md.load_defs(store, o)
        assert len(latest) == 1 and latest[0]['version'] == 3 and latest[0]['defn'] == v1
        out = await md.restore_defs(store, o)
        assert out['applied'] == 1
        assert 'price' not in store.get('RbItem')['fields']   # 协议面按历史 defn 装配

    _run_in_sqlite(_run)


# ─── workflow 定义持久化（kind=workflow，落 __workflowDef；审计 §8 N1）───────

def test_wf_persist_idempotent_versioning_and_list():
    """workflow：persist 同名同形幂等 + 异形 version+1 + list version desc"""

    async def _run(store):
        o = {'tenant': 't6', 'env': 'dev', 'kind': 'workflow'}
        w1 = _wf_defn('WfPersist')
        w2 = _wf_defn('WfPersist', 'Item(){ _id title }')
        r1 = await md.persist_def(store, w1, o)
        assert r1['version'] == 1
        assert (await md.persist_def(store, w1, o))['version'] == 1   # 同名同形幂等
        r2 = await md.persist_def(store, w2, o)
        assert r2['version'] == 2                                     # 异形 version+1
        rows = await md.list_defs(store, {'tenant': 't6', 'env': 'dev',
                                          'name': 'WfPersist', 'kind': 'workflow'})
        assert [r['version'] for r in rows] == [2, 1]                 # version desc
        assert rows[0]['_id'] == md.def_id('t6', 'dev', 'WfPersist', 2)

    _run_in_sqlite(_run, ('__workflowDef',))


def test_wf_load_defs_latest_active():
    """workflow：load_defs 取各 name 最新 active"""

    async def _run(store):
        o = {'tenant': 't7', 'env': 'dev', 'kind': 'workflow'}
        await md.persist_def(store, _wf_defn('WfLoad'), o)
        await md.persist_def(store, _wf_defn('WfLoad', 'Item(){ _id title }'), o)
        await md.persist_def(store, {'name': 'WfOther', 'steps': [{'op': 'fail', 'message': 'x'}]}, o)
        rows = await md.load_defs(store, o)
        assert {r['name']: r['version'] for r in rows} == {'WfLoad': 2, 'WfOther': 1}

    _run_in_sqlite(_run, ('__workflowDef',))


def test_wf_restore_defs_rebuilds_workflow_registry():
    """workflow：restore_defs(kind=workflow) 重建 workflow 注册表（幂等）"""

    async def _run(store):
        md._applied.clear()
        o = {'tenant': 't8', 'env': 'dev'}
        await md.persist_def(store, _wf_defn('WfRestore'), {**o, 'kind': 'workflow'})
        assert 'WfRestore' not in store.workflows()          # 落库未注册
        out = await md.restore_defs(store, {**o, 'kind': 'workflow'})
        assert out['applied'] == 1
        assert 'WfRestore' in store.workflows()              # 重建后可见
        assert (await md.restore_defs(store, {**o, 'kind': 'workflow'}))['applied'] == 0

    _run_in_sqlite(_run, ('__workflowDef',))


def test_wf_rollback_append_only_and_reload():
    """workflow：rollback 追加式 → load_defs 按历史 defn，本进程重注册"""

    async def _run(store):
        md._applied.clear()
        o = {'tenant': 't10', 'env': 'dev', 'kind': 'workflow'}
        v1 = _wf_defn('WfRb')
        v2 = _wf_defn('WfRb', 'Item(){ _id title }')
        await md.persist_def(store, v1, o)
        await md.persist_def(store, v2, o)
        rb = await md.rollback_to(store, {'tenant': 't10', 'env': 'dev',
                                          'name': 'WfRb', 'version': 1, 'kind': 'workflow'})
        assert rb['version'] == 3 and rb['defn'] == v1       # 追加式
        latest = await md.load_defs(store, o)
        assert latest[0]['version'] == 3 and latest[0]['defn'] == v1
        assert store.get_workflow('WfRb')['steps'][0]['gql'] == 'Item(){ _id }'  # 本进程重注册

    _run_in_sqlite(_run, ('__workflowDef',))


def test_a3_host_restore_defs_both_kinds():
    """宿主 restore_defs 同时重建 schema 与 workflow 两类（一次调用闭环）"""

    async def _run(store):
        md._applied.clear()
        o = {'tenant': 't9', 'env': 'dev'}
        await store.persist_def({'name': 'HostItem', 'fields': {'_id': {'type': 'string'}}}, o)
        await store.persist_workflow_def(_wf_defn('HostWf'), o)
        assert not store.has('HostItem')
        assert 'HostWf' not in store.workflows()
        out = await store.restore_defs(o)
        assert out['applied'] == 2                            # 一次调用重建两类
        assert store.has('HostItem')
        assert 'HostWf' in store.workflows()

    _run_in_sqlite(_run, ('__schemaDef', '__workflowDef'))
