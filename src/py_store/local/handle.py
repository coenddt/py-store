"""本地磁盘数据源 —— PyMongo ``Database`` 兼容手柄（Host 侧的「驱动替身」）

``create_db(load, save)`` 产出一个 **PyMongo Database 兼容**的 db（``db[name]`` →
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

from ..core import core as _local_core
from ..executors import DeleteResult, UpdateResult


class _Cursor:
    """PyMongo 游标最小替身（仅 ``to_list``，与 exec_mongo 调用点一致）"""

    def __init__(self, docs):
        self._docs = docs

    async def to_list(self, length=None):
        return list(self._docs)


class LocalCollection:
    def __init__(self, load, save, name):
        self._load = load
        self._save = save
        self._name = name

    def _eval(self, cmd):
        """单命令求值：load 快照 → core.local_eval → 返回包络 {result, changed, collections}"""
        return _local_core.local_eval(self._load(), {'collection': self._name, **cmd})

    def _read(self, cmd):
        """读命令：changed 恒空，直接取 result"""
        return self._eval(cmd)['result']

    def _write(self, cmd):
        """写命令：changed 非空则回写，返回 result"""
        out = self._eval(cmd)
        if out.get('changed'):
            self._save(out['changed'], out['collections'])
        return out['result']

    # ── 读（与 exec_mongo 调用点一一对应） ────────────────────────
    def find(self, filter=None, projection=None, **opts):
        return _Cursor(self._read({
            'kind': 'find', 'filter': filter or {}, 'projection': projection,
        }))

    async def find_one(self, filter=None, projection=None, **opts):
        return self._read({
            'kind': 'findOne', 'filter': filter or {}, 'projection': projection,
        })

    async def count_documents(self, filter=None, **opts):
        return self._read({'kind': 'countDocuments', 'filter': filter or {}})

    async def aggregate(self, pipeline, **opts):
        return _Cursor(self._read({
            'kind': 'aggregate', 'pipeline': list(pipeline or []), 'options': opts or {},
        }))

    def list_indexes(self):
        return _Cursor([])  # local v1 无索引

    async def create_index(self, *a, **kw):
        return None  # 声明即告警（由 __init__ 发 local_indexes_ignored），此处不静默建索引

    # ── 写 ──────────────────────────────────────────────────────
    async def insert_one(self, doc, **opts):
        return self._write({'kind': 'insertOne', 'doc': doc})

    async def insert_many(self, docs, upsert_by_id=False, **opts):
        return self._write({
            'kind': 'insertMany', 'docs': list(docs), 'upsertById': bool(upsert_by_id),
        })

    async def replace_one(self, filter, doc, upsert=False, **opts):
        # core 无独立 replaceOne：映射为 insertMany(upsertById=true) 单词形态
        # （exec_mongo 的 upsertById 分支逐条走 replaceOne({_id}, doc, upsert=True)）
        if not upsert:
            raise ValueError('local replace_one 仅支持 upsert 语义（upsertById 路径）；请勿它用')
        return self._write({'kind': 'insertMany', 'docs': [doc], 'upsertById': True})

    async def find_one_and_update(self, filter, update, **opts):
        return self._write({
            'kind': 'findOneAndUpdate', 'filter': filter, 'update': update,
            'options': opts or {},
        })

    async def update_many(self, filter, update, **opts):
        # core 回喂 {modifiedCount} → 塑形为 PyMongo UpdateResult（crud 读 .modified_count）
        out = self._write({'kind': 'updateMany', 'filter': filter, 'update': update})
        return UpdateResult(out['modifiedCount'])

    async def delete_many(self, filter, **opts):
        # core 回喂 {deletedCount} → 塑形为 PyMongo DeleteResult（crud 读 .deleted_count）
        out = self._write({'kind': 'deleteMany', 'filter': filter})
        return DeleteResult(out['deletedCount'])


class LocalDb:
    """PyMongo ``Database`` 兼容（仅需 ``__getitem__`` 取集合）"""

    def __init__(self, load, save):
        self._load = load
        self._save = save

    def __getitem__(self, name):
        return LocalCollection(self._load, self._save, name)


def create_db(load, save) -> LocalDb:
    return LocalDb(load, save)
