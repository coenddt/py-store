"""缓存状态 provider —— 「注记为真」（04 §4.3，与 ``nodejs-store/src/cache/provider.js`` 同构）。

命中真实性由**本次请求选中链路是否 ``role == 'cache'``**决定，不由皮伪造：
读请求级选中链路记录（``context.py#current_selection``，来源 02 的 ``plan['links']``）后：

  1. 无 ``role == 'cache'`` 链路 ⇒ ``'BYPASS'``（本请求不涉及缓存）；
  2. 有且本次**选中**它 ⇒ ``'HIT'``；有但**未选中** ⇒ ``'MISS'``；
  3. 读不到选中信息（非查询请求 / 未记录）⇒ ``'BYPASS'``（绝不猜）。

「选中」判定：``plan['links']`` 为各 schema 的候选链路，已由 core ``resolve_read_links``
按择优排序 —— 取每组**首项**为本次选中落点；任一所查 schema 选中 ``role=cache`` 即 ``HIT``。

契约（逐字对齐 §4.3）：返回值恒为 ``HIT|MISS|BYPASS`` 之一；**provider 自身不得吞异常、
不得返回非法值** —— 回落 ``BYPASS`` + 反馈留痕统一由 ``cache_status``（``cache/__init__.py``）处理。
"""

from __future__ import annotations

from typing import Any, Callable

from .context import current_selection


def create_link_cache_provider(spec: dict | None = None) -> Callable[[Any], str]:
    """创建「链路选择驱动」的缓存状态 provider。

    :param spec: ``{'selection_of': <(ctx) -> links|None>}``；缺省读请求级
        ``current_selection()``（可注入以复用于非 contextvars 场景 / 测试）
    """
    spec = spec or {}
    sel_of = spec.get('selection_of')
    # 缺省读请求级记录：current_selection() 无参，包一层吞掉 ctx（对齐 node `currentSelection`）
    selection_of = sel_of if callable(sel_of) else (lambda ctx=None: current_selection())

    def provider(ctx: Any = None) -> str:
        sel = selection_of(ctx)
        if sel is None:
            return 'BYPASS'                        # 3. 读不到 ⇒ 绝不猜
        groups = [v for v in sel.values() if isinstance(v, list)]
        flat = [link for g in groups for link in g]
        if not any(isinstance(link, dict) and link.get('role') == 'cache' for link in flat):
            return 'BYPASS'                        # 1. 无 cache 链路 ⇒ 本请求不涉缓存
        selected_cache = any(
            g and isinstance(g[0], dict) and g[0].get('role') == 'cache' for g in groups)
        return 'HIT' if selected_cache else 'MISS'  # 2. 选中判定（与 node 同规：候选序首项）

    return provider


__all__ = ['create_link_cache_provider']
