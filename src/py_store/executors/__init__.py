"""
SQL 执行器注册与结果塑形（Phase 4）

分层（对齐铁律 1/8）：
  core ``dialect_translate``（纯逻辑，产 SQL + rowShape）
    → 执行器 ``{mysql,postgres,sqlite}``（绑定参数 + 执行 + ``restore_rows``）
    → 本模块把中立包络 ``{docs, rows, affectedRows}`` 塑形为 **Mongo 驱动等价返回值**

塑形规则与 ``crud/exec.py`` 的 Mongo 分支逐一对应，保证 Mongo / SQL 两条路径对上层
（``crud/query|write|mutation``）透明。对齐 ``nodejs-store/src/executors/index.js``。
"""

from collections.abc import Awaitable, Callable
from typing import Any, cast

from . import mongo, mysql, postgres, sqlite

_BACKENDS = {"mysql": mysql, "postgres": postgres, "sqlite": sqlite}


def create_connection(
    kind: str, driver: Any, options: dict[str, Any] | None = None
) -> dict[str, Any]:
    """创建 SQL 数据源连接描述符 ``{'kind', 'exec'}``（driver 为对应驱动实例/连接）"""
    mod = _BACKENDS.get(kind)
    if mod is None:
        raise ValueError(f"未知 SQL 后端: {kind}（支持 mysql/postgres/sqlite）")
    return cast("dict[str, Any]", mod.create(driver, options))


def idempotent_tx_ops(
    commit_call: Callable[[], Awaitable[None]],
    rollback_call: Callable[[], Awaitable[None]],
) -> tuple[Callable[[], Awaitable[None]], Callable[[], Awaitable[None]]]:
    """commit/rollback 幂等包装（三 SQL executor 显式事务句柄共享）

    两函数共享同一 ``closed`` 位：任一首次调用执行并置位，其后重复 / 交叉调用为
    no-op（与各 executor 原内联实现语义一致）。入参为无参协程工厂（如
    ``conn.commit``），在置位后调用。
    """
    closed = False

    async def commit() -> None:
        """提交当前事务；幂等，重复调用为 no-op。"""
        nonlocal closed
        if closed:
            return
        closed = True
        await commit_call()

    async def rollback() -> None:
        """回滚当前事务；幂等，重复调用为 no-op。"""
        nonlocal closed
        if closed:
            return
        closed = True
        await rollback_call()

    return commit, rollback


def savepoint_ops(
    exec_sql: Callable[[str], Awaitable[None]],
) -> tuple[
    Callable[[str], Awaitable[None]],
    Callable[[str], Awaitable[None]],
    Callable[[str], Awaitable[None]],
]:
    """SAVEPOINT 原语三件套（savepoint / release_savepoint / rollback_to_savepoint，共享）

    ``exec_sql(sql)`` 为该后端的单语句执行入口（mysql 走 cursor 包装，
    postgres / sqlite 直 ``execute``）。SQL 文本仍为标准 SAVEPOINT 语法。
    """

    async def savepoint(name: str) -> None:
        """保存点（嵌套事务用）；name 由 Host 生成（``sp_<n>``），非用户输入"""
        await exec_sql("SAVEPOINT %s" % name)

    async def release_savepoint(name: str) -> None:
        """释放保存点（RELEASE SAVEPOINT），其后的嵌套作用域收尾用。"""
        await exec_sql("RELEASE SAVEPOINT %s" % name)

    async def rollback_to_savepoint(name: str) -> None:
        """回滚到保存点（ROLLBACK TO SAVEPOINT），保存点本身保留可复用。"""
        await exec_sql("ROLLBACK TO SAVEPOINT %s" % name)

    return savepoint, release_savepoint, rollback_to_savepoint


def make_with_transaction(
    open_transaction: Callable[[], Awaitable[dict[str, Any]]],
    wrap: Callable[[Callable[[], Awaitable[Any]]], Awaitable[Any]] | None = None,
) -> Callable[[Callable[[Any, dict[str, Any]], Awaitable[Any]]], Awaitable[Any]]:
    """with_transaction 工厂（三 SQL executor 共享）：基于 ``open_transaction`` 的
    显式事务句柄构造事务执行入口（无第二套事务路径）。

    ``wrap`` 可选：包装「开事务→body→提交/回滚」整个窗口。单连接驱动的
    事务边界（commit/BEGIN/rollback）是连接级共享状态、跨协程互踩，sqlite 因此
    传入 per-connection 锁；池形态（mysql/postgres）每个事务独占连接，不需要。
    """

    async def with_transaction(
        body: Callable[[Any, dict[str, Any]], Awaitable[Any]],
    ) -> Any:
        """事务执行：body(execute_on_tx, tx=None) 的全部 plan 落在同一连接同一事务，
        任一失败整体回滚。第二参数为事务句柄（供上层读保存点原语）；
        py 位置参数语义下调用方必须接收该参数"""

        async def _attempt():
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

        if wrap is None:
            return await _attempt()
        return await wrap(_attempt)

    return with_transaction


async def _noop_release() -> None:
    """单连接形态的 release 占位（幂等 no-op，不关闭连接）"""
    return None


async def open_acquire(
    driver: Any,
) -> tuple[Any, Callable[[], Awaitable[None]]]:
    """显式 checkout：返回 ``(conn, release)``；``release`` 为 async 幂等函数

    三形态统一（与 ``mysql.acquire`` 上下文管理器语义一致，仅改为「显式持有」）：
      - 池且 ``acquire()`` 返回 async 上下文管理器（asyncpg）→ 显式进出取专用连接；
      - 池且 ``acquire()`` 返回协程（asyncmy）→ await 取连接，用毕交回池；
      - 单连接（无 ``acquire``）→ 直用 driver，release 为 no-op。
    asyncpg / mysql 执行器的显式事务句柄均以此为基础（禁第二套 checkout 路径）。
    """
    acquire_fn = getattr(driver, "acquire", None)
    if acquire_fn is None:
        return driver, _noop_release

    got = acquire_fn()
    if hasattr(got, "__aenter__"):
        conn = await got.__aenter__()
        released = False

        async def release() -> None:
            """交回专用连接（退出 async 上下文）；幂等，重复调用为 no-op。"""
            nonlocal released
            if released:
                return
            released = True
            await got.__aexit__(None, None, None)

        return conn, release

    conn = await got
    released = False

    async def release_pooled() -> None:
        """把连接交回驱动池（``driver.release``）；幂等，重复调用为 no-op。"""
        nonlocal released
        if released:
            return
        released = True
        release_fn = getattr(driver, "release", None)
        if release_fn is not None:
            await release_fn(conn)

    return conn, release_pooled


class UpdateResult:
    """PyMongo ``UpdateResult`` 的最小等价物（SQL 路径回喂给 ``crud/write.py``）"""

    def __init__(self, modified_count: int, matched_count: int | None = None) -> None:
        """``matched_count`` 缺省取 ``modified_count``（SQL 侧两者同值）。"""
        self.modified_count = modified_count
        self.matched_count = (
            matched_count if matched_count is not None else modified_count
        )


class DeleteResult:
    """PyMongo ``DeleteResult`` 的最小等价物（SQL 路径回喂给 ``crud/write.py``）"""

    def __init__(self, deleted_count: int) -> None:
        """仅承载 ``deleted_count``（PyMongo ``DeleteResult`` 最小契约）。"""
        self.deleted_count = deleted_count


def _scalar(rows: list[dict[str, Any]] | None) -> Any:
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


def shape_result(cmd: dict[str, Any], out: dict[str, Any]) -> Any:
    """中立包络 → Mongo 驱动等价返回值

    Python 的 Mongo 驱动（PyMongo）写结果用 snake_case **属性**（``modified_count`` /
    ``deleted_count``）暴露，故 SQL 路径回喂同形对象；这与 JS 侧 ``shapeResult`` 返回
    ``{modifiedCount}`` / ``{deletedCount}`` 是**各自驱动等价**，上层 ``crud/*`` 语义一致。
    """
    kind = cmd.get("kind")
    if kind in ("find", "aggregate"):
        return out.get("docs") or []
    if kind in ("findOne", "findOneAndUpdate"):
        docs = out.get("docs") or []
        return docs[0] if docs else None
    if kind == "countDocuments":
        return _scalar(out.get("rows"))
    if kind == "insertOne":
        doc = cmd.get("doc")
        # 阶段2：autoincrement 主键 —— doc 无 `_id`（core 不注入）→ 从执行包络回读
        # 自增值（PG/SQLite RETURNING 走 rows；MySQL/SQLite lastrowid 走 insertId）。
        # 包络两者皆无 → 保持无 `_id` 返回（SQL 侧不该发生；Mongo 路径已在执行器报错）。
        if isinstance(doc, dict) and not doc.get("_id"):
            rid = None
            if (
                out.get("rows")
                and isinstance(out["rows"][0], dict)
                and "_id" in out["rows"][0]
            ):
                rid = out["rows"][0]["_id"]
            elif out.get("insertId") is not None:
                rid = out["insertId"]
            if rid is not None:
                return {**doc, "_id": rid}
        return doc
    if kind == "insertMany":
        return {"insertedCount": len(cmd.get("docs") or [])}
    if kind == "updateMany":
        return UpdateResult(cast(int, out.get("affectedRows")))
    if kind == "deleteMany":
        return DeleteResult(cast(int, out.get("affectedRows")))
    return out


__all__ = [
    "DeleteResult",
    "UpdateResult",
    "create_connection",
    "mongo",
    "mysql",
    "open_acquire",
    "postgres",
    "shape_result",
    "sqlite",
]
