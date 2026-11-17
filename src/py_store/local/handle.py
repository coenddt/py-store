"""本地磁盘数据源 —— PyMongo ``Database`` 兼容手柄（Host 侧的「驱动替身」）

``create_db(load, save, database=None)`` 产出一个 **PyMongo Database 兼容**的 db（``db[name]`` →
集合对象），使 local 源可**复用** ``py_store/executors/mongo.py#exec_mongo`` ——
命令求值一律下沉 ``core.local_eval``，本模块只做手柄适配与 IO 编排
（禁在宿主编写 filter / 管道 / 算子）。

io 契约：``(load: () -> snapshot, save: (changed: list[str], snapshot) -> None)``
  - ``load()``：取当前「整目录集合快照」``{集合名: [文档…]}``；
  - ``save()``：在 core 判定有变更时回写变更集合（``changed`` 非空才调用）。

★ 实况修正（对执行文档 §4.3）：
  1. ``local_eval`` 是 ``Registry`` 的**实例方法**，非模块级导出；故
     ``from ..core import core`` 得到单例 ``Registry`` 后调 ``core.local_eval(...)``。
  2. core **无** ``replaceOne`` 命令；其驱动语义 = 「按 ``_id`` 命中则覆盖、未命中则插入」，
     即 core ``insertMany(upsertById=true)`` 的单词形态 —— 故 ``replace_one`` 映射为
     后者（对齐 ``nodejs-store/src/local/handle.js``，归档幂等由此承接）。
  3. **写结果须为 PyMongo 等价对象**：core 回喂 camelCase dict（``{modifiedCount}`` /
     ``{deletedCount}``），而 py 侧 ``crud/*`` 按 PyMongo 驱动语义用 **snake_case 属性**
     读取（``result.modified_count`` / ``result.deleted_count``，见
     ``crud/write.py`` 第 166/202 行）——故 ``update_many`` / ``delete_many`` 的 core
     结果须塑形为 ``executors.UpdateResult`` / ``executors.DeleteResult``（与 SQL 路径
     经 ``executors.shape_result`` 回喂同形；JS 侧驱动本就返回 ``{modifiedCount}`` 对象，
     故 node 手柄无需此步）。

方法集与 ``py_store/executors/mongo.py`` 第 109–161 行的调用点逐一对应
（**注意 async 形态差异**：``find`` / ``list_indexes`` 同步返回游标，``aggregate``
为协程）。``opts`` 内的 ``session`` 被忽略 —— local 的事务快照已绑定在 ``handle``
上（见 ``local/__init__.py``）。
"""

from __future__ import annotations

from typing import Any, Callable, cast

from ..core import core as _local_core
from ..executors import DeleteResult, UpdateResult


class _Cursor:
    """PyMongo 游标最小替身（仅 ``to_list``，与 exec_mongo 调用点一致）"""

    def __init__(self, docs: list[dict[str, Any]]) -> None:
        """持有待返回的文档列表。"""
        self._docs = docs

    async def to_list(self, length: int | None = None) -> list[dict[str, Any]]:
        """以协程形态返回全部文档（``length`` 忽略，与 exec_mongo 调用点一致）。"""
        return list(self._docs)


class LocalCollection:
    """本地磁盘数据源的集合句柄：load/save 委托宿主 IO，读写均落到内存快照。"""

    def __init__(
        self,
        load: Callable[[], dict[str, list[dict[str, Any]]]],
        save: Callable[[list[str], dict[str, list[dict[str, Any]]]], None],
        name: str,
    ) -> None:
        """绑定 IO 闭包（``load``/``save``）与集合名。"""
        self._load = load
        self._save = save
        self._name = name

    def _eval(self, cmd: dict[str, Any]) -> dict[str, Any]:
        """单命令求值：load 快照 → core.local_eval → 返回包络 {result, changed, collections}"""
        return cast(
            "dict[str, Any]",
            _local_core.local_eval(self._load(), {"collection": self._name, **cmd}),
        )

    def _read(self, cmd: dict[str, Any]) -> Any:
        """读命令：changed 恒空，直接取 result"""
        return self._eval(cmd)["result"]

    def _write(self, cmd: dict[str, Any]) -> Any:
        """写命令：changed 非空则回写，返回 result"""
        out = self._eval(cmd)
        if out.get("changed"):
            self._save(out["changed"], out["collections"])
        return out["result"]

    # ── 读（与 exec_mongo 调用点一一对应） ────────────────────────
    def find(
        self,
        filter: dict[str, Any] | None = None,
        projection: Any | None = None,
        **opts: Any,
    ) -> _Cursor:
        """读查询：同步返回游标替身（与 PyMongo 同步游标形态一致）。"""
        return _Cursor(
            self._read(
                {
                    "kind": "find",
                    "filter": filter or {},
                    "projection": projection,
                }
            )
        )

    async def find_one(
        self,
        filter: dict[str, Any] | None = None,
        projection: Any | None = None,
        **opts: Any,
    ) -> Any:
        """按过滤条件取第一条文档（无则 ``None``）。"""
        return self._read(
            {
                "kind": "findOne",
                "filter": filter or {},
                "projection": projection,
            }
        )

    async def count_documents(
        self, filter: dict[str, Any] | None = None, **opts: Any
    ) -> int:
        """按过滤条件计数（协程形态）。"""
        return cast(int, self._read({"kind": "countDocuments", "filter": filter or {}}))

    async def aggregate(self, pipeline: Any | None, **opts: Any) -> _Cursor:
        """执行聚合管道，返回游标替身（协程形态）。"""
        return _Cursor(
            self._read(
                {
                    "kind": "aggregate",
                    "pipeline": list(pipeline or []),
                    "options": opts or {},
                }
            )
        )

    def list_indexes(self) -> _Cursor:
        """返回空游标：local v1 无索引概念（同步形态）。"""
        return _Cursor([])  # local v1 无索引

    async def create_index(self, *a: Any, **kw: Any) -> None:
        """no-op 返回 ``None``：local v1 不建索引（忽略由调用方告警）。"""
        return (
            None  # 声明即告警（由 __init__ 发 local_indexes_ignored），此处不静默建索引
        )

    # ── 写 ──────────────────────────────────────────────────────
    async def insert_one(self, doc: dict[str, Any], **opts: Any) -> Any:
        """插入单条文档（写命令，变更落盘）。"""
        return self._write({"kind": "insertOne", "doc": doc})

    async def insert_many(
        self,
        docs: list[dict[str, Any]],
        upsert_by_id: bool = False,
        **opts: Any,
    ) -> Any:
        """批量插入；``upsert_by_id`` 为真时按 ``_id`` 命中即覆盖。"""
        return self._write(
            {
                "kind": "insertMany",
                "docs": list(docs),
                "upsertById": bool(upsert_by_id),
            }
        )

    async def replace_one(
        self,
        filter: dict[str, Any],
        doc: dict[str, Any],
        upsert: bool = False,
        **opts: Any,
    ) -> Any:
        """按 ``_id`` 命中覆盖、未命中插入；仅支持 upsert 语义（core 无独立 replaceOne）。"""
        # core 无独立 replaceOne：映射为 insertMany(upsertById=true) 单词形态
        # （exec_mongo 的 upsertById 分支逐条走 replaceOne({_id}, doc, upsert=True)）
        if not upsert:
            raise ValueError(
                "local replace_one 仅支持 upsert 语义（upsertById 路径）；请勿它用"
            )
        return self._write({"kind": "insertMany", "docs": [doc], "upsertById": True})

    async def find_one_and_update(
        self, filter: dict[str, Any], update: dict[str, Any], **opts: Any
    ) -> Any:
        """查找一条并按 ``update`` 修改（写命令）。"""
        return self._write(
            {
                "kind": "findOneAndUpdate",
                "filter": filter,
                "update": update,
                "options": opts or {},
            }
        )

    async def update_many(
        self, filter: dict[str, Any], update: dict[str, Any], **opts: Any
    ) -> UpdateResult:
        """批量更新，core 结果塑形为 PyMongo ``UpdateResult``（crud 读 ``modified_count``）。"""
        # core 回喂 {modifiedCount} → 塑形为 PyMongo UpdateResult（crud 读 .modified_count）
        out = self._write({"kind": "updateMany", "filter": filter, "update": update})
        return UpdateResult(out["modifiedCount"])

    async def delete_many(self, filter: dict[str, Any], **opts: Any) -> DeleteResult:
        """批量删除，core 结果塑形为 PyMongo ``DeleteResult``（crud 读 ``deleted_count``）。"""
        # core 回喂 {deletedCount} → 塑形为 PyMongo DeleteResult（crud 读 .deleted_count）
        out = self._write({"kind": "deleteMany", "filter": filter})
        return DeleteResult(out["deletedCount"])


class LocalDb:
    """PyMongo ``Database`` 兼容（仅需 ``__getitem__`` 取集合）"""

    def __init__(
        self,
        load: Callable[[], dict[str, list[dict[str, Any]]]],
        save: Callable[[list[str], dict[str, list[dict[str, Any]]]], None],
        database: str | None = None,
    ) -> None:
        """绑定 IO 闭包与库名（``database`` 仅作 IO 寻址标识，不进入 core 命令）。"""
        self._load = load
        self._save = save
        self.database = database  # 仅作 IO 寻址标识；读写经 load/save 闭包定位分区，不进入 core 命令

    def __getitem__(self, name: str) -> LocalCollection:
        """按名取集合手柄（PyMongo ``db[name]`` 兼容）。"""
        return LocalCollection(self._load, self._save, name)


def create_db(
    load: Callable[[], dict[str, list[dict[str, Any]]]],
    save: Callable[[list[str], dict[str, list[dict[str, Any]]]], None],
    database: str | None = None,
) -> LocalDb:
    """构造 PyMongo 兼容手柄。

    :param database: 仅作 IO 寻址标识（手柄读写经 ``load``/``save`` 闭包定位分区），
        不进入发往 core 的命令（对齐 ``nodejs-store/src/local/handle.js``）。
    """
    return LocalDb(load, save, database)
