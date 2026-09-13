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

from . import (
    crud,
    datasource,
    feedback,
    permission,
    schema,
)
from . import (
    executors as executors,
)
from . import (
    introspect as introspect,
)
from .sync import sync_schema


def _build_pipeline(gql, params=None):
    """解析 GQL 并构建 pipeline，返回 `{tokens, ast, pipeline, projection}`"""
    return schema.core.build_pipeline(
        gql, params if params is not None else {}, permission.get_context())


class Store:
    """以属性方式访问各 API（**全显式类方法，无动态查找**）

    CRUD / mutation 各方法均支持可选 ``route_override``
    （``{'source', 'namespace'}`` 多租户路由，覆盖命令定位；权限与计算列
    仍按结构 schema 判定，见 multi-datasource-routing-plan.md §6）。

    驼峰命名对齐 JS 端（nodejs-store）既有约定，蛇形为 Python 风格命名；
    两者指向同一实现，仅为命名差异（新增 API 只需定义一次）。
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

    async def sync_schema(self, backend: str, driver: Any, introspect_options: dict | None = None,
                          overlay: list | None = None, datasource: str | None = None,
                          namespace: str | None = None, register_defs: bool = True) -> list[dict[str, Any]]:
        return await sync_schema(backend, driver, introspect_options, overlay,
                                 datasource, namespace, register_defs)

    def build_pipeline(self, gql: str, params: dict | None = None) -> dict[str, Any]:
        return _build_pipeline(gql, params)

    # ── 驼峰别名（与上方同名蛇形方法为**同一实现**，仅命名差异）──
    queryOne = query_one
    queryWithCount = query_with_count
    queryFederated = query_federated
    insertMany = insert_many
    updateMany = update_many
    syncSchema = sync_schema
    buildPipeline = build_pipeline

    # ── 其余 API 显式绑定（staticmethod：避免实例化后 self 注入）──
    # Schema 管理
    register = staticmethod(schema.register)
    has = staticmethod(schema.has)
    get = staticmethod(schema.get)
    # 数据源连接（多后端路由）
    setConnections = staticmethod(crud.set_connections)
    set_connections = staticmethod(crud.set_connections)
    # 权限控制（ContextVar 上下文）
    setContext = staticmethod(permission.set_context)
    getContext = staticmethod(permission.get_context)
    set_context = staticmethod(permission.set_context)
    get_context = staticmethod(permission.get_context)
    scopedRoles = staticmethod(permission.scoped_roles)
    scoped_roles = staticmethod(permission.scoped_roles)
    runAsInternal = staticmethod(permission.run_as_internal)
    run_as_internal = staticmethod(permission.run_as_internal)
    # 自定义权限错误（实例可被 store.PermissionError 捕获）
    PermissionError = permission.PermissionError
    # 上下文强制开关（fail-secure：开启后 ctx 缺失报 ERR_NO_CONTEXT，内部调用走 run_as_internal）
    setRequireContext = staticmethod(schema.set_require_context)
    set_require_context = staticmethod(schema.set_require_context)
    requireContext = staticmethod(schema.require_context)
    require_context = staticmethod(schema.require_context)
    # 反馈事件通道（兜底/降级/拦截的统一出口，接入自动反馈闭环）
    setFeedbackSink = staticmethod(feedback.set_sink)
    set_feedback_sink = staticmethod(feedback.set_sink)
    # 置最后：`list` 遮蔽内置名，须位于全部方法/类型注解之后
    list = staticmethod(schema.list)


store = Store()

# 索引名对齐 MongoDB 自动命名（k1_v1_k2_v2），用于幂等创建
async def _create_indexes_if_needed():
    """按数据源分派：Mongo 源执行索引创建；SQL 后端**不建索引**（indexes 仅元数据）"""
    names = schema.list()
    for name in names:
        s = schema.get(name)
        # 索引创建是初始化的辅助动作（非命令路由）：schema 绑定的 source 暂未在
        # 当前连接映射中时软跳过，不阻塞 init（命令路由的 fail fast 不在此处）；
        # 其余配置错误（namespace 形态不匹配等）按 fail-fast 由 db_of_schema 上抛
        if not datasource.has_connection(datasource.source_of_schema(name)):
            continue
        db = datasource.db_of_schema(name)  # Mongo 按 (datasource, namespace) 解析；SQL 源返回 None
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
