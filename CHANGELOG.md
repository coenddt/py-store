# Changelog

## [Unreleased]

### Added

- **显式会话（Session / Unit of Work）**：`store.session()`（`async with`），会话内同一 SQL 源的
  全部命令落到同一事务连接，退出统一提交 / 异常统一回滚；**惰性开事务**，空会话不占连接；
  会话可嵌套（内层作用域在已有事务上开 `SAVEPOINT sp_<n>`，内层失败只回滚本层）。
- **执行器显式事务原语** `open_transaction`（sqlite / postgres / mysql）：返回
  `{'exec','commit','rollback','release'}`（三者幂等）；`with_transaction` 改为基于其实现。
- **会话内跨源写 fail-closed**：同一会话写 ≥2 个数据源时先全部回滚、再抛 `NonAtomicWriteError`，
  绝不提交半截。
- **跨源写 `nonAtomic` 程序化声明**：无会话的一次写调用涉及 ≥2 个数据源时，按顺序执行并发出一条
  `non_atomic_write` 反馈（`code: nonAtomic`，含涉及源）——非原子边界显式声明，绝不静默。
- **执行器保存点原语** `savepoint` / `release_savepoint` / `rollback_to_savepoint`
  （sqlite / postgres / mysql）：事务句柄新增三原语；`with_transaction` 的 body 追加第二参数
  （事务句柄）；py 位置参数语义下调用方须接收该参数（既有调用点已同步改签名）。
- **嵌套作用域保存点**：嵌套 `transaction`、嵌套 `session` 与会话内 `transaction` 在已有事务上开
  `SAVEPOINT sp_<n>`，退出按成败 `RELEASE` / `ROLLBACK TO` + `RELEASE`——内层失败只回滚内层、
  外层可继续；句柄无原语时降级并入外层并发 `nested_savepoint_unsupported`（允许降级、禁止静默）。

### Changed

- `update` 的「权限探针 + 写」整体纳入同一事务作用域（`run_atomic`），消除探针与写之间的并发窗口；
  调用级 `now` 仅取一次（两次规划共用）。
- 嵌套事务 / 嵌套会话不再「整体并入外层」：改为保存点隔离（内层失败只回滚本层）。
- 事务边界文档改为三档口径（单命令 / `transaction` / `session`）。

## 2.3.1 (2026-09-27)

### Bug Fixes

- **发布流水线修通（release-pypi 质量门禁）**：门禁环境此前与 `ci.yml` 不一致，直接挡死 publish，
  导致 `v2.2.0` / `v2.3.0` 两次 release-pypi 全红、PyPI 最新仍停在 `2.1.0`。两处根因：
  ① 只装 `.[dev]`，而 `tests/test_coverage_margin.py` / `tests/test_sql_commit_and_registry.py`
  在**收集期**即 `import aiosqlite` —— 缺依赖不是 skip 而是 collection error；
  ② 不起 MySQL / PG / Mongo 服务，而 `tests/test_scenario_course_platform.py` 的 SQL 后端以
  Mongo 产出的 oracle 对拍，缺 Mongo 时 sqlite 仍可跑却拿不到 oracle → 直接判失败（不是 skip）。
  现与 `ci.yml` 完全对齐：起三服务 + 建测试库 + 装 `.[dev,mysql,postgres,sqlite]`。
  本版**能力与 2.3.0 完全一致**，仅修复发布门禁；PyPI 首个 2.3.x 即本版。

## 2.3.0 (2026-09-27)

### New Features

- **调用档位（profile）门面**：`store.set_profile('standard' | 'text2query')` / `store.get_profile()`
  （模块级别名 `setProfile` / `getProfile`）；未知档位由 core 抛错，**禁静默回落**。
- **`text2query()` 上下文管理器**：`with store.text2query(): ...`（模块级 `py_store.text2query()`
  同构）—— 进入即设档 + 强制用户上下文，退出恢复原档（嵌套安全、异常亦恢复）。
- **档位违规错误 + 自动反馈**：`text2query` 档越限抛 `ProfileViolation`（`status = 400`；core
  前缀 `ERR_TEXT2QUERY:` 映射），同时产出 `profile_blocked` 反馈事件（含 `profile` / `feature` /
  `layer` / `hint`）—— 允许拦截，禁止静默。
- **`route_override` 受信来源门禁（Host 兜底）**：`text2query` 档传非空 `route_override` 即
  `ProfileViolation` + emit（core 已判，Host 再兜一层）；`standard` 档保持受信可用（CWE-639）。

### Breaking Changes

- **`object` / `array` 列改落 JSON 列**：DDL 生成器由「跳过 object/array 字段」改为建 JSON 列
  （MySQL `JSON` / PG `jsonb` / SQLite `TEXT`），`course-platform` 示例 DDL（sqlite/mysql/postgres）同步。
- **U1~U4 分档**：数组字段过滤（U1）、对象整值过滤（U2）、对象点号路径过滤（U3）/排序（U4）——
  `standard` 档放行（四库可下推；U2 对象键序差异**告警**），`text2query` 档显式 Err；
  数组索引路径（`tags.0`）两档一律 Err。
- **超深关系嵌套不再静默降级**：深度 / 分页深度超限由「静默返回残缺数据」改为**显式 Err**（两档一致）。
- **根级 `$pipeline` 按档分流**：`standard` 档放行（Mongo 源可用；SQL 源逐阶段翻译、无法映射即
  `PushdownUnsupportedError`），`text2query` 档 `ProfileViolation`；`$out` / `$merge` 写副作用阶段两档均拒。

### Bug Fixes

- **索引创建失败改走统一反馈通道（对齐 nodejs-store，评审项 R7-m1）**：`init()` 建索引失败此前直接
  `print` 到 stderr —— 宿主 `set_sink` 无法接管，且绕过了统一反馈通道。现改为
  `feedback.emit({'type': 'index_create_failed', ...})`（无 sink 时仍由 feedback 默认落 stderr，
  不双份打印）。索引创建失败依旧不阻塞 `init`，语义不变。

### Tooling

- 新增 `tests/test_host_paths.py`：两阶段读路径（取 ID → 回表 → 还原排序）、联邦降级告警、
  `init` 入参校验等 Host 执行路径补测。
- `tests/test_coverage_margin.py` 扩充：原生核心加载器（生产禁从相邻仓库兜底）、数据源路由守卫、
  MySQL / PG / Mongo 执行器守卫与事务提交回滚、introspection 后端分发、DDL 边界、权限包装、
  嵌套同源事务并入等此前未覆盖分支。

## 2.2.0 (2026-09-27)

### Bug Fixes

- **SQL 非事务写路径未提交（P0）**：`sqlite` / `mysql` 执行器的非事务路径此前只执行、不提交 —— `aiosqlite` 默认非 autocommit，写入仅当前连接可见却对外报成功（静默丢数据 / 读取幻象）。现改为成功后显式 `commit`、失败先 `rollback` 再上抛，绝不提交半截写入。（`postgres` 的 `asyncpg` 在无显式事务块时逐语句隐式提交，语义已满足，不改。）
- **归档表被重复注册（P0）**：`schema.register` 曾在建镜像后递归调用 `core.register(归档 def)`，而 core 在注册业务表时**已自动派生** `<Name>Deleted`，导致 `store.list()` 出现重复名、`generate_ddl()` 产出重复 `CREATE TABLE`。现改为 Host 仅直接写镜像、不再二次注册 core。
- **纵深防御告警**：`schema.list()` / `ddl.generate()` 现按序去重；一旦仍检出重复（上游失守）即发出 `schemaDuplicateName` / `ddlDuplicateTable` 反馈事件（同签名只告警一次），不静默。

### Tooling

- 新增 `.github/workflows/ci.yml`：ruff + mypy + pytest，语句覆盖率门禁 ≥90%（分支口径单独出具）。
- 新增回归用例 `tests/test_sql_commit_and_registry.py`（提交契约 + 归档唯一性）与覆盖率余量用例 `tests/test_coverage_margin.py`。
- `stress/stress.py` 清理 7 处多余 `noqa` 及导入排序；lint 范围扩至 `src tests stress`。
- `.coverage` 移出版本库并加入 `.gitignore`。

## 2.0.0 (2026-09-14)

### Breaking Changes

- **计算列 `lookup` 形态归一为 `agg` 算子（多后端归一化 P4）**：schema `computes`
  的 `lookup`（Mongo 专用）形态移除，改用归一 `agg` 白名单算子 `$count`/`$sum`/`$avg`/
  `$min`/`$max`：`{"$count": "orders"}`（关系整名计数）、`{"$sum": "orders.amount"}`
  （必须带单级「关系.字段」）。`$count` 只取关系整名，`$sum/$avg/$min/$max` 必须带字段，
  二级路径首批不支持，且与 `fn`/`asyncFn` 互斥。空集语义（§9.7）：`$count` → `0`；
  `$sum/$avg/$min/$max` → `None`。执行按后端下推：SQL 走派生表
  `LEFT JOIN (… GROUP BY fk)`，Mongo 走 `$lookup` + `$addFields`。
- **删除用户 `$pipeline` 直通与 `store.aggregate()`**：多后端归一化执行计划 P3
  砍掉「直通聚合」逃生舱（对齐 D3 / D18）。GQL 中的 `$pipeline` 参数**显式报错**
  （不再静默忽略）；`store.aggregate(...)` / `crud.aggregate(...)`、
  `store.set_allow_user_pipeline(...)` 一并移除。归一聚合（`$group`/`$sum`）将按
  统一 GQL 语法（固定阶段序，下推优先 + 内存兜底）重新设计后回归。
- **读路径关系权限收口（R0-1）**：GQL 显式请求的关系，若 `relation.read` 不可读、
  或目标 model 的 `schema.read` 不可读，规划期**直接报错** `ERR_PERMISSION`
  （错误码稳定前缀 `ERR_PERMISSION:`，Host 映射为 403）。此前是「静默裁剪该关系字段、
  查询照样成功」，现在改为直接失败。`ctx = None`（未设置上下文）维持 fail-open 放行，未变。
- **SQL 后端 `object` / `array` 字段不再静默丢弃**：
  - 读：显式投影 schema 声明为 `object` / `array` 的字段（SQL 侧无对应列）→ **显式报错**
    （此前静默把这列从 SELECT 里丢掉、返回残缺行）；
  - 写：`$set` / `$inc` 目标是 `object` / `array` 字段 → **显式报错**
    （此前静默跳过、数据悄悄不落库）。`$unset` 维持跳过语义不变。

### New Features

- **归一化 P5：根级聚合与关系聚合谓词**：新增根级 `$group` / `$having` 聚合；新增 §9.6
  关系聚合谓词（`$condition` 中以关系名作键的 semi/anti-join 谓词，主形式
  `filter` / `agg` / `having`，简写 `$exists` / `$count` / `$sum`… + `$of`）。
  宿主无需改动，纯 API 能力增强。

### Migration

- 计算列原 `lookup` 声明改写为 `agg`：关系计数 `{"$count": "<关系名>"}`、关系字段聚合
  `{"$sum"|"$avg"|"$min"|"$max": "<关系>.<字段>"}`；结果语义不变，且 SQL 后端自此同语法可用。
- 关系/计算列查询不受影响（`$condition`/`$sort`/`$skip`/`$limit` + 关系字段照常）。
- 原先依赖 `store.aggregate(schema, pipeline)` 的调用方：等待归一聚合语法上线后改写。
- **受影响调用要捕获权限错误**：显式请求不可读关系现在会直接抛 `ERR_PERMISSION`
  （Host 侧 403），不要再假设「查询成功 + 关系被裁剪」了，按需自己 try/except。
- **别依赖 `object` / `array` 字段在 SQL 后端被静默忽略 / 静默不落库**：这些字段在 SQL 后端
  仍**不支持读写**（DDL 不建列），只是失败方式由「静默」变成「显式报错」，跨后端代码要按
  「可能报错」处理。

### Bug Fixes

- **SQL 布尔列回读归一**：MySQL `TINYINT(1)` / SQLite `INTEGER` 的布尔列此前回读为
  `1`/`0`，现由 core 按 schema 声明类型归一为 `true`/`false`（PG 原生 `BOOLEAN` no-op），
  与 Mongo 一致。
- **PostgreSQL 浮点过滤报错**：整数列与浮点值比较（如 `{"$gt": 2.5}`）此前被 PG 推断为
  `integer` 而报 `invalid input syntax for type integer`，现由 core 显式 `CAST` 修复。

## 1.0.0 (2026-09-12)

**破坏性版本**：多数据源定位模型全面重构，消除「静默走错库」的一切可能。
与 `nodejs-store` 1.0.0 同构对齐。
对应方案：`rust-store/.trae/documents/multi-datasource-routing-plan.md`。

### Breaking Changes

1. **命令契约新增定位三元组**：core 产出的每条 Command 均携带
   `source`（连接名，缺省 `"default"`）与 `namespace`（连接内的库/schema 名，
   `None` = 连接默认）。自定义执行器的调用方需适配新字段。
2. **schema 定义新增 `namespace` 字段**（可选字符串）：MongoClient 形态下必须声明
   （= db 名）；PG/MySQL/SQLite 用于声明 schema/database/attached db。
   归档表自动继承 `(source, namespace)`。
3. **Registry 唯一性校验**：`(source, namespace, collection)` 三元组全局唯一，
   冲突注册即抛错（此前按 collection 名反查，存在串源隐患）。
4. **Mongo 连接两种形态严格校验（不猜）**：
   - PyMongo Database：命令 `namespace` 非 None → 显式报错；
   - PyMongo MongoClient：命令缺 `namespace` → 显式报错
     （`client.get_database(ns)[collection]`）。
5. **删除 collection → source 反查**：路由只按命令自带 `source` 精确执行。
6. **绑定层 plan 方法签名变更**：`plan_query` / `plan_mutation` / `plan_update` 等全部
   新增可选 `route_override` 尾参（见下）。依赖 `rust-store-py` 的代码需同步升版。

### New Features

- **多租户动态路由（route_override）**：同一条 GQL / 写请求，按调用传入的
  `{ "source", "namespace" }` override 命令定位，实现「单 schema 定义 × N 租户」，
  注册量不随租户数增长。权限与计算列仍按结构 schema 判定，override 只改定位。
- **SQL 跨 namespace 下推**：同连接跨 schema/db 的 GQL 关联生成
  `"ns_a"."t" JOIN "ns_b"."t"` 原生 SQL（零退化）；仅 Mongo 跨 db 走内存联邦。
- **introspect / sync_schema 支持 `namespace`**：回写 def 的 namespace，与手动声明
  等价；SQLite introspect 支持 attached db 过滤。

### Migration

- 单库用法（`init(db)` + schema 无 `datasource`/`namespace`）行为零变更
  （命令 `source="default"`、`namespace=None`）。
- 多库/多租户：schema 声明 `namespace`，或查询/写入时传 route override。

## 0.1.0

初始版本。
