# py-store

**面向 Python asyncio 的统一数据层，覆盖 MongoDB、MySQL、SQLite、PostgreSQL 与本地磁盘（local） —— 用纯 JSON 定义模型，用 MongoDB 风格的 GQL 树形语法查询，开箱即得基于角色的访问控制、计算列与软删除。**

![PyPI version](https://img.shields.io/pypi/v/storepy)
![license](https://img.shields.io/pypi/l/storepy)
![python versions](https://img.shields.io/pypi/pyversions/storepy)
![backends](https://img.shields.io/badge/backends-MongoDB%20%7C%20MySQL%20%7C%20SQLite%20%7C%20PostgreSQL%20%7C%20Local-blue)
![query dialect](https://img.shields.io/badge/query%20dialect-GQL%20(MongoDB--flavoured)-green)

> English docs: [README.md](README.md)

`py-store` 让 Python 服务通过**单一 schema 定义与单一查询方言**同时对接 MongoDB（原生聚合）、MySQL、PostgreSQL、SQLite 与本地磁盘（local）。嵌套关系会编译为**每个后端各一条原生查询** —— 你永远不必手写 `$lookup` 或裸 SQL。

> 也在找 Node.js 版本？见 [`nodejs-store`](https://github.com/coenddt/nodejs-store)（npm `nodejs-store`）。两者都是共享 Rust 引擎 [`rust-store`](https://github.com/coenddt/rust-store) 之上的薄宿主。
>
> **Store 家族（Gitee）：** [`rust-store`](https://gitee.com/coenddt/rust-store)（共享 Rust 核心）· [`nodejs-store`](https://gitee.com/coenddt/nodejs-store)（Node.js 宿主，npm `nodejs-store`）· [`py-store`](https://gitee.com/coenddt/py-store)（本仓库 —— Python 宿主，pip `storepy`）

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
- [向量检索](#向量检索)
- [事务边界](#事务边界)
- [触发器](#触发器)
- [事务型能力](#事务型能力)
- [场景事例](#场景事例)
- [性能](#性能)
- [常见问题](#常见问题)
- [相关项目](#相关项目)
- [框架使用契约](#框架使用契约)

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
- **多租户 SaaS。** 一份 schema 定义，N 个租户：通过 `(source, database, schema, collection)` 定位 schema，并在执行时用 `{"source", "database", "schema"}` 覆盖把任意查询或写入重新指向目标。

典型具体场景（完整走查见 [`doc/use-cases/`](doc/use-cases/)）：

| 场景 | 为什么适合 py-store |
| --- | --- |
| 按租户 schema/数据库隔离的多租户 SaaS | 每租户一个 `database` / `schema` + 运行时路由覆盖，仅需一份 schema |
| FastAPI / 管理后台 | Schema 驱动 CRUD、软删除、计算列、RBAC |
| 今天 MongoDB，明天 PostgreSQL | 同一 GQL + 同一 schema，只有数据源改变 |
| AI 数据问答 / text-to-query agent | plan-only 的 `build_pipeline`、确定性的 command JSON、反馈事件 |
| 同一产品中混用 SQL + Mongo | 跨源查询，SQL 原生下推、Mongo 内存联邦 |
| 审计友好的 CRUD | 每个 schema 自动获得 `<Model>Deleted` 归档表/集合 |

## 何时不应使用

把边界说清楚能帮你省时间：

- **你想要带迁移引擎的完整 ORM（Alembic、Django migrations）。** `py-store` 是*数据层*，不是迁移工具。它能**读取** SQL 后端的物理结构（`sync_schema` → 内省），但从不回写 DDL。另有一个可选的 `generate_ddl()`，可从已注册 schema 渲染出 `CREATE TABLE` 文本 —— 纯文本，绝不连接或写入数据库。
- **你想要以 Pydantic 模型为中心的 ORM。** 这里的 schema 是运行时 JSON dict，带来的是跨语言一致性（同一 schema 可在 Python 与 Node.js 中运行），而非 Pydantic 类型校验。
- **你只用一种数据库，且很少做关联。** 直接用驱动（或单库 ODM/ORM）会更简单。
- **你需要裸聚合的逃生通道。** 用户 `$pipeline` 透传已移除 —— 含 `$pipeline` 的 GQL 一律在解析期显式失败。原生逃生口只保留显式入口：`store.aggregate()`（严格 **opt-in**，默认关闭，需 `init(connections, {"allowNativeAggregate": True})` 显式开启）、`execute_native()`（Mongo 原生聚合）与 `execute_raw()`（原生 SQL）。日常查询请使用 `$condition` / `$group` / `$having` / relations；任何无法被安全翻译的东西都会**显式失败**，而不是静默处理。

## 横向对比

以下仅为整体定位，不是基准测试 —— 请始终对照各工具的最新文档核实。

| | py-store | SQLAlchemy | Beanie / Motor | Tortoise ORM | SQLModel | Django ORM |
| --- | --- | --- | --- | --- | --- | --- |
| 主要形态 | JSON schema + GQL 数据层 | SQL 工具集 + ORM | 异步 MongoDB ODM / 驱动 | 异步 ORM | Pydantic + SQLAlchemy | Django 内置 ORM |
| 后端 | MongoDB、MySQL、SQLite、PostgreSQL、local | PostgreSQL、MySQL、SQLite、Oracle、MSSQL | MongoDB | PostgreSQL、MySQL、SQLite、Oracle、MSSQL | PostgreSQL、MySQL、SQLite、… | PostgreSQL、MySQL、SQLite、Oracle |
| Mongo **与** SQL 共用一套查询方言 | ✅（MongoDB 风格 GQL） | ➖（仅 SQL） | ➖（仅 Mongo） | ➖（仅 SQL） | ➖（仅 SQL） | ➖（仅 SQL） |
| 单条查询读出嵌套关系 | ✅ 声明式 relations → `$lookup` / `JOIN` | ⚠️ 手动 `selectinload`/join | ✅ `Link`/`fetch_links` | ✅ `prefetch_related` | ⚠️ 经 SQLAlchemy | ✅ `prefetch_related` |
| 内置角色 / 字段级 RBAC + 属主注入 | ✅ | ➖ | ➖ | ➖ | ➖ | ➖（权限在应用层） |
| 读取时计算列（同步 / 异步 / 关系聚合） | ✅ | ➖（hybrid property） | ➖ | ➖ | ➖ | ➖ |
| 自动开通软删除归档表 | ✅ | ➖ | ➖ | ➖ | ➖ | ➖ |
| 迁移 / DDL 引擎 | ➖（内省只读；可选 `generate_ddl` 文本） | ✅（Alembic） | ➖ | ✅（Aerich） | ✅（Alembic） | ✅ |
| 框架耦合 | 无（asyncio） | 无 | 无 | 无 | 无 | Django |
| Python 与 Node 共享原生核心 | ✅（Rust `rust-store`） | ➖ | ➖ | ➖ | ➖ | ➖ |

### 它与具体库的差异

以下仅为定位说明，基于这些项目在撰写时公开的文档 —— 请对照你自己的需求核实。

- **对比 SQLAlchemy / SQLModel / Django ORM** —— 它们都只支持 SQL 并以模型类为中心：不面向 MongoDB，也都不提供 schema 声明的角色/字段级访问控制或读取时计算列。`py-store` 把同一段 GQL 编译为原生 MongoDB 聚合或参数化 SQL。
- **对比 Beanie / Motor** —— 仅支持 MongoDB。`py-store` 采用相同的 MongoDB 风格查询写法，但同一条查询也能在 MySQL、SQLite 与 PostgreSQL 上运行。
- **对比 Tortoise ORM / pyloquent** —— 基于 SQL 后端的异步 Python ORM，使用模型类，并且（就 Tortoise 而言）带有迁移工具。`py-store` 没有迁移引擎 —— 内省只读取物理结构，`generate_ddl()` 也只*渲染* `CREATE TABLE` 文本而不触碰数据库 —— 且把模型描述为普通 dict，这恰恰是 schema 能移植到 Node.js 宿主的原因。
- **对比 `nodejs-store`** —— 同一套引擎、同一套 GQL，只不过用 JavaScript。选择与你服务相匹配的宿主即可；schema 与查询语义可互换。

一句话：想要**模型类、Pydantic 校验和迁移**就用 ORM；想要**一份运行时 schema + 一套横跨 MongoDB 与 SQL 的查询方言**，并且内置 RBAC 与计算列，就用 `py-store`。

## 安装

```bash
pip install storepy
```

> 发行包名是 `storepy`；导入包名是 `py_store`：
> `from py_store import init, store`。

需要 Python 3.10+ 以及一个受支持的后端（MongoDB / MySQL / SQLite / PostgreSQL / 本地磁盘）。

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
| local（本地磁盘） | 集合以 JSON 文件落盘，core 本地求值器执行（无 SQL、无驱动依赖）；见[本地磁盘数据源（local）](#本地磁盘数据源local) |

GQL 树形查询在每个后端各编译为一条原生查询（local 源除外 —— 命令在 core 本地求值器内直接求值）—— 再也不用手写 `$lookup` 或裸 SQL。

### 本地磁盘数据源（local）

零外部服务、零原生 DB 引擎：集合以 JSON 文件落盘，命令在 Rust core 的本地求值器内直接执行（语义与 MongoDB 驱动一致）。

```python
from py_store import init, local

await init({'default': local.connect({'dir': './data/store'})})
```

- **用法**：`local.connect({'dir': …})` 返回连接描述符（`kind: 'local'`），与 Mongo/SQL 连接一样传给 `await init()`。
- **落盘格式**：`<dir>/<物理集合名>.json`（文档数组）；写入先写 `.tmp` 中间文件再原子 `rename`。
- **事务语义**：快照隔离 —— 事务期读写内存快照，`commit()` 整目录落盘、`rollback()` 丢弃，天然原子。
- **护栏**：单集合上限 100,000 文档（超限显式报错，不静默截断）；不建索引（仅顺序扫描），声明 `schema.indexes` 即发 `local_indexes_ignored` 反馈事件。
- **并发限制**：仅单进程 —— 进程内写经目录锁串行化；跨进程并发不在 v1 保证范围。

## 特性

- **纯 JSON schema，零代码** —— 一个模型就是一个 dict：fields、relations、computes、indexes。
- **GQL 树形查询 → 单条原生查询** —— 嵌套关系在一条查询内解析；再也不用手写 `$lookup`。
- **规范化聚合** —— 在同一段 GQL 中支持根级 `$group` / `$having` 与关系聚合谓词（semi/anti-join），并下推到全部五个后端。
- **读取时默认值与计算列** —— 写入只存用户数据；读取时补齐默认值并运行 `fn` / `asyncFn` / 关系 `agg` 计算列。
- **智能 mutation** —— `mutation()` 依据 `_id` + 唯一索引自动识别 upsert，并递归填充关系子文档。
- **内置软删除** —— 每个 schema 自动注册一个 `<Model>Deleted` 归档集合/表；`remove()` 先归档再删除。
- **权限上下文** —— 基于 `ContextVar` 的角色（`super_admin`/`admin`/`guest`/`creator`…）、schema/字段级读写白名单、自动属主条件注入。
- **多数据源 & 多租户** —— 通过 `(source, database, schema, collection)` 定位 schema；按请求用路由覆盖重新指向目标。
- **异步优先，Rust 核心** —— 构建在 PyMongo `AsyncMongoClient` 与共享的 Rust 核心（含 SQL 方言）之上。
- **mutation 关系谓词** —— `update` / `remove` 按关联表字段过滤，下推到全部五个后端（此前 MongoDB 侧是静默 no-op）。
- **自增主键** —— `_id` 声明 `{"type": "int", "strategy": "autoincrement"}` 即用数据库自增整数 ID；做不到自增的场景显式报错。
- **索引 DDL** —— `schema.indexes` 编译为真实 `CREATE [UNIQUE] INDEX` 语句（按后端、逐字节一致）；生成器仍只产文本。

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

> **破坏性变更**：用户 `$pipeline` 透传已移除 —— 包含 `$pipeline` 的 GQL 现在在解析期显式失败（`ERR_GQL_PARSE:`），而不是被静默忽略，也不再按档位放行。原生聚合逃生通道只保留显式入口：`store.aggregate()`（严格 opt-in，默认关闭，需 `init(connections, {"allowNativeAggregate": True})`）、`execute_native()` 与 `execute_raw()`。

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
- 蛇形命名 API（唯一定名，无驼峰别名）：`query_one`、`insert_many`、`update_many`、`build_pipeline`、…

### 事务与原生 SQL

```python
async def transfer():
    rows = await store.execute_raw(
        "default", "SELECT * FROM accounts WHERE _id = ? FOR UPDATE", [acc_id])
    await store.execute_raw(
        "default", "UPDATE accounts SET balance = ? WHERE _id = ?", [new_balance, acc_id],
        is_write=True)

await store.transaction("default", transfer)
```

- `store.transaction(source, fn)` 在单个 SQL 源上开启事务作用域：`fn` 内的每个 `execute_raw` / CRUD 调用都落到该源的事务连接，`commit` / `rollback` 作为一个整体（复用内部的 `run_in_transaction`）。Mongo 源按**运行时能力探测**（replica set / sharded）以 session 事务执行；standalone 或探测失败则按原样执行 `fn` 并发 `mongo_transaction_unsupported`（`deployment: standalone|unknown`）—— 绝不假装已原子。不支持事务（无 `with_transaction`）的执行器亦按原样执行，并发出一条 `transaction_not_atomic` 反馈（允许降级，绝不静默假装已事务化）。同源嵌套 transaction 会开保存点（内层失败只回滚本层）；句柄无保存点原语时降级并入外层并发 `nested_savepoint_unsupported`。
- `store.execute_raw(source, sql, params=None, is_write=None)` 执行原生 SQL，编译由 core 的 `raw_stmt_compile` 完成，按 `params` 类型自动分两档：**位置档**（list/tuple/None）SQL 原样透传，占位符沿用各后端原生风格（MySQL / SQLite 用 `?`，PostgreSQL 用 `$1..$n`）；**命名档**（dict）SQL 文本中的 `:name` 编译为方言占位符（同名复用、跳过 `::` cast / 引号 / 注释边界；缺名 / 多余名显式抛 `RawSqlError`）。仅限 SQL 源 —— Mongo 源会抛出 `RawSqlError`（`py_store.RawSqlError` / `store.RawSqlError`）。
- `is_write` 缺省时按 SQL 首词推断（SELECT / WITH / EXPLAIN / SHOW / PRAGMA / TABLE 视为读，其余按写 —— 默认写是安全方向）；显式传入则覆盖推断。返回 `{"rows", "affectedRows"}`：读取行，写取影响行数。
- `store.execute_native(source, collection, pipeline=None, options=None)` 在 Mongo 源上执行原生聚合管道（对标 SQL 侧 `execute_raw` 的 Mongo 对位逃生口）：`pipeline` 为原生聚合管道，`options` 沿用驱动原生键名（`allowDiskUse` / `batchSize` / `hint` / `maxTimeMS`…，宿主不做白名单）。事务 / 会话作用域内自动透传 session（由事务强制接管，`options.session` 不可覆盖）；统一按读路径解析，`$merge` / `$out` 写管道请自行开事务。仅限 Mongo 源 —— SQL 源抛 `NativeCommandError` 并指引 `execute_raw`；MongoClient 形态须经 schema 声明 `database`。返回 `{"rows"}`。
- **非事务路径显式提交**：在 SQL 源上，`store.transaction` 之外的写命令由执行器显式提交（成功 `commit`；失败先 `rollback` 再上抛）。`aiosqlite` 默认非 autocommit，若缺少这次提交，写入只在当前连接可见、而 `execute_raw` 仍报成功 —— 是静默丢数据的隐患。需要整组原子的多语句写入，请放进 `store.transaction`。

### 会话（Session / 工作单元）

```python
async with store.session() as s:
    await s.insert("Order", {...})
    await s.update("Account", cond, {...})
    await s.execute_raw("pg_main", "SELECT ... FOR UPDATE", [1])
```

- 会话内同一 SQL 源的**全部命令落到同一事务连接**：退出统一提交，异常统一回滚；
- **惰性开事务**：会话内没有任何命令时不占用连接；
- **跨源写 fail-closed**：同一会话内写入了 ≥2 个数据源时，退出先全部回滚再抛
  `NonAtomicWriteError`（跨源无分布式事务，绝不提交半截）；
- Mongo 源按**运行时能力探测**（replica set / sharded）事务化；standalone 或探测失败则按原样执行（非原子），并发出一条 `mongo_transaction_unsupported`（`deployment: standalone|unknown`）反馈；
- 会话可嵌套：内层作用域在已有事务上开保存点（`SAVEPOINT sp_<n>`），退出按成败
  `RELEASE`（成功）/ `ROLLBACK TO` + `RELEASE`（失败）——**内层失败只回滚内层，外层可继续**；
  事务句柄未提供保存点原语时降级并入外层，并发出一条 `nested_savepoint_unsupported` 反馈。

### DDL 生成

```python
sql = store.generate_ddl("mysql")                       # 所有已注册模型
sql = store.generate_ddl("postgres", ["Course", "CourseDeleted"])
```

`store.generate_ddl(backend, names=None)` 把一个已注册 schema def 映射为一条 `CREATE TABLE` —— 是 `sync_schema()`（只*读取*）的逆操作。生成器是**纯文本**：它绝不连接、也绝不写入数据库（铁律 6 依然成立）。

- 只有标量字段成为列；`object` / `array` 字段不建列。
- 每张表都会获得 `__present` 哨兵列；`timestamps` 模型还会获得 `createdAt` / `updatedAt`；`<collection>_deleted` 归档表与其它已注册 def 一样生成。
- `<Name>Deleted` 归档 def 由 **Rust core 在注册模型时自动派生**；Python Host 只补镜像（`collection` 为 `<c>_deleted`、含 `deletedAt` 字段、`idPrefix` 为空），**绝不把它再注册进 core**。因此 `schema.list()` 与 `generate_ddl()` 中每张归档表只出现一次；一旦真的出现重复（上游回归），它们会去重并发出 `schemaDuplicateName` / `ddlDuplicateTable` 反馈事件，而不是静默产出重复的 `CREATE TABLE`。
- 不生成 `CREATE INDEX` —— SQL 后端仅把索引保留为元数据。
- MySQL 的 `__present` 为 `VARCHAR(255)`；若某 schema 的 present 令牌串会超限，则发出 `ddlPresentOverflow` 反馈事件，而非静默失败。

## 多数据源连接

每个 schema 通过 `(source, database, schema（仅 PostgreSQL）, collection)` 定位 —— 该元组在注册表中必须全局唯一（重复注册会报错，而不是静默错误路由）。定义文件本身不携带落点；`source` / `database` / `schema` 由定义目录结构 + 连接配置解析。

<!-- SPEC:LOCATION:BEGIN -->
### Location: directory semantics + connection config (definitions carry no location)

A schema definition file contains no location fields (no `source` / `database` / `schema`; `namespace` is removed). Location is resolved from the definition directory layout plus the connection config:

- Under the definitions root `<defs-root>/`: the first directory level is the `database`; PostgreSQL adds a second level for `schema` (Mongo / MySQL / SQLite have no such level); deeper levels are free-form and flattened at load time (no hierarchy semantics).
- The connection config (`store.config.json`) declares `sources` (`kind` + `databases`) and `defs`; `kind` decides whether that database directory is read one level deeper for `schema`.
- Location fields are `source` / `database` / `schema` (PG only) / `collection`; the word `namespace` is removed.
- Same-named schemas: exactly one primary (no `replica`); the rest declare `{ "name": "...", "replica": true }`, add only a link, and must not repeat the structure. Zero or two-or-more primaries is an error.
- A duplicated `name` within one load batch is an error and the service does not start; re-loading the same `name` across versions bumps its version by 1.
- Writes are synchronized within a single connection, across the primary plus all links, in one transaction; a write spanning a cross-connection link is explicitly rejected or degraded with a feedback event (never silent).
<!-- SPEC:LOCATION:END -->

```python
# 多个 Mongo 服务端：每个连接一个 source
await init({"mongo_main": db, "pg_a": {"kind": "postgres", "exec": exec}})

# SQL 跨 database / PG-schema 关联会原生下推（"db_a"."t" JOIN "db_b"."t"）；
# 只有 Mongo 跨库关系会回退到内存联邦。
```

**多租户路由覆盖** —— 一份 schema 定义，N 个租户。任意查询/写入都接受 `{ "source", "database", "schema" }` 覆盖，在执行时重新定位命令（权限与计算列仍遵循结构 schema）：

```python
await store.query('User($condition:@c0){...}', params, {"database": "tenant_42"})
await store.insert("Order", data, {"source": "pg_cluster", "schema": "tenant_7"})
```

**`route_override` 是受信的服务端参数** —— 它不带来源校验，因此把用户可控的输入转发给它，会让调用方把 `source`/`database`/`schema` 重新指向另一个租户（CWE-639 授权绕过面）。切勿在此传入原始请求数据。

旧版单库用法（`init(db)` + 不带落点的 schema）保持不变：命令携带 `source: "default"`，`database` / `schema` 取连接默认。

## 权限上下文

```python
# 每个请求设置一次（在 router/依赖层）
store.set_context({"userId": uid, "roles": ["editor"]})

# 内部/定时任务 —— 绕过权限检查
await store.run_as_internal(lambda: store.remove("Post", {"_id": pid}))
```

- `super_admin`/`admin`/`internal` 角色可通过一切；其他角色按 schema 级与字段级 `read`/`write` 白名单检查；`guest` 永远不能写。
- `creator` 是伪角色，由 `doc.<归属键> == ctx.userId` 解析（归属键由 schema `ownerKey` 声明，缺省 `createdBy`，可自定义如 `"_id"`）；授予它的 schema 会自动在查询上注入属主条件，并在 update/remove 时做归属校验。
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
        "bidders": {"type": "array", "default": []},            # 数组字段 —— 内联 agg 目标
    },
    "relations": {
        "items": {"model": "OrderItem", "type": "many",
                  "localField": "_id", "foreignField": "orderId"},
    },
    "computes": {
        "total": {"type": "float", "depends": ["amount"], "fn": lambda d: d["amount"] * 1.1},
        "itemCount": {"type": "int", "agg": {"$count": "items"}},                 # 关系聚合
        "biddersCount": {"type": "int", "depends": ["bidders"], "agg": {"$size": "bidders"}},
        "maxBid": {"type": "float", "depends": ["bidders"], "agg": {"$max": "bidders.amount"}},
        "bidderCount": {"type": "int", "depends": ["bidders"], "agg": {"$countDistinct": "bidders.bidderId"}},
    },
    "indexes": [
        {"keys": {"status": 1}},
        {"keys": {"code": 1}, "options": {"unique": True}},
    ],
    "read": ["editor", "viewer"],      # 可选的 schema 级角色白名单
    "write": ["editor"],
    "ownerKey": "_id",                 # 可选：`creator` 伪角色的归属键；缺省 "createdBy"
}
```

类型：`string | int | long | float | double | boolean | array | object | date | any`。

值得提前了解的边界规则（全部**显式失败**，绝不静默降级）：

- 直接对数组字段、整个对象字段或对象点路径做过滤，在每个后端都会被拒绝 —— 请把跨实体语义建模为 `relations`。
- 关系谓词只支持**一层**关系；像 `orders.items.price` 这样的路径会被拒绝。
- 无法读取的关系是错误，而不是静默的 `False`。

### 内联数组聚合

`agg` 路径首段命中 fields 声明的**数组字段**（而非关系）时，对文档自身的数组做聚合 —— 编译进同一条管道，不新增查询阶段：

| 算子 | 路径 | 空数组 / 缺字段 |
|---|---|---|
| `$size` | 数组字段本身 | `0` |
| `$sum` | `数组.元素字段` | `0` |
| `$avg` / `$max` / `$min` | `数组.元素字段` | `null` |
| `$countDistinct` | `数组.元素字段` | `0` —— 去重并剔除 null 后计数 |

规则（违规在注册期显式失败，绝不静默降级）：

- `depends` 必须包含路径首段字段。
- 数组元素仅一层：`bidders.amount` 合法；再往数组深层嵌套会被拒绝。
- `$count` 仅限关系 —— 数组请用 `$size`。
- 仅 MongoDB 与 local（进程内）求值可用；SQL 后端显式报错。

<!-- SPEC:NAMING-STYLE:BEGIN -->
### Naming: freeform definitions, system-directed translation

Definitions (`collection`, fields, referenced relation fields, computed-column keys, `fnRef` values, index names) may use any style; the engine translates them to the target style. Contract keys (`fnRef`, `localField`, `foreignField`, `asyncFn`, `type`, ...) and the schema `name` are never translated.

| Target | Style | Example (`orderTotal`) |
|---|---|---|
| MySQL / PostgreSQL / SQLite (physical) | snake_case | `order_total` |
| MongoDB (physical) | camelCase | `orderTotal` |
| Node.js / Java / C# / Rust (code; computed columns follow) | camelCase | `orderTotal` |
| Go (code; computed columns follow) | PascalCase (must be exported) | `OrderTotal` |
| Python (code; computed columns follow) | snake_case | `order_total` |

Canonicalization (single implementation `core::naming`, re-exported by the bindings; hosts must not re-implement it): split on `_`, `-`, `.`, space and at lower/digit-to-upper boundaries; a trailing uppercase in a run followed by a lowercase starts the next token (`HTTPServer` -> `[http, server]`, `userID` -> `[user, id]`); digits stay inside a token (`order2Items` -> `[order2, items]`). Reassembly: snake = `t1_t2`, camel = `t1T2`, pascal = `T1T2`.

Two logical names in one schema that canonicalize equal (`orderTotal` vs `order_total`) is an error `ERR_NAME_CONFLICT:` and the service does not start (never silently overwritten). The `fields` key space is fully open — no field name is reserved: contract keys (`type`, `default`, ...) live inside a field's definition object at a different level, so `fields: { type: { type: "string" } }` is valid. Literal reserved names are only `_id` (framework primary key) and `_score` (vector score column).
<!-- SPEC:NAMING-STYLE:END -->

<!-- SPEC:FNREF:BEGIN -->
### Computed columns: `fnRef` binding by composite name + canonical match

Computed columns live at the schema top level, `computes: { <key>: { type, fn | asyncFn | agg, fnRef?, depends?, read? } }` (`fn` / `asyncFn` / `agg` are mutually exclusive).

- The logical `fnRef` defaults to `<schema.name>.<computed-column key>` (generated, never hand-written); since `name` is globally unique, the `fnRef` is globally unique too.
- Host implementations bind by canonicalization: both the implementation's name in the host language style and the schema's logical `fnRef` are canonicalized to token sequences and compared. So Node's `orderAmountLabel` and Python's `order_amount_label` bind to the same logical computed column.
- Reusing one implementation across schemas: write an explicit shared name (e.g. `"fnRef": "common.moneyLabel"`); naming goes from required to optional.
- Every declared `fnRef` must have an implementation, otherwise the service fails to start with `ERR_FN_MISSING`.
<!-- SPEC:FNREF:END -->

## 向量检索

向量就是带维度与度量声明的普通字段值：你产出 embedding，存储负责持久化、排序并回传评分。
库内不内置任何 embedding 模型。

### schema 声明

```python
{
  'name': 'Artwork',
  'collection': 'artworks',
  'fields': {
    'title': {'type': 'string', 'default': ''},
    'caption': {'type': 'string', 'default': ''},
    'embedding': {'type': 'vector', 'dim': 1024, 'metric': 'cosine'},  # 'cosine' | 'l2' | 'ip'
    'embeddingHash': {'type': 'string', 'default': ''},                # 可选：回填摘要
  },
  'indexes': [
    # 声明原生近似索引意图（pgvector / Atlas Search）
    {'keys': {'embedding': 'vector'}, 'options': {'metric': 'cosine', 'method': 'hnsw'}},
  ],
}
```

- `type: 'vector'` 必须声明整数 `dim` 与 `metric: 'cosine' | 'l2' | 'ip'` 之一；`dim` / `metric`
  非法在**注册期**即报错，不拖到查询期。
- 两个上限不可混用：**存储**上限 16000 维；**原生索引**上限更低且分后端——pgvector HNSW /
  IVFFlat 为 2000 维，MongoDB Atlas `vectorSearch` 为 4096 维。DDL 生成对超限索引显式报错，
  不静默跳过。
- 向量索引项只接受单字段：`{'keys': {<字段>: 'vector'}, 'options': {...}}`；选项为
  `metric`、`method`（`hnsw` | `ivfflat` | `flat`）与 Atlas 的 `indexName`。
- 向量为 `None` 的行（含迁移前存量行）不参与评分。

### 查询算子

```python
rows = await store.query(
    'Artwork($condition:@c){ _id, title, _score }',
    {'c': {'embedding': {'$near': {'$vector': [0.12, -0.03], '$limit': 10}}}},
)
```

- `$near` 只接受三个键：`$vector`（必填查询向量）、`$limit`（必填 TopK，正整数）、
  `$minScore`（可选，`_score` 下限）。其他键一律拒绝。
- `metric` 由字段声明决定，**不是** `$near` 的参数。三种度量统一为「越大越相似」：
  `cosine` 为余弦相似度，`ip` 为内积，`l2` 为 `1 / (1 + 欧氏距离)`。
- 过滤条件写在同一个 `$condition` 内、与 `$near` 同级并列，例如
  `{'status': 'public', 'embedding': {'$near': {...}}}`。
- `_score` 按需投影：必须在投影里显式写出；不含 `$near` 的查询永不返回 `_score` 键。
- 向量长度与 `dim` 不符、零向量、缺 `$limit` 在**所有后端**都报错。

### 降级语义

`(await store.capabilities())['vector']` 为静态能力报告：
`{supported, native, backend, metrics, maxDim}`。

- `native: True` 表示该目标能跑近似索引（pgvector 已安装，或声明了 Atlas Search 向量索引）。
  `native` 是目标的**静态**属性，不会因为某次查询回退而改变。
- 没有原生索引时——MySQL、未启用 `vec0` 的 SQLite、未声明 Atlas Search 索引的自建 MongoDB，
  以及嵌入式 `local` / `memory` 载体——查询仍按**应用层精确检索**执行，同时宿主记录
  `vector_degraded` 降级事件。降级必须可观测，禁止静默。
- PostgreSQL 带过滤（`$condition` 内除 `$near` 还有条件）回退为精确扫描：pgvector 近似索引
  属后过滤，会破坏 ACL 与 TopK 正确性。
- 三种观测方式：请求作用域内 `py_store.degraded.begin()` … `store.degraded_status()`；反馈
  通道的 `vector_degraded` 事件；`store-api` 的 `x-store-degraded` 响应头。事件字段为
  `type / code / layer / backend / field / metric / message / hint`。

### 迁移步骤

- 存量表不会被隐式改列。须显式加列并重新注册字段：`ddl.generate_migration(...)` 产出
  `ALTER TABLE ... ADD COLUMN embedding vector(1024)`（vector 列不接受 `DEFAULT`；存量行在回填前
  保持 `NULL`）。
- 已存在字段的 `dim` / `metric` 变更不作为迁移支持——须显式重建列与其索引。
- 回填用
  `await store.reindex_vectors(schema, field=..., hashField=..., sourceFields=..., embed=...)`：
  按 `_id` 锚点分页（不用 `skip`），对 `sourceFields` 做 `sha256` 摘要，只对摘要变化的行重新
  embed。`force=True` 全量重算；`dryRun=True` 只报告不调 `embed`。向量与摘要同一次 update
  写入。
- 增量场景用 `store.register_vector_reindex(cfg)` 注入 `<Schema>.vectorReindex` 触发器，按需
  刷新单行。
- DDL 生成只打印索引语句，存储从不执行它。大表须在业务外执行（PostgreSQL 用
  `CREATE INDEX CONCURRENTLY`）。

## 事务边界

| 场景 | 原子性 |
|---|---|
| 单命令 API（`insert` / `insert_many` / `update_many` / `upsert` / `remove` / `count` / `exists`） | 单 SQL 源内天然原子（单条 SQL）；Mongo 单文档原子 |
| `store.transaction(source, fn)` | 单 SQL 源内原子：作用域内所有命令同连接、同事务；同源嵌套开保存点（内层失败只回滚本层） |
| `store.session(...)` | 会话内单 SQL 源**跨多次调用**原子；跨源写被显式拦截（`NonAtomicWriteError`） |
| 无会话的跨源多写 | 非原子（无 2PC / Saga 支持），按数据源顺序执行，并经反馈通道声明 `nonAtomic`（事件 `non_atomic_write`，含涉及源） |
| Mongo 多步写 | replica set / sharded：单 Mongo 源原子（session 事务）；standalone：非原子并显式声明 `mongo_transaction_unsupported` |
| 工作流 run（`workflow.run`） | 单源 run **跨步骤**整体原子（外层 `run_atomic` 包住步骤循环、内层 mutation 嵌套并入）；多源 / 源预扫失败按顺序执行并经反馈通道声明非原子 |
| 触发链（schema `triggers`，insert / update 事件） | 与源写同一 `run_atomic` 包络：单源**真事务**（同事务回滚）；触及第二数据源按顺序执行并声明 `non_atomic_write`；`store.session()` 内对跨源写 fail-closed |

- **Mongo 源**：会话内按运行时能力探测结果事务化；不可事务（standalone / 探测失败）按原样执行，
  并发出 `mongo_transaction_unsupported` 反馈（`deployment: standalone|unknown`）（允许降级，绝不静默假装已事务化）；
- **未实现 `open_transaction` 的 SQL 执行器**：会话内按原样执行，并发出
  `session_not_atomic` 反馈（允许降级，绝不静默假装已事务化）；
- **未实现 `with_transaction` 的 SQL 执行器**：`store.transaction` / 顶层原子包络内按原样执行，
  并发出 `transaction_not_atomic` 反馈（与 `session_not_atomic` 对称，允许降级，绝不静默假装已事务化）；
- **归档幂等**：`remove` 的归档采用按 `_id` upsert 的语义，因此部分失败后的重试不会再因重复 `_id` 而失败。
- 读一致性：只有在显式会话内的多条读才共享同一事务连接；会话外读不额外开启事务。
- **跨源写（非会话）**：一次写调用涉及 ≥2 个数据源时**无法原子**，按顺序执行，并发出一条
  `non_atomic_write` 反馈（`code: nonAtomic`，含涉及源列表）——允许降级、禁止静默。
  把写收敛到单源，或放入 `store.session()` 内（后者对跨源写直接 fail-closed）。

## 触发器

schema 声明式触发链：写事件（`insert` / `update` / `remove`）由 Rust 核心在规划期展开为有序副作用步骤（`plan.triggers`），宿主在源写同一原子包络内依次执行。声明形态（`triggers` 三类键 + T 字段表）与占位符语法以 rust-store README「触发器」章节为准；core 侧展开输出跨端逐字节一致（`rust-store/fixtures/triggers/cases.json` golden 守护）。

**回调装配**（`py_store/crud/triggers.py`）：

```python
async def _grant_points(args, ctx, host):
    await host['store'].update('User', {'_id': args['userId']}, {'$inc': {'points': args['amount']}})

store.set_trigger_fn('grantPoints', _grant_points)
store.assert_trigger_fns_covered(defns)  # 启动期：声明的 fnRef 缺实现 ⇒ ERR_TRIGGER_FN_MISSING
```

**执行语义**：

- **命中判定** —— update 事件先判 `onFields 值真的变化`（结构深比较，no-op 抑制——值未变不触发）→ 再判 `when`（`eq/ne/gt/gte/lt/lte/in/and/or/not`）；insert 事件无 before，跳过字段级检查；`remove` 以归档（删除前）文档为根。系统字段（`createdAt` / `updatedAt` / `deletedAt`）不进触发器探针投影，其变化天然不触发。
- **占位符** —— `{{root.*}}`（变化后值）/ `{{before.*}}`（变化前值）/ `{{now}}` 只做整值替换；内嵌拼接显式报 `ERR_TRIGGER_PLACEHOLDER`，禁静默漂移。
- **去重** —— 同一次顶层调用内 `(step.name, _id)` 只执行一次。
- **回调式** —— `impl(args, ctx, {'store': store})`；回调内 `store.*` 落到当前事务连接 → 与主写同事务。
- **定时（cron）** —— `schedule` 事件由宿主定时任务插件（`py_store/scheduler/__init__.py`）枚举并复用同一触发链；占位符仅允许 `{{now}}`。

**边界**：单源真事务 / 跨源 `nonAtomic` 声明 / `store.session()` 内跨源 fail-closed（见[事务边界](#事务边界)）；**不支持级联**（触发写不再触发任何触发器）；`update_many` 不支持字段级触发（显式拒绝，禁静默降级）；命令式步骤 `op: "upsert"` 注册期 `Err`。

## 事务型能力

面向事务型业务场景（订单、库存——写竞争 + 复杂读）的能力增补。完整语义、用法与显式报错清单：
**[doc/transaction-capabilities.zh-CN.md](doc/transaction-capabilities.zh-CN.md)** ·
[English](doc/transaction-capabilities.md).

- **mutation 关系谓词** —— `update_many('Inventory', {'product': {'category': 'meat'}}, {'$inc': {'stock': 10}})`：条件键命中已声明关系即 semi/anti-join，归一为 preCommand（aggregate 取 `_id`）+ `_id $in`。
- **`$group by` one 关系路径** —— `by: ['product.category']` 编译为 `$lookup`+`$unwind`（Mongo）/ `LEFT JOIN`（SQL）；many 路径显式报错（扇出破坏计数语义）。
- **自增主键** —— `_id: {'type': 'int', 'strategy': 'autoincrement'}`；PG/SQLite 经 `INSERT…RETURNING` 回读、MySQL 经 lastrowid；MongoDB 与 `insert_many` 显式报 `AUTOINCREMENT_NOT_SUPPORTED`（禁 ObjectId 静默顶替）。
- **索引 DDL** —— `schema.indexes`（Mongo 形态）→ `ddl.generate` 产出 `CREATE [UNIQUE] INDEX idx_<表>_<字段>`，MySQL/PostgreSQL/SQLite 三方言逐字节一致。
- **声明式迁移** —— `ddl.diff_defs(old, new)` + `ddl.generate_migration(backend, old, new)`：白名单制（加表/加列/加索引/类型放宽），纯函数按方言产 SQL；白名单外显式报 `MIGRATION_UNSUPPORTED`。

## 工作流编排（首批）

把「多步数据操作的编排」用数据表达：工作流定义（Workflow defn）是与 schema defn 同构的纯 JSON、
运行记录（run）落库为内建 schema `__workflowRun`（普通 GQL 即查，可观测性零新接口）。执行是既有
mutation 步骤序列机制的推广——线性步骤 + 步骤级 `when` 守卫 + fail-fast。引擎落在宿主层
（`py_store/workflow.py`），core 零改动；对齐 `nodejs-store/src/workflow.js`（双宿主输出逐字节
一致由 parity 锚单测守护）。

```python
from py_store import workflow

workflow.register({                       # 注册即静态校验；白名单外显式 Err（WORKFLOW_UNSUPPORTED）
    'name': 'placeOrder',
    'run': ['admin', 'ops'],              # 三级白名单 read/write/run（run 缺省回退 write）
    'steps': [
        {'op': 'query', 'as': 'inv',
         'gql': 'Inventory($condition:@c0){_id, stock}',
         'params': {'c0': {'productId': '{{input.productId}}', 'warehouse': '{{input.warehouse}}'}}},
        {'op': 'fail', 'when': {'exists': '{{inv._id}}', 'is': None}, 'message': '库存记录不存在'},
        {'op': 'fail', 'when': {'lt': '{{inv.stock}}', 'than': '{{input.qty}}'}, 'message': '库存不足'},
        {'op': 'mutation', 'model': 'Inventory',
         'data': {'_id': '{{inv._id}}', 'stock': '{{dec:{{inv.stock}},{{input.qty}}}}'}},
    ],
})

run = await workflow.run('placeOrder', {'productId': 'p1', 'warehouse': 'w1', 'qty': 30})
# run['status'] ∈ succeeded | failed | rejected | drySucceeded | dryFailed
# 统一契约：业务失败不抛错，错误在 run['error']（失败才有值；succeeded 态恒为 null）
```

- **步骤白名单**（首批仅三种，白名单外注册即 `WORKFLOW_UNSUPPORTED`）：`query`（store.query；
  结果单条化，>1 行显式 Err）、`mutation`（store.mutation / upsert，继承其步骤序列与占位符机制）、
  `fail`（显式业务断言失败：run 记 failed + stepIndex + message）。步骤可选 `when` 守卫
  （exists / is / eq / ne / lt / lte / gt / gte），不满足记 `skipped`——显式留痕，绝不静默跳过。
- **占位符**：`{{input.<path>}}`（本次 run 输入）、`{{<as>.<path>}}`（前序步骤结果，必须前向引用）、
  `{{dec:<a>,<b>}}`（递减）；整值替换保类型、内嵌替换字符串化。gql 内禁占位符（参数走 params
  绑定，防注入）；数组下标路径不支持（逐行处理请走宿主代码编排）。
- **权限**：defn 内嵌三级角色白名单（复用四级 RBAC 语义：admin / super_admin 放行、guest 拒绝、
  internal 放行、creator 按 Missing 通过）；run 继承触发者 Context，每步 query/mutation 都过 core
  权限判定——工作流是「权限内的一次次普通调用」，不存在超级身份。`require_context(true)` 开启时
  无 ctx 拒跑（fail-secure 优先于 dry-run）。rejected 同样落库（拒绝可审计）。
- **原子性**：单源 run 整体原子（外层 `run_atomic` 包住整个步骤循环，内层 mutation 嵌套并入——
  任一步失败整体回滚）；多源 / 预扫失败按顺序执行并发反馈事件（`workflow_non_atomic` /
  `workflow_prescan_failed`，禁静默）。run 记录时序：先落 `running`（进程崩溃可见）→ 步骤事务 →
  终态在事务外独立提交（业务回滚不影响失败 run 可查）。
- **dry-run**：`workflow.run(name, input, dry_run=True)`——query 真实执行（只读安全），
  mutation / fail 记 `wouldRun`；终态 `drySucceeded | dryFailed`。
- **run 落库**：`__workflowRun` 由模块导入即自举注册（幂等）；SQL 后端首次启用工作流需执行
  `ddl.generate(backend, ['__workflowRun'])` 建表（Mongo 无需，首次写入自动建集合）。
  其 `write` 为显式空名单（普通角色 GQL 篡改 run 审计被 R2 拒绝；模块内部写入走 internal 上下文）。

### 首批明确不做（检出即 Err，边界与能力同权重）

| 不做 | 理由 | 出口 |
|---|---|---|
| 循环 / 并行 / 子工作流 / 人工审批 | DAG 与人工等待语义复杂度爆炸；线性 + `when` 覆盖首批场景 | 宿主代码用 store API 编排 |
| 自动补偿（Saga）/ 自动重试 | 反向操作语义负担大；步骤无自动幂等保证 | 人查 run 记录显式处置 |
| 步骤级宿主回调 | 任意代码击穿白名单治理 | schema computes（读）/ 宿主代码（写） |
| 工作流定义存库 / 热更 | 依赖 schema 版本化先行（路线图下一步） | defn 暂与 schema 同模式：代码内 JSON + register |
| 定时触发 / 事件触发 | 触发器是常驻 IO 职责，属调度层，与「纯编排」正交 | 应用层自行调用 `run` |
| gql 内嵌占位符 / 数组下标路径 | 注入面 / 数组逐行处理语义 | params 绑定 / 宿主代码编排 |

## 场景事例

上文参考章节逐项介绍了各能力的定义。下面用同一个库在具体业务情境中做实事 —— 每个示例都可直接复制运行，后端取自[支持的后端](#支持的后端)。

### 1. 用纯 Python 定义 schema 并立即查询

**业务背景。** 你要给一个小服务加一个 `Post` 模型，不想手写 DDL、ODM 类或仓储层。模型本身是数据，不是代码。

```python
from pymongo import AsyncMongoClient
from py_store import init, store

await init(AsyncMongoClient("mongodb://localhost:27017")["mydb"])

store.register({
    "name": "Post",
    "collection": "posts",
    "idPrefix": "PT",
    "fields": {
        "title":  {"type": "string", "default": ""},
        "status": {"type": "string", "default": "draft"},
        "tags":   {"type": "array",  "default": []},
    },
})

await store.insert("Post", {"title": "Hello"})
items = await store.query(
    "Post($condition:@c0,$sort:@s1,$limit:@l) { title, status }",
    {"c0": {"status": "draft"}, "s1": {"createdAt": -1}, "l": 20},
)
```

**结果。** `items` 为 `[{"_id": "PT…", "title": "Hello", "status": "draft"}]` —— `status` 你从未写入，是读取时由字段 `default` 填充的。

**优势所在。** 模型就是一个普通 dict，没有类继承、没有元类、没有导入期注册仪式 —— `register()` 加上 `init()` 模型就存在了。字符串 `_id` 由框架生成（`idPrefix` + base36 时间戳 + 随机），而且这份 dict 对 Node.js 宿主（[`nodejs-store`](https://github.com/coenddt/nodejs-store)）同样是合法 schema，所以在这里写的 schema 不会被锁死在 Python。

### 2. 一份 schema，换后端不改业务代码

**业务背景。** 你在本地 SQLite 文件上开发，线上是 PostgreSQL。你不想要两套查询方言，也不想上线时重写。

```python
import aiosqlite
from py_store import executors, init, store

# 本地开发：SQLite 文件
db = await aiosqlite.connect("dev.db")
await init({"default": executors.create_connection("sqlite", db)})
```

```python
import asyncpg
from py_store import executors, init, store

# 生产环境：PostgreSQL 连接池
pool = await asyncpg.create_pool(dsn="postgres://user:pass@host:5432/db")
await init({"default": executors.create_connection("postgres", pool)})
```

两种环境下，后续业务代码完全相同：

```python
items = await store.query(
    "Post($condition:@c0) { title, status }",
    {"c0": {"status": "draft"}},
)
```

**结果。** 两种环境返回相同结构的文档；差异仅在那两行数据源。

**优势所在。** GQL 由共享的 Rust core 按后端编译 —— MongoDB 走原生聚合，SQLite/PostgreSQL/MySQL 走参数化 SQL。业务代码里既没有 SQL 字符串也没有方言分支，因此「换数据库」改的是连接、不是查询。`local` 本地磁盘数据源（见[示例 7](#7-测试与离线缓存用的本地磁盘数据源)）以 `local.connect(...)` 同样接入。

### 3. 一次查询取回课程及其课时、分类、评价（避免 N+1）

**业务背景。** 课程详情页需要课程、它的分类、课时列表与评价。经典缺陷是 N+1：一次查课程，再为每个关系各查一次。

```python
course = await store.query_one(
    "Course($condition:@c0){ _id, title, "
    "  category { _id, name }, "
    "  lessons { _id, title, seq }, "
    "  reviews { _id, score } }",
    {"c0": {"_id": "c1"}},
)
```

**结果。** 一个文档，关系已挂载：

```python
{"_id": "c1", "title": "Python入门",
 "category": {"_id": "cat2", "name": "后端"},
 "lessons": [{"_id": "l1", "title": "安装", "seq": 1}, {"_id": "l2", "title": "入门", "seq": 2}],
 "reviews": [{"_id": "r1", "score": 5}, {"_id": "r2", "score": 4}]}
```

`one` 关系合并进父文档，`many` 关系成为列表（空时 `[]`，缺失的 `one` 为 `None`）。

**优势所在。** 关系在 schema 里声明一次，选择集本身就足以让引擎解析它们：MongoDB 编译为 `$lookup`，SQL 编译为 `JOIN`，每个后端只有一次查询。没有 ORM 的 `selectinload` / `prefetch_related` 需要记，也没有 `$lookup` 需要手写，缺失关系是 `None`/`[]` 而非崩溃。子列表还能按父级排序分页：`lessons($sort:@s0,$limit:@l0){ _id, title }`。

### 4. 组织 / 多租户模型的行级数据权限

**业务背景。** 在线学习笔记：学生只能读写自己的笔记，审计日志仅管理员可见，课程的 `secret` 字段仅管理员可读。你希望这在数据层强制，而不是在每个 handler 里重写一遍。

```python
store.register({
    "name": "StudyNote", "collection": "study_notes",
    "read": ["creator", "admin"], "write": ["creator", "admin"],
    "fields": {"_id": {"type": "string"}, "title": {"type": "string"},
               "createdBy": {"type": "string"}},
    "relations": {},
})
```

```python
# 按请求设置一次，在路由 / 依赖层
store.set_context({"userId": "u1", "roles": ["student"]})

# 只返回 u1 的笔记 —— 属主条件自动注入
mine = await store.query("StudyNote($condition:@c0){ _id, title }", {"c0": {"title": "笔记A"}})
```

**结果。** `mine == [{"_id": "n1", "title": "笔记A"}]`。他人的笔记永远不会返回，显式去查也返回 `[]` —— 属主条件与你的条件做 AND，绝不会替换它。

**优势所在。** `creator` 是由 `doc.createdBy == ctx.userId` 判定的伪角色，因此归属是数据事实而非逐 handler 的检查 —— 授予该角色的 schema 会在读 *和* 写路径（含 `count`/`exists`）自动注入属主条件与归属校验。字段级白名单同理：`student` 请求标记为 `read: ["admin"]` 的字段时拿不到该字段，而显式请求一个不可读的**关系**会抛 `store.PermissionError`（`status = 403`），而非静默返回 `False`。特权角色（`super_admin`/`admin`/`internal`）一律放行；`guest` 永远不能写。

### 5. 计算列在 Python 侧零成本复用

**业务背景。** 课程列表要展示 `revenue = price * enrolledCount` 与 `lessonCount = 课时数`。你希望两者都在读取时算出，不维护任何冗余列。

```python
store.register({
    "name": "Course", "collection": "courses",
    "fields": {"_id": {"type": "string"}, "price": {"type": "float"},
               "enrolledCount": {"type": "int"}},
    "relations": {
        "lessons": {"model": "Lesson", "type": "many",
                    "localField": "_id", "foreignField": "courseId"},
    },
    "computes": {
        # 同步计算列：对单个文档
        "revenue": {"type": "float", "depends": ["price", "enrolledCount"],
                    "fn": lambda d: (d.get("price") or 0) * (d.get("enrolledCount") or 0)},
        # 关系聚合计算列：相关课时数
        "lessonCount": {"type": "int", "agg": {"$count": "lessons"}},
        # 异步计算列：对整个结果页 (async def fn(items, ctx) -> None)
        "displayName": {"type": "string", "depends": ["name"], "asyncFn": my_display_name},
    },
})
```

```python
rows = await store.query(
    "Course($condition:@c0){ _id, price, revenue, lessonCount }",
    {"c0": {"_id": "c2"}},
)
```

**结果。** `rows == [{"_id": "c2", "price": 100.5, "revenue": 301.5, "lessonCount": 2}]`。你没有请求 `enrolledCount`，`revenue` 却算对了 —— `depends` 把它拉了进来 —— 且它不会泄漏到输出中。

**优势所在。** 依赖字段为计算自动注入、并从投影中自动移除，因此线上返回的正是你所请求的。`fn` 是对一行的一个回调；`asyncFn` 是对整个结果的一次批量处理（`async def fn(items, ctx)`）；`agg` 复用与查询过滤相同的关系聚合机制，所以「课时数」无需手写 join。跨 schema 复用以名字为准：给该列显式写 `"fnRef": "common.moneyLabel"`，Python 的 `common_money_label` 与 Node 的 `commonMoneyLabel` 就绑定到同一个逻辑列（按规范化名绑定；`fnRef` 无实现会以 `ERR_FN_MISSING` 让服务启动失败）。

### 6. 用聚合替代手写 SQL 报表

**业务背景。** 看板需要「按状态分组的课程，带数量与价格合计，只保留课程数大于 1 的分组」。与其每个指标一条 SQL，不如一次问清。

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

**结果。** `[{"status": "published", "n": 5, "total": 1000000000000110.8}]` —— 即 `GROUP BY status ... HAVING count(*) > 1`，按价格合计排序。

**优势所在。** `$group` / `$having` 是 GQL 的一部分，所以同一调用在 MongoDB 上跑原生聚合、在 SQL 上跑参数化 `GROUP BY ... HAVING` —— 没有单独的报表 SQL，没有方言分支。执行顺序固定且有文档（`WHERE → GROUP BY → HAVING → sort → skip/limit`），算子白名单是 `$count` / `$sum` / `$avg` / `$min` / `$max`，省略 `by`（或传 `[]`）得到全表单行。父级也可用子级聚合过滤而不产生扇出 —— `{"orders": {"$count": {"$gt": 3}}}` 在 SQL 上变为 `EXISTS` / `NOT EXISTS`，绝不重复父行。

### 7. 测试与离线缓存用的本地磁盘数据源

**业务背景。** 你希望同一份数据层代码能在无需安装数据库的 CI 或沙箱里运行，并有一个重启后仍在的持久缓存 —— 零外部服务。

```python
from py_store import init, local, store

await init({"default": local.connect({"dir": "./data/store"})})

store.register({"name": "User", "collection": "users",
               "fields": {"_id": {"type": "string"}, "name": {"type": "string"}},
               "relations": {}})
await store.insert("User", {"name": "Ada"})
rows = await store.query("User{ _id, name }")
```

**结果。** `rows == [{"_id": "…", "name": "Ada"}]`，且 `./data/store/users.json` 现在是一个文档的 JSON 数组。

**优势所在。** `local.connect({"dir": …})` 返回的是普通连接描述符，因此直接塞进同一个 `init()`，所有 GQL/CRUD 调用照常工作 —— 无需 SQL 引擎、无需驱动（`local` 源由 core 的 local 求值器直接求值，语义与 MongoDB 一致）。写入走 `.tmp` 临时文件加原子 rename，`store.transaction` / `store.session` 得到快照隔离、构造上即原子。护栏是显式而非静默的：每集合 100,000 文档硬上限、无索引（声明 `schema.indexes` 会发出 `local_indexes_ignored` 反馈事件）、仅支持单进程。

### 8. 工作流编排：串联多步业务动作

**业务背景。** 「下单」是：读库存、库存记录不存在则失败、库存不足则失败、然后扣减。你希望把这些步骤及其守卫表达为数据，fail-fast 执行，事后可审计。

```python
from py_store import workflow

workflow.register({
    "name": "placeOrder",
    "run": ["admin", "ops"],
    "steps": [
        {"op": "query", "as": "inv",
         "gql": "Inventory($condition:@c0){_id, stock}",
         "params": {"c0": {"productId": "{{input.productId}}", "warehouse": "{{input.warehouse}}"}}},
        {"op": "fail", "when": {"exists": "{{inv._id}}", "is": None}, "message": "库存记录不存在"},
        {"op": "fail", "when": {"lt": "{{inv.stock}}", "than": "{{input.qty}}"}, "message": "库存不足"},
        {"op": "mutation", "model": "Inventory",
         "data": {"_id": "{{inv._id}}", "stock": "{{dec:{{inv.stock}},{{input.qty}}}}"}},
    ],
})

run = await workflow.run("placeOrder", {"productId": "p1", "warehouse": "w1", "qty": 30})
```

**结果。** `run["status"]` 取 `succeeded | failed | rejected | drySucceeded | dryFailed` 之一；业务失败不抛错 —— 原因在 `run["error"]`（成功时为 null）。run 落库到内建 `__workflowRun` schema，用普通 GQL 即可查询。

**优势所在。** 定义是与 schema defn 同构的纯 JSON，在 `register()` 时静态校验：未知步骤类型、后向占位符引用、缺操作数的 `when`、`gql` 内嵌占位符，都在注册期失败（`WORKFLOW_UNSUPPORTED`）而非运行期。步骤是白名单（`query` / `mutation` / `fail`），占位符是 `{{input.x}}`、`{{step.field}}`、`{{dec:a,b}}`，每一步都在调用者的权限上下文下运行 —— 不存在超级身份。单源 run 端到端原子；`fail` 守卫把 run 记为 `failed` 并带上 `stepIndex`，绝不静默跳过。`workflow.run(name, input, dry_run=True)` 会真实执行读取，并把写标记为 `wouldRun`。

### 9. fail-secure 模式：一个开关关掉所有开口兜底

**业务背景。** 你要上生产，不能依赖「调用方总是记得设置上下文」。你希望安全默认就是强制默认。

```python
from py_store import secure

# 启动时执行一次
secure.secure_mode(admin_roles=["admin"])
```

**结果。** 未设置上下文时，每次查询/写入都抛 `ERR_NO_CONTEXT` 而非放行（fail-open → fail-secure）。后台任务必须显式声明：

```python
await store.run_as_internal(lambda: store.remove("Post", {"_id": pid}))   # 仍会放行
```

未配置的 schema 对*所有人*（含 admin）拒绝，直到声明白名单为止；无上下文注册新定义也会被拒。`secure.relax_mode()` 恢复宽松默认；`secure.is_secure()` 报告当前状态。

**优势所在。** `secure_mode()` 一并翻转三个原本独立的开关（require-context、unconfigured-roles-closed、meta-policy-closed），你不会开了其中一个却忘了另外两个。`run_as_internal` 与「缺失上下文」在语义上*截然不同*，这正是系统任务能继续工作、而忘记设置上下文能 fail-closed 的原因 —— 「无上下文」过去恰恰把这两种情况混为一谈。降级路径同样不静默：`set_feedback_sink` 仍会收到结构化事件，而不是让失败悄无声息。

### 10. 多数据源：MySQL + Mongo 挂在同一个 store，一条联邦 GQL

**业务背景。** 用户在 MongoDB、订单在 MySQL，而一个个人主页两样都要。你不想维护两个客户端、两套查询语言，再在中间手写一次 join。

```python
import asyncmy
from pymongo import AsyncMongoClient
from py_store import executors, init, store

await init({
    "mongo_main": AsyncMongoClient("mongodb://mongo-host:27017")["shop"],
    "mysql_main": executors.create_connection(
        "mysql", await asyncmy.create_pool(host="mysql-host", user="app",
                                           password="…", db="shop", autocommit=True)),
})

# 每个定义固定自己的源；关系是跨源的
store.register({
    "name": "User", "collection": "users", "datasource": "mongo_main",
    "fields": {"_id": {"type": "string"}, "name": {"type": "string"}},
    "relations": {
        "orders": {"model": "Order", "type": "many",
                   "localField": "_id", "foreignField": "userId"}},
})
store.register({
    "name": "Order", "collection": "orders", "datasource": "mysql_main",
    "fields": {"_id": {"type": "string"}, "userId": {"type": "string"},
               "code": {"type": "string"}, "amount": {"type": "float"}},
    "relations": {},
})

rows = await store.query_federated(
    "User($condition:@c0){ _id, name, orders { code, amount } }",
    {"c0": {"name": "ada"}},
)
```

**结果。** 每个用户的 MySQL 订单嵌在同一份文档里返回 —— store 按源取数、内存 join、再水合成树：

```python
[{"_id": "u1", "name": "ada", "orders": [{"code": "A1", "amount": 30.0}, ...]}]
```

**优势所在。** schema 由 `(source, database, collection)` 定位，每个定义携带自己的 `datasource`，一份注册表覆盖所有后端。SQL 跨 database / PG-schema 的 join 原生下推（`"db_a"."t" JOIN "db_b"."t"`）；Mongo 跨源关系走联邦路径（`query_federated`）—— 某一段无法下推时你会收到结构化的 `federation_degraded` 事件，而不是悄悄变慢。这正是本仓库四库压测用 8 个并发 worker 跑的拓扑（Mongo 用户 → MySQL 订单，再加一个 PG 源，见[性能](#性能)）。

### 11. 事务 + 原生 SQL：一笔不可能落地一半的转账

**业务背景。** 账户间转账：锁源行、校验余额、一侧扣款、另一侧入账、追加一条台账。任何一步失败都必须整体回滚。

```python
async def transfer(amount, from_id, to_id):
    rows = await store.execute_raw(
        "pg_main", "SELECT balance FROM accounts WHERE _id = $1 FOR UPDATE", [from_id])
    if rows["rows"][0]["balance"] < amount:
        raise ValueError("insufficient funds")        # 整个作用域回滚

    await store.execute_raw(
        "pg_main", "UPDATE accounts SET balance = balance - $1 WHERE _id = $2",
        [amount, from_id], is_write=True)
    await store.execute_raw(
        "pg_main", "UPDATE accounts SET balance = balance + $1 WHERE _id = $2",
        [amount, to_id], is_write=True)
    await store.insert("Ledger", {"fromId": from_id, "toId": to_id, "amount": amount})
    #  ↑ 普通 CRUD 调用 —— 落在同一条事务连接上

await store.transaction("pg_main", lambda: transfer(30, "acc_a", "acc_b"))
```

**结果。** 四条语句要么一起提交、要么一起不提交。`execute_raw` 返回 `{"rows", "affectedRows"}`（读给 rows、写给受影响行数），`store.transaction(source, fn)` 让 `fn` 内的一切复用同一条连接 —— 原生 SQL 与 CRUD 一视同仁。

**优势所在。** 原生 SQL 由 core 的 `raw_stmt_compile` 编译（命名 `:name` 参数或位置 `?`/`$1`），转义与方言细节不必进你的代码，逃生舱口却完全是原生的（`FOR UPDATE` 照用）。Mongo 源按运行期探针（副本集 / 分片）包进 session 事务；standalone Mongo 或无事务原语的执行器按原样执行 `fn` 并发出 `mongo_transaction_unsupported` / `transaction_not_atomic` —— 降级会被宣告，绝不静默假装。同源嵌套作用域变成保存点：内层失败只回滚本层。

### 12. 会话（Session / 工作单元）：多次写入、一次提交、跨源 fail-closed

**业务背景。** 「创建订单」要碰三张表：插订单、扣库存、追加审计行。你希望这一切构成一个工作单元，而不是手工管连接。

```python
async with store.session() as s:
    order = await s.insert("Order", {"userId": "u1", "amount": 30})
    await s.update("Inventory", {"_id": "sku1"}, {"$inc": {"stock": -1}})
    await s.insert("AuditLog", {"kind": "order", "ref": order["_id"]})
# 正常退出 → 一次提交；任何异常 → 一次回滚
```

**结果。** 同一 SQL 源上的每条命令都落在同一条事务连接上；会话退出时统一提交一次，任何异常统一回滚。无命令的会话绝不检出连接（惰性启动）。

**优势所在。** 会话可嵌套并带保存点（`SAVEPOINT sp_<n>`），内层失败只回滚内层、外层继续。危险情况 fail-closed：向 ≥2 个数据源写入的会话整体回滚并抛 `NonAtomicWriteError` —— 不存在会被静默提交一半的分布式事务。无原语的执行器发出 `session_not_atomic` 事件，而不是假装。

### 13. DDL 生成：schema 进，CREATE TABLE 文本出

**业务背景。** 模型在代码里注册好了，DBA（或 Alembic）现在需要物理表。你想要一个迁移起点，且不引入第二份真相来源。

```python
sql = store.generate_ddl("postgres")                           # 所有已注册模型
sql = store.generate_ddl("sqlite", ["Order", "OrderDeleted"])  # 只要这两张
```

**结果。** 纯 `CREATE TABLE` 文本 —— 从不执行、从不写库 —— 由已注册 def 生成：

```sql
CREATE TABLE "bench_orders" (
  "_id" TEXT PRIMARY KEY,
  "user_id" TEXT,
  "amount" REAL,
  "qty" INTEGER,
  "status" TEXT,
  "__present" TEXT
);
```

**优势所在。** `generate_ddl(backend, names)` 是 `sync_schema()`（只*读*）的逆操作。自由命名的字段按后端翻译（`userId` 在 SQL 上是 `user_id`、在 Mongo 上是 `userId`），每张表都带 `__present` 哨兵列，`<Model>Deleted` 归档表与模型一起恰好生成一次。不发 `CREATE INDEX`（SQL 后端把索引当元数据保留），边界情况大声失败（`ddlPresentOverflow`）而非静默。

### 14. 触发器：同一事务里的声明式审计流水

**业务背景。** `Order` 的每次插入、改金额的更新、删除都必须留下审计行 —— 且审计写失败时，业务写也不能落地。

```python
DEFS = [{
    "name": "Order", "collection": "orders", "idPrefix": "OD",
    "fields": {"_id": {"type": "string"}, "amount": {"type": "float"},
               "status": {"type": "string"}},
    "triggers": {
        "insert": [{"name": "aud_ins", "into": "AuditLog", "op": "insert",
                    "data": {"_id": "{{now}}", "kind": "ins",
                             "ref": "{{root._id}}", "amount": "{{root.amount}}"}}],
        "update": [{"name": "aud_upd", "onFields": ["amount"], "into": "AuditLog",
                    "op": "insert",
                    "data": {"_id": "{{now}}", "kind": "upd",
                             "ref": "{{root._id}}", "amount": "{{root.amount}}"}}],
        "remove": [{"name": "aud_del", "fnRef": "auditRemove",
                    "args": {"id": "{{before._id}}"}}],
    },
}, {
    "name": "AuditLog", "collection": "audit_logs", "idPrefix": "AL",
    "fields": {"_id": {"type": "string"}, "kind": {"type": "string"},
               "ref": {"type": "string"}, "amount": {"type": "float"}},
}]

for d in DEFS:
    store.register(d)

async def audit_remove(args, ctx, host):
    await host["store"].insert("AuditLog", {"kind": "del", "ref": args["id"]})

store.set_trigger_fn("auditRemove", audit_remove)
store.assert_trigger_fns_covered(DEFS)   # 启动期：声明的 fnRef 无实现 ⇒ ERR_TRIGGER_FN_MISSING
```

**结果。** 写事件在 plan 期由 Rust core 展开成有序触发链，由宿主在与源写同一个原子包内执行：`insert` 与 `update(amount)` 留下命令式审计行；删除订单时以删除前文档（`{{before._id}}`）运行 `auditRemove` 回调。

**优势所在。** 触发链是数据、声明在 schema 上。`update` 事件只在被监视字段真的变化时触发（结构化深比较 —— 值没变绝不触发）；`{{root.*}}` / `{{before.*}}` / `{{now}}` 是整值替换（嵌进字符串会抛 `ERR_TRIGGER_PLACEHOLDER`）；重复的 `(step.name, _id)` 只跑一次；回调收到 `(args, ctx, {"store": store})`，回调内任何 `store.*` 都命中同一条事务连接。回调或命令步骤失败会整体回滚 —— 且不级联：触发器写出的数据绝不会再触发其他触发器。

### 15. 软删除归档：remove() 带走完整审计痕迹

**业务背景。** 合规要求删除也要留痕：行从所有列表里消失，但归档仍可查 —— 包括*何时*被删。

```python
r = await store.remove("Order", {"_id": "o1"})
# r == {"deletedCount": 1, "archivedCount": 1}

archived = await store.query("OrderDeleted{ _id, amount, deletedAt }")
```

**结果。** 文档先被复制进自动派生的 `OrderDeleted` 归档（带 `deletedAt`），再从 `orders` 删除。归档是普通已注册 def —— 普通 GQL 直接可查，并在 `generate_ddl()` 里恰好出现一次。

**优势所在。** 每个已注册模型在注册时由 Rust core 免费派生 `<Model>Deleted` 归档集合/表。归档写入按 `_id` upsert，所以重建相同 `_id` 不会冲突、部分失败后的重试会幂等重新归档。空条件的 `remove` 被直接拒绝 —— 忘写过滤条件不可能触发全表删除。

### 16. FastAPI：一个依赖搞定每请求权限上下文

**业务背景。** 内部管理 API：分页列表、创建、软删。权限必须在数据层对每个端点强制，而不是逐路由重写。

```python
from fastapi import Depends, FastAPI, Request
from py_store import init, store

app = FastAPI()

@app.on_event("startup")
async def startup():
    await init({"default": mongo_db})
    store.register({
        "name": "Article", "collection": "articles", "idPrefix": "AR",
        "fields": {"_id": {"type": "string"}, "title": {"type": "string", "default": ""},
                   "body": {"type": "string", "default": ""},
                   "status": {"type": "string", "default": "draft"},
                   "authorId": {"type": "string", "default": ""}},
        "read": ["editor", "viewer", "creator"],
        "write": ["editor", "creator"],
    })

async def current_user(request: Request) -> dict:
    user = {"userId": request.headers["x-user-id"],
            "roles": request.headers.getlist("x-role")}
    store.set_context(user)          # 一个依赖，覆盖整个请求
    return user

@app.get("/articles")
async def list_articles(page: int = 0, page_size: int = 50,
                        user: dict = Depends(current_user)):
    return await store.query_with_count(
        "Article($sort:@s1){ _id, title, status }",
        {"s1": {"createdAt": -1}, "page": page, "pageSize": page_size})
    # → {"items", "total", "hasMore", "page", "pageSize"}   (pageSize 上限 5000)

@app.delete("/articles/{article_id}")
async def delete_article(article_id: str, user: dict = Depends(current_user)):
    return await store.remove("Article", {"_id": article_id})   # 先归档
```

**结果。** 角色校验（schema/字段白名单、`creator` 归属）在每次 store 调用时自动运行，无逐路由代码；`delete` 留下可审计的归档行。

**优势所在。** 权限上下文存在 `ContextVar` 里，在 `async` 依赖里设置即覆盖整个请求路径（跑在线程池里的 sync 依赖做不到）。默认缺上下文是 fail-open（向后兼容）；启动时翻转 `store.set_require_context(True)`，忘设上下文就抛 `ERR_NO_CONTEXT` 而非放行。空条件写（`update_many` / `remove` 传 `{}`）被直接拒绝，漏传过滤条件不可能变成全表写。完整演练见
[doc/use-cases/04-fastapi-admin-backend.md](doc/use-cases/04-fastapi-admin-backend.md)。

## 性能

下面的数字来自两个来源：撰写本 README 的这台机器上运行的微型基准（SQLite，无需任何服务 —— 查询/写入模式可直接复制运行），以及仓库自带的四库压测。

### 微型基准（SQLite，本机实测）

实测于 **2026-10-15**，Python 3.14.4，Windows amd64，`aiosqlite` 文件库，py-store 跑在 Rust core（`rust-store-py`）上。每项数字为丢弃 1 次预热后 4 次运行的中位数。种子数据：200 个用户；写入基准进行中订单表增至 30,000 行。

| 操作 | 数据量 | 中位耗时 | 吞吐 / 对比 |
|---|---|---|---|
| `insert_many`（10 批 × 1,000） | 10,000 行 | 392 ms | **≈ 25,500 行/秒** |
| 循环单条 `insert` | 1,000 行 | 3,439 ms | ≈ 290 行/秒 → 批量快 ≈ 88 倍 |
| GQL 树查询 —— 1 次调用，关系嵌套 | 100 用户 + 15,000 子行 | 517 ms | 1 次调用 |
| 手写 N+1 —— 同一份数据 | 100 用户 + 15,000 子行 | 718 ms | 101 次调用 → **慢 1.4 倍** |
| `$group` 下推 SQL | 10,000 行 | 15 ms | 1 次调用 |
| 全量拉回 + Python 侧 groupby | 10,000 行 | 600 ms | **慢 41 倍** |
| 计算列（读取时 `fn`） | 2,000 行 | 29 ms | ≈ 与手写循环持平（29 ms） |

怎么读这张表：

- **聚合是大头。** `$group` 编译成在 SQLite *内部*执行的 `GROUP BY`；手写替代方案要为把 10,000 行搬过驱动、再在 Python 里重新聚合买单。
- **树查询赢在调用次数，而不只是耗时。** 1 次往返对 101 次。对本机 SQLite 文件每次额外调用都很便宜，所以墙钟差距是 1.4 倍；跨网络连 MySQL/PostgreSQL/MongoDB 时，那 101 次调用每次都要付一个 RTT，差距随之拉大。
- **批量是最大的单项杠杆。** 同样的行按每 1,000 行一次 `insert_many` 写入，比 1,000 次单条插入快约 88 倍 —— 每次单条插入都要付一次规划加一次自己的提交。
- **计算列是诚实的，不是魔法。** `fn` 逐行跑在 Python 侧，所以与手写循环持平，同时省掉你原本要手工维护的簿记工作（依赖注入、投影清理）。

### 四库压测（仓库留档）

`stress/stress.py`（本仓库）同时压 MongoDB + MySQL + PostgreSQL + SQLite —— 8 workers × 200 轮 × 每轮 5 操作（一条跨 3 源联邦查询、一次 SQLite 写、一次 PostgreSQL count、一次 Mongo 查询、一次 MySQL `$inc` 更新）。留档运行记录见
[doc/2026-09-12-压测报告.md](doc/2026-09-12-压测报告.md)：**8,000 次操作，0 错误，824 QPS**，联邦查询 p50 **19.3 ms** / p95 24.2 ms。（待核实：撰写本 README 的机器未复跑 —— 需要运行中的 MySQL/PG/Mongo 服务；这些是仓库留档数字，可用该报告中的命令复现。）

### 时间花在哪（设计说明）

- **规划在 Rust core。** GQL 解析、权限检查、计算列规划、命令规划与 SQL 方言翻译全部跑在 Rust（`rust-store-py`，经 PyO3）；Python 宿主每条命令过一次 FFI 边界，然后做驱动 IO —— 每命令一次跨界，不是每行一次。
- **下推优先于搬运数据。** `JOIN` / `$lookup`、`GROUP BY` / `HAVING`、`EXISTS` / `NOT EXISTS` semi/anti-join、按父级的子列表排序分页，都编译成每个后端一条语句 —— 活儿在数据所在处干。
- **读取时再水合。** 扁平 JOIN 行每次结果集只重组一次成嵌套文档；缺失的 `one` 关系变 `None`、空的 `many` 变 `[]` —— 不发额外查询。
- **已知上限。** Python 侧的工作是真实存在的：`fn` 计算列逐行执行、`asyncFn` 每页执行、跨源 Mongo 联邦在 Python 内存里 join。`local` 磁盘源每集合上限 100,000 文档且不保留索引。这些数字都不是对你硬件的承诺 —— 把它们当预算前，先复跑微型基准（它只需要 `aiosqlite`）。

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
使用 `store.set_context({"userId": ..., "roles": [...]})` 加上 schema 级 `read`/`write` 白名单。`creator` 伪角色会带来自动的归属校验与属主条件注入（归属键由 schema `ownerKey` 声明，缺省 `createdBy`）。`guest` 永远不能写。开启 `set_require_context(True)` 可获得失败安全行为。

**如何做软删除？**
每个已注册模型都会自动获得一个 `<Model>Deleted` 归档集合/表。`store.remove()` 先归档文档再删除；重新创建相同的 `_id` 不会冲突，因为归档写入是按 `_id` upsert。

**它能用于多租户应用吗？**
可以。通过 `(source, database, schema, collection)` 定位 schema，并按请求传入 `{"source", "database", "schema"}` 路由覆盖。只把 `route_override` 当作受信的服务端输入。

**它会执行迁移吗？**
不会。`sync_schema()` 只通过内省*读取*物理结构（introspect → 合并 overlay → 注册）。schema 变更 / DDL 是你迁移工具的职责（如 Alembic）。若想要一个起点，`store.generate_ddl(backend)` 可从已注册 schema 渲染 `CREATE TABLE` 文本 —— 但它只是纯文本生成：绝不执行、也不写入 DDL。

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

- [`nodejs-store`](https://github.com/coenddt/nodejs-store)（[Gitee](https://gitee.com/coenddt/nodejs-store)）—— Node.js 孪生版（npm `nodejs-store`，camelCase API）。
- [`rust-store`](https://github.com/coenddt/rust-store)（[Gitee](https://gitee.com/coenddt/rust-store)）—— 共享的 Rust 核心及其 `rust-store-node` / `rust-store-py` 绑定。
- `text-to-query` —— 把自然语言问题转换为该数据层的 GQL + params 的技能。

## 框架使用契约

- 定义层（数据）：模型 / 权限 / 编排 / 接口 —— 一律纯 JSON，可发布、可回滚、可热更。
- 回调层（代码）：数据源 IO、计算列实现、外部调用、事务 —— 以 `fnRef` 声明、启动时注入；不可序列化，禁止入库。
- 可观测层：一切降级/拦截/兜底事件落 `__feedback`（可用 GQL 查询），绝不静默。

## 许可证

[MIT](LICENSE)
