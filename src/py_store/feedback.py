"""反馈事件通道 —— 兜底/降级/拦截触发的统一出口

事件形状（对齐 rust-store 联邦 degraded 契约）::

    {type, code, layer, message, hint, ...}
      - type  : 事件类别（federation_degraded / sql_pushdown_unsupported ...）
      - code  : 机器可读代码（crossSourceSort / pushdownUnsupported ...）
      - layer : 命中的防护层（federation / dialect ...）
      - message: 人可读描述
      - hint  : 修复指引（供上游排查/加固）

默认无 sink 时打 stderr（向后兼容）；宿主可 ``set_sink(fn)`` 接管，
接入自动反馈闭环（允许被拦截，禁止静默失守）。
"""

import sys

_sink = None


def set_sink(fn):
    """注册反馈事件回调 ``fn(event: dict)``；传 None 恢复默认 stderr 行为"""
    global _sink
    _sink = fn


def emit(event):
    """产出一条反馈事件：有 sink 回调之；否则打印 stderr（允许拦截，禁止静默）"""
    if _sink is not None:
        _sink(event)
        return
    print(
        f"[py-store][{event.get('layer', '?')}/{event.get('code', '?')}] "
        f"{event.get('message', '')}（hint: {event.get('hint', '-')}）",
        file=sys.stderr,
    )
