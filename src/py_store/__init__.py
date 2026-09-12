"""
py-store — 轻量多后端数据层（Python 版，Rust 单核心架构；支持 MongoDB / MySQL / SQLite / PostgreSQL）

核心理念:
  1. 纯 JSON schema 定义，零代码
  2. Rust core 统一实现 GQL 解析 / 权限 / 计算列 / 命令规划（core-py 绑定）
  3. src/py_store/*.py 为薄 Host 适配层：驱动 IO + 回调 + 占位符替换
  4. Node 侧（core-node）复用同一 Rust core，双端语义天然一致

用法:
    from py_store import init, store

    await init(db)                       # 单库简写（Mongo db 实例）
    await init({                         # 多数据源（schema 的 datasource 绑定路由）
        'default': mongo_db,
        'mysql_a': executors.create_connection('mysql', mysql_pool),
    })

    items = await store.query(`Model($condition:@c0) { field1, field2 }`, {'c0': {...}})
"""

from collections.abc import Mapping
from typing import Any

from pymongo.errors import PyMongoError

from . import crud, datasource, executors, feedback, introspect, permission, schema
from .sync import sync_schema


def _build_pipeline(gql, params=None):
    """解析 GQL 并构建 pipeline，返回 `{tokens, ast, pipeline, projection}`"""
    return schema.core.build_pipeline(
        gql, params if params is not None else {}, permission.get_context())


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
    'queryFederated': crud.query_federated,
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
    'buildPipeline': _build_pipeline,
    'build_pipeline': _build_pipeline,
    # 原生聚合查询
    'aggregate': crud.aggregate,
    # 结构同步（SQL 数据源：introspect → schemaFromRows → mergeSchema → register）
    'syncSchema': sync_schema,
    'sync_schema': sync_schema,
    # 数据源连接（多后端路由）
    'setConnections': crud.set_connections,
    'set_connections': crud.set_connections,
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
    # 用户 $pipeline 直通开关（Registry 级守卫，AI 问数宿主建议关闭）
    'setAllowUserPipeline': schema.set_allow_user_pipeline,
    'set_allow_user_pipeline': schema.set_allow_user_pipeline,
    # 反馈事件通道（兜底/降级/拦截的统一出口，接入自动反馈闭环）
    'setFeedbackSink': feedback.set_sink,
    'set_feedback_sink': feedback.set_sink,
    # 蛇形命名别名（Python 风格调用）
    'query_one': crud.query_one,
    'query_with_count': crud.query_with_count,
    'query_federated': crud.query_federated,
    'insert_many': crud.insert_many,
    'update_many': crud.update_many,
}


class Store:
    """以属性方式访问 _store_map，支持 store.query(...) 调用形态

    CRUD / mutation / aggregate 各方法均支持可选 ``route_override``
    （``{'source', 'namespace'}`` 多租户路由，覆盖命令定位；权限与计算列
    仍按结构 schema 判定，见 multi-datasource-routing-plan.md §6）。
    """

    async def query(self, gql: str, params: dict | None = None,
                    route_override: dict | None = None) -> list[dict[str, Any]]:
        return await crud.query(gql, params, route_override)

    async def query_one(self, gql: str, params: dict | None = None,
                        route_override: dict | None = None) -> dict[str, Any] | None:
        return await crud.query_one(gql, params, route_override)

    async def query_with_count(self, gql: str, params: dict | None = None,
                               route_override: dict | None = None) -> dict[str, Any]:
        return await crud.query_with_count(gql, params, route_override)

    async def query_federated(self, gql: str, params: dict | None = None) -> list[dict[str, Any]]:
        """跨库联邦查询（一条 GQL 跨多数据源：各源取数 → 内存 join → 统一后处理）"""
        return await crud.query_federated(gql, params)

    async def insert(self, schema_name: str, data: dict,
                     route_override: dict | None = None) -> dict[str, Any]:
        return await crud.insert(schema_name, data, route_override)

    async def insert_many(self, schema_name: str, docs: list[dict],
                          route_override: dict | None = None) -> list[dict[str, Any]]:
        return await crud.insert_many(schema_name, docs, route_override)

    async def update(self, schema_name: str, condition: dict, data: dict,
                     options: dict | None = None,
                     route_override: dict | None = None) -> dict[str, Any] | None:
        return await crud.update(schema_name, condition, data, options, route_override)

    async def update_many(self, schema_name: str, condition: dict, data: dict,
                          route_override: dict | None = None) -> dict[str, Any]:
        return await crud.update_many(schema_name, condition, data, route_override)

    async def remove(self, schema_name: str, condition: dict,
                     route_override: dict | None = None) -> dict[str, Any]:
        return await crud.remove(schema_name, condition, route_override)

    async def exists(self, schema_name: str, condition: dict,
                     route_override: dict | None = None) -> bool:
        return await crud.exists(schema_name, condition, route_override)

    async def count(self, schema_name: str, filter: dict | None = None,
                    route_override: dict | None = None) -> int:
        return await crud.count(schema_name, filter, route_override)

    async def mutation(self, schema_name: str, data: dict | list[dict],
                       route_override: dict | None = None) -> Any:
        return await crud.mutation(schema_name, data, route_override)

    async def upsert(self, schema_name: str, condition: dict, data: dict,
                     options: dict | None = None,
                     route_override: dict | None = None) -> dict[str, Any] | None:
        return await crud.upsert(schema_name, condition, data, options, route_override)

    async def aggregate(self, schema_name: str, pipeline: list[dict[str, Any]],
                        route_override: dict | None = None) -> list[dict[str, Any]]:
        return await crud.aggregate(schema_name, pipeline, route_override)

    async def sync_schema(self, backend: str, driver: Any, introspect_options: dict | None = None,
                          overlay: list | None = None, datasource: str | None = None,
                          namespace: str | None = None, register_defs: bool = True) -> list[dict[str, Any]]:
        return await sync_schema(backend, driver, introspect_options, overlay,
                                 datasource, namespace, register_defs)

    def build_pipeline(self, gql: str, params: dict | None = None) -> dict[str, Any]:
        return _build_pipeline(gql, params)

    def __getattr__(self, name):
        return _store_map[name]


# 自定义权限错误（实例可被 store.PermissionError 捕获）
Store.PermissionError = permission.PermissionError

store = Store()

# 索引名对齐 MongoDB 自动命名（k1_v1_k2_v2），用于幂等创建
async def _create_indexes_if_needed():
    """按数据源分派：Mongo 源执行索引创建；SQL 后端**不建索引**（indexes 仅元数据）"""
    names = schema.list()
    for name in names:
        s = schema.get(name)
        # 索引创建是初始化的辅助动作（非命令路由）：schema 绑定的 source 暂未在
        # 当前连接映射中时跳过，不阻塞 init（命令路由的 fail fast 不在此处）
        try:
            db = datasource.db_of_schema(name)  # Mongo 按 (datasource, namespace) 解析；SQL 源返回 None
        except Exception:  # noqa: BLE001
            continue
        if db is None:
            continue  # SQL 后端不建索引（铁律 6）
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
                print(f'[py-store] 创建索引失败 {s["collection"]}: {e}', file=sys.stderr)


async def init(connections):
    """
    初始化 store — 传入数据源连接映射

      - 多源：``init({'default': db, 'mongo_b': client,
            'pg_a': executors.create_connection('postgres', pool)})``
      - 单源简写：``init(db)`` / ``init(client)``（PyMongo async 的 db 实例或
        MongoClient，自动归一为 ``{'default': 连接}``）

    连接按命令的 ``source`` 路由、``namespace`` 定位库（schema 声明）；缺省绑定回落 ``default``。
    """
    if connections is None or (
            not isinstance(connections, Mapping)
            and not callable(getattr(connections, '__getitem__', None))):
        raise TypeError('init(connections) 需要数据源连接映射（或单个 PyMongo 的 db 实例）')
    datasource.set_connections(connections)

    # 自动创建索引（仅 Mongo 源）— 幂等安全
    await _create_indexes_if_needed()

    return store
