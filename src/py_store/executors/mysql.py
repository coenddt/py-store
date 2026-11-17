"""MySQL 执行器（驱动：asyncmy）

只做「绑定参数 + 执行 + 回喂」（铁律 1/8）：SQL 全部由 core ``dialect_translate``
产出（占位符为 ``?``），本模块不拼任何 SQL。MySQL 无 ``RETURNING``，写后回读由 core
产出「UPDATE/INSERT + SELECT」两条语句，本模块按序执行并取回读结果即可。
返回中立包络 ``{docs, rows, affectedRows}``，由 ``./__init__.py#shape_result`` 塑形。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from ..schema import (  # type: ignore[attr-defined]  # core 经 schema 模块统一收口（并行维护）
    core as _core,
)
from ._values import normalize_rows


@asynccontextmanager
async def acquire(driver: Any) -> AsyncIterator[Any]:
    """兼容连接池与单连接：池用 ``acquire()``，单连接直接复用"""
    acquire_fn = getattr(driver, "acquire", None)
    if acquire_fn is None:
        yield driver
        return
    got = acquire_fn()
    if hasattr(got, "__aenter__"):
        async with got as conn:
            yield conn
        return
    # 少数版本 ``acquire()`` 为协程：await 取连接，释放交回池
    conn = await got
    try:
        yield conn
    finally:
        release = getattr(driver, "release", None)
        if release is not None:
            await release(conn)


def _plain(row: Any) -> dict[str, Any]:
    """驱动行（DictCursor 已是 dict）转纯 dict 后再交 core（绑定层只认纯 JSON）"""
    return dict(row)


def _to_pyformat(text: str) -> str:
    """core 的 MySQL 方言产出 ``?`` 占位符（对齐 Node 的 mysql2），

    但 asyncmy 沿用 PyMySQL 的 ``%s`` paramstyle，直接传 ``?`` 会抛
    ``not all arguments converted during string formatting``。此处只做占位符风格转换，
    不拼任何 SQL（SQL 仍全部由 core 产出）：跳过引号内的 ``?``，仅替换占位符。

    引号内外的字面 ``%``（如三态 present_key 的 ``LIKE '%,field,%'``）在 ``%`` 式
    paramstyle 下会被误作格式说明符 → 一律转义为 ``%%``（否则 ``execute(sql, args)``
    做 ``sql % args`` 时抛 ``not enough arguments for format string``）。``?`` 只在引号
    外是占位符（引号内的 ``?`` 是字符串字面量，保持不变）。
    """
    out = []
    quote = None
    for ch in text:
        if ch in ("'", '"', "`"):
            if quote is None:
                quote = ch
            elif ch == quote:
                quote = None
            out.append(ch)
        elif ch == "?":
            # 引号内是字符串字面量里的问号，不是占位符
            out.append("?" if quote is not None else "%s")
        elif ch == "%":
            # 字面 % 在 pyformat 下必须转义为 %%（引号内外均如此）
            out.append("%%")
        else:
            out.append(ch)
    return "".join(out)


def create(driver: Any, options: dict[str, Any] | None = None) -> dict[str, Any]:
    """创建执行器描述符（可直接作为 ``init(connections)`` 的一个 SQL 数据源连接）"""
    if driver is None:
        raise TypeError("mysql 执行器需要 asyncmy 的连接或连接池")

    from asyncmy.cursors import DictCursor

    async def run_stmts(conn: Any, plan: dict[str, Any]) -> dict[str, Any]:
        """在指定连接上依序执行 plan.stmts（事务内与池路径共用）"""
        docs = None
        rows = None
        affected_rows = 0
        insert_id = None
        async with conn.cursor(DictCursor) as cur:
            for stmt in plan.get("stmts") or []:
                await cur.execute(
                    _to_pyformat(stmt["text"]), list(stmt.get("params") or [])
                )
                if cur.description is not None:
                    rows = normalize_rows([_plain(r) for r in await cur.fetchall()])
                    shape = stmt.get("rowShape")
                    if shape:
                        docs = _core.restore_rows(shape, rows)
                else:
                    affected_rows = int(cur.rowcount or 0)
                    # 阶段2：autoincrement 主键写后自增值回读（MySQL 无 RETURNING，
                    # lastrowid = 本连接最近一次 INSERT 生成的自增值；
                    # getattr 防御：无该属性的驱动/测试桩等价「无自增值」）
                    insert_id = getattr(cur, "lastrowid", None)
        return {
            "docs": docs,
            "rows": rows,
            "affectedRows": affected_rows,
            "insertId": insert_id,
        }

    async def exec_(plan: dict[str, Any]) -> dict[str, Any]:
        """非事务路径：整个 plan 固定在同一连接执行，成功后显式 commit
        （池 autocommit=True 时为幂等 no-op；单连接 autocommit=False 时杜绝「只执行不提交」）；
        失败则 rollback 后上抛，绝不提交半截写入"""
        async with acquire(driver) as conn:
            try:
                out = await run_stmts(conn, plan)
            except BaseException:  # 收尾路径必须覆盖取消/中断：先回滚再原样重抛，禁吞异常（语义与 datasource 收尾一致）
                await conn.rollback()
                raise
            await conn.commit()
            return out

    async def open_transaction() -> dict[str, Any]:
        """显式事务句柄（asyncmy）

        池形态经 ``open_acquire`` checkout 专用连接后 ``BEGIN``，
        commit/rollback 后由 release 归还连接。
        """
        from . import (  # 延迟导入：避免与包 __init__ 相互导入
            idempotent_tx_ops,
            open_acquire,
            savepoint_ops,
        )

        conn, release = await open_acquire(driver)
        async with conn.cursor() as cur:
            await cur.execute("BEGIN")
        commit, rollback = idempotent_tx_ops(conn.commit, conn.rollback)

        async def exec_sql(sql: str) -> None:
            """单语句执行入口（asyncmy 需经 cursor 执行）"""
            async with conn.cursor() as cur:
                await cur.execute(sql)

        savepoint, release_savepoint, rollback_to_savepoint = savepoint_ops(exec_sql)

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
        "kind": "mysql",
        "exec": exec_,
        "with_transaction": with_transaction,
        "open_transaction": open_transaction,
    }
