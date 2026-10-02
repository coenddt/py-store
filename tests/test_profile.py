"""档位（profile）门禁 —— 双门禁分离：standard 放开 / text2query 功能收缩

对标 nodejs-store/tests/profile.test.js：
  1. 档位读写与未知档 ValueError（禁静默回落默认档）；
  2. ``text2query()`` 上下文：进入设档、退出恢复原档（token-set/reset，嵌套安全）；
  3. text2query 档强制 ctx：无 ctx 即 ``ProfileViolation`` + emit ``profile_blocked``（禁静默）；
  4. ``_call`` 前缀映射：``ERR_TEXT2QUERY:`` → ``ProfileViolation``（400）并提取 feature；
  5. standard 档 fail-open：无 ctx 照常查询（既有调用方零感知）；
  6. ``route_override`` 受信来源 Host 兜底：text2query 档非空即拒 + emit（layer='host'）；
     standard 档放行（判决唯一在 core，Host 仅兜底）。

运行：$env:LOCAL_CORE='1'; $env:PYTHONPATH='src'; python -m pytest tests/test_profile.py -q
"""

import asyncio

import pytest

from py_store import crud as _crud_mod
from py_store import feedback, store
from py_store import permission as perm
from py_store import schema as _sc
from py_store.crud import exec as _exec_mod
from py_store.crud.query import _guard_route_override


def _register_test_schemas():
    """模块 schema 注册入口（conftest 模块隔离夹具在首用例前调用；import 零副作用）"""
    _sc.register({
        'name': 'PqModel', 'collection': 'pq_model', 'idPrefix': 'PQ', 'timestamps': False,
        'fields': {'title': 'string'}, 'relations': {}, 'read': None, 'write': None,
    })


def _run(coro):
    return asyncio.run(coro)


# ── 最小内存驱动 mock（与 test_require_context._Coll 同构） ──

class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    async def to_list(self, length=None):
        return list(self._docs)


class _Coll:
    def __init__(self, docs=None):
        self.docs = docs if docs is not None else []

    def find(self, query=None, projection=None):
        return _Cursor(self.docs)

    async def aggregate(self, pipeline):
        return _Cursor(self.docs)


class _FakeDb:
    def __init__(self, docs=None):
        self._coll = _Coll(docs)

    def __getitem__(self, name):
        return self._coll


def _mock(docs=None):
    perm.set_context(None)
    db = _FakeDb(docs)
    _crud_mod.set_db(db)
    return db._coll


@pytest.fixture(autouse=True)
def _reset_profile():
    """每用例复位档位/上下文/反馈 sink —— 档位为进程级全局，防用例间串扰"""
    _sc.set_profile('standard')
    perm.set_context(None)
    feedback.set_sink(None)
    yield
    _sc.set_profile('standard')
    perm.set_context(None)
    feedback.set_sink(None)


# ── 1. 档位读写 ───────────────────────────────────────────────

def test_profile_default_is_standard():
    assert _sc.get_profile() == 'standard'
    assert store.get_profile() == 'standard'
    assert store.getProfile() == 'standard'


def test_profile_roundtrip():
    _sc.set_profile('text2query')
    assert _sc.get_profile() == 'text2query'
    store.setProfile('standard')
    assert store.get_profile() == 'standard'


def test_profile_unknown_raises_and_keeps_previous():
    with pytest.raises(ValueError):
        _sc.set_profile('nope')
    # 未知档必须 Err，且不得静默回落到默认档（前值 standard 保持不变）
    assert _sc.get_profile() == 'standard'
    _sc.set_profile('text2query')
    with pytest.raises(ValueError):
        store.set_profile('Standard')  # 大小写敏感，非标准值同样 Err
    assert _sc.get_profile() == 'text2query'


# ── 2. text2query() 上下文 ────────────────────────────────────

def test_text2query_context_sets_and_restores():
    assert _sc.get_profile() == 'standard'
    with store.text2query():
        assert _sc.get_profile() == 'text2query'
    assert _sc.get_profile() == 'standard'


def test_text2query_context_nested_restores_each_level():
    _sc.set_profile('standard')
    with store.text2query():
        assert _sc.get_profile() == 'text2query'
        with store.text2query():
            assert _sc.get_profile() == 'text2query'
        assert _sc.get_profile() == 'text2query'
    assert _sc.get_profile() == 'standard'


def test_text2query_context_restores_on_exception():
    with pytest.raises(RuntimeError):
        with store.text2query():
            assert _sc.get_profile() == 'text2query'
            raise RuntimeError('boom')
    assert _sc.get_profile() == 'standard'


# ── 3. text2query 档强制 ctx + 反馈 ───────────────────────────

def test_text2query_blocks_without_ctx_and_emits():
    _mock([{'_id': '1', 'title': 'a'}])
    events = []
    feedback.set_sink(events.append)
    with store.text2query():
        with pytest.raises(store.ProfileViolation) as ei:
            _run(_crud_mod.query('PqModel{title}'))
    # ProfileViolation = 调用方合约违反（400），非授权问题（403）
    assert ei.value.status == 400
    assert 'text2query' in str(ei.value)
    # 拦截必须产反馈（允许拦截，禁止静默）
    assert len(events) == 1
    ev = events[0]
    assert ev['type'] == 'profile_blocked'
    assert ev['code'] == 'profileBlocked'
    assert ev['layer'] == 'core'
    assert ev['profile'] == 'text2query'
    # 该条文案（无 [..]）不含门禁项名 → feature 显式留白，不伪造
    assert ev['feature'] is None
    assert ev['message']
    assert ev['hint']


def test_text2query_with_user_ctx_passes():
    _mock()
    perm.set_context({'userId': 'u1', 'roles': ['user']})
    with store.text2query():
        assert _run(_crud_mod.query('PqModel{title}')) == []


# ── 4. _call 前缀映射（含 feature 提取） ──────────────────────

def test_call_maps_profile_prefix_with_feature():
    events = []
    feedback.set_sink(events.append)

    def boom():
        raise RuntimeError('ERR_TEXT2QUERY:text2query 档禁用 [$pipeline 直通]（功能收缩）')

    with pytest.raises(store.ProfileViolation) as ei:
        _exec_mod._call(boom)
    assert str(ei.value) == 'text2query 档禁用 [$pipeline 直通]（功能收缩）'
    assert ei.value.status == 400
    assert events[0]['feature'] == '$pipeline 直通'


def test_call_leaves_other_errors_untouched():
    def boom():
        raise ValueError('ERR_NO_CONTEXT')

    # 非档位前缀原样上抛（不吞错、不误映射）
    with pytest.raises(ValueError):
        _exec_mod._call(boom)


# ── 5. standard 档零感知（fail-open） ─────────────────────────

def test_standard_profile_fail_open_without_ctx():
    _mock([{'_id': '1', 'title': 'a'}])
    assert _sc.get_profile() == 'standard'
    items = _run(_crud_mod.query('PqModel{title}'))
    assert len(items) == 1 and items[0]['title'] == 'a'


# ── 6. route_override 受信来源 Host 兜底 ──────────────────────

def test_text2query_blocks_route_override_and_emits_host():
    _mock()
    events = []
    feedback.set_sink(events.append)
    with store.text2query():
        with pytest.raises(store.ProfileViolation) as ei:
            _run(_crud_mod.query('PqModel{title}', route_override={'source': 'x'}))
    # 档位拒绝 = 调用方合约违反（400）
    assert ei.value.status == 400
    assert 'route_override' in str(ei.value)
    # 兜底命中必产反馈：layer='host' 表明 core 层未拦住（自动反馈原则）
    assert len(events) == 1
    ev = events[0]
    assert ev['type'] == 'profile_blocked'
    assert ev['code'] == 'profileBlocked'
    assert ev['layer'] == 'host'
    assert ev['profile'] == 'text2query'
    assert ev['feature'] == 'route_override'
    assert ev['hint']


def test_standard_route_override_not_blocked_by_host_guard():
    # standard 档：受信可用，Host 兜底放行；未携带（None）同样放行
    events = []
    feedback.set_sink(events.append)
    _guard_route_override({'source': 'x'})
    _guard_route_override(None)
    assert events == []


def test_text2query_query_one_and_with_count_also_blocked():
    _mock()
    feedback.set_sink(lambda _e: None)
    with store.text2query():
        with pytest.raises(store.ProfileViolation):
            _run(_crud_mod.query_one('PqModel{title}', route_override={'source': 'x'}))
        with pytest.raises(store.ProfileViolation):
            _run(_crud_mod.query_with_count('PqModel{title}', route_override={'source': 'x'}))
