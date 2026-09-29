"""
SQL 执行器注册与结果塑形（Phase 4）

分层（对齐铁律 1/8）：
  core ``dialect_translate``（纯逻辑，产 SQL + rowShape）
    → 执行器 ``{mysql,postgres,sqlite}``（绑定参数 + 执行 + ``restore_rows``）
    → 本模块把中立包络 ``{docs, rows, affectedRows}`` 塑形为 **Mongo 驱动等价返回值**

塑形规则与 ``crud/exec.py`` 的 Mongo 分支逐一对应，保证 Mongo / SQL 两条路径对上层
（``crud/query|write|mutation``）透明。对齐 ``nodejs-store/src/executors/index.js``。
"""

from . import mongo, mysql, postgres, sqlite

_BACKENDS = {'mysql': mysql, 'postgres': postgres, 'sqlite': sqlite}


def create_connection(kind, driver, options=None):
    """创建 SQL 数据源连接描述符 ``{'kind', 'exec'}``（driver 为对应驱动实例/连接）"""
    mod = _BACKENDS.get(kind)
    if mod is None:
        raise ValueError(f'未知 SQL 后端: {kind}（支持 mysql/postgres/sqlite）')
    return mod.create(driver, options)


async def _noop_release():
    """单连接形态的 release 占位（幂等 no-op，不关闭连接）"""
    return None


async def open_acquire(driver):
    """显式 checkout：返回 ``(conn, release)``；``release`` 为 async 幂等函数

    三形态统一（与 ``mysql.acquire`` 上下文管理器语义一致，仅改为「显式持有」）：
      - 池且 ``acquire()`` 返回 async 上下文管理器（asyncpg）→ 显式进出取专用连接；
      - 池且 ``acquire()`` 返回协程（asyncmy）→ await 取连接，用毕交回池；
      - 单连接（无 ``acquire``）→ 直用 driver，release 为 no-op。
    asyncpg / mysql 执行器的显式事务句柄均以此为基础（禁第二套 checkout 路径）。
    """
    acquire_fn = getattr(driver, 'acquire', None)
    if acquire_fn is None:
        return driver, _noop_release

    got = acquire_fn()
    if hasattr(got, '__aenter__'):
        conn = await got.__aenter__()
        released = False

        async def release():
            nonlocal released
            if released:
                return
            released = True
            await got.__aexit__(None, None, None)

        return conn, release

    conn = await got
    released = False

    async def release_pooled():
        nonlocal released
        if released:
            return
        released = True
        release_fn = getattr(driver, 'release', None)
        if release_fn is not None:
            await release_fn(conn)

    return conn, release_pooled


class UpdateResult:
    """PyMongo ``UpdateResult`` 的最小等价物（SQL 路径回喂给 ``crud/write.py``）"""

    def __init__(self, modified_count, matched_count=None):
        self.modified_count = modified_count
        self.matched_count = matched_count if matched_count is not None else modified_count


class DeleteResult:
    """PyMongo ``DeleteResult`` 的最小等价物（SQL 路径回喂给 ``crud/write.py``）"""

    def __init__(self, deleted_count):
        self.deleted_count = deleted_count


def _scalar(rows):
    """取行首列标量（COUNT 等聚合列无稳定别名，取首个值；PG 的 bigint 为字符串需数值化）"""
    if not rows:
        return 0
    row = rows[0]
    v = next(iter(row.values()), 0) if isinstance(row, dict) else row
    if isinstance(v, str):
        try:
            return int(v)
        except ValueError:
            return v
    return v


def shape_result(cmd, out):
    """中立包络 → Mongo 驱动等价返回值

    Python 的 Mongo 驱动（PyMongo）写结果用 snake_case **属性**（``modified_count`` /
    ``deleted_count``）暴露，故 SQL 路径回喂同形对象；这与 JS 侧 ``shapeResult`` 返回
    ``{modifiedCount}`` / ``{deletedCount}`` 是**各自驱动等价**，上层 ``crud/*`` 语义一致。
    """
    kind = cmd.get('kind')
    if kind in ('find', 'aggregate'):
        return out.get('docs') or []
    if kind in ('findOne', 'findOneAndUpdate'):
        docs = out.get('docs') or []
        return docs[0] if docs else None
    if kind == 'countDocuments':
        return _scalar(out.get('rows'))
    if kind == 'insertOne':
        return cmd.get('doc')
    if kind == 'insertMany':
        return {'insertedCount': len(cmd.get('docs') or [])}
    if kind == 'updateMany':
        return UpdateResult(out.get('affectedRows'))
    if kind == 'deleteMany':
        return DeleteResult(out.get('affectedRows'))
    return out


__all__ = [
    'DeleteResult',
    'UpdateResult',
    'create_connection',
    'mongo',
    'mysql',
    'open_acquire',
    'postgres',
    'shape_result',
    'sqlite',
]
