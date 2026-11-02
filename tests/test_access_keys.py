"""accessKeys 声明式行级权限（py 宿主端到端）

判决唯一在 Rust core；本文件验证 py_HOST 侧的三件事：
  1. schema 声明原样透传（注册期校验由 core 执行，非法声明直接抛错）；
  2. ctx 清单经 Host 透传后，读路径自动注入行条件、写路径自动做子集校验；
  3. 错误语义稳定：``ERR_PERMISSION:ACCESS:`` 前缀由 Host 映射为 ``PermissionError``。

运行：PYTHONPATH=src LOCAL_CORE=1 python -m pytest tests/test_access_keys.py -q
"""

import asyncio

import pytest

from py_store import datasource as _ds
from py_store import init, store
from py_store import permission as perm
from py_store import schema as _sc
from py_store.memory import connect

_PREFIX = "ERR_PERMISSION:ACCESS:"


def _run(coro):
    return asyncio.run(coro)


def _register_test_schemas():
    """conftest 模块夹具重放（清场后恢复本模块 schema）"""
    _sc.register({
        'name': 'AkItem',
        'collection': 'ak_items',
        'idPrefix': 'ak_',
        'timestamps': False,
        'fields': {
            'satCode': {'type': 'string'},
            'title': {'type': 'string'},
            'tags': {'type': 'array'},
            'createdBy': {'type': 'string'},
        },
        'relations': {},
        'accessKeys': {
            'satCode': {'read': 'satsRead', 'write': 'satsWrite', 'bypassRoles': ['admin']},
            'tags': {'read': 'tagsRead', 'write': 'tagsWrite'},
        },
    })
    # owner 条件（read=['creator']）+ accessKeys：验证 bypass 只跳过该键，owner 仍生效
    _sc.register({
        'name': 'AkOwned',
        'collection': 'ak_owned',
        'idPrefix': 'ako_',
        'timestamps': False,
        'read': ['creator'],
        'fields': {
            'satCode': {'type': 'string'},
            'title': {'type': 'string'},
            'createdBy': {'type': 'string'},
        },
        'relations': {},
        'accessKeys': {
            'satCode': {'read': 'satsRead', 'write': 'satsWrite', 'bypassRoles': ['admin']},
        },
    })
    # 字段白名单（fields.*.write）叠加：不可写字段被过滤，access 校验只看可落库字段
    _sc.register({
        'name': 'AkGuarded',
        'collection': 'ak_guarded',
        'idPrefix': 'akg_',
        'timestamps': False,
        'fields': {
            'satCode': {'type': 'string', 'write': ['viewer']},
            'note': {'type': 'string', 'write': ['nobody']},
        },
        'relations': {},
        'accessKeys': {
            'satCode': {'read': 'satsRead', 'write': 'satsWrite'},
        },
    })


def _setup():
    """内存后端 + 预置数据（ctx=None 系统写入，不受 accessKeys 管控）"""
    _run(init(connect({'database': 'akdb', 'store': {}})))

    async def _seed():
        await store.insert('AkItem', {
            '_id': 'ak_r1', 'satCode': 'GEO', 'title': 'a', 'tags': ['x'], 'createdBy': 'u1',
        })
        await store.insert('AkItem', {
            '_id': 'ak_r2', 'satCode': 'LEO', 'title': 'b', 'tags': ['x', 'y'], 'createdBy': 'u2',
        })
        await store.insert('AkItem', {
            '_id': 'ak_r3', 'satCode': 'MARS', 'title': 'c', 'tags': ['y'], 'createdBy': 'u1',
        })
        await store.insert('AkOwned', {
            '_id': 'ako_1', 'satCode': 'GEO', 'title': 'o1', 'createdBy': 'u1',
        })
        await store.insert('AkOwned', {
            '_id': 'ako_2', 'satCode': 'GEO', 'title': 'o2', 'createdBy': 'u2',
        })
        await store.insert('AkOwned', {
            '_id': 'ako_3', 'satCode': 'MARS', 'title': 'o3', 'createdBy': 'u1',
        })

    _run(_seed())


def _ctx(roles=('viewer',), user_id='u1', **lists):
    perm.set_context({'userId': user_id, 'roles': list(roles), **lists})


def _teardown():
    perm.set_context(None)
    _ds.set_connections({})


def _ids(rows):
    return sorted(r['_id'] for r in rows)


# ─── 1. 声明校验（注册期 fail-fast） ──────────────────────────

def test_invalid_declaration_rejected():
    with pytest.raises(Exception) as ei:
        _sc.register({
            'name': 'AkBad',
            'collection': 'ak_bad',
            'timestamps': False,
            'fields': {'title': {'type': 'string'}},
            'relations': {},
            'accessKeys': {'ghost': {'read': 'x'}},
        })
    assert 'accessKeys' in str(ei.value), f"错误应说明 accessKeys: {ei.value}"


# ─── 2. 读路径：单值成员 + 数组交集（多键 AND） ───────────────

def test_read_injects_member_and_intersection():
    try:
        _setup()
        _ctx(satsRead=['GEO', 'LEO'], tagsRead=['x'])
        rows = _run(store.query('AkItem{ _id title }'))
        # r3 satCode 不在清单；r1/r2 命中（r2 tags 交集 x）
        assert _ids(rows) == ['ak_r1', 'ak_r2'], rows
    finally:
        _teardown()


# ─── 3. ctx 缺键 → fail closed ────────────────────────────────

def test_missing_ctx_key_fails_closed():
    try:
        _setup()
        _ctx(tagsRead=['x'])  # 缺 satsRead
        with pytest.raises(store.PermissionError) as ei:
            _run(store.query('AkItem{ _id }'))
        assert 'ACCESS:' in str(ei.value), f"错误应含 ACCESS 语义: {ei.value}"
    finally:
        _teardown()


# ─── 4. 空清单 → 读空结果（与缺键严格区分） ──────────────────

def test_empty_read_list_returns_nothing():
    try:
        _setup()
        _ctx(satsRead=[], tagsRead=['x'])
        assert _run(store.query('AkItem{ _id }')) == []
    finally:
        _teardown()


# ─── 5. 写路径：单值越界 / 数组非子集拒绝，子集放行 ──────────

def test_write_scalar_out_of_list_denied():
    try:
        _setup()
        _ctx(satsWrite=['GEO', 'LEO'], tagsWrite=['x'])
        with pytest.raises(store.PermissionError, match='ACCESS:'):
            _run(store.insert('AkItem', {'satCode': 'MARS', 'title': 'n', 'tags': ['x']}))
    finally:
        _teardown()


def test_write_array_not_subset_denied():
    try:
        _setup()
        _ctx(satsWrite=['GEO', 'LEO'], tagsWrite=['x'])
        with pytest.raises(store.PermissionError, match='ACCESS:'):
            _run(store.insert('AkItem', {'satCode': 'GEO', 'title': 'n', 'tags': ['x', 'z']}))
    finally:
        _teardown()


def test_write_subset_ok():
    try:
        _setup()
        _ctx(satsWrite=['GEO', 'LEO'], tagsWrite=['x'])
        row = _run(store.insert('AkItem', {'satCode': 'GEO', 'title': 'n', 'tags': ['x']}))
        assert str(row['_id']).startswith('ak_')
    finally:
        _teardown()


# ─── 6. bypassRoles 只跳过该键，owner 条件仍生效 ─────────────

def test_bypass_role_skips_key_only():
    try:
        _setup()
        _ctx(roles=('admin',), user_id='u1')  # 未提供 satsRead，但 admin 命中 bypass
        rows = _run(store.query('AkOwned{ _id title }'))
        # access 键被跳过，但 read=['creator'] 的 owner 条件仍注入 → 只看到 u1 自己的行
        assert _ids(rows) == ['ako_1', 'ako_3'], rows
    finally:
        _teardown()


# ─── 7. 字段白名单叠加：不可写字段被过滤、access 校验照常 ────

def test_field_whitelist_filters_before_access_check():
    try:
        _setup()
        _ctx(satsRead=['GEO', 'LEO'], satsWrite=['GEO', 'LEO'])
        # note 不可写 → 过滤；satCode=GEO 在清单 → 放行，落库不含 note
        row = _run(store.insert('AkGuarded', {'satCode': 'GEO', 'note': 'secret'}))
        stored = _run(store.query('AkGuarded{ _id satCode note }'))
        assert [r['_id'] for r in stored] == [row['_id']]
        assert 'note' not in stored[0], stored
        # 同一 schema 下越界值仍被 access 校验拒绝
        with pytest.raises(store.PermissionError, match='ACCESS:'):
            _run(store.insert('AkGuarded', {'satCode': 'MARS', 'note': 'secret'}))
    finally:
        _teardown()
