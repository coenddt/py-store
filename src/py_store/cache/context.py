"""请求级「选中链路」记录（04 §4.4，与 ``nodejs-store/src/cache/context.js`` 同构）
—— 只读透传 02 的 ``plan['links']``，不改 plan 语义。

查询路径拿到 plan 后以 ``await run_with_selection(plan.get('links'), fn)`` 包裹执行；
缓存 provider（``provider.py``）经 ``current_selection()`` 读取，据此判定本次请求是否
真的由 ``role == 'cache'`` 链路供数（注记为真的依据）。用 ``contextvars.ContextVar``
实现请求级隔离（等价 node 的 ``AsyncLocalStorage``；asyncio 每个 Task 持有自己的 context）。

未记录（非查询请求 / 02 未启用 / 单库 plan 无 ``links``）⇒ ``current_selection()``
返回 ``None``，provider 恒 ``BYPASS``（安全方向，绝不猜）。
"""

from __future__ import annotations

import contextvars
import inspect
from typing import Any, Callable

_selection_var: contextvars.ContextVar = contextvars.ContextVar('store_link_selection', default=None)


async def run_with_selection(links: Any, fn: Callable[[], Any]) -> Any:
    """在「本次选中链路」作用域内执行 ``fn``（可同步或协程；返回值原样透出）。

    :param links: 02 的 ``plan['links']``（``{ '<schema>': [{source, database, schema, role, priority}] }``）；
        空 ⇒ 记为 ``None``
    """
    token = _selection_var.set(links if links is not None else None)
    try:
        out = fn()
        if inspect.isawaitable(out):
            out = await out
        return out
    finally:
        _selection_var.reset(token)


def current_selection() -> Any:
    """取本次请求的选中链路信息（未记录 ⇒ ``None``）。"""
    return _selection_var.get()


__all__ = ['run_with_selection', 'current_selection']
