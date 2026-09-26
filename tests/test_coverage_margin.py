"""覆盖率余量：补真实分支断言（执行文档 §5 步骤 7）

优先缺口模块（本机 term-missing 实测）：executors/__init__.py、executors/sqlite.py、
introspect/sqlite.py、ddl.py（去重告警）、crud/exec.py。
全部零外部依赖（sqlite 真实驱动），不依赖 MySQL / PG / Mongo 服务。

运行：PYTHONPATH=src python -m pytest tests/test_coverage_margin.py -q
"""

import asyncio

import aiosqlite
import pytest

from py_store import datasource, ddl, feedback, store
from py_store.crud import exec as crud_exec
from py_store.executors import _scalar, create_connection, shape_result
from py_store.executors import sqlite as sqlite_exec
from py_store.introspect import sqlite as introspect_sqlite


@pytest.fixture(autouse=True)
def _isolate():
    feedback.set_sink(None)
    yield
    datasource.set_connections({})
    feedback.set_sink(None)


def _run(coro):
    return asyncio.run(coro)


# ─── executors/__init__.py ───────────────────────────────────

def test_create_connection_unknown_backend():
    with pytest.raises(ValueError, match='未知 SQL 后端'):
        create_connection('oracle', None)


def test_scalar_variants():
    assert _scalar([]) == 0
    assert _scalar([{'c': '42'}]) == 42
    assert _scalar([{'c': 'nope'}]) == 'nope'
    assert _scalar([('raw',)]) == ('raw',)


def test_shape_result_unknown_kind_passthrough():
    env = {'docs': [1], 'rows': [[2]], 'affectedRows': 0}
    assert shape_result({'kind': 'weird'}, env) is env


# ─── executors/sqlite.py ─────────────────────────────────────

def test_sqlite_create_requires_connection():
    with pytest.raises(TypeError, match='aiosqlite'):
        sqlite_exec.create(None)


def test_sqlite_with_transaction_rolls_back(tmp_path):
    db_path = tmp_path / 'tx.db'

    async def scenario():
        db = await aiosqlite.connect(db_path)
        await db.execute('CREATE TABLE t (_id TEXT PRIMARY KEY)')
        await db.commit()
        desc = create_connection('sqlite', db)

        async def body(exec_on_tx):
            await exec_on_tx({'stmts': [{'text': "INSERT INTO t (_id) VALUES ('y')"}]})
            raise RuntimeError('boom')

        with pytest.raises(RuntimeError, match='boom'):
            await desc['with_transaction'](body)
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


# ─── introspect/sqlite.py ────────────────────────────────────

def test_introspect_sqlite_requires_connection():
    with pytest.raises(TypeError, match='aiosqlite'):
        _run(introspect_sqlite.introspect(None))


def test_introspect_sqlite_missing_attached_db(tmp_path):
    db_path = tmp_path / 'intro.db'

    async def scenario():
        db = await aiosqlite.connect(db_path)
        try:
            with pytest.raises(RuntimeError, match='attached db 不存在'):
                await introspect_sqlite.introspect(db, {'database': 'nope'})
        finally:
            await db.close()

    _run(scenario())


def test_introspect_sqlite_collects_indexes(tmp_path):
    db_path = tmp_path / 'idx.db'

    async def scenario():
        db = await aiosqlite.connect(db_path)
        try:
            await db.execute('CREATE TABLE t (_id TEXT PRIMARY KEY, v TEXT)')
            await db.execute('CREATE INDEX idx_t_v ON t(v)')
            await db.commit()
            out = await introspect_sqlite.introspect(db)
        finally:
            await db.close()
        return out

    out = _run(scenario())
    names = [i['name'] for i in out['indexes']]
    assert 'idx_t_v' in names


# ─── ddl.py 表名去重告警 ─────────────────────────────────────

def test_ddl_generate_dedupe_emits_feedback():
    events = []
    feedback.set_sink(events.append)
    store.register({
        'name': 'CovX', 'collection': 'cov_x', 'idPrefix': 'cx',
        'fields': {'_id': {'type': 'string'}},
    })
    sql = ddl.generate('sqlite', ['CovX', 'CovX'])
    assert sql.count('CREATE TABLE "cov_x"') == 1
    assert any(e.get('code') == 'ddlDuplicateTable' for e in events), events


# ─── crud/exec.py 连接薄包装 ─────────────────────────────────

def test_crud_exec_connection_wrappers():
    crud_exec.set_connections({'default': 'sentinel'})
    assert crud_exec._get_db() == 'sentinel'
    assert crud_exec._get_db('default') == 'sentinel'
