"""宿主安全面回归（第二轮自检：core 报来的三项宿主问题）

  P2-a ``ctx.internal`` 不得由请求上下文注入（set_context / scoped_context 收口）
  P2-b update/upsert/mutation 的 returns 以落库 doc 为基，只叠加计算列
       （显式 null 不得被默认值替换，对齐 core ``DefaultFill::Insert``）
  P1   exists 走带 ctx 的读权限出口（等待 core ``plan_exists_with_ctx``，当前 skip）

对拍 nodejs-store/tests/host-security.test.js。
"""

import asyncio

import aiosqlite
import pytest

from py_store import permission as perm
from py_store import schema as _sc


def _run_in_sqlite(body):
    async def _main():
        from py_store import datasource as _ds
        from py_store import executors, init, store
        from py_store import metadef as md
        md.ensure_builtins()
        prev = dict(_ds._connections)
        db = await aiosqlite.connect(':memory:')
        await db.executescript("""
            CREATE TABLE r2_null (
              _id TEXT PRIMARY KEY, title TEXT, nick TEXT, __present TEXT
            );
            CREATE TABLE r2_null_deleted (
              _id TEXT PRIMARY KEY, title TEXT, nick TEXT, deleted_at INTEGER, __present TEXT
            );
            INSERT INTO r2_null (_id, title, nick, __present)
              VALUES ('rn1', 't0', NULL, ',_id,title,nick,');
        """)
        await db.commit()
        _sc.register({
            'name': 'R2Null', 'collection': 'r2_null', 'idPrefix': 'rn', 'timestamps': False,
            'fields': {
                '_id': {'type': 'string'},
                'title': {'type': 'string'},
                'nick': {'type': 'string', 'default': 'anon'},
            },
        })
        await init({'default': executors.create_connection('sqlite', db)})
        try:
            return await body(store, db)
        finally:
            await db.close()
            _ds._connections = prev

    return asyncio.new_event_loop().run_until_complete(_main())


def test_set_context_rejects_internal():
    with pytest.raises(perm.PermissionError, match=r'ctx\.internal 属可信标记'):
        perm.set_context({'userId': 'u1', 'roles': ['creator'], 'internal': True})


def test_scoped_context_rejects_internal():
    with pytest.raises(perm.PermissionError, match=r'ctx\.internal 属可信标记'):
        with perm.scoped_context({'userId': 'u1', 'internal': True}):
            pass


def test_update_returns_preserve_explicit_null():
    async def body(store, db):
        ret = await store.update('R2Null', {'_id': 'rn1'}, {'title': 't1'})
        assert ret['title'] == 't1'
        assert ret['nick'] is None, f'returns 的 nick 应保持落库值 null，实际 {ret["nick"]!r}'
        cur = await db.execute('SELECT nick FROM r2_null WHERE _id = ?', ('rn1',))
        row = await cur.fetchone()
        assert row[0] is None

    _run_in_sqlite(body)


@pytest.mark.skip(reason='等待 core 导出 plan_exists_with_ctx（第二轮跨方向项 P1）')
def test_exists_respects_read_permission():
    async def body(store, db):
        perm.set_context({'userId': 'u1', 'roles': []})
        try:
            visible = await store.exists('R2Null', {'_id': 'rn1'})
        finally:
            perm.set_context(None)
        assert visible is False

    _run_in_sqlite(body)
