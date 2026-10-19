"""并发原语执行入口 —— claim / compare_and_set / renew_lease（C1）

纯逻辑（命令规划、方言翻译）全在 Rust core（``plan_claim`` /
``plan_compare_and_set``，mode=``atomic_commands``）；本模块只做宿主三件事：
时钟供给（``now`` 入参注入，core 无时钟）、源路由判定、事务执行
（``crud.exec.run_atomic_commands``）。

源路由契约：claim/compareAndSet 仅支持**单一源**（SQL 三后端）——plan 命令序列的
定位四元组提取出多个源时显式报错（跨源无同事务语义，禁静默选首源）；Mongo 源在
执行层被显式拒绝（``exec_mongo`` 的 concurrencyPrimitivesSqlOnly 分支）。

matched 语义（A2 核心，三后端一致）：``compareAndSet.matched`` = WHERE 命中行数。
PostgreSQL rowCount 与 SQLite ``changes()`` 天然为命中行数；MySQL 由 core 方言
两步化承接（stmt1 ``SELECT _id FOR UPDATE`` 取命中行，matched = 行数；不依赖
``CLIENT_FOUND_ROWS``——该 flag 是握手期建连选项，宿主执行器拿到的是现成连接，
无法代设），禁把 changed 当 matched 返回。SQL 单条 UPDATE 无独立 modified 计数
来源，``modified`` 与 ``matched`` 同值（命中行即为该语句触及的行；语义声明见
README「声明式并发原语」）。
"""

from collections.abc import Mapping

from ..datasource import PushdownUnsupportedError
from ..permission import get_context
from ..schema import get as _get_schema
from ..schema import get_core
from .exec import _call, _now_for, run_atomic_commands


def _single_source(plan):
    """从 atomic_commands plan 的命令序列提取唯一源（多源/缺源显式报错）"""
    sources = {(c or {}).get('source') or 'default' for c in plan.get('commands') or []}
    if len(sources) != 1:
        listed = ', '.join(sorted(sources))
        raise PushdownUnsupportedError(
            listed, 'claim', ['concurrencyPrimitivesSingleSource'],
            [f'claim/compareAndSet 仅支持单一数据源（同事务语义）；实际涉及: {listed}'])
    return next(iter(sources))


async def claim(schema_name, filter, update, *, limit=1, sort=None, route_override=None):
    """条件领取 N 行并原子置标；返回领到的行（含领取后最新值）

    MySQL/PG 走 ``FOR UPDATE SKIP LOCKED`` 两步同事务；SQLite 走 ``IN (子查询)``
    原子领取 + 同一子查询取行（filter 不得引用 update 将修改的列，否则取行漏命）。
    ``now`` 由宿主供给（core 无时钟）；Mongo 源显式拒绝。
    """
    _get_schema(schema_name)  # 未注册 schema 先行显式报错（与写路径入口对齐）
    now = _now_for(schema_name)
    plan = _call(lambda: get_core().plan_claim(
        schema_name, filter or {}, update or {}, sort, limit, now,
        get_context(), route_override))
    return await run_atomic_commands(_single_source(plan), plan)


async def compare_and_set(schema_name, filter, update, *, route_override=None):
    """条件更新并返回 ``{"matched": int, "modified": int}``

    单命令 ``atomic_commands`` plan（同事务包裹执行）；``matched`` = WHERE 命中行数
    （三后端语义一致，见模块 docstring）；空 filter 放行（语义 = 匹配全部，
    调用方自行承担范围风险——与 update_many 行为对齐）。

    matched 提取统一规则：包络 rows 非空取 ``len(rows)``（MySQL 两步 CAS 的
    stmt1 ``SELECT _id FOR UPDATE`` 行数即命中数），否则取 ``affectedRows``
    （PG rowCount / SQLite ``changes()`` 天然 = 命中行数）。
    """
    _get_schema(schema_name)
    now = _now_for(schema_name)
    plan = _call(lambda: get_core().plan_compare_and_set(
        schema_name, filter or {}, update or {}, now,
        get_context(), route_override))
    env = await run_atomic_commands(_single_source(plan), plan)
    if isinstance(env, Mapping) and env.get('rows'):
        matched = len(env['rows'])
    else:
        matched = int(env.get('affectedRows') or 0) if isinstance(env, Mapping) else 0
    return {'matched': matched, 'modified': matched}


async def renew_lease(schema_name, filter, lease_fields, *, route_override=None):
    """租约续期：compare_and_set 组合门面（不新开命令 kind，01 已裁决）

    归属条件由调用方 ``filter`` 携带（如 ``{"_id": {"$in": ids},
    "claimed_by": worker_id}``），宿主不隐式注入身份字段；续期时间戳由调用方写入
    ``lease_fields``（如 ``{"lease_until": <ms>}``）——门面与 core 均无时钟。
    返回值同 compare_and_set。
    """
    return await compare_and_set(schema_name, filter, lease_fields,
                                 route_override=route_override)
