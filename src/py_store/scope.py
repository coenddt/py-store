"""执行作用域（R2）：一请求/一安全域一份 {view, sink, meta, secure}；未进入回退 base。"""

import contextlib
import contextvars
from collections.abc import Callable
from typing import Any


class _Scope:
    """单次请求的作用域快照（view/sink/meta/secure 四槽，进入作用域后不可变）。"""

    # 槽序按字母序（RUF023：__slots__ 须自然排序），与 __init__ 形参顺序无关
    __slots__ = ("meta", "secure", "sink", "view")

    def __init__(
        self,
        view: Any,
        sink: Callable[[dict[str, Any]], Any] | None = None,
        meta: dict[str, Any] | None = None,
        secure: bool | None = None,
    ) -> None:
        """构造作用域快照：view（core 派生视图）、sink（反馈 sinks）、meta、secure（安全域配置）"""
        self.view = view
        self.sink = sink
        self.meta = meta
        self.secure = secure


_scope_var: contextvars.ContextVar[_Scope | None] = contextvars.ContextVar(
    "store_scope", default=None
)


def current_scope() -> _Scope | None:
    """当前生效作用域（_Scope 实例）；未进入作用域时返回 None（调用方回退 base）"""
    return _scope_var.get()  # None = 未进入作用域


def with_scope(
    view: Any,
    sink: Callable[[dict[str, Any]], Any] | None = None,
    meta: dict[str, Any] | None = None,
    secure: bool | None = None,
) -> Any:
    """上下文管理器：进入作用域，退出即 reset（嵌套安全，V3 已证）。"""
    return _ctx(view, sink, meta, secure)


@contextlib.contextmanager
def _ctx(
    view: Any,
    sink: Callable[[dict[str, Any]], Any] | None,
    meta: dict[str, Any] | None,
    secure: bool | None,
) -> Any:
    """with_scope 的实现体：set/_Scope 入栈、退出时 token reset（嵌套安全）"""
    token = _scope_var.set(_Scope(view, sink, meta, secure))
    try:
        yield
    finally:
        _scope_var.reset(token)
