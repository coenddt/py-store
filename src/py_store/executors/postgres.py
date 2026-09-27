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
            if shape or not stmt.get('isWrite'):
                # SELECT 或带 RETURNING 的写语句 → 取结果集；
                # 无 rowShape 的读（Host 原生 SQL 逃生口 execute_raw）亦取行
                records = await conn.fetch(stmt['text'], *params)
                rows = normalize_rows([dict(r) for r in records])
                docs = _core.restore_rows(shape, rows)
            else:
                affected_rows = _affected(await conn.execute(stmt['text'], *params))
        return {'docs': docs, 'rows': rows, 'affectedRows': affected_rows}

    async def exec_(plan):
        return await run_stmts(driver, plan)

    async def open_transaction():
        """显式事务句柄（asyncpg）

        必须用 ``conn.transaction()`` 的显式 ``start/commit/rollback``；
        asyncpg 不允许手写 BEGIN/COMMIT 与 ``transaction()`` 混用。
        """
        from . import open_acquire  # 延迟导入：避免与包 __init__ 相互导入

        conn, release = await open_acquire(driver)
        tx = conn.transaction()
        await tx.start()
        closed = False

        async def commit():
            nonlocal closed
            if closed:
                return
            closed = True
            await tx.commit()

        async def rollback():
            nonlocal closed
            if closed:
                return
            closed = True
            await tx.rollback()

        async def savepoint(name):
            """保存点（嵌套事务用）；name 由 Host 生成（``sp_<n>``），非用户输入"""
            await conn.execute('SAVEPOINT %s' % name)

        async def release_savepoint(name):
            await conn.execute('RELEASE SAVEPOINT %s' % name)

        async def rollback_to_savepoint(name):
            await conn.execute('ROLLBACK TO SAVEPOINT %s' % name)

        return {
            'exec': lambda plan: run_stmts(conn, plan),
            'commit': commit,
            'rollback': rollback,
            'release': release,
            'savepoint': savepoint,
            'release_savepoint': release_savepoint,
            'rollback_to_savepoint': rollback_to_savepoint,
        }

    async def with_transaction(body):
        """事务执行：基于 ``open_transaction`` 的显式事务句柄（无第二套事务路径）；
        body(execute_on_tx, tx=None) 的全部 plan 落在同一事务，任一失败整体回滚。
        第二参数为事务句柄（供上层读保存点原语），可选——旧单参写法继续可用"""
        tx = await open_transaction()
        try:
            out = await body(tx['exec'], tx)
            await tx['commit']()
            return out
        except BaseException:
            await tx['rollback']()
            raise
        finally:
            await tx['release']()

    return {
        'kind': 'postgres',
        'exec': exec_,
        'with_transaction': with_transaction,
        'open_transaction': open_transaction,
    }
