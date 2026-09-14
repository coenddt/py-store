# py-store

**面向 Python asyncio 的统一数据层，覆盖 MongoDB、MySQL、SQLite 与 PostgreSQL —— 用纯 JSON 定义模型，用 MongoDB 风格的 GQL 树形语法查询，开箱即得基于角色的访问控制、计算列与软删除。**

![PyPI version](https://img.shields.io/pypi/v/storepy)
![license](https://img.shields.io/pypi/l/storepy)
![python versions](https://img.shields.io/pypi/pyversions/storepy)
![backends](https://img.shields.io/badge/backends-MongoDB%20%7C%20MySQL%20%7C%20SQLite%20%7C%20PostgreSQL-blue)
![query dialect](https://img.shields.io/badge/query%20dialect-GQL%20(MongoDB--flavoured)-green)

> English docs: [README.md](README.md)

`py-store` 让 Python 服务通过**单一 schema 定义与单一查询方言**同时对接 MongoDB（原生聚合）、MySQL、PostgreSQL 与 SQLite。嵌套关系会编译为**每个后端各一条原生查询** —— 你永远不必手写 `$lookup` 或裸 SQL。

> 也在找 Node.js 版本？见 [`nodejs-store`](https://github.com/coenddt/nodejs-store)（npm `nodejs-store`）。两者都是共享 Rust 引擎 [`rust-store`](https://github.com/coenddt/rust-store) 之上的薄宿主。

**安装：** 发行包名为 `storepy`；导入包名为 `py_store`。

```bash
pip install storepy
```

```python
from py_store import init, store
```

---

## 目录

- [它是什么](#它是什么)
- [何时使用](#何时使用)
- [何时不应使用](#何时不应使用)
- [横向对比](#横向对比)
- [安装](#安装)
- [快速开始](#快速开始)
- [支持的后端](#支持的后端)
- [特性](#特性)
- [GQL 树形查询](#gql-语法)
- [聚合](#聚合)
- [查询与写入 API](#查询与写入-api)
- [多数据源连接](#多数据源连接)
- [权限上下文](#权限上下文)
- [反馈事件](#反馈事件)
- [Schema 参考](#schema-参考)
- [事务边界](#事务边界)
- [常见问题](#常见问题)
- [相关项目](#相关项目)

---

## 它是什么

面向 Python asyncio 的轻量、后端无关的数据层。你把模型一次性描述为纯 JSON（`fields`、`relations`、`computes`、`indexes`、`read`/`write` 角色白名单）。库会从这份描述中推导出：

- **命令规划**（GQL → Mongo command JSON）—— 由 Rust 核心 `rust-store-py` 执行，
- **方言翻译**（command JSON → 参数化 SQL），面向 MySQL / PostgreSQL / SQLite，
- **权限检查**（schema 级 + 字段级读写、属主条件注入），
- **计算列**、**软删除归档**，以及**结果再水合**（扁平的 JOIN 行 → 嵌套文档）。

MongoDB 是*主方言*：查询用 MongoDB 风格的 GQL 编写，其余三个关系型后端向它适配。正因如此，一份 schema 才能同时可移植到文档库与三种关系库。

### 它与 nodejs-store 和 rust-store 的关系

```
                 ┌──────────────────────────────┐
   Node.js  ──▶  │  nodejs-store (npm, host)    │ ─┐
                 └──────────────────────────────┘  │  rust-store-node (napi-rs)
                                                   ▼
                                     ┌───────────────────────────────┐
                                     │ rust-store/core (pure logic)  │
                                     │ GQL · permissions · computes  │
                                     │ command planning · dialects   │
                                     └───────────────────────────────┘
                                                   ▲
                 ┌──────────────────────────────┐  │  rust-store-py (PyO3)
   Python   ──▶  │  py-store (pip, host)        │ ─┘
                 └──────────────────────────────┘
```

**Rust 核心**负责 GQL 解析、权限检查、计算列、命令规划与 SQL 方言翻译 —— 它从不接触数据库。**宿主**（`py-store`、`nodejs-store`）负责驱动 IO、回调与占位符替换。因此 Python 与 Node.js 之间的行为不会漂移：实现只有一份。

## 何时使用

在下列任一情形成立时，就适合选用 `py-store`：

- **一套代码，多个数据库。** 同一服务在开发环境跑 MongoDB、生产环境跑 PostgreSQL（或按租户切换），而你不想要两套数据访问层。
- **需要嵌套 / 关系型读取，又不想写 `$lookup` 或 JOIN。** Order → items、Course → lessons、User → orders —— 全部在 schema 中表达一次，由单条查询解析。
- **正在构建管理后台、FastAPI 服务或内部 CRUD API**，希望在不引入完整 ORM 的前提下获得 schema 驱动的 CRUD、软删除、计算列与角色校验。
- **需要行级 / 字段级访问控制。** 按角色配置白名单，`guest` 永远不能写，`creator` 归属按 `doc.createdBy` 校验，属主条件会自动注入查询。
- **正在构建 AI / 自然语言数据问答层。** 本库在设计时就考虑了 AI 查询宿主：`store.build_pipeline(...)` 可在不执行的情况下暴露规划好的查询，而降级 / 无法下推的路径会发出结构化反馈事件，而不是静默失败。
- **正在 MongoDB 与 SQL 之间迁移**，并希望在过渡期保持同一套查询语法。
- **多租户 SaaS。** 一份 schema 定义，N 个租户：把 schema 绑定到 `(source, namespace, collection)`，并在执行时用 `{"source", "namespace"}` 覆盖把任意查询或写入重新指向目标。

典型具体场景（完整走查见 [`doc/use-cases/`](doc/use-cases/)）：

| 场景 | 为什么适合 py-store |
| --- | --- |
| 按租户 schema/数据库隔离的多租户 SaaS | 每租户一个 `namespace` + 运行时路由覆盖，仅需一份 schema |
| FastAPI / 管理后台 | Schema 驱动 CRUD、软删除、计算列、RBAC |
| 今天 MongoDB，明天 PostgreSQL | 同一 GQL + 同一 schema，只有数据源改变 |
| AI 数据问答 / text-to-query agent | plan-only 的 `build_pipeline`、确定性的 command JSON、反馈事件 |
| 同一产品中混用 SQL + Mongo | 跨源查询，SQL 原生下推、Mongo 内存联邦 |
| 审计友好的 CRUD | 每个 schema 自动获得 `<Model>Deleted` 归档表/集合 |

## 何时不应使用

把边界说清楚能帮你省时间：

- **你想要带迁移引擎的完整 ORM（Alembic、Django migrations）。** `py-store` 是*数据层*，不是迁移工具。它能**读取** SQL 后端的物理结构（`sync_schema` → 内省），但从不回写 DDL。
- **你想要以 Pydantic 模型为中心的 ORM。** 这里的 schema 是运行时 JSON dict，带来的是跨语言一致性（同一 schema 可在 Python 与 Node.js 中运行），而非 Pydantic 类型校验。
- **你只用一种数据库，且很少做关联。** 直接用驱动（或单库 ODM/ORM）会更简单。
- **你需要裸聚合的逃生通道。** `$pipeline` 透传与 `store.aggregate()` 已被有意移除。请使用 `$condition` / `$group` / `$having` / relations；任何无法被安全翻译的东西都会**显式失败**，而不是静默处理。

## 横向对比

以下仅为整体定位，不是基准测试 —— 请始终对照各工具的最新文档核实。

| | py-store | SQLAlchemy | Beanie / Motor | Tortoise ORM | SQLModel | Django ORM |
| --- | --- | --- | --- | --- | --- | --- |
| 主要形态 | JSON schema + GQL 数据层 | SQL 工具集 + ORM | 异步 MongoDB ODM / 驱动 | 异步 ORM | Pydantic + SQLAlchemy | Django 内置 ORM |
| 后端 | MongoDB、MySQL、SQLite、PostgreSQL | PostgreSQL、MySQL、SQLite、Oracle、MSSQL | MongoDB | PostgreSQL、MySQL、SQLite、Oracle、MSSQL | PostgreSQL、MySQL、SQLite、… | PostgreSQL、MySQL、SQLite、Oracle |
| Mongo **与** SQL 共用一套查询方言 | ✅（MongoDB 风格 GQL） | ➖（仅 SQL） | ➖（仅 Mongo） | ➖（仅 SQL） | ➖（仅 SQL） | ➖（仅 SQL） |
| 单条查询读出嵌套关系 | ✅ 声明式 relations → `$lookup` / `JOIN` | ⚠️ 手动 `selectinload`/join | ✅ `Link`/`fetch_links` | ✅ `prefetch_related` | ⚠️ 经 SQLAlchemy | ✅ `prefetch_related` |
| 内置角色 / 字段级 RBAC + 属主注入 | ✅ | ➖ | ➖ | ➖ | ➖ | ➖（权限在应用层） |
| 读取时计算列（同步 / 异步 / 关系聚合） | ✅ | ➖（hybrid property） | ➖ | ➖ | ➖ | ➖ |
| 自动开通软删除归档表 | ✅ | ➖ | ➖ | ➖ | ➖ | ➖ |
| 迁移 / DDL 引擎 | ➖（内省只读） | ✅（Alembic） | ➖ | ✅（Aerich） | ✅（Alembic） | ✅ |
| 框架耦合 | 无（asyncio） | 无 | 无 | 无 | 无 | Django |
| Python 与 Node 共享原生核心 | ✅（Rust `rust-store`） | ➖ | ➖ | ➖ | ➖ | ➖ |

### 它与具体库的差异

以下仅为定位说明，基于这些项目在撰写时公开的文档 —— 请对照你自己的需求核实。

- **对比 SQLAlchemy / SQLModel / Django ORM** —— 它们都只支持 SQL 并以模型类为中心：不面向 MongoDB，也都不提供 schema 声明的角色/字段级访问控制或读取时计算列。`py-store` 把同一段 GQL 编译为原生 MongoDB 聚合或参数化 SQL。
- **对比 Beanie / Motor** —— 仅支持 MongoDB。`py-store` 采用相同的 MongoDB 风格查询写法，但同一条查询也能在 MySQL、SQLite 与 PostgreSQL 上运行。
- **对比 Tortoise ORM / pyloquent** —— 基于 SQL 后端的异步 Python ORM，使用模型类，并且（就 Tortoise 而言）带有迁移工具。`py-store` 没有迁移引擎 —— 内省只读取物理结构 —— 且把模型描述为普通 dict，这恰恰是 schema 能移植到 Node.js 宿主的原因。
- **对比 `nodejs-store`** —— 同一套引擎、同一套 GQL，只不过用 JavaScript。选择与你服务相匹配的宿主即可；schema 与查询语义可互换。

一句话：想要**模型类、Pydantic 校验和迁移**就用 ORM；想要**一份运行时 schema + 一套横跨 MongoDB 与 SQL 的查询方言**，并且内置 RBAC 与计算列，就用 `py-store`。

## 安装

```bash
pip install storepy
```

> 发行包名是 `storepy`；导入包名是 `py_store`：
> `from py_store import init, store`。

需要 Python 3.10+ 以及一个受支持的后端（MongoDB / MySQL / SQLite / PostgreSQL）。

可选的驱动 extra：

```bash
pip install "storepy[mysql]"     # asyncmy
pip install "storepy[postgres]"  # asyncpg
pip install "storepy[sqlite]"    # aiosqlite
```

## 快速开始

```python
from pymongo import AsyncMongoClient
from py_store import init, store

client = AsyncMongoClient("mongodb://localhost:27017")
await init(client["mydb"])   # 为已注册的 schema 幂等创建索引

# 注册一个 schema（纯 JSON）
store.register({
    "name": "Post",                  # GQL 中使用的模型名
    "collection": "posts",           # 可选，默认取 name
    "idPrefix": "PT",                # 字符串 _id：前缀 + base36 时间戳 + 随机串
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

# 写入 —— 只存用户数据；默认值在读取时补齐
doc = await store.insert("Post", {"title": "Hello"})

# 查询 —— GQL 树形语法，值经 @key 从 params 中引用
items = await store.query(
    "Post($condition:@c0,$sort:@s1,$limit:@l) { title, status, statusLabel }",
    {"c0": {"status": "draft"}, "s1": {"createdAt": -1}, "l": 20},
)
```

同一份 schema 与同一条查询可原封不动地在 PostgreSQL 上运行 —— 只需改动 `init()` 的数据源：

```python
await init({"default": {"kind": "postgres", "exec": exec}})
items = await store.query("Post($condition:@c0) { title, status }", {"c0": {"status": "draft"}})
```

## 支持的后端

| 后端 | 说明 |
| --- | --- |
| MongoDB | 原生聚合管道（`find`/`aggregate`/`$lookup`），PyMongo `AsyncMongoClient`（pymongo >= 4.9） |
| MySQL | 参数化 SQL，`information_schema` 内省（`asyncmy`） |
| SQLite | 参数化 SQL，`sqlite_master` + `PRAGMA` 内省（`aiosqlite`） |
| PostgreSQL | 参数化 SQL（`$n`），支持 `RETURNING`（`asyncpg`） |

GQL 树形查询在每个后端各编译为一条原生查询 —— 再也不用手写 `$lookup` 或裸 SQL。

## 特性

- **纯 JSON schema，零代码** —— 一个模型就是一个 dict：fields、relations、computes、indexes。
- **GQL 树形查询 → 单条原生查询** —— 嵌套关系在一条查询内解析；再也不用手写 `$lookup`。
- **规范化聚合** —— 在同一段 GQL 中支持根级 `$group` / `$having` 与关系聚合谓词（semi/anti-join），并下推到全部四个后端。
- **读取时默认值与计算列** —— 写入只存用户数据；读取时补齐默认值并运行 `fn` / `asyncFn` / 关系 `agg` 计算列。
- **智能 mutation** —— `mutation()` 依据 `_id` + 唯一索引自动识别 upsert，并递归填充关系子文档。
- **内置软删除** —— 每个 schema 自动注册一个 `<Model>Deleted` 归档集合/表；`remove()` 先归档再删除。
- **权限上下文** —— 基于 `ContextVar` 的角色（`super_admin`/`admin`/`guest`/`creator`…）、schema/字段级读写白名单、自动属主条件注入。
- **多数据源 & 多租户** —— 通过 `(source, namespace, collection)` 定位 schema；按请求用路由覆盖重新指向目标。
- **异步优先，Rust 核心** —— 构建在 PyMongo `AsyncMongoClient` 与共享的 Rust 核心（含 SQL 方言）之上。

## GQL 语法

```text
Model($condition:@c0,$sort:@s1,$skip:@sk,$limit:@l1) {
  field1, field2, obj.subField,
  Relation($condition:@c2,$sort:@s3,$limit:@l2) { f3, Nested { f4 } }
}
```

- 值来自 params dict：`{"c0": {...}, "s1": {...}}`。
- 对象子字段使用点号表示法；关系在 schema 中声明（`type: "many" | "one"`）并自动解析 —— **不要手写 `$lookup`**。
- `many` 关系返回列表（为空时返回 `[]`）；`one` 关系合并进父文档（缺失时为 `None`）。
- 关系级 `$sort`/`$skip`/`$limit` 是**按父级取 top-N**（每个父级各有自己的窗口；在 SQL 上翻译为窗口函数）。

> **破坏性变更**：用户 `$pipeline` 透传与 `store.aggregate()` 已被移除（裸聚合逃生通道）。包含 `$pipeline` 的 GQL 现在会显式失败，而不是被静默忽略。

## 聚合

规范化聚合**内置于 GQL** —— 没有独立 API，也没有裸管道。

**根级 `$group` + `$having`**（GROUP BY / HAVING）：

```python
rows = await store.query(
    "Course($condition:@c0,$group:@g0,$having:@h0,$sort:@s0,$limit:@l0){ status, n, total }",
    {
        "c0": {"status": {"$ne": "deleted"}},
        "g0": {"by": ["status"], "agg": {"n": {"$count": "*"}, "total": {"$sum": "price"}}},
        "h0": {"n": {"$gt": 1}},
        "s0": {"total": -1},
        "l0": 20,
    },
)
```

- 白名单操作符：`$count` / `$sum` / `$avg` / `$min` / `$max`。
- 固定执行顺序：`$condition`（WHERE）→ `$group`（GROUP BY）→ `$having`（HAVING）→ `$sort` → `$skip`/`$limit` → projection。
- 省略 `by`（或传 `[]`）表示对全表做单组聚合；空输入情况下仍返回一行（`$count` → `0`，其余 → `None`）。

**关系聚合谓词（semi / anti-join）** —— 用关系上的聚合过滤父级，而不产生扇出：

```python
await store.query("Product($condition:@c0,$sort:@s0){ _id, name }", {
    "c0": {
        "$and": [
            {"status": "onSale"},
            {"orders": {"$count": {"$gt": 3}}},                                   # 订单数 > 3
            {"$not": {"orders": {"$sum": {"$of": "amount", "$gt": 10000}}}},      # 不是大户
        ],
    },
    "s0": {"name": 1},
})
```

在 SQL 上翻译为 `EXISTS` / `NOT EXISTS`，在 MongoDB 上翻译为 `$lookup` + `$match`。

**关系滚动计算列** —— 在 schema 中声明一次，按名称请求：

```python
"computes": {
    "itemCount": {"type": "int", "agg": {"$count": "items"}},       # 为空时 0
    "itemsTotal": {"type": "float", "agg": {"$sum": "items.qty"}},  # 为空时 None
}
```

## 查询与写入 API

```python
items  = await store.query(gql, params)              # list[dict]
one    = await store.query_one(gql, params)          # dict | None
page   = await store.query_with_count(gql, params)   # {'items','total','hasMore','page','pageSize'}（pageSize 上限 5000）
exists = await store.exists("Post", {"_id": pid})
n      = await store.count("Post", {"status": "active"})

doc    = await store.insert("Post", {...})           # 自动 _id / createdAt / updatedAt
docs   = await store.insert_many("Post", [{...}, ...])
await store.update("Post", {"_id": pid}, {"status": "live"})     # 普通字段 → $set
await store.update("Post", {"_id": pid}, {"$inc": {"views": 1}}) # 以 '$' 开头的键作为操作符透传
await store.update_many("Post", {"type": t}, {"status": "live"})
r      = await store.remove("Post", {"_id": pid})    # 先归档到 <collection>_deleted
await store.mutation("Post", {...})                  # 智能 upsert + 递归关系子文档
await store.upsert("Post", {"code": "A1"}, {...})    # 显式条件 upsert（不做关系处理）
```

说明：

- 持久化前会剔除 `None` 值；`_id` 不能通过 `update` 修改。
- `createdAt`/`updatedAt` 由框架维护 —— 不要手动设置。单位取决于 schema 的 `timestamps` 设置：默认毫秒，当 `timestamps: "s"` 时为秒。
- 带**空条件**（`{}`、`None`、`{"$and": []}`）的 `update_many` / `remove` 会被直接拒绝 —— 它绝不会退化为全表写入。
- 提供蛇形别名：`query_one`、`insert_many`、`update_many`、`build_pipeline`、…

## 多数据源连接

每个 schema 通过三元组 `(source, namespace, collection)` 定位 —— 该三元组在注册表中必须全局唯一（重复注册会报错，而不是静默错误路由）。

- `source` —— `init({...})` 中的连接键（默认 `"default"`）。
- `namespace` —— 连接内的数据库/schema：Mongo db 名、PG schema、MySQL database、SQLite attached db。可选；`None` = 连接默认。
- `collection` —— 表/集合名。

```python
# 多个 Mongo 服务端：每个连接一个 source
await init({"mongo_main": db, "pg_a": {"kind": "postgres", "exec": exec}})

# 同一个 MongoClient 服务多个数据库：声明 namespace（db 名）
await init({"cluster": client})
store.register({"name": "User", "collection": "users", "datasource": "cluster",
                "namespace": "tenant_42", ...})

# SQL 跨 namespace 关联会原生下推（"ns_a"."t" JOIN "ns_b"."t"）；
# 只有 Mongo 跨库关系会回退到内存联邦。
```

**多租户路由覆盖** —— 一份 schema 定义，N 个租户。任意查询/写入都接受 `{ "source", "namespace" }` 覆盖，在执行时重新定位命令（权限与计算列仍遵循结构 schema）：

```python
await store.query('User($condition:@c0){...}', params, {"namespace": "tenant_42"})
await store.insert("Order", data, {"source": "pg_cluster", "namespace": "tenant_7"})
```

**`route_override` 是受信的服务端参数** —— 它不带来源校验，因此把用户可控的输入转发给它，会让调用方把 `source`/`namespace` 重新指向另一个租户（CWE-639 授权绕过面）。切勿在此传入原始请求数据。

旧版单库用法（`init(db)` + 不带 `datasource`/`namespace` 的 schema）保持不变：命令携带 `source: "default"`、`namespace: None`。

## 权限上下文

```python
# 每个请求设置一次（在 router/依赖层）
store.set_context({"userId": uid, "roles": ["editor"]})

# 内部/定时任务 —— 绕过权限检查
await store.run_as_internal(lambda: store.remove("Post", {"_id": pid}))
```

- `super_admin`/`admin`/`internal` 角色可通过一切；其他角色按 schema 级与字段级 `read`/`write` 白名单检查；`guest` 永远不能写。
- `creator` 是伪角色，由 `doc.createdBy == ctx.userId` 解析；授予它的 schema 会自动在查询上注入属主条件，并在 update/remove 时做归属校验。
- 未设置上下文 → 权限检查禁用（向后兼容）。
- 拒绝访问会抛出 `store.PermissionError`（带 `status = 403`）。

### 失败安全模式（可选启用）

「无上下文」既可能表示*系统调用*，也可能表示*调用方忘了设置上下文* —— 默认情况下后者会静默通过所有检查（fail-open，为向后兼容保留）。对安全敏感宿主，可在启动时一次性启用上下文强制：

```python
store.set_require_context(True)
# 此后每个无上下文的查询/写入都会抛出 `ERR_NO_CONTEXT:...`
# 内部任务必须显式声明：
await store.run_as_internal(lambda: store.remove("Post", {"_id": pid}))
```

`run_as_internal` 会把调用标记为 `{"internal": True}`，它在语义上区别于上下文缺失，且总能通过。`set_require_context(False)` 恢复默认行为。

## 反馈事件

降级 / 拒绝下推的路径绝不静默失败 —— 它们会发出结构化事件：

```python
{type, code, layer, message, hint, ...}   # federation_degraded / sql_pushdown_unsupported / ...
```

- 默认 sink 打印到 stderr；宿主（例如 AI 数据问答服务）可接管以实现自动反馈闭环：

```python
store.set_feedback_sink(lambda event: log.warning("store feedback: %s", event))
```

- SQL 下推被拒会抛出 `PushdownUnsupportedError`（一个 `RuntimeError`）**并**发出事件；捕获它即可把该段重新放到 Mongo 源上执行。

## Schema 参考

```python
{
    "name": "Order",
    "collection": "orders",
    "idPrefix": "OD",
    "timestamps": True,                # True（毫秒，默认）| False | "ms" | "s"（秒）；自动维护 createdAt/updatedAt
    "fields": {
        "_id": "string",                                        # 简写
        "title": {"type": "string", "default": ""},
        "meta": {"type": "object", "default": {}, "fields": {...}},  # 嵌套对象字段
    },
    "relations": {
        "items": {"model": "OrderItem", "type": "many",
                  "localField": "_id", "foreignField": "orderId"},
    },
    "computes": {
        "total": {"type": "float", "depends": ["amount"], "fn": lambda d: d["amount"] * 1.1},
        "itemCount": {"type": "int", "agg": {"$count": "items"}},
    },
    "indexes": [
        {"keys": {"status": 1}},
        {"keys": {"code": 1}, "options": {"unique": True}},
    ],
    "read": ["editor", "viewer"],      # 可选的 schema 级角色白名单
    "write": ["editor"],
}
```

类型：`string | int | long | float | double | boolean | array | object | date | any`。

值得提前了解的边界规则（全部**显式失败**，绝不静默降级）：

- 直接对数组字段、整个对象字段或对象点路径做过滤，在每个后端都会被拒绝 —— 请把跨实体语义建模为 `relations`。
- 关系谓词只支持**一层**关系；像 `orders.items.price` 这样的路径会被拒绝。
- 无法读取的关系是错误，而不是静默的 `False`。

## 事务边界

- **单一 SQL 源**：`mutation` 的父子步骤序列与 `remove`（归档 + 删除）在同一条检出的连接上的单个驱动事务内运行 —— 任一步骤失败都会回滚整个序列。
- **每条 SQL 写入命令**自身是原子的：多语句计划（例如 MySQL 写入 + 读回）在执行器中被事务包裹。
- **Mongo 源**：单文档写入是原子的；多步骤的 `mutation` 与 `remove` 顺序执行，步骤之间**不**原子（Mongo 事务需要副本集）。如果一致性要求跨越 Mongo 上的多个步骤，要么把这些模型放到 SQL 源上，要么在应用层加入补偿。
- **归档幂等**：`remove` 的归档采用按 `_id` upsert 的语义，因此部分失败后的重试不会再因重复 `_id` 而失败。
- **跨源步骤**（父与子绑定到不同数据源）无法原子化 —— 按设计顺序执行。

## 常见问题

**如何在 Python 中用一份 schema 同时对接 MongoDB 与 PostgreSQL？**
把 schema 定义为一个 dict 一次，用数据源调用 `init()`，然后对任一后端运行同一段 GQL。MongoDB 使用原生聚合；MySQL/PostgreSQL/SQLite 使用参数化 SQL。见[快速开始](#快速开始)。

**如何在不写 `$lookup` 或 JOIN 的情况下查询嵌套 / 关联数据？**
在 `relations` 中声明关系（`{"model", "type": "many" | "one", "localField", "foreignField"}`），并在 GQL 选择集中引用关系名。在 Mongo 上它变成 `$lookup`，在 SQL 上变成 `JOIN`，以嵌套文档形式返回。

**支持 GROUP BY / COUNT / SUM / AVG 吗？**
支持 —— 规范化聚合是 GQL 的一部分：根级 `$group` / `$having` 与关系聚合谓词。见[聚合](#聚合)。

**我能用子级聚合来过滤父级吗（“订单数超过 3 的商品”）？**
可以 —— 关系聚合谓词在不产生扇出的情况下实现 semi/anti-join；SQL 使用 `EXISTS`/`NOT EXISTS`。

**如何实现行级权限？**
使用 `store.set_context({"userId": ..., "roles": [...]})` 加上 schema 级 `read`/`write` 白名单。`creator` 伪角色会带来自动的归属校验与属主条件注入。`guest` 永远不能写。开启 `set_require_context(True)` 可获得失败安全行为。

**如何做软删除？**
每个已注册模型都会自动获得一个 `<Model>Deleted` 归档集合/表。`store.remove()` 先归档文档再删除；重新创建相同的 `_id` 不会冲突，因为归档写入是按 `_id` upsert。

**它能用于多租户应用吗？**
可以。把 schema 绑定到 `(source, namespace, collection)`，并按请求传入 `{"source", "namespace"}` 路由覆盖。只把 `route_override` 当作受信的服务端输入。

**它会执行迁移吗？**
不会。`sync_schema()` 只通过内省*读取*物理结构（introspect → 合并 overlay → 注册）。schema 变更 / DDL 是你迁移工具的职责（如 Alembic）。

**能在不执行的情况下查看生成的查询吗？**
可以 —— `store.build_pipeline(gql, params)` 返回编译后的计划，不执行、也不应用权限/计算列。

**当 SQL 下推无法完成时会怎样？**
命令会抛出 `PushdownUnsupportedError` **并**通过 `set_feedback_sink` 发出结构化反馈事件（`sql_pushdown_unsupported`）。跨源分页/排序降级会发出 `federation_degraded` 事件。没有任何东西会静默失败。

**它与 nodejs-store、rust-store 是什么关系？**
`rust-store` 是共享的 Rust 引擎（GQL 解析、权限、计算列、命令规划、SQL 方言翻译 —— 纯逻辑、无 IO）。`py-store`（pip `storepy`）与 [`nodejs-store`](https://github.com/coenddt/nodejs-store) 是它前面的薄宿主：负责驱动 IO、回调与占位符替换。Python 与 Node 使用相同的 schema、相同的 GQL、相同的语义。

**为什么 pip 包叫 `storepy` 而导入名是 `py_store`？**
在 PyPI 上的发行包名是 `storepy`；可导入包是 `py_store`。用 `pip install storepy` 安装，然后 `from py_store import init, store`。

## 开发

```bash
# 在仓库根目录运行完整测试套件（当 MySQL/PG/Mongo 不可达时 e2e 用例会自动跳过）
$env:PYTHONPATH='py-store/src'; python -m pytest py-store/tests/ -q

# 对接自定义后端
$env:MYSQL_URI='mysql://user:pass@host:3306/db'; $env:PG_URI='postgres://user:pass@host:5432/db'; $env:MONGO_URI='mongodb://host:27017/db'
```

- 单元/契约测试套件（`test_py_store.py`、`test_host_contract.py`、`test_multi_datasource.py`）无需外部服务。
- Rust 核心（`GQL 解析 / 规划 / 方言`）位于 `../rust-store/core`，通过 `rust-store-py` 绑定使用 —— 纯逻辑从不放在本仓库。
- `src/py_store/` 是薄 Host 层：驱动 IO、回调、占位符替换。请保持其如此。

## 相关项目

- [`nodejs-store`](https://github.com/coenddt/nodejs-store) —— Node.js 孪生版（npm `nodejs-store`，camelCase API）。
- [`rust-store`](https://github.com/coenddt/rust-store) —— 共享的 Rust 核心及其 `rust-store-node` / `rust-store-py` 绑定。
- `text-to-query` —— 把自然语言问题转换为该数据层的 GQL + params 的技能。

## 许可证

[MIT](LICENSE)
