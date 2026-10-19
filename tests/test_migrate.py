"""迁移编排器（C3/A4）测试：SQLite 真库（aiosqlite 内存库）

对拍 nodejs-store/tests/migrate.test.js；执行文档 04 §4.4 四用例 + old_defs 显式传入补充用例。
运行：pytest tests/test_migrate.py -x
"""

import asyncio

import aiosqlite
import pytest

from py_store import metadef as md
from py_store.migrate import MigrationUnsupported, _UNSUPPORTED_HINT


def _note_v1():
    return {
        'name': 'Note', 'collection': 'notes', 'idPrefix': 'n', 'timestamps': True,
        'fields': {'_id': {'type': 'string'}, 'title': {'type': 'string'}},
    }


def _run_in_sqlite(body):
    """建 sqlite 内存库 → init → 执行业务协程（body 为 async def(store, db)）

    进程级连接映射是全局单例：用前快照、用后还原（对齐 tests/test_metadef.py harness）。
    """

    async def _main():
        from py_store import datasource as _ds
        from py_store import executors, init, store
        md.ensure_builtins()
        prev = dict(_ds._connections)
        db = await aiosqlite.connect(':memory:')
        await init({'default': executors.create_connection('sqlite', db)})
        try:
            return await body(store, db)
        finally:
            await db.close()
            _ds._connections = prev

    return asyncio.new_event_loop().run_until_complete(_main())


async def _tables(db):
    cur = await db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    try:
        return [r[0] for r in await cur.fetchall()]
    finally:
        await cur.close()


async def _cols(db, table):
    cur = await db.execute(f"SELECT name FROM pragma_table_info('{table}')")
    try:
        return [r[0] for r in await cur.fetchall()]
    finally:
        await cur.close()


async def _version_rows(db):
    cur = await db.execute(
        'SELECT tenant, env, backend, name, version FROM __schemaMigration ORDER BY version')
    try:
        return [dict(zip(('tenant', 'env', 'backend', 'name', 'version'), r))
                for r in await cur.fetchall()]
    finally:
        await cur.close()


def test_add_table_old_defs_none_applies_and_records():
    """① 加表：old_defs=None（introspect 探测）→ applied 且版本行落库、物理表存在"""

    async def body(store, db):
        r = await store.migrate(backend='sqlite', source='default',
                                defs={'Note': _note_v1()}, driver=db)
        assert r == {'applied': [{'name': 'Note', 'from': 0, 'to': 1, 'version': 1}],
                     'skipped': []}
        tables = await _tables(db)
        assert 'notes' in tables
        assert '__schemaMigration' in tables
        rows = await _version_rows(db)
        assert len(rows) == 1
        assert rows[0]['backend'] == 'sqlite'
        assert rows[0]['name'] == 'Note'
        assert rows[0]['version'] == 1

    _run_in_sqlite(body)


def test_add_column_applies_then_rerun_skips_unchanged():
    """② 加列 applied → 重跑同 defs skipped unchanged，物理列落库"""

    async def body(store, db):
        v1 = _note_v1()
        await store.migrate(backend='sqlite', source='default', defs={'Note': v1}, driver=db)
        v2 = {**v1, 'fields': {**v1['fields'], 'viewCount': {'type': 'int'}}}
        r2 = await store.migrate(backend='sqlite', source='default',
                                 defs={'Note': v2}, driver=db)
        assert r2['applied'] == [{'name': 'Note', 'from': 1, 'to': 2, 'version': 2}]
        cols = await _cols(db, 'notes')
        assert 'view_count' in cols
        r3 = await store.migrate(backend='sqlite', source='default',
                                 defs={'Note': v2}, driver=db)
        assert r3 == {'applied': [], 'skipped': [{'name': 'Note', 'reason': 'unchanged'}]}
        rows = await _version_rows(db)
        assert len(rows) == 2

    _run_in_sqlite(body)


def test_drop_field_rejected_with_verbatim_hint():
    """③ 删字段（白名单外）：抛 MigrationUnsupported 含固定分步建议逐字"""

    async def body(store, db):
        v1 = _note_v1()
        await store.migrate(backend='sqlite', source='default', defs={'Note': v1}, driver=db)
        v2 = {**v1, 'fields': {'_id': {'type': 'string'}}}  # 删 title
        with pytest.raises(MigrationUnsupported) as ei:
            await store.migrate(backend='sqlite', source='default',
                                defs={'Note': v2}, driver=db)
        e = ei.value
        assert isinstance(e, md.MetaDefError), '应为 MetaDefError 子类'
        assert _UNSUPPORTED_HINT in str(e), f'消息应逐字含固定分步建议（got: {e}）'
        assert e.skipped[0]['name'] == 'Note'
        assert '删除字段' in '；'.join(e.skipped[0]['reason'])
        rows = await _version_rows(db)
        assert len(rows) == 1, '被拦后不落新版本行'

    _run_in_sqlite(body)


def test_midway_failure_rolls_back_without_orphan_version_row():
    """④ 中途失败回滚：第二条语句（撞名索引）失败 → 首条 ALTER 已回滚、无孤儿版本行"""

    async def body(store, db):
        base = {
            'name': 'Item', 'collection': 'items', 'idPrefix': 'i', 'timestamps': False,
            'fields': {'_id': {'type': 'string'}, 'title': {'type': 'string'}},
        }
        await store.migrate(backend='sqlite', source='default',
                            defs={'Item': base}, old_defs=None, driver=db)
        # 预置撞名索引：v2 的 addIndex 语句（语句序在 addColumn 之后）必然失败 → 事务整体回滚
        await db.execute('CREATE INDEX idx_items_title ON items(title)')
        await db.commit()
        bump = {**base, 'fields': {**base['fields'], 'score': {'type': 'int'}},
                'indexes': [{'keys': {'title': 1}}]}
        with pytest.raises(Exception, match='already exists'):
            await store.migrate(backend='sqlite', source='default',
                                defs={'Item': bump}, old_defs=None, driver=db)
        cols = await _cols(db, 'items')
        assert 'score' not in cols, f'score 列应已回滚（got {cols}）'
        rows = await _version_rows(db)
        assert [r['version'] for r in rows] == [1], '无孤儿版本行（仅 v1）'

    _run_in_sqlite(body)


def test_old_defs_explicit_skips_introspect():
    """⑤ old_defs 显式传入：跳过 introspect，from/to 取自版本行演进"""

    async def body(store, db):
        v1 = _note_v1()
        await store.migrate(backend='sqlite', source='default', defs={'Note': v1}, driver=db)
        v2 = {**v1, 'fields': {**v1['fields'], 'tag': {'type': 'string'}}}
        r = await store.migrate(backend='sqlite', source='default', defs={'Note': v2},
                                old_defs={'Note': v1})
        assert r['applied'] == [{'name': 'Note', 'from': 1, 'to': 2, 'version': 2}]
        cols = await _cols(db, 'notes')
        assert 'tag' in cols

    _run_in_sqlite(body)


def test_missing_driver_with_old_defs_none_raises():
    """⑥ 参数校验：old_defs=None 且缺 driver 显式报错（不静默）"""

    async def body(store, db):
        with pytest.raises(md.MetaDefError, match='需传 driver'):
            await store.migrate(backend='sqlite', source='default',
                                defs={'Note': _note_v1()})

    _run_in_sqlite(body)
