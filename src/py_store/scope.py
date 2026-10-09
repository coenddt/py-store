"""执行作用域（R2）：一请求/一安全域一份 {view, sink, meta, secure}；未进入回退 base。"""
import contextlib
import contextvars


class _Scope:
    # 槽序按字母序（RUF023：__slots__ 须自然排序），与 __init__ 形参顺序无关
    __slots__ = ('meta', 'secure', 'sink', 'view')
    def __init__(self, view, sink=None, meta=None, secure=None):
        self.view = view
        self.sink = sink
        self.meta = meta
        self.secure = secure


_scope_var: contextvars.ContextVar = contextvars.ContextVar('store_scope', default=None)


def current_scope():
    return _scope_var.get()                 # None = 未进入作用域


def with_scope(view, sink=None, meta=None, secure=None):
    """上下文管理器：进入作用域，退出即 reset（嵌套安全，V3 已证）。"""
    return _ctx(view, sink, meta, secure)


@contextlib.contextmanager
def _ctx(view, sink, meta, secure):
    token = _scope_var.set(_Scope(view, sink, meta, secure))
    try:
        yield
    finally:
        _scope_var.reset(token)
