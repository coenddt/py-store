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
    last = str(tag).rsplit(" ", 1)[-1]
    return int(last) if last.isdigit() else 0


def create(driver, options=None):
    """创建执行器描述符（可直接作为 ``init(connections)`` 的一个 SQL 数据源连接）"""
    if driver is None or not hasattr(driver, "fetch"):
        raise TypeError("postgres 执行器需要 asyncpg 的连接或连接池")

    async def run_stmts(conn, plan):
        """在指定连接上依序执行 plan.stmts（事务内与池路径共用）"""
        docs = None
        rows = None
        affected_rows = 0
        for stmt in plan.get("stmts") or []:
            params = list(stmt.get("params") or [])
            shape = stmt.get("rowShape")
            if shape or not stmt.get("isWrite") or "RETURNING" in stmt["text"].upper():
                # SELECT 或带 RETURNING 的写语句 → 取结果集（阶段2：autoincrement 的
                #  无 rowShape，靠文本含 RETURNING 识别）；
                # 无 rowShape 的读（Host 原生 SQL 逃生口 execute_raw）亦取行
                records = await conn.fetch(stmt["text"], *params)
                rows = normalize_rows([dict(r) for r in records])
                if shape:
                    docs = _core.restore_rows(shape, rows)
            else:
                affected_rows = _affected(await conn.execute(stmt["text"], *params))
        return {"docs": docs, "rows": rows, "affectedRows": affected_rows}

    async def exec_(plan):
        """非事务路径：整个 plan 直接在池取到的连接上执行（autocommit 语义）。"""
        return await run_stmts(driver, plan)

    async def open_transaction():
        """显式事务句柄（asyncpg）

        必须用 ``conn.transaction()`` 的显式 ``start/commit/rollback``；
        asyncpg 不允许手写 BEGIN/COMMIT 与 ``transaction()`` 混用。
        """
        from . import (  # 延迟导入：避免与包 __init__ 相互导入
            idempotent_tx_ops,
            open_acquire,
            savepoint_ops,
        )

        conn, release = await open_acquire(driver)
        tx = conn.transaction()
        await tx.start()
        commit, rollback = idempotent_tx_ops(tx.commit, tx.rollback)

        savepoint, release_savepoint, rollback_to_savepoint = savepoint_ops(
            conn.execute
        )

        return {
            "exec": lambda plan: run_stmts(conn, plan),
            "commit": commit,
            "rollback": rollback,
            "release": release,
            "savepoint": savepoint,
            "release_savepoint": release_savepoint,
            "rollback_to_savepoint": rollback_to_savepoint,
        }

    from . import (  # 延迟导入：避免与包 __init__ 相互导入
        make_with_transaction,
    )

    with_transaction = make_with_transaction(open_transaction)

    return {
        "kind": "postgres",
        "exec": exec_,
        "with_transaction": with_transaction,
        "open_transaction": open_transaction,
    }
