"""
CRUD 包 —— 薄 Host 适配层（对齐 nodejs-store/src/crud.js 的分工）

核心原则：
  - 全部纯逻辑（GQL 解析、权限、命令规划、结果后处理）在 Rust core
  - 本包只做 Host 三件事：命令执行（唯一 IO）、占位符替换、原生回调（asyncFn）
  - 写入时不补默认值（DB 存最少数据）；读取时由 core 补默认值 + 计算列
"""

from .exec import _get_db, set_connections, set_db
from .mutation import aggregate, mutation, upsert
from .query import query, query_federated, query_one, query_with_count
from .write import count, exists, insert, insert_many, remove, update, update_many

__all__ = [
    'set_db', 'set_connections', '_get_db',
    'query', 'query_one', 'query_with_count', 'query_federated',
    'insert', 'insert_many', 'update', 'update_many', 'remove', 'exists', 'count',
    'mutation', 'upsert', 'aggregate',
]
