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
    'create_connection', 'shape_result', 'UpdateResult', 'DeleteResult',
    'mongo', 'mysql', 'postgres', 'sqlite',
]
