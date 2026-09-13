---
name: "py-store"
description: "Python 宿主多后端数据层（分发包 storepy / 导入包 py_store）：纯 JSON schema + GQL 树查询，一份查询跑 MongoDB/MySQL/SQLite/PostgreSQL，含权限与计算列；本体是薄 Host，纯逻辑在 rust-store 的 core-py 绑定。调用场景：安装/使用 py-store、写 py_store 的 query/insert/update/mutation、配置多数据源路由或权限、排查 py-store 行为时。"
---

# py-store —— Python 多后端数据层（薄 Host）

## 1. 仓库定位

- `py-store` 是 **Python 宿主薄适配层**，把 `rust-store/core`（经 `core-py` PyO3 绑定）产出的「Mongo 命令 JSON」执行到真实后端。
- 端到端链路：**GQL → core 解析/规划 → Command（Mongo 命令 JSON，带 `source`/`namespace`/`collection` 三元组）→（SQL 源）core `dialect_translate` → 后端 SQL → executor 执行 → core 后处理（默认值 / 计算列 / 权限裁剪）**。
- 职责边界（源码注释反复强调，勿越界）：`src/py_store/*.py` 只做三件事——**驱动 IO（唯一 IO 边界）、占位符替换、原生回调（`asyncFn`）**；schema/GQL/权限/计算列/命令规划/结果后处理全部在 Rust core（`src/py_store/crud/__init__.py` 头部）。
- 依赖关系：`py-store` 的运行依赖是 `pymongo>=4.9` + `rust-store-py>=2.0.0,<3.0.0`（见 `py-store/pyproject.toml`）。**原生核心不在本仓库**，在独立的 `rust-store` 仓库；`nodejs-store` 是同一 core 的 Node 同构宿主。

## 2. 安装/依赖

```bash
pip install storepy                 # 分发包名是 storepy，不是 py-store（PyPI 近似名规则拒绝）
# 或本地开发（在 py-store/ 目录）：
pip install -e ".[dev]"             # dev = pytest + ruff + mypy（见 pyproject.toml）
```

- 导入名与分发包名不同：`from py_store import init, store`（README Installation）。
- 可选后端驱动（extras）：`mysql -> asyncmy`、`postgres -> asyncpg`、`sqlite -> aiosqlite`（`pyproject.toml [project.optional-dependencies]`）。
- 要求 Python **>= 3.10**；Mongo 走 PyMongo 的 `AsyncMongoClient`（pymongo >= 4.9）。
- **原生核心加载**（`src/py_store/core.py`）：生产只从 pip 的 `rust-store-py` 加载；开发期若要用相邻 `rust-store/core-py/dist` 的调试产物，必须显式 `LOCAL_CORE=1` 且 `NODE_ENV != 'production'`，否则报 `ImportError`。
  - 从源码跑测试前建议：`python -m maturin develop --manifest-path ../rust-store/core-py/Cargo.toml`（core.py 的报错提示命令）。

## 3. 快速上手（与当前 README 一致）

```python
from pymongo import AsyncMongoClient
from py_store import init, store

client = AsyncMongoClient("mongodb://localhost:27017")
await init(client["mydb"])            # 幂等创建已注册 schema 的索引（仅 Mongo 源）

store.register({
    "name": "Post",
    "collection": "posts",
    "idPrefix": "PT",
    "fields": {
        "title": {"type": "string", "default": ""},
        "status": {"type": "string", "default": "draft"},
        "tags": {"type": "array", "default": []},
    },
    "computes": {
        "statusLabel": {"type": "string", "depends": ["status"],
                        "fn": lambda doc: doc["status"].upper()},
    },
    "indexes": [{"keys": {"status": 1, "createdAt": -1}}],
})

doc = await store.insert("Post", {"title": "Hello"})   # 写入只存用户数据，默认值在读取时补

items = await store.query(
    "Post($condition:@c0,$sort:@s1,$limit:@l) { title, status, statusLabel }",
    {"c0": {"status": "draft"}, "s1": {"createdAt": -1}, "l": 20},
)
```

SQL 后端连接用执行器描述符：

```python
from py_store import init, store, executors

await init({"default": mongo_db, "pg_a": executors.create_connection("postgres", pg_pool)})
store.register({"name": "Order", "collection": "orders", "datasource": "pg_a",
                "namespace": "public", "fields": {"amount": {"type": "float"}}})
```

## 4. 核心 API 清单（逐条标注来源，均**真实存在**）

来源：`src/py_store/__init__.py`（`class Store`）、`src/py_store/crud/__init__.py`、`src/py_store/executors/__init__.py`、`src/py_store/introspect/__init__.py`、`src/py_store/sync.py`。

### 4.1 读（`crud/query.py`）

| API（蛇形 = 驼峰，同一实现） | 签名 | 存在 | 说明 |
| --- | --- | --- | --- |
| `query` | `(gql, params=None, route_override=None) -> list[dict]` | ✅ | GQL 树查询 |
| `query_one` / `queryOne` | `(gql, params=None, route_override=None) -> dict \| None` | ✅ | 单条 |
| `query_with_count` / `queryWithCount` | `(gql, params=None, route_override=None) -> {'items','total','hasMore','page','pageSize'}` | ✅ | pageSize 上限 5000 |
| `query_federated` / `queryFederated` | `(gql, params=None) -> list[dict]` | ✅ | 跨源联邦（**无 route_override 参数**） |

### 4.2 写（`crud/write.py` / `crud/mutation.py`）

| API | 签名 | 存在 | 说明 |
| --- | --- | --- | --- |
| `insert` | `(schema_name, data, route_override=None)` | ✅ | 自动 `_id`/`createdAt`/`updatedAt` |
| `insert_many` / `insertMany` | `(schema_name, docs, route_override=None)` | ✅ | |
| `update` | `(schema_name, condition, data, options=None, route_override=None)` | ✅ | 普通字段 → `$set`；`$`-前缀键当算子直通 |
| `update_many` / `updateMany` | `(schema_name, condition, data, route_override=None)` | ✅ | 空条件被拒（见坑 8.2） |
| `remove` | `(schema_name, condition, route_override=None)` | ✅ | 先归档到 `<collection>_deleted` |
| `exists` | `(schema_name, condition, route_override=None) -> bool` | ✅ | |
| `count` | `(schema_name, filter=None, route_override=None) -> int` | ✅ | |
| `mutation` | `(schema_name, data: dict \| list, route_override=None)` | ✅ | 智能 upsert + 递归处理关系子文档 |
| `upsert` | `(schema_name, condition, data, options=None, route_override=None)` | ✅ | 显式条件 upsert，不处理关系 |
| `aggregate` | — | ❌ | **已移除**（README/CHANGELOG 2.0.0）；用户 `$pipeline` 直通亦已移除并显式报错 |

### 4.3 Schema / 数据源 / 权限 / 反馈 / 结构同步

| API | 存在 | 来源 |
| --- | --- | --- |
| `register(defn)` / `has(name)` / `get(name)` / `list()` | ✅ | `schema.py`（`Store` 内为 staticmethod） |
| `set_connections(...)` / `setConnections(...)` | ✅ | `crud/exec.py` |
| `set_context` / `setContext`、`get_context` / `getContext` | ✅ | `permission.py`（ContextVar） |
| `scoped_roles` / `scopedRoles`（**contextmanager**） | ✅ | `permission.py` |
| `run_as_internal(fn)` / `runAsInternal(fn)`（async） | ✅ | `permission.py` |
| `PermissionError`（`status = 403`） | ✅ | `permission.py` |
| `set_require_context` / `setRequireContext`、`require_context` / `requireContext` | ✅ | `schema.py` |
| `set_feedback_sink(fn)` / `setFeedbackSink` | ✅ | `feedback.py` |
| `sync_schema` / `syncSchema(backend, driver, introspect_options=None, overlay=None, datasource=None, namespace=None, register_defs=True)` | ✅ | `sync.py` |
| `build_pipeline` / `buildPipeline(gql, params=None)` | ✅ | `__init__.py::_build_pipeline` → core |
| `parse_gql` | ❌ | README 声称有蛇形别名 `parse_gql`，**代码未导出**（见第 10 节） |
| `executors.create_connection(kind, driver, options=None)` | ✅ | `executors/__init__.py`（kind ∈ mysql/postgres/sqlite） |
| `introspect.run(backend, driver, options=None)` | ✅ | `introspect/__init__.py` |

## 5. GQL 查询语法

```text
Model($condition:@c0,$sort:@s1,$skip:@sk,$limit:@l1) {
  field1, field2, obj.subField,
  Relation($condition:@c2,$sort:@s3,$limit:@l2) { f3, Nested { f4 } }
}
```

- 值全部来自 params dict，用 `@key` 引用；关系名后「参数列表 + 选择集」可同时出现。
- 支持的根级/关系级参数：`$condition`、`$sort`、`$skip`、`$limit`；**根级聚合**：`$group`、`$having`（见下）。
- 对象子字段用点号；关系在 schema 里声明（`type: "many" | "one"`）自动解析——**不要手写 `$lookup`**。
- `$pipeline` 参数**显式报错**（"直通已移除"，`rust-store/core/src/pipeline/parse.rs`），不是静默忽略。

### 根级 `$group` / `$having`（已实现，见 `rust-store/core/src/pipeline/group.rs` + `core/src/dialect/select/group_agg.rs`）

```text
Course($condition:@c0, $group:@g0, $having:@h0, $sort:@s0, $skip:@sk, $limit:@l0) { ... }
```

- `$group` 规格：`{ "by": ["status","meta.level"], "agg": { "n": {"$count":"*"}, "total": {"$sum":"price"} } }`。
- 聚合算子白名单：`$count` / `$sum` / `$avg` / `$min` / `$max`（`$count:"*"` = 行数）。
- 固定执行序：`$condition`(WHERE) → `$group`(GROUP BY) → `$having`(HAVING) → `$sort` → `$skip/$limit` → 投影；有 `$group` 时排序/分页作用于**分组结果**，`$sort` 键域 = `by` 键 ∪ `agg` 别名。
- `$having` 必须与 `$group` 同用，否则报错。
- `by` 仅支持标量域（含 object 点号路径），关系/数组/裸对象/schema 外字段显式报错；`agg` 仅支持本表标量字段。

### 计算列

- `fn`（同步）/ `asyncFn`（异步，Host 两段式）/ `agg`（关系聚合，**已归一，取代旧 `lookup`**）。
- `agg` 形态：`{"$count": "<关系名>"}` 或 `{"$sum"|"$avg"|"$min"|"$max": "<关系>.<字段>"}`；与 `fn`/`asyncFn` 互斥；空集语义 `$count → 0`，其余 `→ None`（CHANGELOG 2.0.0）。

## 6. 多后端 / 方言 / 多数据源

- 后端：**MongoDB / MySQL / SQLite / PostgreSQL**。GQL 树查询编译成**每后端一条原生查询**。
- 定位三元组 `(source, namespace, collection)` 全局唯一，重复注册抛错；`source` 是 `init({...})` 的连接键（缺省 `"default"`），`namespace` 是连接内 db/schema（`None` = 连接默认）。
- Mongo 连接两形态**严格校验不猜**：db 实例 → 命令 `namespace` 必须为 `None`；MongoClient → `namespace` 必须非 `None`。
- SQL 同连接跨 namespace 关联**下推**为 `"ns_a"."t" JOIN "ns_b"."t"`；仅 Mongo 跨 db 走内存联邦。
- **多租户路由**：任意 query/write 末参可传 `{"source","namespace"}` override（权限与计算列仍按结构 schema 判定）。
  - ⚠️ `route_override` 是**受信服务端参数**，禁止透传用户输入（否则 CWE-639 越权跨租户）。
- 权限：schema 级 `read`/`write` 角色白名单 + 字段级 `field.read`/`field.write` + 关系级 `rel.read` + 计算列级 `comp.read`（`rust-store/core/src/permission.rs`）；`super_admin`/`admin`/`internal` 全放行，`guest` 永不写，`creator` 是伪角色（`doc.createdBy == ctx.userId`）。
- **SQL 后端不建索引**（`indexes` 仅元数据，铁律 6）；只有 Mongo 源在 `init` 时幂等建索引。

## 7. 测试与发布

```bash
# 在 py-store/ 目录（pyproject 已配 testpaths=tests、pythonpath=src）
pip install -e ".[dev]"
python -m pytest                       # e2e 用例在 MySQL/PG/Mongo 不可达时自动 skip
python -m ruff check src tests
python -m mypy src/py_store
```

- 测试文件：`tests/test_py_store.py`、`tests/test_host_contract.py`、`tests/test_multi_datasource.py`、`tests/test_require_context.py`（无需外部服务）、`tests/test_real_backends_e2e.py`、`tests/test_federation_e2e.py`、`tests/test_scenario_course_platform.py`（需真实库）。
- 真实库连接可用环境变量覆盖：`MYSQL_URI` / `PG_URI` / `MONGO_URI`（README Development）。
- 场景测试样板见 `example/course-platform/`（scenario.json / schema.json / ddl/*.sql / seed/seed.json）。
- 发布：`.github/workflows/release-pypi.yml`，推 `v*` tag 触发；**先发 `rust-store-py` 再发 `storepy`**（否则 `pip install storepy` 缺依赖失败）。质量门禁 = ruff + mypy + pytest 全绿 → build → PyPI Trusted Publishing（OIDC）。
- 压测脚本：`stress/stress.py`。

## 8. 常见坑

1. **分发包名 ≠ 导入名**：装 `storepy`，`from py_store import ...`。
2. **R4 批量写空条件一票否决**：`update_many` / `remove` 的条件为 `{}`、`None` 或空逻辑组（`{"$and":[]}` / `{"$or":[]}`）时**显式拒绝**，绝不落全表（`rust-store/core/src/command/mutate/*`）。
3. **`__present` 是 SQL 内部哨兵列**（`rust-store/core/src/dialect/write/mod.rs`）：用于区分「字段显式 null」与「字段缺失」，由翻译层注入/消费，Host 与用户**不要**碰它。
4. **Mongo 命令 vs SQL 差异**：上层返回值已由 executor 塑形为「PyMongo 驱动等价物」（SQL 路径返回 `UpdateResult`/`DeleteResult` 最小等价类，见 `executors/__init__.py::shape_result`），上层 CRUD 代码对两条路径透明。
5. **默认是 fail-open**：未设置权限上下文时所有检查放行（向后兼容）。安全敏感宿主应在启动时 `store.set_require_context(True)`，此后缺 ctx 抛 `ERR_NO_CONTEXT:...`，内部任务显式用 `run_as_internal`。
6. **不得静默失守方向**：`scoped_roles` 用 token set/reset（嵌套安全），不要用 `set_context(...)` + finally 清空（会误清外层且"无上下文=全放行"）。
7. **时间戳单位**：schema `timestamps` 仅接受 `True/False/'ms'/'s'`（缺省毫秒），非法值**注册即报错**；`createdAt`/`updatedAt` 由框架维护，勿手写。core 无时钟，`now` 由 Host 提供。
8. **迁移注意（2.0.0 Breaking）**：计算列 `lookup` 形态已移除 → 改用 `agg`；`store.aggregate()` / 用户 `$pipeline` 已移除。
9. **U1~U4 全局显式报错**：数组字段直接过滤（U1）、对象深度等值过滤（U2）、对象点号路径过滤（U3）/排序（U4）在所有后端统一报错，不静默降级。
10. **SQL 下推不可翻译时显式抛错**：`PushdownUnsupportedError`（`RuntimeError`，`datasource.py`）并同时走 `feedback` 通道；可捕获后改用 Mongo 源执行该段。
11. **`__init__` 里 `list` 是 staticmethod 且置于末尾**（遮蔽内置名），扩展该类时注意顺序。

## 9. 相关 skill / 文档

- `py-store-scenario-test`（父仓库 `.trae/skills`）：搭 `example/` 真实业务场景 + 跑真实四库 e2e 测试。
- 设计文档（**不在本仓库**，在 `rust-store/.trae/documents/`）：`multi-datasource-routing-plan.md`、`未处理-多后端归一化执行计划.md` 等（本仓库 CHANGELOG 引用了这些路径）。
- 本仓库内文档：`doc/test-eval/`、`doc/code-review/`、`doc/fix-plan/`、`doc/2026-09-12-测试报告.md`、`doc/2026-09-12-压测报告.md`。

## 10. 文档与代码不一致（实测差异）

- **README "Query & write API" 末尾**称"Snake-case aliases available: `query_one`, `insert_many`, `update_many`, `parse_gql`, `build_pipeline`, ..."——`parse_gql` 在 `py_store` 全包无导出（`py_store/__init__.py` 只有 `build_pipeline`/`buildPipeline`），以源码为准。
- **README Development** 示例用仓库根路径（`$env:PYTHONPATH='py-store/src'; python -m pytest py-store/tests/`）；在本仓库内直接 `python -m pytest` 即可（`pyproject.toml` 已配 `pythonpath = ["src"]`）。
- 旧文档/README 中 `create_connection` 示例若写成 `{"kind":"postgres","exec": exec}`，实为 `executors.create_connection(...)` 的**返回形状**；手写该 dict 需自行保证 `exec` 契约。
