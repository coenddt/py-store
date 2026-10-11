"""04 · 缓存角色：注记为真（A8/A9）—— Python 侧，对齐 nodejs-store/tests/cache-link.test.js

A8：无 provider 恒 BYPASS；注入 provider 后 `HIT` 仅在本次**选中**链路 `role == 'cache'`
    时出现（有 cache 链路但未选中 ⇒ `MISS`；无 cache 链路 / 未记录 ⇒ `BYPASS`）；
    异常 / 非法值 ⇒ `BYPASS` + feedback 留痕。
A9：载体不可用（Redis 连接 / 命令失败）⇒ 显式 `RedisIoError`，禁静默当空快照。

既有 `py-store/tests/test_cache.py` 是注记位语义的回归基线，本文件不触碰它。
运行：$env:LOCAL_CORE='1'; $env:PYTHONPATH='py-store/src'; python -m pytest py-store/tests/test_cache_link.py -q
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

import pytest

from py_store import cache, feedback
from py_store import schema as _sc
from py_store.cache.context import current_selection, run_with_selection
from py_store.cache.provider import create_link_cache_provider
from py_store.cache.redis_io import RedisIoError, create_redis_io


def _collect(body):
    """收集期间 emit 的事件（用后恢复原 sink，并复位 provider 防跨用例污染）"""
    prev = feedback.get_sink()
    events = []
    feedback.set_sink(events.append)
    try:
        body()
    finally:
        feedback.set_sink(prev)
        cache.set_cache_status(None)
    return events


def _in_scope(links, fn):
    """在「本次选中链路」作用域内执行 fn（run_with_selection 为协程，经 asyncio.run 驱动）"""
    return asyncio.run(run_with_selection(links, fn))


L_CACHE_FIRST = {
    'User': [
        {'source': 'cache_src', 'database': 'db', 'schema': None, 'role': 'cache', 'priority': 0},
        {'source': 'main_src', 'database': 'db', 'schema': None, 'role': 'primary', 'priority': 1},
    ],
}
L_CACHE_SECOND = {
    'User': [
        {'source': 'main_src', 'database': 'db', 'schema': None, 'role': 'primary', 'priority': 0},
        {'source': 'cache_src', 'database': 'db', 'schema': None, 'role': 'cache', 'priority': 1},
    ],
}
L_NO_CACHE = {
    'User': [
        {'source': 'main_src', 'database': 'db', 'schema': None, 'role': 'primary', 'priority': 0},
    ],
}


def test_a8_1_no_provider_bypass():
    """A8①：无 provider → 恒 BYPASS（注记必须为真）"""
    cache.set_cache_status(None)
    assert cache.cache_status({}) == 'BYPASS'


def test_a8_2_selected_cache_hit():
    """A8②：声明 cache 链路且本次选中 → HIT"""
    cache.set_cache_status(create_link_cache_provider())
    try:
        assert _in_scope(L_CACHE_FIRST, lambda: cache.cache_status({})) == 'HIT'
    finally:
        cache.set_cache_status(None)


def test_a8_3_cache_not_selected_miss():
    """A8③：有 cache 链路但本次未选中 → MISS"""
    cache.set_cache_status(create_link_cache_provider())
    try:
        assert _in_scope(L_CACHE_SECOND, lambda: cache.cache_status({})) == 'MISS'
    finally:
        cache.set_cache_status(None)


def test_a8_3b_no_cache_or_untracked_bypass():
    """A8③b：无 cache 链路 / 未记录 → BYPASS（绝不猜）"""
    cache.set_cache_status(create_link_cache_provider())
    try:
        assert _in_scope(L_NO_CACHE, lambda: cache.cache_status({})) == 'BYPASS'
        assert current_selection() is None, '作用域外应为 None'
        assert cache.cache_status({}) == 'BYPASS', '未记录选中信息 ⇒ BYPASS'
    finally:
        cache.set_cache_status(None)


def test_a8_4_provider_error_or_invalid_bypass_with_feedback():
    """A8④：provider 抛错 / 返回非法值 → BYPASS 且 feedback 留痕"""
    def _failed():
        def _boom(ctx):
            raise RuntimeError('boom')

        cache.set_cache_status(_boom)
        assert cache.cache_status({}) == 'BYPASS'

    failed = _collect(_failed)
    assert len(failed) == 1
    assert failed[0]['code'] == 'cacheStatusFailed'
    assert failed[0]['layer'] == 'host'

    def _invalid():
        cache.set_cache_status(lambda ctx: 'NOPE')
        assert cache.cache_status({}) == 'BYPASS'

    invalid = _collect(_invalid)
    assert len(invalid) == 1
    assert invalid[0]['code'] == 'cacheStatusInvalid'


def test_a8_e2e_real_plan_primary_first_miss():
    """A8 端到端：真实 plan.links（primary 优先）⇒ MISS（注记与选中一致）"""
    _sc.register({
        'name': 'ClUser', 'collection': 'cl_users', 'idPrefix': 'clu_', 'timestamps': False,
        'datasource': 'cl_main', 'fields': {'name': {'type': 'string'}}, 'relations': {},
    })
    core = _sc.get_core()
    ds = {'sources': {'cl_cache': 'mongodb', 'cl_main': 'mongodb'}}
    gql = 'ClUser{name}'

    # core 语义：links 恒以 `role=primary`（rank 最优）为首，即本次选中主链路。
    # 故真实计划的注记应为 MISS（未命中缓存），绝非 HIT —— 这正是「注记为真」。
    core.apply_link_decls({
        'sources': {
            'cl_cache': {'kind': 'mongodb', 'databases': ['cldb']},
            'cl_main': {'kind': 'mongodb', 'databases': ['cldb']},
        },
        'defs': ['schema'],
        'links': {
            'ClUser': [
                {'source': 'cl_main', 'role': 'primary', 'priority': 0, 'database': 'cldb'},
                {'source': 'cl_cache', 'role': 'cache', 'priority': 5, 'database': 'cldb'},
            ],
        },
    })
    plan = core.plan_federated(gql, {}, None, ds)
    assert plan.get('links') and plan['links'].get('ClUser'), 'plan 应含 ClUser 链路'
    assert plan['links']['ClUser'][0]['role'] == 'primary', 'primary（rank 最优）为选中首项'
    assert any(l['role'] == 'cache' for l in plan['links']['ClUser']), '仍含 cache 候选（未选中）'

    cache.set_cache_status(create_link_cache_provider())
    try:
        assert _in_scope(plan['links'], lambda: cache.cache_status({})) == 'MISS'
        # 对照：若本次选中 cache（cache 为首项，如 P2 主链路不可达被跳过）⇒ HIT（见 A8②）
        cache_first = {
            'ClUser': [
                {'source': 'cl_cache', 'role': 'cache', 'priority': 0, 'database': 'cldb'},
            ],
        }
        assert _in_scope(cache_first, lambda: cache.cache_status({})) == 'HIT'
    finally:
        cache.set_cache_status(None)


class _ScanFailClient:
    def scan(self, pattern):
        raise RuntimeError('conn refused')

    def get(self, key):
        return None

    def set(self, key, value):
        pass


class _GetFailClient:
    def scan(self, pattern):
        return ['store/db/orders']

    def get(self, key):
        raise RuntimeError('read timeout')

    def set(self, key, value):
        pass


class _EmptyScanClient:
    def scan(self, pattern):
        return []

    def get(self, key):
        return None

    def set(self, key, value):
        pass


def test_a9_redis_unavailable_explicit_error():
    """A9：载体不可用（Redis 命令失败）⇒ 显式 Err，禁静默当空快照"""
    io = create_redis_io(client=_ScanFailClient())
    with pytest.raises(RedisIoError) as ei:
        asyncio.run(io['load']('db'))
    assert ei.value.op == 'scan'

    io3 = create_redis_io(client=_GetFailClient())
    with pytest.raises(RedisIoError) as ei3:
        asyncio.run(io3['load']('db'))
    assert ei3.value.op == 'get'

    # 连接正常（scan 空）⇒ 空快照（非静默掩盖：无键即空，可区分）
    io2 = create_redis_io(client=_EmptyScanClient())
    assert asyncio.run(io2['load']('db')) == {}
