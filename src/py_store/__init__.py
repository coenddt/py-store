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
    ask,
    cache,
    crud,
    datasource,
    ddl,
    feedback,
    metadef,
    naming,
    permission,
    resource,
    schema,
    secure,
    workflow,
)
from . import (
    executors as executors,
)
from . import (
    introspect as introspect,
)
from . import (
    load as load,
)
from . import (
    local as local,
)
from .ask import AskExhausted as AskExhausted
from .ask import AskResult as AskResult
from .crud import triggers as triggers
from .crud.triggers import assert_trigger_fns_covered as assert_trigger_fns_covered
from .crud.triggers import set_trigger_fn as set_trigger_fn
from .datasource import NativeCommandError as NativeCommandError
from .datasource import NonAtomicWriteError as NonAtomicWriteError
from .datasource import RawSqlError as RawSqlError
from .datasource import Session as Session
from .llm import get_llm as get_llm
from .llm import make_openai_compat as make_openai_compat
from .llm import register_llm as register_llm
from .load import load_defs as load_defs
from .schema import assert_fns_covered as assert_fns_covered
from .schema import set_fn as set_fn
from .schema import text2query
from .scope import current_scope as current_scope
from .scope import with_scope as with_scope
from .sync import sync_schema


def _build_pipeline(gql, params=None):
    """解析 GQL 并构建 pipeline，返回 `{tokens, ast, pipeline, projection}`"""
    return schema.get_core().build_pipeline(
        gql, params if params is not None else {}, permission.get_context())


class Store:
    """以属性方式访问各 API（**全显式类方法，无动态查找**）

    CRUD / mutation 各方法均支持可选 ``route_override``
    （``{'source', 'database', 'schema'}`` 多租户路由，覆盖命令定位；权限与计算列
    仍按结构 schema 判定，见 multi-datasource-routing-plan.md §6）。

    Python 侧仅暴露蛇形命名（D12 / A8）。
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
                          database: str | None = None, schema: str | None = None,
                          register_defs: bool = True) -> list[dict[str, Any]]:
        return await sync_schema(backend, driver, introspect_options, overlay,
                                 datasource, database, schema, register_defs)

    def build_pipeline(self, gql: str, params: dict | None = None) -> dict[str, Any]:
        return _build_pipeline(gql, params)

    # ── 事务 + 原生 SQL（复用 datasource.run_in_transaction；见 README「事务边界」）──
    async def transaction(self, source: str, fn) -> Any:
        """事务作用域：单 SQL 源「同连接 + 同事务」执行 fn（复用 run_in_transaction）

        fn 内 execute_raw / CRUD 均落到该源的事务连接（commit/rollback 一体）；
        Mongo 源或执行器未实现事务时按原样执行（跨源无法原子），绝不静默假装已事务化。
        单源场景 source 传 ``'default'``。

        同源嵌套 transaction 会开保存点（内层失败只回滚本层）；句柄无保存点原语时
        降级并入外层并发 ``nested_savepoint_unsupported``。
        """
        return await datasource.run_in_transaction(source, fn)

    def session(self):
        """会话（工作单元）：``async with`` 语法，退出统一提交 / 异常统一回滚

        用法::

            async with store.session() as s:
                await s.insert('Order', {...})
                await s.update('Account', cond, {...})
                await s.execute_raw('pg_main', 'SELECT ... FOR UPDATE', [1])

        约束：同一会话内写命令只允许落在**单一数据源**；跨源写退出时抛
        ``NonAtomicWriteError``（先全部回滚，绝不提交半截）。

        嵌套：内层会话作为嵌套作用域在已有事务上开保存点，内层失败只回滚本层
        （生命周期仍交外层）。
        """
        return Session()

    async def execute_raw(self, source: str, sql: str, params: list | tuple | dict | None = None,
                          is_write: bool | None = None) -> dict[str, Any]:
        """在指定 SQL 源执行原生 SQL（事务内可用；编译由 core raw_stmt_compile 完成）

        两档参数风格：位置档（params 为 list/tuple/None）→ 占位符为各后端原生风格
        （mysql/sqlite 用 ``?``，postgres 用 ``$1..$n``），SQL 原样透传；命名档
        （params 为 dict）→ SQL 文本中的 ``:name`` 编译为方言占位符（同名复用、
        跳过 ``::`` cast / 引号 / 注释边界；缺名 / 多余名显式报错）。
        ``is_write`` 缺省时按 SQL 首词推断（读白名单外一律按写——安全方向）。
        仅支持 SQL 源（Mongo 源抛 RawSqlError）。返回 rows / affectedRows。
        """
        return await datasource.execute_raw(source, sql, params, is_write)

    async def execute_native(self, source: str, collection: str,
                             pipeline: list | None = None,
                             options: dict | None = None) -> dict[str, Any]:
        """在指定 Mongo 源执行原生聚合管道（事务内可用；对标 SQL 侧 execute_raw）

        ``pipeline`` 为原生聚合管道（list[dict]），``options`` 为驱动原生透传项
        （allowDiskUse/batchSize/hint/maxTimeMS…，宿主不做白名单）。事务 / 会话
        作用域内自动透传 session（由事务强制接管，``options.session`` 不可覆盖）；
        统一按读路径解析，``$merge``/``$out`` 写管道请自行开事务。
        仅支持 Mongo 源（SQL 源抛 NativeCommandError 并指引 execute_raw）。
        返回 ``{'rows': list}``。
        """
        return await datasource.execute_native(source, collection, pipeline, options)

    def generate_ddl(self, backend: str, names: list | None = None) -> str:
        """从已注册 schema def 生成指定后端 DDL 文本（纯函数，不连库、不回写；铁律 6）"""
        return ddl.generate(backend, names)

    # ── meta-store 定义控制面（定义持久化与版本化；见 metadef.py）──
    async def persist_def(self, defn: dict, opts: dict | None = None) -> dict:
        """持久化定义（同名同形幂等，异形 version+1；A1/A2）"""
        return await metadef.persist_def(self, defn, opts)

    async def list_defs(self, opts: dict | None = None) -> list[dict]:
        """列定义行（按 version desc；name 缺省列全部）"""
        return await metadef.list_defs(self, opts or {})

    # 注：定义控制面的「各 name 最新 active 行」经 `metadef.load_defs` 模块函数调用；
    # 门面 `store.load_defs` 归运行期目录装载器（见类体下文
    # `load_defs = staticmethod(load.load_defs)` 与 bootstrap.py），二者不得同名遮蔽。

    async def restore_defs(self, opts: dict | None = None) -> dict:
        """从持久化定义重建注册表：schema + workflow 两类（网关 reload 重装配前调用）"""
        base = opts or {}
        s = await metadef.restore_defs(self, {**base, 'kind': 'schema'})
        w = await metadef.restore_defs(self, {**base, 'kind': 'workflow'})
        return {'total': s['total'] + w['total'], 'applied': s['applied'] + w['applied']}

    async def rollback_to(self, opts: dict) -> dict:
        """回滚到历史版本（追加式，重新 register 该版本 defn）"""
        return await metadef.rollback_to(self, opts)

    # 定义控制面：workflow 定义（kind=workflow；见 metadef.py）
    async def persist_workflow_def(self, defn: dict, opts: dict | None = None) -> dict:
        """持久化 workflow 定义（同名同形幂等，异形 version+1）"""
        return await metadef.persist_def(self, defn, {**(opts or {}), 'kind': 'workflow'})

    async def list_workflow_defs(self, opts: dict | None = None) -> list[dict]:
        """列 workflow 定义行（按 version desc；name 缺省列全部）"""
        return await metadef.list_defs(self, {**(opts or {}), 'kind': 'workflow'})

    async def load_workflow_defs(self, opts: dict | None = None) -> list[dict]:
        """各 name 的最新 active workflow 定义行"""
        return await metadef.load_defs(self, {**(opts or {}), 'kind': 'workflow'})

    async def rollback_workflow_to(self, opts: dict) -> dict:
        """回滚 workflow 定义到历史版本（追加式）"""
        return await metadef.rollback_to(self, {**(opts or {}), 'kind': 'workflow'})

    def ensure_builtins(self) -> None:
        """自举内建定义表 __schemaDef/__workflowDef（幂等）"""
        return metadef.ensure_builtins()

    # ── 反馈事件落库（A6；见 feedback.py）──
    def enable_feedback_table(self):
        """一键接线：注册内建 __feedback 并把 sink 指向落库；返回 disposer（恢复原 sink）"""
        return feedback.enable_feedback_table(self)

    def set_feedback_meta(self, tenant: str = '', env: str = '') -> None:
        """注入进程级 ns 标签（tenant/env），供落库事件附加（进程级隔离下天然单 ns）"""
        feedback.set_meta({'tenant': tenant, 'env': env})

    async def flush_feedback(self):
        """等待全部在途 __feedback 落库完成（graceful shutdown 前调用；对齐 node store.flush_feedback）"""
        await feedback.flush()

    def register_provider(self, kind, mod):
        """注册资源 provider 类型（转发模块级注册表；mod 须具 create 属性）"""
        return resource.register_provider(kind, mod)

    def configure_resource(self, cfg):
        return resource.configure(cfg)

    async def resource_put(self, **kw):
        return await resource.put(**kw)

    async def resource_open(self, rid, **kw):
        return await resource.open(rid, **kw)

    async def resource_remove(self, rid):
        return await resource.remove(rid)

    async def resource_url(self, ref, **kw):
        return await resource.url(ref, **kw)

    # ── 缓存状态注记（B6；见 cache.py）──
    def set_cache_status(self, fn):
        """注册缓存状态 provider：`x-cache` 注记位唯一取值来源；未注册恒 BYPASS"""
        return cache.set_cache_status(fn)

    def cache_status(self, ctx: dict | None = None) -> str:
        """当前响应的缓存状态注记（恒为 HIT|MISS|BYPASS 之一）"""
        return cache.cache_status(permission.get_context() if ctx is None else ctx)

    # ── 工作流编排（首批：线性 + when 守卫 + fail-fast；见 workflow.py 与设计文档）──
    def register_workflow(self, defn: dict, ctx: dict | None = None) -> dict:
        """注册工作流定义（可选 ``ctx`` 过定义层门禁；默认 Open。白名单外显式 Err 含 WORKFLOW_UNSUPPORTED）"""
        return workflow.register(defn, ctx)

    def workflows(self, ctx: dict | None = None) -> list[str]:
        """全部可见工作流名（read 白名单过滤）"""
        return workflow.list(ctx)

    def get_workflow(self, name: str, ctx: dict | None = None) -> dict:
        """按名取工作流定义（read 白名单过滤；不可见与不存在同形——防枚举）"""
        return workflow.get(name, ctx)

    async def run_workflow(self, name: str, input: dict | None = None, *,
                           dry_run: bool = False,
                           route_override: dict | None = None) -> dict[str, Any]:
        """触发工作流 → 完整 run 文档（终态 failed/rejected 不抛错，以 run.status + error 表达）"""
        return await workflow.run(name, input, dry_run=dry_run, route_override=route_override)

    # ── AI 问数（L1，只读）：自然语言 → LLM 翻译 → text2query 沙箱执行 → 结构化回喂 ──
    # 护栏（档位/ctx/route_override）全部服务端硬编码于 ask.py，零暴露进 LLM 消息面（D5）
    # （先于 ask 赋值取 describe_for_ai：赋值后类体命名空间的 ask 不再是模块）
    describe_for_ai = staticmethod(ask.describe_for_ai)
    ask = staticmethod(ask.ask)
    # 问数结果/耗尽错误（实例可被 store.AskExhausted 捕获；对齐 PermissionError 先例）
    AskResult = AskResult
    AskExhausted = AskExhausted

    # ── 其余 API 显式绑定（staticmethod：避免实例化后 self 注入）──
    # Schema 管理
    register = staticmethod(schema.register)
    # 运行期目录装载（读 store.config.json → 收集定义 → core 纯规划 → 带定位批量注册）
    load_defs = staticmethod(load.load_defs)
    # 公开回调注入与启动期缺实现校验（对齐 nodejs-store store.set_fn / assert_fns_covered）
    set_fn = staticmethod(schema.set_fn)
    assert_fns_covered = staticmethod(schema.assert_fns_covered)
    # 触发器回调注入与启动期缺实现校验（对齐 nodejs-store store.setTriggerFn / assertTriggerFnsCovered）
    set_trigger_fn = staticmethod(triggers.set_trigger_fn)
    assert_trigger_fns_covered = staticmethod(triggers.assert_trigger_fns_covered)
    has = staticmethod(schema.has)
    get = staticmethod(schema.get)
    # 数据源连接（多后端路由）
    set_connections = staticmethod(crud.set_connections)
    # 权限控制（ContextVar 上下文）
    set_context = staticmethod(permission.set_context)
    get_context = staticmethod(permission.get_context)
    scoped_roles = staticmethod(permission.scoped_roles)
    run_as_internal = staticmethod(permission.run_as_internal)
    # 自定义权限错误（实例可被 store.PermissionError 捕获）
    PermissionError = permission.PermissionError
    # 原生 SQL 入口错误（实例可被 store.RawSqlError 捕获）
    RawSqlError = datasource.RawSqlError
    # 原生 Mongo 命令入口错误（实例可被 store.NativeCommandError 捕获）
    NativeCommandError = datasource.NativeCommandError
    # 上下文强制开关（fail-secure：开启后 ctx 缺失报 ERR_NO_CONTEXT，内部调用走 run_as_internal）
    set_require_context = staticmethod(schema.set_require_context)
    require_context = staticmethod(schema.require_context)
    # RBAC 动态策略（判决唯一在 core；本层仅透传配置与查询面）
    set_rbac = staticmethod(permission.set_rbac)
    rbac_enabled = staticmethod(permission.rbac_enabled)
    rbac_can = staticmethod(permission.rbac_can)
    rbac_readable_fields = staticmethod(permission.rbac_readable_fields)
    rbac_writable_fields = staticmethod(permission.rbac_writable_fields)
    rbac_row_condition = staticmethod(permission.rbac_row_condition)
    # 角色清单与未配置姿态（清单化语义，判决唯一在 core；本层仅透传配置）
    set_exempt_roles = staticmethod(permission.set_exempt_roles)
    set_deny_write_roles = staticmethod(permission.set_deny_write_roles)
    set_unconfigured_policy = staticmethod(permission.set_unconfigured_policy)
    # 定义层门禁策略（判决唯一在 core）：closed 时仅 internal/白名单可注册或覆盖
    set_meta_policy = staticmethod(schema.set_meta_policy)
    # 统一安全模式（fail-secure 一键入口；见 secure.py）：同时翻转上下文强制 +
    # 未配白名单全拒 + 定义注册门禁；admin_roles 为允许改定义的角色（默认仅 internal）
    secure_mode = staticmethod(secure.secure_mode)
    is_secure = staticmethod(secure.is_secure)
    relax_mode = staticmethod(secure.relax_mode)
    # 查询档位（判决唯一在 core）：standard 默认放开 / text2query 功能收缩
    # 进入档即等效强制 ctx；未知档由 core 抛 ValueError 上抛（禁静默回落默认档）
    set_profile = staticmethod(schema.set_profile)
    get_profile = staticmethod(schema.get_profile)
    # 档位拒绝错误（实例可被 store.ProfileViolation 捕获；权限错误另见 PermissionError）
    ProfileViolation = crud.ProfileViolation
    # text2query 便捷上下文（进入设档、退出恢复；同 scoped_roles 的 token-set/reset）
    text2query = staticmethod(text2query)
    # 反馈事件通道（兜底/降级/拦截的统一出口，接入自动反馈闭环）
    set_feedback_sink = staticmethod(feedback.set_sink)
    # 置最后：`list` 遮蔽内置名，须位于全部方法/类型注解之后
    list = staticmethod(schema.list)


store = Store()

# 触发链执行器装配：回调实现经门面（host['store']）消费 CRUD API
triggers.set_store(store)

# 索引名对齐 MongoDB 自动命名（k1_v1_k2_v2），用于幂等创建
async def _create_indexes_if_needed():
    """按数据源分派：Mongo 源执行索引创建；SQL 后端**不建索引**（indexes 仅元数据）"""
    names = schema.list()
    for name in names:
        s = schema.get(name)
        # 索引创建是初始化的辅助动作（非命令路由）：schema 绑定的 source 暂未在
        # 当前连接映射中时软跳过，不阻塞 init（命令路由的 fail fast 不在此处）；
        # 其余配置错误（database 形态不匹配等）按 fail-fast 由 db_of_schema 上抛
        if not datasource.has_connection(datasource.source_of_schema(name)):
            continue
        # local 源 v1 无索引（仅顺序扫描）：声明 indexes 即告警（禁静默忽略），直接跳过建索引
        if datasource.is_local(datasource.get_connection(datasource.source_of_schema(name))):
            if s.get('indexes'):
                feedback.emit({
                    'type': 'local_indexes_ignored',
                    'code': 'localIndexesIgnored',
                    'layer': 'local',
                    'message': f'schema {name} 声明了 indexes，但 local 数据源 v1 不建索引（仅顺序扫描）',
                    'hint': 'local 源请勿声明 indexes；需要索引请改用 sqlite/mongo 等后端',
                    'schema': name,
                })
            continue
        db = datasource.db_of_schema(name)  # Mongo 按 (datasource, database) 解析；SQL 源返回 None
        if db is None:
            continue  # SQL 后端不建索引（铁律 6）
        # 物理集合名：与命令执行路径同源（core::naming 的 camelCase 翻译，单点）
        coll = db[naming.physical(s['collection'])]
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

                # 检查是否已有同 key 模式的索引（忽略选项差异）；键按目标介质翻译为物理名
                phys_keys = [(naming.physical(k), v) for k, v in keys.items()]
                name_from_keys = '_'.join(f'{k}_{v}' for k, v in phys_keys)
                if any(ei.get('name') == name_from_keys for ei in existing_indexes):
                    continue

                await coll.create_index(phys_keys, **final_options)
            except PyMongoError as e:
                # 索引创建失败不阻塞 init（辅助动作），但必须走统一反馈通道：
                # 无 sink 时由 feedback 默认落 stderr（不双份打印），宿主可 set_sink 接管。
                # 对齐 nodejs-store/src/index.js（评审项 R7-m1）。
                feedback.emit({
                    'type': 'index_create_failed',
                    'code': 'indexCreateFailed',
                    'layer': 'host',
                    'message': f'创建索引失败 {s["collection"]}: {e}',
                    'hint': ('检查该集合的索引定义与连接权限；'
                             '索引缺失不影响读写，相关查询将退化为全表扫描'),
                })


async def init(connections):
    """
    初始化 store — 传入数据源连接映射

      - 多源：``init({'default': db, 'mongo_b': client,
            'pg_a': executors.create_connection('postgres', pool)})``
      - 单源简写：``init(db)`` / ``init(client)``（PyMongo async 的 db 实例或
        MongoClient，自动归一为 ``{'default': 连接}``）

    连接按命令的 ``source`` 路由、``database`` 定位库（schema 声明）；缺省绑定回落 ``default``。
    """
    if connections is None or (
            not isinstance(connections, Mapping)
            and not callable(getattr(connections, '__getitem__', None))):
        raise TypeError('init(connections) 需要数据源连接映射（或单个 PyMongo 的 db 实例）')
    datasource.set_connections(connections)

    # 自动创建索引（仅 Mongo 源）— 幂等安全
    await _create_indexes_if_needed()

    return store


def __getattr__(name: str):
    """惰性暴露框架统一入口 ``create_app``。

    放在模块级 ``__getattr__``（PEP 562）而非顶部 ``from .bootstrap import create_app``，
    以规避包内循环导入；因此无模块级导入，兼容 ruff E402 / I001。
    """
    if name == "create_app":
        from .bootstrap import create_app

        return create_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

