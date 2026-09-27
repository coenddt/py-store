"""SQLite 执行器（驱动：aiosqlite）

只做「绑定参数 + 执行 + 回喂」（铁律 1/8）：SQL 全部由 core ``dialect_translate``
产出，本模块不拼任何 SQL；带 ``rowShape`` 的语句结果交 core ``restore_rows`` 还原为
嵌套文档。返回值是中立包络 ``{docs, rows, affectedRows}``，由 ``./__init__.py``
依 ``command.kind`` 塑形为 Mongo 驱动等价返回值。
"""

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
    if db is None or not hasattr(db, 'execute'):
        raise TypeError('sqlite 执行器需要 aiosqlite 的 Connection 实例')

    async def run_stmts(plan):
        """依序执行 plan.stmts（单连接，事务内与池路径共用）"""
        docs = None
        rows = None
        affected_rows = 0
        for stmt in plan.get('stmts') or []:
            cur = await db.execute(stmt['text'], _bind(stmt.get('params')))
            try:
                if cur.description is not None:
                    cols = [d[0] for d in cur.description]
                    rows = [dict(zip(cols, r, strict=True)) for r in await cur.fetchall()]
                    shape = stmt.get('rowShape')
                    if shape:
                        docs = _core.restore_rows(shape, rows)
                else:
                    affected_rows = int(cur.rowcount or 0)
            finally:
                await cur.close()
        return {'docs': docs, 'rows': rows, 'affectedRows': affected_rows}

    async def exec_(plan):
        """非事务路径：plan 成功后显式提交（aiosqlite 默认非 autocommit，
        不提交则仅当前连接可见 —— 落盘场景会丢数据/读到幻象，且无任何告警）；
        失败则回滚后上抛，绝不提交半截写入"""
        try:
            out = await run_stmts(plan)
        except BaseException:
            await db.rollback()
            raise
        await db.commit()
        return out

    async def open_transaction():
        """显式事务句柄（aiosqlite 单连接）

        - 进入前先 ``commit()`` 清理遗留隐式事务（与既有 ``with_transaction`` 一致）；
        - ``commit`` / ``rollback`` 幂等；``release`` 为 no-op（单连接不归还）。
        """
        await db.commit()
        await db.execute('BEGIN')
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

        return {'exec': run_stmts, 'commit': commit, 'rollback': rollback, 'release': release}

    async def with_transaction(body):
        """事务执行：基于 ``open_transaction`` 的显式事务句柄（无第二套事务路径）；
        body(execute_on_tx) 的全部 plan 落在同一事务，任一失败整体回滚"""
        tx = await open_transaction()
        try:
            out = await body(tx['exec'])
            await tx['commit']()
            return out
        except BaseException:
            await tx['rollback']()
            raise
        finally:
            await tx['release']()

    return {
        'kind': 'sqlite',
        'exec': exec_,
        'with_transaction': with_transaction,
        'open_transaction': open_transaction,
    }
