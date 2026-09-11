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
    """
    out = []
    quote = None
    for ch in text:
        if quote is not None:
            out.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"', '`'):
            quote = ch
            out.append(ch)
            continue
        out.append('%s' if ch == '?' else ch)
    return ''.join(out)


def create(driver, options=None):
    """创建执行器描述符（可直接作为 ``init(connections)`` 的一个 SQL 数据源连接）"""
    if driver is None:
        raise TypeError('mysql 执行器需要 asyncmy 的连接或连接池')

    from asyncmy.cursors import DictCursor

    async def exec_(plan):
        docs = None
        rows = None
        affected_rows = 0
        for stmt in plan.get('stmts') or []:
            async with acquire(driver) as conn:
                async with conn.cursor(DictCursor) as cur:
                    await cur.execute(_to_pyformat(stmt['text']), list(stmt.get('params') or []))
                    if cur.description is not None:
                        rows = [_plain(r) for r in await cur.fetchall()]
                        shape = stmt.get('rowShape')
                        if shape:
                            docs = _core.restore_rows(shape, rows)
                    else:
                        affected_rows = int(cur.rowcount or 0)
        return {'docs': docs, 'rows': rows, 'affectedRows': affected_rows}

    return {'kind': 'mysql', 'exec': exec_}
