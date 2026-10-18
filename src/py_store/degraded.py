"""请求级降级容器（与 nodejs-store/src/degraded.js 语义逐条一致）。

容器用 ContextVar 保存**可变对象**：Starlette 的 BaseHTTPMiddleware 经
``call_next`` 在子 task 执行 endpoint，子 task 复制到的是同一容器引用，
故子 task 内 ``record`` 的写入对父 task 可见（值不可变则不可见）。
"""

from __future__ import annotations

import contextvars
from typing import Any

from .feedback import emit

_BUCKET: contextvars.ContextVar = contextvars.ContextVar(
    'py_store_degraded', default=None)


def begin() -> contextvars.Token:
    """建立请求容器（HTTP 中间件调用）"""
    return _BUCKET.set({'events': []})


def end(token: contextvars.Token) -> None:
    try:
        _BUCKET.reset(token)
    except ValueError:  # 跨上下文 reset（理论上不可达）：不掩盖，仅复位失败
        pass


def record(event: dict[str, Any]) -> None:
    evt = dict(event or {})
    bucket = _BUCKET.get()
    if bucket is not None:
        bucket['events'].append(evt)
    emit(evt)


def emit_plan(plan: dict[str, Any] | None, fallback_type: str) -> None:
    for d in (plan or {}).get('degraded') or []:
        item = dict(d or {})
        item.setdefault('type', fallback_type)
        item['type'] = item.get('type') or fallback_type
        record(item)


def status() -> dict[str, Any]:
    bucket = _BUCKET.get()
    events = list(bucket['events']) if bucket else []
    return {'degraded': bool(events), 'events': events}


def vector_degraded(*, backend: str, field: str, metric: str,
                    message: str, hint: str) -> None:
    record({
        'type': 'vector_degraded',
        'code': 'vectorDegraded',
        'layer': 'host',
        'backend': backend,
        'field': field,
        'metric': metric,
        'message': message,
        'hint': hint,
    })
