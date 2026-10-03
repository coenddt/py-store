"""缓存状态注记（B6）—— 协议层 `x-cache` 响应头的唯一取值来源。

本轮只落**注记位**：不实现任何实际缓存。无 provider（默认）恒返回 'BYPASS'
（响应未经过缓存）——注记必须为真，禁把无缓存谎报为 HIT/MISS。
provider 由接入方在实现缓存时经 set_cache_status 注入，签名 (ctx) -> 'HIT'|'MISS'|'BYPASS'。
"""

from .feedback import emit

_VALUES = ('HIT', 'MISS', 'BYPASS')
_provider = None


def set_cache_status(fn):
    """注册缓存状态 provider；传 None / 非可调用恢复默认（恒 BYPASS）"""
    global _provider
    _provider = fn if callable(fn) else None


def cache_status(ctx=None):
    """取当前响应的缓存状态注记（恒为 HIT|MISS|BYPASS 之一；非法/异常 → BYPASS + 反馈留痕）"""
    if _provider is None:
        return 'BYPASS'
    try:
        v = _provider(ctx)
    except Exception as e:  # 异常回落 BYPASS 且必须留痕（禁静默）
        emit({'type': 'cache_status_failed', 'code': 'cacheStatusFailed', 'layer': 'host',
              'message': f'缓存状态 provider 抛错: {e}',
              'hint': '检查 set_cache_status 注入的 provider；本轮注记位不实现缓存，异常即回落 BYPASS'})
        return 'BYPASS'
    if v not in _VALUES:
        emit({'type': 'cache_status_invalid', 'code': 'cacheStatusInvalid', 'layer': 'host',
              'message': f'缓存状态取值非法: {v!r}（值域 HIT|MISS|BYPASS）',
              'hint': 'provider 返回值必须为 HIT/MISS/BYPASS 之一；非法值回落 BYPASS'})
        return 'BYPASS'
    return v
