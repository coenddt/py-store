"""
MongoStore — 轻量 MongoDB 数据层（Python 版）

核心理念:
  1. 纯 JSON schema 定义，零代码
  2. 读取时自动补默认值 + 执行计算列
  3. GQL 树形查询 → 一次 $lookup 聚合
  4. 写入只存用户数据，不补默认值

用法:
    from mongo_store import init, store

    await init(db)
    items = await store.query(`Model($condition:@c0) { field1, field2 }`, {'c0': {...}})
"""

from typing import Any

from pymongo.errors import PyMongoError

from . import crud, permission, pipeline, schema


async def aggregate(schema_name: str, pipeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """对指定 schema 执行 MongoDB 原生聚合查询"""
    return await crud.aggregate(schema_name, pipeline)


_store_map = {
    # Schema 管理
    'register': schema.register,
    'get': schema.get,
    'has': schema.has,
    'list': schema.list,
    # CRUD
    'query': crud.query,
    'queryOne': crud.query_one,
    'queryWithCount': crud.query_with_count,
    'exists': crud.exists,
    'count': crud.count,
    'insert': crud.insert,
    'insertMany': crud.insert_many,
    'update': crud.update,
    'updateMany': crud.update_many,
    'remove': crud.remove,
    # Mutation — 智能持久化（upsert/insert + 父子关联填充）
    'mutation': crud.mutation,
    # Upsert — 显式条件 upsert（不处理父子关系）
    'upsert': crud.upsert,
    # 底层工具（调试/高级用法）
    'parseGQL': pipeline.parse_gql,
    'buildPipeline': pipeline.build_pipeline,
    # 原生聚合查询
    'aggregate': aggregate,
    # 权限控制（ContextVar 上下文）
    'setContext': permission.set_context,
    'getContext': permission.get_context,
    'set_context': permission.set_context,
    'get_context': permission.get_context,
    'scopedRoles': permission.scoped_roles,
    'scoped_roles': permission.scoped_roles,
    'runAsInternal': permission.run_as_internal,
    'run_as_internal': permission.run_as_internal,
    'PermissionError': permission.PermissionError,
    # 蛇形命名别名（Python 风格调用）
    'query_one': crud.query_one,
    'query_with_count': crud.query_with_count,
    'insert_many': crud.insert_many,
    'update_many': crud.update_many,
    'parse_gql': pipeline.parse_gql,
    'build_pipeline': pipeline.build_pipeline,
}


class Store:
    """以属性方式访问 _store_map，支持 store.query(...) 调用形态"""

    async def query(self, gql: str, params: dict | None = None) -> list[dict[str, Any]]:
        return await crud.query(gql, params)

    async def query_one(self, gql: str, params: dict | None = None) -> dict[str, Any] | None:
        return await crud.query_one(gql, params)

    async def query_with_count(self, gql: str, params: dict | None = None) -> dict[str, Any]:
        return await crud.query_with_count(gql, params)

    async def insert(self, schema_name: str, data: dict) -> dict[str, Any]:
        return await crud.insert(schema_name, data)

    async def insert_many(self, schema_name: str, docs: list[dict]) -> list[dict[str, Any]]:
        return await crud.insert_many(schema_name, docs)

    async def update(self, schema_name: str, condition: dict, data: dict,
                     options: dict | None = None) -> dict[str, Any] | None:
        return await crud.update(schema_name, condition, data, options)

    async def update_many(self, schema_name: str, condition: dict, data: dict) -> dict[str, Any]:
        return await crud.update_many(schema_name, condition, data)

    async def remove(self, schema_name: str, condition: dict) -> dict[str, Any]:
        return await crud.remove(schema_name, condition)

    async def exists(self, schema_name: str, condition: dict) -> bool:
        return await crud.exists(schema_name, condition)

    async def count(self, schema_name: str, filter: dict | None = None) -> int:
        return await crud.count(schema_name, filter)

    async def mutation(self, schema_name: str, data: dict | list[dict]) -> Any:
        return await crud.mutation(schema_name, data)

    async def upsert(self, schema_name: str, condition: dict, data: dict,
                     options: dict | None = None) -> dict[str, Any] | None:
        return await crud.upsert(schema_name, condition, data, options)

    async def aggregate(self, schema_name: str, pipeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return await crud.aggregate(schema_name, pipeline)

    def __getattr__(self, name):
        return _store_map[name]


store = Store()

# 索引名对齐 MongoDB 自动命名（k1_v1_k2_v2），用于幂等创建
async def _create_indexes_if_needed(db):
    names = schema.list()
    for name in names:
        s = schema.get(name)
        coll = db[s['collection']]
        try:
            index_cursor = await coll.list_indexes()
            existing_indexes = await index_cursor.to_list(length=None)
        except PyMongoError:
            existing_indexes = []

        for idx in s.get('indexes') or []:
            try:
                keys = idx.get('keys')
                if not keys:
                    continue
                # 合并 inline 选项（unique/sparse/expireAfterSeconds 等）与显式 options
                explicit_options = idx.get('options') or {}
                final_options = {k: v for k, v in idx.items() if k not in ('keys', 'options')}
                final_options.update(explicit_options)

                # 检查是否已有同 key 模式的索引（忽略选项差异）
                name_from_keys = '_'.join(f'{k}_{v}' for k, v in keys.items())
                if any(ei.get('name') == name_from_keys for ei in existing_indexes):
                    continue

                await coll.create_index(list(keys.items()), **final_options)
            except PyMongoError as e:
                import sys
                print(f'[MongoStore] 创建索引失败 {s["collection"]}: {e}', file=sys.stderr)


async def init(db):
    """初始化 store — 传入 PyMongo AsyncMongoClient 的 db 实例"""
    if db is None or not hasattr(db, 'collection'):
        raise TypeError('init(db) 需要 PyMongo async 的 db 实例')
    crud.set_db(db)

    # 自动创建索引 — 幂等安全
    await _create_indexes_if_needed(db)

    return store
