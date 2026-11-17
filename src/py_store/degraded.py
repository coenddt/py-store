"""请求级降级容器（与 nodejs-store/src/degraded.js 语义逐条一致）。

容器用 ContextVar 保存**可变对象**：Starlette 的 BaseHTTPMiddleware 经
``call_next`` 在子 task 执行 endpoint，子 task 复制到的是同一容器引用，
故子 task 内 ``record`` 的写入对父 task 可见（值不可变则不可见）。
"""

from __future__ import annotations

import contextvars
import inspect
from typing import Any

from .feedback import emit

_BUCKET: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "py_store_degraded", default=None
)


def begin() -> contextvars.Token[dict[str, Any] | None]:
    """建立请求容器（HTTP 中间件调用）"""
    return _BUCKET.set({"events": []})


def end(token: contextvars.Token[dict[str, Any] | None]) -> None:
    """重置请求容器；跨上下文 reset 抛 ValueError 时保持当前容器原样（不掩盖）"""
    try:
        _BUCKET.reset(token)
    except ValueError:
        # 跨上下文 reset 失败无补救路径：token 必来自 begin() 的同栈配对（理论上不可达），
        # 失败时保持当前容器原样（不掩盖、不制造新事件），与 nodejs 侧语义一致。
        pass


def record(event: dict[str, Any]) -> None:
    """记一条降级事件：拷贝后写入请求容器（无容器则只广播），并经 feedback.emit 广播"""
    evt = dict(event or {})
    bucket = _BUCKET.get()
    if bucket is not None:
        bucket["events"].append(evt)
    emit(evt)


def emit_plan(plan: dict[str, Any] | None, fallback_type: str) -> None:
    """逐条转发计划中的 degraded 事件，缺失 type 的条目补 fallback_type"""
    for d in (plan or {}).get("degraded") or []:
        item = dict(d or {})
        item.setdefault("type", fallback_type)
        item["type"] = item.get("type") or fallback_type
        record(item)


def status() -> dict[str, Any]:
    """读当前请求的降级状态：{degraded: 是否有事件, events: 事件列表快照}；无容器视为空"""
    bucket = _BUCKET.get()
    events = list(bucket["events"]) if bucket else []
    return {"degraded": bool(events), "events": events}


async def with_request(fn: Any) -> dict[str, Any]:
    """请求级降级作用域：建立请求容器执行 fn（同步 / 异步均可），返回 ``{result, status}``
    （协议皮用于 ``x-store-degraded``；与 nodejs ``degraded.withRequest`` 同构）。

    fn 抛错时容器随 finally 清理、异常原样传播（nodejs ``_als.run`` 同语义）。
    """
    token = begin()
    try:
        result = fn()
        if inspect.isawaitable(result):
            result = await result
        return {"result": result, "status": status()}
    finally:
        end(token)


def vector_degraded(
    *, backend: str, field: str, metric: str, message: str, hint: str
) -> None:
    """记录一条向量检索降级事件（固定 type=vector_degraded / code=vectorDegraded / layer=host）"""
    record(
        {
            "type": "vector_degraded",
            "code": "vectorDegraded",
            "layer": "host",
            "backend": backend,
            "field": field,
            "metric": metric,
            "message": message,
            "hint": hint,
        }
    )
