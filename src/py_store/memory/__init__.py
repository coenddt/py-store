"""进程内内存数据源 —— 宿主门面（py 侧）

``connect({'database', 'store', 'io'})`` 产出一个**连接描述符**（``dict``，``kind == 'memory'``），
供 ``init`` / ``set_connections`` 注册为数据源；命令求值一律经 ``handle`` →
``core.local_eval``（与 ``local`` 同族：core 侧两者都归 ``DataSource::Local``）。

描述符契约（与执行文档 §4.3 / 常驻契约卡逐字对齐）：

  ``{'kind': 'memory', 'database', 'handle', 'handle_for(database)', 'open_transaction(database)', 'with_transaction(fn, database)'}``

  - ``handle`` 复用 ``..local.handle.create_db``（**手柄与介质无关，禁复制**）；
  - **io 注入点（04 固化）**：整量 io 契约 ``{'load': (database) -> snapshot,
    'save': (database, changed, collections) -> None}`` —— ``load``/``save``
    **必须带 ``database`` 形参**，使同一 io 可服务多个库（消除静默串源）；
    缺省实现用进程内分区 ``dict``（``options['store']``），``options['io']`` 可注入
    （Redis 由此接入）；io 方法抛错原样上抛（禁吞）；
  - ``open_transaction(db)``：取该分区深拷贝为 ``staged``，``commit()`` 以 ``staged`` 整体替换分区，
    ``rollback()`` 丢弃（与 ``local/__init__.py`` 同形）。

键空间 = ``database`` 非空 ⇒ ``<database>/<collection>``，否则 ``<collection>``；
io 的 ``database`` 为空 ⇒ key 不带前缀（与载体键空间规则一致）。

★ 描述符必须是 ``dict``（``Mapping``）——``datasource._kind_of`` 按 Mapping 取 ``kind``
（见执行文档 §8-1）。对齐 ``nodejs-store/src/memory/index.js``。

边界（继承 ``local`` 既有硬边界）：单集合文档量上限 / 无索引 / **单进程语义**（跨进程不保证）。
本模块**只做 IO**：禁止实现 filter / 管道 / 算子（求值全部下沉 ``core.local_eval``）。
"""

from __future__ import annotations

import copy
from typing import Any

from ..local.handle import create_db

MEMORY_KIND = "memory"

# 进程内键空间：``<database>/<collection>``（``database`` 空则 ``<collection>``）→ 文档数组
_DEFAULT_STORE: dict[str, list] = {}


def _clone(value: Any) -> Any:
    """深拷贝（与落盘 JSON 语义一致：与调用方隔离，模拟 local 每次读盘的新对象）"""
    return copy.deepcopy(value)


def _key(database: str | None, collection: str) -> str:
    """拼分区键：``database`` 非空 ⇒ ``<database>/<collection>``，否则 ``<collection>``。"""
    return f"{database}/{collection}" if database else collection


def _map_io(store: dict) -> dict:
    """默认 io：进程内分区 dict（按 ``<database>/<collection>`` 界定分区）。"""

    def load(database):
        """读该库分区：收集同前缀键并深拷贝返回（不属本库的子分区键剔除）。"""
        prefix = f"{database}/" if database else ""
        out: dict[str, list] = {}
        for k, docs in store.items():
            if not k.startswith(prefix):
                continue
            coll = k[len(prefix) :]
            if database and "/" in coll:
                continue  # 同前缀但不属本库分区（如 db='a' 不命中 'a/b/c'）
            out[coll] = _clone(docs)
        return out

    def save(database, changed, collections) -> None:
        """回写变更集合：按分区键深拷贝写入 ``store``（``changed`` 非空才被调用）。"""
        for name in list(changed):
            value = collections.get(name)
            store[_key(database, name)] = _clone([] if value is None else value)

    return {"load": load, "save": save}


def _bind_io(io: dict, database: str | None):
    """把带 ``database`` 形参的整量 io 适配为 ``create_db`` 需要的 ``(load, save)`` 二元组"""
    db = database
    return (
        lambda: io["load"](db),
        lambda changed, collections: io["save"](db, changed, collections),
    )


def connect(options: dict | None = None) -> dict:
    """创建进程内内存数据源连接（描述符）。

    :param options: ``{'database': <库名>, 'store': <分区 dict>, 'io': <整量 io 实现>}``；
        ``database`` 缺省 ``None``（键空间不带前缀）；``io`` 注入时优先（Redis 由此接入），
        缺省用 ``store``（缺省进程内全局分区）构造内存 io。
    """
    options = options or {}
    store: dict = (
        options["store"] if options.get("store") is not None else _DEFAULT_STORE
    )
    io: dict = options.get("io") or _map_io(store)
    database = options.get("database")

    def handle_for(db):
        """按库绑定 io 构造分区手柄。"""
        load, save = _bind_io(io, db)
        return create_db(load, save, db)

    desc = {
        "kind": MEMORY_KIND,
        "database": database,
        "handle": handle_for(database),
        "handle_for": handle_for,
    }

    async def open_transaction(db=None):
        """事务原语：快照隔离（读 io 快照 ``staged``，commit 才整体写回分区）。

        :param db: 目标库；缺省沿用描述符自身 ``database``
        """
        db = database if db is None else db
        load, save = _bind_io(io, db)
        state: dict[str, Any] = {"staged": load(), "closed": False}

        def _tx_load():
            """取事务期内存快照 ``staged``（不读 io）。"""
            return state["staged"]

        def _tx_save(_changed, collections):
            """事务期写操作：仅更新内存快照 ``staged``，不写回分区。"""
            state["staged"] = collections

        async def commit():
            """提交事务：以 ``staged`` 整体写回该库分区；重复调用为 no-op。"""
            if state["closed"]:
                return
            state["closed"] = True
            staged = state["staged"]
            save(list(staged.keys()), staged)

        async def rollback():
            """回滚事务：丢弃 ``staged`` 并关闭事务（不写回分区）。"""
            state["closed"] = True

        async def release():
            """释放事务句柄：标记关闭，防止后续 commit 写回。"""
            state["closed"] = True

        return {
            "session": {"snapshot": lambda: state["staged"]},
            "handle": create_db(_tx_load, _tx_save, db),
            "commit": commit,
            "rollback": rollback,
            "release": release,
        }

    async def with_transaction(fn, db=None):
        """便捷包装（与 sqlite 执行器 with_transaction 同形）。"""
        tx = await open_transaction(db if db is not None else database)
        try:
            out = await fn(None, tx)
            await tx["commit"]()
            return out
        except BaseException:  # 收尾路径必须覆盖取消/中断：先回滚再原样重抛，禁吞异常（语义与 datasource 收尾一致）
            await tx["rollback"]()
            raise
        finally:
            await tx["release"]()

    desc["open_transaction"] = open_transaction
    desc["with_transaction"] = with_transaction
    return desc


__all__ = ["MEMORY_KIND", "connect"]
