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
            CREATE TABLE r2_deny (_id TEXT PRIMARY KEY, title TEXT, __present TEXT);
            CREATE TABLE r2_rbac (_id TEXT PRIMARY KEY, title TEXT, __present TEXT);
            CREATE TABLE r2_allow (
              _id TEXT PRIMARY KEY, title TEXT, secret TEXT, __present TEXT
            );
            INSERT INTO r2_deny VALUES ('d1', 't', ',_id,title,');
            INSERT INTO r2_rbac VALUES ('r1', 't', ',_id,title,');
            INSERT INTO r2_allow VALUES ('a1', 't', 's', ',_id,title,secret,');
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
        # E1 用：denyRead 显式拒绝 / RBAC 无授权 / 有权通过 / 字段白名单
        # 6.0.0 语义：``read: []`` 表示「未配置」（不限制），显式拒绝读必须写 ``denyRead: True``。
        _sc.register({'name': 'R2Deny', 'collection': 'r2_deny', 'timestamps': False,
                      'denyRead': True,
                      'fields': {'_id': {'type': 'string'}, 'title': {'type': 'string'}}})
        _sc.register({'name': 'R2Rbac', 'collection': 'r2_rbac', 'timestamps': False,
                      'read': ['viewer'],
                      'fields': {'_id': {'type': 'string'}, 'title': {'type': 'string'}}})
        _sc.register({'name': 'R2Allow', 'collection': 'r2_allow', 'timestamps': False,
                      'read': ['viewer'],
                      'fields': {'_id': {'type': 'string'}, 'title': {'type': 'string'},
                                 'secret': {'type': 'string'}}})
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


def test_exists_deny_read_denied():
    async def body(store, db):
        perm.set_context({'userId': 'u1', 'roles': ['viewer']})
        try:
            with pytest.raises(perm.PermissionError, match='无访问权限'):
                await store.exists('R2Deny', {'_id': 'd1'})
        finally:
            perm.set_context(None)

    _run_in_sqlite(body)


def test_exists_rbac_denied():
    _POLICY = {
        'mode': 'enforce',
        'roles': {'viewer': {}},
        'grants': [{'role': 'viewer', 'model': 'R2Allow', 'actions': ['read'],
                    'readFields': ['_id', 'title']}],
    }

    async def body(store, db):
        store.set_rbac(_POLICY)
        perm.set_context({'userId': 'u1', 'roles': ['viewer']})
        try:
            with pytest.raises(perm.PermissionError):
                await store.exists('R2Rbac', {'_id': 'r1'})
        finally:
            perm.set_context(None)
            store.set_rbac(None)

    _run_in_sqlite(body)


def test_exists_allowed():
    _POLICY = {
        'mode': 'enforce',
        'roles': {'viewer': {}},
        'grants': [{'role': 'viewer', 'model': 'R2Allow', 'actions': ['read'],
                    'readFields': ['_id', 'title']}],
    }

    async def body(store, db):
        store.set_rbac(_POLICY)
        perm.set_context({'userId': 'u1', 'roles': ['viewer']})
        try:
            assert await store.exists('R2Allow', {'title': 't'}) is True
        finally:
            perm.set_context(None)
            store.set_rbac(None)

    _run_in_sqlite(body)


@pytest.mark.skip(reason='core plan_exists_with_ctx 仅校验表级 read/RBAC（rust-store/core/src/command/write.rs:186-205），'
                         '条件字段白名单未校验 —— 跨方向项')
def test_exists_rejects_condition_on_unreadable_field():
    _POLICY = {
        'mode': 'enforce',
        'roles': {'viewer': {}},
        'grants': [{'role': 'viewer', 'model': 'R2Allow', 'actions': ['read'],
                    'readFields': ['_id', 'title']}],
    }

    async def body(store, db):
        from py_store import store as _store
        _store.set_rbac(_POLICY)
        perm.set_context({'userId': 'u1', 'roles': ['viewer']})
        try:
            with pytest.raises(perm.PermissionError):
                await store.exists('R2Allow', {'secret': 's'})
        finally:
            perm.set_context(None)
            _store.set_rbac(None)

    _run_in_sqlite(body)
