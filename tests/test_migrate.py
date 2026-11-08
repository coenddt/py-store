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


def test_datetime_and_bool_fields_align_after_introspect():
    """D3：内省把 datetime/bool 列反推为 number，新声明同族类型不得判「非放宽变更」。

    改前：_align_types 只归一数值族 → 再迁移时报
    `字段 "published_at" 类型 "number" → "datetime" 非放宽变更`。
    """

    async def body(store, db):
        v1 = {
            'name': 'Post', 'collection': 'posts', 'idPrefix': 'p', 'timestamps': False,
            'fields': {
                '_id': {'type': 'string'},
                'publishedAt': {'type': 'datetime'},
                'active': {'type': 'bool'},
            },
        }
        await store.migrate(backend='sqlite', source='default',
                            defs={'Post': v1}, old_defs=None, driver=db)
        v2 = {**v1, 'fields': {**v1['fields'], 'title': {'type': 'string'}}}
        r = await store.migrate(backend='sqlite', source='default',
                                defs={'Post': v2}, old_defs=None, driver=db)
        assert r['applied'] == [{'name': 'Post', 'from': 1, 'to': 2, 'version': 2}], r
        cols = await _cols(db, 'posts')
        assert 'title' in cols
        assert cols.count('published_at') == 1 and cols.count('active') == 1

    _run_in_sqlite(body)


def _default_ts_creator_v1():
    """未声明 timestamps（默认建）+ creator 行控（隐含 created_by）——D1/D2 触发面"""
    return {
        'name': 'Task', 'collection': 'tasks', 'idPrefix': 't',
        'write': ['creator'],
        'fields': {'_id': {'type': 'string'}, 'title': {'type': 'string'}},
    }


def test_default_timestamps_and_creator_columns_not_treated_as_dropped():
    """D1/D2：默认 timestamps + creator 权限的表，内省迁移不得把内建列当「待删字段」。

    改前：_strip_builtin_fields 用真值判断 timestamps（未声明 → 不剔）+ 从不剔归属键，
    迁移抛 MigrationUnsupported（删除字段 created_at/updated_at/created_by）。
    改后：剔除清单取 ddl.builtin_field_names（与 DDL 生成同一份内建列定义）。
    """

    async def body(store, db):
        v1 = _default_ts_creator_v1()
        await store.migrate(backend='sqlite', source='default',
                            defs={'Task': v1}, old_defs=None, driver=db)
        v2 = {**v1, 'fields': {**v1['fields'], 'priority': {'type': 'int'}}}
        r = await store.migrate(backend='sqlite', source='default',
                                defs={'Task': v2}, old_defs=None, driver=db)
        assert r['applied'] == [{'name': 'Task', 'from': 1, 'to': 2, 'version': 2}], r
        cols = await _cols(db, 'tasks')
        assert 'priority' in cols
        # 内建列仍在（未被当作删除、也未重复 ADD COLUMN）
        for builtin in ('created_at', 'updated_at', 'created_by', '__present'):
            assert builtin in cols, f'{builtin} 应保留（got {cols}）'
        assert cols.count('created_at') == 1 and cols.count('created_by') == 1

    _run_in_sqlite(body)


def _legacy_note_defn():
    """camelCase 时代的表：默认 timestamps + read/write creator（隐含 created_by），
    物理列只有 _id/title/status/createdAt/updatedAt，没有 __present / created_at 等。"""
    return {
        'name': 'LegacyNote', 'collection': 'legacy_notes', 'idPrefix': 'ln',
        'read': ['creator'], 'write': ['creator'],
        'fields': {
            '_id': {'type': 'string'}, 'title': {'type': 'string'}, 'status': {'type': 'string'},
        },
    }


async def _create_legacy_table(db):
    await db.execute("""CREATE TABLE legacy_notes (
        _id TEXT PRIMARY KEY,
        title TEXT,
        status TEXT,
        createdAt INTEGER,
        updatedAt INTEGER
    )""")
    await db.commit()


def test_legacy_table_missing_builtin_columns_autofilled_with_warning():
    """⓪ 老表缺内建列（camelCase 时代）：迁移先自动补列再继续，并发出可定位告警

    改前（上一版）：显式抛 MigrationUnsupported，老库必须人工补列才能迁。
    改后：按当前 DDL 自动补列 → 老库照样迁得过去；补列动作必须告警（表名 + 全部缺失列），
    只有补列失败才中断（不吞成静默通过）。
    """
    from py_store import feedback

    seen = []
    feedback.set_sink(seen.append)
    try:
        async def body(store, db):
            await _create_legacy_table(db)
            r = await store.migrate(backend='sqlite', source='default',
                                    defs={'LegacyNote': _legacy_note_defn()}, driver=db)
            assert r['applied'] == [{'name': 'LegacyNote', 'from': 0, 'to': 1, 'version': 1}], r
            cols = await _cols(db, 'legacy_notes')
            for col in ('__present', 'created_at', 'updated_at', 'created_by'):
                assert col in cols, f'应自动补列 {col}（got {cols}）'
            rows = await _version_rows(db)
            assert [x['version'] for x in rows if x['name'] == 'LegacyNote'] == [1]

        _run_in_sqlite(body)
    finally:
        feedback.set_sink(None)
    hit = [e for e in seen if e.get('code') == 'migrationAutoAddBuiltinColumns']
    assert hit, f'必须发出自动补列告警（got {seen}）'
    assert 'legacy_notes' in hit[0]['message']
    for col in ('__present', 'created_at', 'updated_at', 'created_by'):
        assert col in hit[0]['message'], f'告警应列出 {col}（got {hit[0]["message"]}）'


def test_legacy_table_migrates_after_columns_added():
    """⓪ 边界：按指引补列后同一 defn 可正常迁移（检测不是永久阻断）"""

    async def body(store, db):
        await _create_legacy_table(db)
        for ddl in (
            'ALTER TABLE legacy_notes ADD COLUMN created_at INTEGER',
            'ALTER TABLE legacy_notes ADD COLUMN updated_at INTEGER',
            'ALTER TABLE legacy_notes ADD COLUMN created_by TEXT',
            'ALTER TABLE legacy_notes ADD COLUMN __present TEXT',
        ):
            await db.execute(ddl)
        await db.commit()
        r = await store.migrate(backend='sqlite', source='default',
                                defs={'LegacyNote': _legacy_note_defn()}, driver=db)
        assert r['applied'] == [{'name': 'LegacyNote', 'from': 0, 'to': 1, 'version': 1}], r
        rows = await _version_rows(db)
        assert [x['version'] for x in rows if x['name'] == 'LegacyNote'] == [1]

    _run_in_sqlite(body)


def test_owner_column_all_null_warns_and_continues():
    """⓪b 归属键漏回填：created_by 全 NULL 时迁移继续，但必须告警（禁止静默全表不可见）

    改前（上一版）：命中即抛 MigrationUnsupported，老库必须先回填才能迁。
    改后：迁移层不猜业务归属 → 放行 + 告警（表名 + 归属键列），版本行照常写入。
    """
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


def test_legacy_table_archived_table_is_backfilled():
    """⓪c 老表已存在但归档表缺失：迁移补建 ``<table>_deleted``，remove 可用

    改前：主表已存在走 diff 分支，addTable 分支不触发 → 不建归档表；remove 报
    ``no such table: arch_notes_deleted``（删除功能不可用）。
    """

    async def body(store, db):
        from py_store import schema as _sc
        await db.execute("""CREATE TABLE arch_notes (
            _id TEXT PRIMARY KEY, title TEXT,
            created_at INTEGER, updated_at INTEGER, created_by TEXT, __present TEXT
        )""")
        await db.execute(
            "INSERT INTO arch_notes (_id, title, created_at, updated_at, created_by, __present) "
            "VALUES ('an_1', '待删', 1, 1, 'u1', ',_id,title,')")
        await db.commit()
        v1 = {
            'name': 'ArchNote', 'collection': 'arch_notes', 'idPrefix': 'an',
            'write': ['creator'],
            'fields': {'_id': {'type': 'string'}, 'title': {'type': 'string'}},
        }
        _sc.register(v1)
        r = await store.migrate(backend='sqlite', source='default',
                                defs={'ArchNote': v1}, driver=db)
        assert r['applied'] == [{'name': 'ArchNote', 'from': 0, 'to': 1, 'version': 1}], r
        tables = await _tables(db)
        assert 'arch_notes_deleted' in tables, f'归档表应补建（got {tables}）'
        rd = await store.remove('ArchNote', {'_id': 'an_1'})
        assert rd == {'deletedCount': 1, 'archivedCount': 1}, rd
        assert (await _cols(db, 'arch_notes'))  # 表仍在
        cur = await db.execute('SELECT count(*) FROM arch_notes')
        assert (await cur.fetchone())[0] == 0
        await cur.close()
        cur = await db.execute('SELECT count(*) FROM arch_notes_deleted')
        assert (await cur.fetchone())[0] == 1
        await cur.close()

    _run_in_sqlite(body)


def test_legacy_archive_table_missing_timestamps_is_backfilled():
    """⓪d 存量归档表缺 timestamps 列（旧版 DDL 建出）：迁移补列后 remove 可用

    改前：归档表由 ``timestamps=False`` 时代建出，缺 created_at / updated_at → 归档
    INSERT 报 ``table legacy_arch_deleted has no column named created_at``，删除直接失败。
    """

    async def body(store, db):
        from py_store import schema as _sc
        await db.execute("""CREATE TABLE legacy_arch (
            _id TEXT PRIMARY KEY, title TEXT,
            created_at INTEGER, updated_at INTEGER, created_by TEXT, __present TEXT
        )""")
        await db.execute("""CREATE TABLE legacy_arch_deleted (
            _id TEXT PRIMARY KEY, title TEXT, created_by TEXT, __present TEXT, deleted_at INTEGER
        )""")
        await db.execute(
            "INSERT INTO legacy_arch (_id, title, created_at, updated_at, created_by, __present) "
            "VALUES ('la_1', '待删', 1, 1, 'u1', ',_id,title,')")
        await db.commit()
        v1 = {
            'name': 'LegacyArch', 'collection': 'legacy_arch', 'idPrefix': 'la',
            'write': ['creator'],
            'fields': {'_id': {'type': 'string'}, 'title': {'type': 'string'}},
        }
        _sc.register(v1)
        r = await store.migrate(backend='sqlite', source='default',
                                defs={'LegacyArch': v1}, driver=db)
        assert r['applied'] == [{'name': 'LegacyArch', 'from': 0, 'to': 1, 'version': 1}], r
        cols = await _cols(db, 'legacy_arch_deleted')
        for col in ('created_at', 'updated_at', 'created_by', '__present'):
            assert col in cols, f'归档表应补齐 {col}（got {cols}）'
        rd = await store.remove('LegacyArch', {'_id': 'la_1'})
        assert rd == {'deletedCount': 1, 'archivedCount': 1}, rd

    _run_in_sqlite(body)


def test_declared_builtin_like_fields_are_not_stripped():
    """D1/D2 边界：用户显式声明 createdAt / createdBy ⇒ 不剔除（否则会误判新增列重复 ADD）"""

    async def body(store, db):
        v1 = {
            'name': 'Decl', 'collection': 'decls', 'idPrefix': 'd',
            'timestamps': False, 'write': ['creator'],
            'fields': {
                '_id': {'type': 'string'},
                'createdAt': {'type': 'number'},
                'createdBy': {'type': 'string'},
            },
        }
        await store.migrate(backend='sqlite', source='default',
                            defs={'Decl': v1}, old_defs=None, driver=db)
        v2 = {**v1, 'fields': {**v1['fields'], 'note': {'type': 'string'}}}
        r = await store.migrate(backend='sqlite', source='default',
                                defs={'Decl': v2}, old_defs=None, driver=db)
        assert r['applied'] == [{'name': 'Decl', 'from': 1, 'to': 2, 'version': 2}], r
        cols = await _cols(db, 'decls')
        assert cols.count('created_at') == 1, f'created_at 不得重复（got {cols}）'
        assert cols.count('created_by') == 1, f'created_by 不得重复（got {cols}）'

    _run_in_sqlite(body)
