"""
权限上下文 — ContextVar 请求上下文 + 自定义错误（薄适配层）

角色评估、字段过滤、所有者条件注入等权限逻辑已全部下沉 Rust core；
本模块只保留 Host 侧职责：
  - 请求上下文的隐式传递（core 的 ctx 一律显式入参，由本模块取出后传入）
  - PermissionError（core 返回的权限类错误消息由 crud 映射为本错误类型）
  - 对 core 权限方法的薄包装（保留 schema 字典入参的外部调用形态）

角色清单（共 9 种）:
    super_admin / admin / internal  ← 自动放行，无所不能
    seller / user / verified / buyer ← 大部分公共模型默认开放
    court ← TBD
    guest ← 默认禁止
    creator 是伪角色，由 core 根据 doc.createdBy === ctx.userId 动态判断
"""

import contextvars
import inspect
from contextlib import contextmanager

from .core import core

_ctx: contextvars.ContextVar = contextvars.ContextVar('py_store_ctx', default=None)


def set_context(ctx):
    """设置当前请求的上下文，每次请求开始时调用一次"""
    _ctx.set(ctx)


def get_context():
    """获取当前请求的上下文，无则 None"""
    return _ctx.get()


@contextmanager
def scoped_roles(roles):
    """以指定角色进入临时权限上下文（token-set/reset，嵌套安全），退出自动恢复原上下文。

    与 run_as_internal 同构，供 AI 查询执行等显式角色注入场景使用，
    取代 set_context + finally set_context(None) 的清空式写法（后者嵌套时会误清外层上下文，
    且「无上下文 = 权限全放行」，清空即静默失守方向）。
    """
    token = _ctx.set({**(get_context() or {}), 'roles': roles})
    try:
        yield
    finally:
        _ctx.reset(token)


@contextmanager
def scoped_context(ctx):
    """以完整上下文 ``ctx`` 进入临时权限上下文（token-set/reset，嵌套安全），退出自动恢复。

    与 :func:`scoped_roles` 同构；区别在于整体替换 ctx（保留调用方原上下文于外层），
    供 AI 问数（``ask``）等把服务端构造的用户上下文显式注入执行面的场景——
    不用「set_context + finally 清空」写法（嵌套时误清外层，静默失守方向）。
    """
    token = _ctx.set(ctx)
    try:
        yield
    finally:
        _ctx.reset(token)


async def run_as_internal(fn):
    """在内部上下文中执行操作（绕过权限检查），结束后自动恢复上下文"""
    prev = get_context()
    token = _ctx.set({**(prev or {}), 'internal': True})
    try:
        out = fn()
        # 与 crud/query.py::_finalize 统一用 isawaitable：对 partial / 包装协程同样稳妥
        # （iscoroutinefunction 对包装后的可等待对象会漏判，退化为同步返回未等待的协程）
        if inspect.isawaitable(out):
            return await out
        return out
    finally:
        _ctx.reset(token)


# ─── core 权限方法包装 ────────────────────────────────────────

def _model(schema):
    """接受 schema 字典或 schema 名称"""
    return schema['name'] if isinstance(schema, dict) else schema


def can_read_schema(schema, ctx):
    return core.can_read(_model(schema), ctx)


def can_write_schema(schema, ctx):
    return core.can_write(_model(schema), ctx)


def should_inject_owner_condition(schema, ctx):
    return core.should_inject_owner(_model(schema), ctx)


def merge_owner_condition(schema, ctx, condition):
    out = core.merge_owner_condition(_model(schema), ctx, condition)
    # core 在「不注入」时返回 null（无法区分原条件为 null）→ 原样返回入参条件
    return condition if out is None else out


def get_readable_fields(schema, ctx):
    return core.readable_fields(_model(schema), ctx)


def get_readable_relations(schema, ctx):
    return core.readable_relations(_model(schema), ctx)


def get_writable_fields(schema, ctx):
    return core.writable_fields(_model(schema), ctx)


def filter_writable_data(schema, ctx, data):
    return core.filter_writable_data(_model(schema), ctx, data)


# ─── RBAC 动态策略（core 判决；本模块零判决，仅透传） ─────────


def set_rbac(policy):
    """注入/清除 RBAC 策略。dict = 注入（解析失败 core 抛错）；None = 清除关闭"""
    return core.set_rbac(policy)


def rbac_enabled():
    """RBAC 策略是否已注入"""
    return core.rbac_enabled()


def rbac_can(model, action, ctx=None):
    """RBAC 动作判决：action ∈ {read, insert, update, remove}；RBAC 不介入 → True"""
    return core.rbac_can(_model(model), action, ctx)


def rbac_readable_fields(model, ctx=None):
    """RBAC 叠加后的可读字段集（静态 ∩ readFields）；无 ctx → None 不裁剪"""
    return core.rbac_readable_fields(_model(model), ctx)


def rbac_writable_fields(model, ctx=None):
    """RBAC 叠加后的可写字段集（静态 ∩ writeFields）；无 ctx → None 不裁剪"""
    return core.rbac_writable_fields(_model(model), ctx)


def rbac_row_condition(model, action, ctx=None):
    """RBAC 行级条件（ownerOnly/condition 的 OR 合并体）；action ∈ {read, update, remove}"""
    return core.rbac_row_condition(_model(model), action, ctx)


def set_exempt_roles(roles):
    """豁免角色清单（命中者在一切判决环节直接放行）。

    判决唯一在 core：本层仅透传配置。默认空——无豁免（清单化语义，
    super_admin/admin 不再默认放行，迁移见迁移指南）。
    """
    return core.set_exempt_roles(roles)


def set_deny_write_roles(roles):
    """拒写角色清单（命中者一切写路径拒绝，读不受影响）。默认空——无拒写。"""
    return core.set_deny_write_roles(roles)


def set_unconfigured_policy(policy):
    """schema 白名单缺失/为空时的默认姿态："open"（默认，放行）| "closed"（全拒）。"""
    return core.set_unconfigured_policy(policy)


# ─── 自定义错误 ──────────────────────────────────────────────


class PermissionError(Exception):
    def __init__(self, message, status=403):
        super().__init__(message)
        self.status = status
