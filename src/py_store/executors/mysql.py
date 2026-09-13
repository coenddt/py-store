"""MySQL 执行器（驱动：asyncmy）

只做「绑定参数 + 执行 + 回喂」（铁律 1/8）：SQL 全部由 core ``dialect_translate``
产出（占位符为 ``?``），本模块不拼任何 SQL。MySQL 无 ``RETURNING``，写后回读由 core
产出「UPDATE/INSERT + SELECT」两条语句，本模块按序执行并取回读结果即可。
返回中立包络 ``{docs, rows, affectedRows}``，由 ``./__init__.py#shape_result`` 塑形。
"""

from contextlib import asynccontextmanager

from ..schema import core as _core


@asynccontextmanager
async def acquire(driver):
    """兼容连接池与单连接：池用 ``acquire()``，单连接直接复用"""
    acquire_fn = getattr(driver, 'acquire', None)
    if acquire_fn is None:
        yield driver
        return
    got = acquire_fn()
    if hasattr(got, '__aenter__'):
        async with got as conn:
            yield conn
        return
    # 少数版本 ``acquire()`` 为协程：await 取连接，释放交回池
    conn = await got
    try:
        yield conn
    finally:
        release = getattr(driver, 'release', None)
        if release is not None:
            await release(conn)


def _plain(row):
    """驱动行（DictCursor 已是 dict）转纯 dict 后再交 core（绑定层只认纯 JSON）"""
    return dict(row)


def _to_pyformat(text):
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
        if ch in ("'", '"', '`'):
            if quote is None:
                quote = ch
            elif ch == quote:
                quote = None
            out.append(ch)
        elif ch == '?':
            # 引号内是字符串字面量里的问号，不是占位符
            out.append('?' if quote is not None else '%s')
        elif ch == '%':
            # 字面 % 在 pyformat 下必须转义为 %%（引号内外均如此）
            out.append('%%')
        else:
            out.append(ch)
    return ''.join(out)


def create(driver, options=None):
    """创建执行器描述符（可直接作为 ``init(connections)`` 的一个 SQL 数据源连接）"""
    if driver is None:
        raise TypeError('mysql 执行器需要 asyncmy 的连接或连接池')

    from asyncmy.cursors import DictCursor

    async def run_stmts(conn, plan):
        """在指定连接上依序执行 plan.stmts（事务内与池路径共用）"""
        docs = None
        rows = None
        affected_rows = 0
        async with conn.cursor(DictCursor) as cur:
            for stmt in plan.get('stmts') or []:
                await cur.execute(_to_pyformat(stmt['text']), list(stmt.get('params') or []))
                if cur.description is not None:
                    rows = [_plain(r) for r in await cur.fetchall()]
                    shape = stmt.get('rowShape')
                    if shape:
                        docs = _core.restore_rows(shape, rows)
                else:
                    affected_rows = int(cur.rowcount or 0)
        return {'docs': docs, 'rows': rows, 'affectedRows': affected_rows}

    async def exec_(plan):
        # 整个 plan 固定在同一连接上执行（池路径也保持语句顺序与连接一致性）
        async with acquire(driver) as conn:
            return await run_stmts(conn, plan)

    async def with_transaction(body):
        """事务执行：显式 BEGIN + commit/rollback（对 autocommit 任意配置均确定成立）；
        body(execute_on_tx) 的全部 plan 落在同一连接同一事务，任一失败整体回滚"""
        async with acquire(driver) as conn:
            async with conn.cursor() as cur:
                await cur.execute('BEGIN')
            try:
                out = await body(lambda plan: run_stmts(conn, plan))
                await conn.commit()
                return out
            except BaseException:
                await conn.rollback()
                raise

    return {'kind': 'mysql', 'exec': exec_, 'with_transaction': with_transaction}
