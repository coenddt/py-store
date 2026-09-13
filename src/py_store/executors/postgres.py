"""PostgreSQL 执行器（驱动：asyncpg）

只做「绑定参数 + 执行 + 回喂」（铁律 1/8）：SQL 全部由 core ``dialect_translate``
产出（占位符为 ``$n``，params 顺序一致），本模块不拼任何 SQL。PG 原生支持
``RETURNING``，故写后回读为单语句。返回中立包络 ``{docs, rows, affectedRows}``，
由 ``./__init__.py#shape_result`` 塑形为 Mongo 驱动等价返回值。
"""

from ..schema import core as _core
from ._values import normalize_rows


def _affected(tag):
    """命令标签（``INSERT 0 1`` / ``UPDATE 3`` / ``DELETE 2``）→ 影响行数"""
    if not tag:
        return 0
    last = str(tag).rsplit(' ', 1)[-1]
    return int(last) if last.isdigit() else 0


def create(driver, options=None):
    """创建执行器描述符（可直接作为 ``init(connections)`` 的一个 SQL 数据源连接）"""
    if driver is None or not hasattr(driver, 'fetch'):
        raise TypeError('postgres 执行器需要 asyncpg 的连接或连接池')

    async def run_stmts(conn, plan):
        """在指定连接上依序执行 plan.stmts（事务内与池路径共用）"""
        docs = None
        rows = None
        affected_rows = 0
        for stmt in plan.get('stmts') or []:
            params = list(stmt.get('params') or [])
            shape = stmt.get('rowShape')
            if shape:
                # SELECT 或带 RETURNING 的写语句 → 取结果集
                records = await conn.fetch(stmt['text'], *params)
                rows = normalize_rows([dict(r) for r in records])
                docs = _core.restore_rows(shape, rows)
            else:
                affected_rows = _affected(await conn.execute(stmt['text'], *params))
        return {'docs': docs, 'rows': rows, 'affectedRows': affected_rows}

    async def exec_(plan):
        return await run_stmts(driver, plan)

    async def with_transaction(body):
        """事务执行：pool → acquire 专用连接 + ``conn.transaction()``；单连接直接用；
        body(execute_on_tx) 的全部 plan 落在同一事务，任一失败整体回滚"""
        if hasattr(driver, 'acquire'):
            async with driver.acquire() as conn:
                async with conn.transaction():
                    return await body(lambda plan: run_stmts(conn, plan))
        async with driver.transaction():
            return await body(lambda plan: run_stmts(driver, plan))

    return {'kind': 'postgres', 'exec': exec_, 'with_transaction': with_transaction}
