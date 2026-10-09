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

R2（03 §3.2）：``emit`` 的 sink 与 ns 标签**作用域优先**——作用域内取
``current_scope().sink`` / ``.meta``，未进入作用域则回退进程级 ``_sink`` / ``_meta``
（fail-open 姿态不变；``fail_count`` / ``_pending`` 仍为进程级聚合，设计 D5 保留）。
"""

import asyncio
import sys
import time

from .scope import current_scope

_sink = None

# 进程级 ns 标签（进程级隔离下天然单 ns；由宿主 set_meta 注入）
_meta = {'tenant': '', 'env': ''}

# 落库失败累计计数（进程级；>0 表示有事件未入表——可观测，不静默）
_fail_count = 0

# 在途落库任务（进程级；graceful shutdown 前经 flush() 收口，消除 fire-and-forget 丢事件窗口 D8）
_pending: set = set()


def _current_sink():
    """当前生效 sink（R2）：作用域 sink 优先，未进入作用域回退进程级 ``_sink``"""
    s = current_scope()
    return s.sink if s is not None and s.sink is not None else _sink


def _current_meta():
    """当前生效 ns 标签（R2）：作用域 meta 优先，未进入作用域回退进程级 ``_meta``"""
    s = current_scope()
    return s.meta if s is not None and s.meta is not None else _meta


def get_sink():
    """当前 sink（None = 默认 stderr 行为）；供接管方保存原值、退出时恢复（token-set/reset 同构）"""
    return _sink


def set_sink(fn):
    """注册反馈事件回调 ``fn(event: dict)``；传 None 恢复默认 stderr 行为"""
    global _sink
    _sink = fn


def set_meta(meta):
    """注入进程级 ns 标签（tenant/env），供落库 sink 附加到事件"""
    global _meta
    _meta = {**_meta, **(meta or {})}


def fail_count():
    """落库失败累计计数（进程级；>0 表示有事件未入表——可观测，不静默）"""
    return _fail_count


def _fail(msg):
    global _fail_count
    _fail_count += 1
    print(f'[py-store][feedback] {msg}', file=sys.stderr)


async def _write_feedback(store, row):
    """落库（internal 上下文——__feedback write 空名单仅放行 internal/admin）；失败不静默、不抛回 emit"""
    from . import metadef as _metadef
    try:
        with _metadef._internal_ctx():
            await store.insert('__feedback', row)
    except Exception as e:  # 落库失败：stderr + 计数，绝不抛回 emit（不破坏主链路）
        _fail(f'__feedback 落库失败: {e}')


def enable_feedback_table(store):
    """一键接线：注册内建 ``__feedback`` 并把 sink 指向落库；返回 disposer（恢复原 sink）

    落库为异步 fire-and-forget（有运行中事件循环时 ``create_task``）；失败走 stderr + 计数，
    绝不抛回 ``emit``。未调用本函数时 ``emit`` 保持原 stderr 行为（不改变默认语义）。
    """
    from . import metadef as _metadef
    _metadef.ensure_builtins()  # 幂等（含 __feedback）
    prev = get_sink()

    def _sink(event):
        # 事件类别键 `type` 与 field 级契约保留键冲突（core §6.3）→ 落库列名为 `eventType`
        e = event or {}
        row = {k: v for k, v in e.items() if k != 'type'}
        row['eventType'] = e.get('type')
        ns = _current_meta()
        row.update({'tenant': ns.get('tenant', ''),
                    'env': ns.get('env', ''), 'now': int(time.time() * 1000)})
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            task = loop.create_task(_write_feedback(store, row))
            _pending.add(task)
            task.add_done_callback(_pending.discard)   # 完成即出集合
        else:
            _fail('无运行中事件循环，__feedback 事件未落库')

    set_sink(_sink)
    return lambda: set_sink(prev)


async def flush():
    """等待全部在途 `__feedback` 落库任务完成（graceful shutdown 前调用）。

    落库失败已由 ``_write_feedback`` 计为 ``fail_count``（不抛），故此处 ``gather`` 不冒泡。
    """
    while _pending:
        await asyncio.gather(*list(_pending), return_exceptions=True)


def emit(event):
    """产出一条反馈事件：作用域/进程级 sink 回调之；否则打印 stderr（允许拦截，禁止静默）"""
    sink = _current_sink()
    if sink is not None:
        sink(event)
        return
    print(
        f"[py-store][{event.get('layer', '?')}/{event.get('code', '?')}] "
        f"{event.get('message', '')}（hint: {event.get('hint', '-')}）",
        file=sys.stderr,
    )
