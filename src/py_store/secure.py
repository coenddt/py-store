"""统一安全模式（fail-secure 一键入口）。

背景：core 的 fail-secure 姿态由三个互相独立的开关拼成——

1. require_context：无 ctx 的读/写一律拒绝（堵「没传上下文 = 系统调用」）；
2. unconfigured policy = closed：schema 未配读/写白名单时默认全拒
   （堵「没配权限 = 全开」）；
3. meta policy = closed：注册/覆盖定义须过门禁
   （堵运行期未授权改 schema/workflow 定义）。

分开配置极易漏配，任何一个漏配都会留下口子；secure_mode 一次翻转三个，消除碎片化。

R2（03 §4.3）：作用域内**并视图**——三开关叠加为派生视图并更新本作用域
（``scope.view`` + ``scope.secure``），只作用于该作用域，不影响 base 与其它作用域；
未进入作用域时保持既有 base 路径（三开关原样翻转 + 进程级 ``_secure`` 标志）。
边界：base 路径的 core 是进程级单例，故进程级标志只反映 base 姿态。
"""

from __future__ import annotations

from . import permission, schema
from .scope import current_scope

_secure = False


def _secure_overrides(admin_roles):
    """三开关的策略覆盖对象（键名以 01 §4.3 ``PolicyOverrides`` JSON 契约为准，camelCase）。

    ``unconfigured`` 是 ``roleRules`` 的**子键**（非顶层）——写成顶层会被 core
    报 ``ERR_POLICY_VIEW_READONLY: 未知策略覆盖键``（03 §8.2 的键名核对）。
    """
    return {
        'requireContext': True,
        'roleRules': {'unconfigured': 'closed'},
        'metaPolicy': {'closed': True, 'roles': admin_roles},
    }


def secure_mode(admin_roles: list[str] | None = None) -> dict:
    """进入 fail-secure 模式（三个开关同时翻转）。

    :param admin_roles: 允许注册/覆盖定义的角色（配合 meta closed；默认仅 internal）
    :returns: ``{'secure': True}``
    """
    global _secure
    roles = list(admin_roles) if admin_roles is not None else []
    s = current_scope()
    if s is not None:
        # 作用域内：派生叠加视图并更新本作用域（视图只读守卫拦的是注册/清空，非策略叠加）
        s.view = s.view.with_policy(_secure_overrides(roles))
        s.secure = True
        return {'secure': True}
    schema.get_core().set_require_context(True)
    permission.set_unconfigured_policy('closed')
    schema.get_core().set_meta_policy(True, roles)
    _secure = True
    return {'secure': True}


def is_secure() -> bool:
    """是否经统一入口处于安全模式。

    作用域内取本作用域标志（True = 本作用域已并视图）；域外回退进程级 base 标志。
    注意：本标志只反映 secure_mode/relax_mode 的统一动作；单独拨动底层开关不改变本值。
    """
    s = current_scope()
    if s is not None and s.secure is not None:
        return s.secure
    return _secure


def relax_mode() -> dict:
    """退出安全模式，恢复 core 默认的 fail-open 姿态（仅供本地脚本/测试；生产禁用）。

    不重置各 schema 自身声明的读/写白名单——那是定义的一部分。

    作用域内与 base 路径同构双向：把三开关显式覆盖回 fail-open 默认值（派生新视图，
    仅影响本作用域），并撤本作用域标志；域外仍走既有进程级路径。
    """
    global _secure
    s = current_scope()
    if s is not None:
        s.view = s.view.with_policy({
            'requireContext': False,
            'roleRules': {'unconfigured': 'open'},
            'metaPolicy': {'closed': False, 'roles': []},
        })
        s.secure = False
        return {'secure': False}
    schema.get_core().set_require_context(False)
    permission.set_unconfigured_policy('open')
    schema.get_core().set_meta_policy(False, [])
    _secure = False
    return {'secure': False}
