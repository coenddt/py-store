"""缓存状态注记（B6）单测：注记必须为真——无 provider 恒 BYPASS；
非法值/抛错回落 BYPASS 且走反馈通道留痕（不打断响应，禁静默）。

运行：$env:LOCAL_CORE='1'; $env:PYTHONPATH='py-store/src'; python -m pytest py-store/tests/test_cache.py -q
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from py_store import cache, feedback


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


def test_no_provider_bypass():
    cache.set_cache_status(None)
    assert cache.cache_status() == 'BYPASS'


def test_invalid_value_bypass_with_feedback():
    def _body():
        cache.set_cache_status(lambda ctx: 'NOPE')
        assert cache.cache_status() == 'BYPASS'

    events = _collect(_body)
    assert len(events) == 1
    assert events[0]['code'] == 'cacheStatusInvalid'
    assert events[0]['layer'] == 'host'


def test_provider_raises_bypass_with_feedback():
    def _body():
        def _boom(ctx):
            raise RuntimeError('boom')

        cache.set_cache_status(_boom)
        assert cache.cache_status() == 'BYPASS'

    events = _collect(_body)
    assert len(events) == 1
    assert events[0]['code'] == 'cacheStatusFailed'
    assert 'boom' in events[0]['message']


def test_valid_value_passthrough():
    def _body():
        cache.set_cache_status(lambda ctx: 'HIT')
        assert cache.cache_status() == 'HIT'

    events = _collect(_body)
    assert events == []
