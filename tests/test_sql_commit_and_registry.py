"""修复回归：① SQL 写路径提交契约（落盘 + 跨连接读回 / 失败回滚）
          ② 归档表注册唯一性（list/DDL 无重复 + 去重告警）

依据执行文档：doc/execution/2026/09/处理中-py-store不足清单修复-执行.md §4.5
运行：PYTHONPATH=src python -m pytest tests/test_sql_commit_and_registry.py -q
"""

import asyncio

import aiosqlite
import pytest

from py_store import datasource, executors, feedback, store
from py_store import schema as schema_mod


@pytest.fixture(autouse=True)
def _isolate_globals():
    datasource.set_connections({})
    feedback.set_sink(None)
    yield
    datasource.set_connections({})
    feedback.set_sink(None)


def _run(coro):
    return asyncio.run(coro)


# ─── ① SQL 写路径提交契约 ────────────────────────────────────

def test_sqlite_write_persists_across_connections(tmp_path):
    """写经 store.execute_raw 落盘后，**新连接**必须读到（证明 exec_ 已提交）"""
    db_path = tmp_path / 'persist.db'

    async def scenario():
        db = await aiosqlite.connect(db_path)
        datasource.set_connections({'default': executors.create_connection('sqlite', db)})
        await store.execute_raw(
            'default', 'CREATE TABLE t (_id TEXT PRIMARY KEY, v TEXT)', is_write=True)
        await store.execute_raw(
            'default', "INSERT INTO t (_id, v) VALUES ('1', 'a')", is_write=True)
        await db.close()

        db2 = await aiosqlite.connect(db_path)
        try:
            cur = await db2.execute('SELECT v FROM t WHERE _id = ?', ('1',))
            rows = await cur.fetchall()
            await cur.close()
        finally:
            await db2.close()
        return rows

    assert _run(scenario()) == [('a',)]


def test_sqlite_plan_failure_rolls_back(tmp_path):
    """plan 第 2 条语句失败 → 第 1 条已写入的行**不得**落盘（rollback 生效）"""
    db_path = tmp_path / 'rollback.db'

    async def scenario():
        db = await aiosqlite.connect(db_path)
        conn = executors.create_connection('sqlite', db)
        datasource.set_connections({'default': conn})
        await store.execute_raw('default', 'CREATE TABLE t (_id TEXT PRIMARY KEY)', is_write=True)

        with pytest.raises(aiosqlite.OperationalError):
            await conn['exec']({'stmts': [
                {'text': "INSERT INTO t (_id) VALUES ('x')"},
                {'text': 'THIS IS NOT SQL'},
            ]})
        await db.close()

        db2 = await aiosqlite.connect(db_path)
        try:
            cur = await db2.execute('SELECT COUNT(*) FROM t')
            n = (await cur.fetchone())[0]
            await cur.close()
        finally:
            await db2.close()
        return n

    assert _run(scenario()) == 0


# ─── ② 归档表注册唯一性 ─────────────────────────────────────

def test_register_does_not_duplicate_archive():
    store.register({
        'name': 'RegUniq', 'collection': 'reg_uniq', 'idPrefix': 'ru',
        'fields': {'_id': {'type': 'string'}, 'name': {'type': 'string'}},
    })
    names = store.list()
    assert names.count('RegUniqDeleted') == 1
    assert len(names) == len(set(names))


def test_generate_ddl_has_no_duplicate_table():
    store.register({
        'name': 'RegDdl', 'collection': 'reg_ddl', 'idPrefix': 'rd',
        'fields': {'_id': {'type': 'string'}},
    })
    # 显式 names：core 注册表在同进程内被其它测试文件共享，全量生成会被无关 schema 干扰
    sql = store.generate_ddl('sqlite', ['RegDdl', 'RegDdlDeleted'])
    assert sql.count('CREATE TABLE "reg_ddl"') == 1
    assert sql.count('CREATE TABLE "reg_ddl_deleted"') == 1


def test_registry_dedupe_emits_feedback():
    """防御网：core 注册表若仍出现重复名 → list() 去重并告警（禁静默）"""
    events = []
    feedback.set_sink(events.append)
    defn = {'name': 'RegDup', 'collection': 'reg_dup', 'idPrefix': 'rq',
            'fields': {'_id': {'type': 'string'}}}
    # 绕过 Host register（其已修），直调 core 两次制造重复
    schema_mod.core.register(defn)
    schema_mod.core.register(defn)

    assert store.list().count('RegDup') == 1
    assert any(e.get('code') == 'schemaDuplicateName' for e in events), events
