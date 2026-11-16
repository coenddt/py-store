"""SQLite 执行器（驱动：aiosqlite）

只做「绑定参数 + 执行 + 回喂」（铁律 1/8）：SQL 全部由 core ``dialect_translate``
产出，本模块不拼任何 SQL；带 ``rowShape`` 的语句结果交 core ``restore_rows`` 还原为
嵌套文档。返回值是中立包络 ``{docs, rows, affectedRows}``，由 ``./__init__.py``
依 ``command.kind`` 塑形为 Mongo 驱动等价返回值。

并发互斥（2026-11-14）：aiosqlite 单连接的事务边界（``commit`` / ``BEGIN`` /
``rollback``）是连接级状态、跨协程共享——两个事务作用域并发时互踩，实测宿主
「顺序 claim 正常、并发 claim 报 cannot start a transaction within a transaction」。
非事务执行（``exec_``）与显式事务（``with_transaction``）全窗口过同一把
per-connection 锁串行化；语句执行本身在 aiosqlite 单线程队列里已串行，加锁不损失
吞吐。同协程重入（``with_transaction`` 窗口内经 tx view 复用、嵌套保存点）经
``_lock_held`` contextvar 判定跳过抢锁（asyncio.Lock 非可重入）。

已知边界（显式声明，非静默）：会话路径（``Session._open_view`` 直接调
``open_transaction``）的事务窗口跨会话生命周期，不持本锁——并发会话与非事务写
互踩时由 SQLite 引擎 fail loud（报错上抛），不会静默错数据。
"""

import asyncio
import contextvars

from ..schema import core as _core


def _bind(params):
    """aiosqlite（sqlite3）只接受 int/float/str/bytes/None；布尔显式转 0/1"""
    out = []
    for v in params or []:
        if v is True:
            out.append(1)
        elif v is False:
            out.append(0)
        else:
            out.append(v)
    return out


def create(db, options=None):
    """创建执行器描述符（可直接作为 ``init(connections)`` 的一个 SQL 数据源连接）"""
    if db is None or not hasattr(db, "execute"):
        raise TypeError("sqlite 执行器需要 aiosqlite 的 Connection 实例")

    lock = asyncio.Lock()
    _lock_held = contextvars.ContextVar('pystore_sqlite_lock_held', default=False)

    async def _with_lock(fn):
        """锁窗口包装：同协程已在窗口内（事务嵌套复用）则直接放行"""
        if _lock_held.get():
            return await fn()
        async with lock:
            token = _lock_held.set(True)
            try:
                return await fn()
            finally:
                _lock_held.reset(token)

    async def run_stmts(plan):
        """依序执行 plan.stmts（单连接，事务内与池路径共用）"""
        docs = None
        rows = None
        affected_rows = 0
        insert_id = None
        for stmt in plan.get("stmts") or []:
            cur = await db.execute(stmt["text"], _bind(stmt.get("params")))
            try:
                if cur.description is not None:
                    cols = [d[0] for d in cur.description]
                    rows = [
                        dict(zip(cols, r, strict=True)) for r in await cur.fetchall()
                    ]
                    shape = stmt.get("rowShape")
                    if shape:
                        docs = _core.restore_rows(shape, rows)
                else:
                    affected_rows = int(cur.rowcount or 0)
                    # 阶段2：autoincrement 主键写后自增值回读（非 RETURNING 的 INSERT 走
                    # lastrowid；带 RETURNING 的写语句走上方 rows 分支）
                    insert_id = cur.lastrowid
            finally:
                await cur.close()
        return {
            "docs": docs,
            "rows": rows,
            "affectedRows": affected_rows,
            "insertId": insert_id,
        }

    async def exec_(plan):
        """非事务路径：plan 成功后显式提交（aiosqlite 默认非 autocommit，
        不提交则仅当前连接可见 —— 落盘场景会丢数据/读到幻象，且无任何告警）；
        失败则回滚后上抛，绝不提交半截写入。全程过锁（执行 + 提交同一窗口，
        防事务边界被其他协程的 commit/BEGIN 插队）"""

        async def _body():
            try:
                out = await run_stmts(plan)
            except BaseException:  # 收尾路径必须覆盖取消/中断：先回滚再原样重抛，禁吞异常（语义与 datasource 收尾一致）
                await db.rollback()
                raise
            await db.commit()
            return out

        return await _with_lock(_body)

    async def open_transaction():
        """显式事务句柄（aiosqlite 单连接）

        - 进入前先 ``commit()`` 清理遗留隐式事务（与既有 ``with_transaction`` 一致）；
        - ``commit`` / ``rollback`` 幂等；``release`` 为 no-op（单连接不归还）。
        - 不自行抢锁：``with_transaction`` 在锁窗口内调用本函数；独立入口（会话）
          见模块 docstring「已知边界」。
        """
        await db.commit()
        await db.execute("BEGIN")
        closed = False

        async def commit():
            nonlocal closed
            if closed:
                return
            closed = True
            await db.commit()

        async def rollback():
            nonlocal closed
            if closed:
                return
            closed = True
            await db.rollback()

        async def release():
            return None

        async def savepoint(name):
            """保存点（嵌套事务用）；name 由 Host 生成（``sp_<n>``），非用户输入"""
            await db.execute("SAVEPOINT %s" % name)

        async def release_savepoint(name):
            await db.execute("RELEASE SAVEPOINT %s" % name)

        async def rollback_to_savepoint(name):
            await db.execute("ROLLBACK TO SAVEPOINT %s" % name)

        return {
            "exec": run_stmts,
            "commit": commit,
            "rollback": rollback,
            "release": release,
            "savepoint": savepoint,
            "release_savepoint": release_savepoint,
            "rollback_to_savepoint": rollback_to_savepoint,
        }

    async def with_transaction(body):
        """事务执行：基于 ``open_transaction`` 的显式事务句柄（无第二套事务路径）；
        body(execute_on_tx, tx=None) 的全部 plan 落在同一事务，任一失败整体回滚。
        第二参数为事务句柄（供上层读保存点原语）；py 位置参数语义下调用方必须接收该参数。
        开事务→body→提交/回滚全窗口过锁（防其他协程的事务边界插队互踩）"""

        async def _body():
            tx = await open_transaction()
            try:
                out = await body(tx["exec"], tx)
                await tx["commit"]()
                return out
            except BaseException:  # 收尾路径必须覆盖取消/中断：先回滚再原样重抛，禁吞异常（语义与 datasource 收尾一致）
                await tx["rollback"]()
                raise
            finally:
                await tx["release"]()

        return await _with_lock(_body)

    return {
        "kind": "sqlite",
        "exec": exec_,
        "with_transaction": with_transaction,
        "open_transaction": open_transaction,
    }
