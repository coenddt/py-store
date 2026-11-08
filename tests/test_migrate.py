"""迁移编排器（C3/A4）测试：SQLite 真库（aiosqlite 内存库）

对拍 nodejs-store/tests/migrate.test.js；执行文档 04 §4.4 四用例 + old_defs 显式传入补充用例。
运行：pytest tests/test_migrate.py -x
"""

import asyncio

import aiosqlite
import pytest

from py_store import metadef as md
from py_store.migrate import _UNSUPPORTED_HINT, MigrationUnsupported


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
        return [dict(zip(('tenant', 'env', 'backend', 'name', 'version'), r, strict=False))
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


def test_legacy_archive_table_missing_timestamps_is_backfilled():
    """⓪d 存量归档表缺 timestamps 列（旧版 DDL 建出）：迁移补列后 remove 可用"""

    async def body(store, db):
        await db.executescript("""
            CREATE TABLE legacy_arch (
              _id TEXT PRIMARY KEY, title TEXT, created_at INTEGER, updated_at INTEGER,
              created_by TEXT, __present TEXT
            );
            CREATE TABLE legacy_arch_deleted (
              _id TEXT PRIMARY KEY, title TEXT, created_by TEXT, __present TEXT,
              deleted_at INTEGER
            );
            INSERT INTO legacy_arch VALUES ('la_1', '待删', 0, 0, 'u1', ',_id,title,');
        """)
        await db.commit()
        defn = {
            'name': 'LegacyArch', 'collection': 'legacy_arch', 'idPrefix': 'la',
            'fields': {
                '_id': {'type': 'string'}, 'title': {'type': 'string'},
                'createdBy': {'type': 'string'},
            },
        }
        store.register(defn)
        r = await store.migrate(backend='sqlite', source='default',
                                defs={'LegacyArch': defn}, driver=db)
        assert r['applied'] == [{'name': 'LegacyArch', 'from': 0, 'to': 1, 'version': 1}]
        cols = await _cols(db, 'legacy_arch_deleted')
        for c in ('created_at', 'updated_at', 'created_by', '__present'):
            assert c in cols, f'归档表应补齐 {c}（got {cols}）'
        rd = await store.remove('LegacyArch', {'_id': 'la_1'})
        assert rd == {'deletedCount': 1, 'archivedCount': 1}

    _run_in_sqlite(body)


def test_missing_archive_table_is_created_on_migrate():
    """⓪c 主表存在但归档表缺失：迁移补建后 remove 可用"""

    async def body(store, db):
        await db.executescript("""
            CREATE TABLE noarch (
              _id TEXT PRIMARY KEY, title TEXT, created_at INTEGER, updated_at INTEGER,
              __present TEXT
            );
            INSERT INTO noarch VALUES ('na_1', '待删', 0, 0, ',_id,title,');
        """)
        await db.commit()
        defn = {
            'name': 'NoArch', 'collection': 'noarch', 'idPrefix': 'na',
            'fields': {'_id': {'type': 'string'}, 'title': {'type': 'string'}},
        }
        store.register(defn)
        r = await store.migrate(backend='sqlite', source='default',
                                defs={'NoArch': defn}, driver=db)
        assert r['applied'] == [{'name': 'NoArch', 'from': 0, 'to': 1, 'version': 1}]
        rd = await store.remove('NoArch', {'_id': 'na_1'})
        assert rd == {'deletedCount': 1, 'archivedCount': 1}

    _run_in_sqlite(body)


def test_legacy_table_missing_builtin_columns_autofilled_with_warning():
    """⓪ 老表缺内建列：迁移先自动补列再继续，并发出可定位告警

    改前（上一版）：显式抛 MigrationUnsupported，老库必须人工补列才能迁。
    改后：按当前 DDL 自动补列 → 老库照样迁得过去；补列动作必须告警（表名 + 全部缺失列），
    只有补列失败才中断（不吞成静默通过）。
    """
    from py_store import feedback

    seen = []
    feedback.set_sink(seen.append)
    try:
        async def body(store, db):
            await db.execute("""CREATE TABLE legacy_notes (
                _id TEXT PRIMARY KEY, title TEXT, status TEXT,
                createdAt INTEGER, updatedAt INTEGER
            )""")
            await db.commit()
            defn = {
                'name': 'LegacyNote', 'collection': 'legacy_notes', 'idPrefix': 'ln',
                'read': ['creator'], 'write': ['creator'],
                'fields': {'_id': {'type': 'string'}, 'title': {'type': 'string'},
                           'status': {'type': 'string'}},
            }
            r = await store.migrate(backend='sqlite', source='default',
                                    defs={'LegacyNote': defn}, driver=db)
            assert r['applied'] == [{'name': 'LegacyNote', 'from': 0, 'to': 1, 'version': 1}], r
            cols = await _cols(db, 'legacy_notes')
            for col in ('__present', 'created_at', 'updated_at', 'created_by'):
                assert col in cols, f'应自动补列 {col}（got {cols}）'

        _run_in_sqlite(body)
    finally:
        feedback.set_sink(None)
    hit = [e for e in seen if e.get('code') == 'migrationAutoAddBuiltinColumns']
    assert hit, f'必须发出自动补列告警（got {seen}）'
    assert 'legacy_notes' in hit[0]['message']
    for col in ('__present', 'created_at', 'updated_at', 'created_by'):
        assert col in hit[0]['message'], f'告警应列出 {col}（got {hit[0]["message"]}）'


def test_owner_column_all_null_warns_and_continues():
    """⓪b 归属键漏回填：created_by 全 NULL 时迁移继续，但必须告警（禁止静默全表不可见）"""
    from py_store import feedback

    seen = []
    feedback.set_sink(seen.append)
    try:
        async def body(store, db):
            await db.execute("""CREATE TABLE null_owner (
                _id TEXT PRIMARY KEY, title TEXT,
                created_at INTEGER, updated_at INTEGER, created_by TEXT, __present TEXT
            )""")
            await db.execute("INSERT INTO null_owner (_id, title, created_by) VALUES ('no_1', 'a', NULL)")
            await db.commit()
            v1 = {
                'name': 'NullOwner', 'collection': 'null_owner', 'idPrefix': 'no',
                'read': ['creator'], 'write': ['creator'],
                'fields': {'_id': {'type': 'string'}, 'title': {'type': 'string'}},
            }
            r = await store.migrate(backend='sqlite', source='default',
                                    defs={'NullOwner': v1}, driver=db)
            assert r['applied'] == [{'name': 'NullOwner', 'from': 0, 'to': 1, 'version': 1}], r
            rows = await _version_rows(db)
            assert [x['version'] for x in rows if x['name'] == 'NullOwner'] == [1]

        _run_in_sqlite(body)
    finally:
        feedback.set_sink(None)
    hit = [e for e in seen if e.get('code') == 'migrationOwnerKeyNullRows']
    assert hit, f'必须发出归属键 NULL 告警（got {seen}）'
    assert 'null_owner' in hit[0]['message'] and 'created_by' in hit[0]['message']
