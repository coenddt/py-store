# 代码评审评测报告：py-store Host 适配层

> 评测轮次：第 1 轮
> 评测时间：2026-09-12
> 评测对象：项目评审（py-store 全部 20 个 .py 源文件约 1,360 行 + 5 个测试文件约 1,220 行）
> 技术栈：Python ≥3.10（asyncio）、pymongo async / asyncmy / asyncpg / aiosqlite 四驱动、rust-store-py 原生绑定
> 评测口径：默认权重（数据层库，薄 Host 架构——纯逻辑在 Rust core，本层只做 IO）
> 脚本证据：`python -m pytest tests -q`（LOCAL_CORE=1 + PYTHONPATH=src，78/78 通过，1.23s；真实四库 e2e 外部库不可达时自动 skip）

## 一、总评

| 总分 | 等级 | 结论 |
|------|------|------|
| **91** / 100 | **S 卓越** | 与 nodejs-store 同构的薄 Host 标杆实现，docstring 与对齐注释为三端最佳；与 Node 侧同源的 3 个 Major（批量 ID 碰撞 / 无事务 / query_one 不限长）建议尽快修复，另有 Python 特有的协议与工程化短板（__getattr__ 协议、死代码、无 lint/pytest 配置） |

**BLOCKED**：无（未命中一票否决清单；无 Blocker/Critical 级问题）

## 二、评分卡

| # | 维度 | 满分 | 得分 | 得分率 | 等级 |
|---|------|------|------|--------|------|
| 1 | 功能正确性 | 15 | 12.0 | 80% | 良 |
| 2 | 可靠性 | 10 | 7.5 | 75% | 中 |
| 3 | 安全性 | 15 | 14.8 | 98.7% | 优 |
| 4 | 性能效率 | 10 | 7.5 | 75% | 中 |
| 5 | 可维护性 | 15 | 13.5 | 90% | 优 |
| 6 | 可读性与规范 | 10 | 9.4 | 94% | 优 |
| 7 | 测试质量 | 10 | 8.9 | 89% | 优 |
| 8 | 文档与可理解性 | 5 | 4.0 | 80% | 优 |
| 9 | 架构与设计 | 10 | 10.0 | 100% | 优 |
| — | 小计 | 100 | 87.1 | | |
| + | 亮点加分 | +5 | +3.5 | | |
| — | **总分** | | **90.6 → 91** | | |

## 三、问题清单（按严重度）

### B Blocker / C Critical

无。

### M Major

| 编号 | 定位 | 问题 | 标准出处 | 修复建议 | 状态 |
|------|------|------|----------|----------|------|
| M-1 | src/py_store/crud/id.py:23-27 | **ID 生成用 `random.choice` 仅 4 位随机**（36⁴ ≈ 168 万组合，Mersenne Twister 非密码学安全）+ 毫秒时间戳：`insert_many` 单次事件循环内批量生成时各 doc 时间戳几乎同毫秒，1,000 docs 碰撞概率约 26%（生日问题），批量插入将以主键冲突失败；ID 可预测可枚举。与 nodejs-store M-1 同源（跨端一致地错，说明该设计决策未被任一端审视） | CWE-338（弱随机）/ CWE-708 | 4 位 `random.choice` 改 `secrets.token_hex(4)` 或 `secrets.choice`（≥8 字符熵）；`_new_id_pool` 与 core `needs_new_id` 契约不变；两端同步修 | ✅ 已修复（第 2 轮） |
| M-2 | src/py_store/crud/mutation.py:16-21、src/py_store/crud/write.py:74-83 | **多步写入无事务/补偿**：mutation 父子步骤逐条 await（步骤 2 失败时步骤 1 已落库）；remove 的「归档 → 物理删除」两命令非原子（重试将重复归档行）。SQL 后端可包 transaction（asyncmy/asyncpg 均支持），Mongo 4.0+ 支持 session 事务，当前对部分失败零防护 | ISO 25010 可靠性（容错性）；CWE-460 | SQL 路径用驱动事务包住 `plan['steps']` 序列；无法事务化的后端在文档显式声明「mutation 非原子」信任边界；remove 归档改 upsert-by-`_id` 语义 | ✅ 已修复（第 3 轮） |
| M-3 | src/py_store/crud/query.py:56-59 | **query_one 未下推 `limit 1`**：复用 `query` 全量取回后取 `[0]`，大集合全量拉取再丢弃，与 PyMongo `find_one`（limit 1）语义不符；`query_with_count` 有 5000 上限而 query_one 无任何行数约束，防线不对称 | CISQ 性能（资源利用） | `query_one` 注入 `$limit: 1` 或走独立 plan 入口；Host 侧兜底告警 | ✅ 已修复（第 2 轮） |

### m Minor

| 编号 | 定位 | 问题 | 标准出处 | 修复建议 | 状态 |
|------|------|------|----------|----------|------|
| m-1 | src/py_store/crud/exec.py:25,58 | `_PERMISSION_MSGS` 以中文文案 frozenset 匹配 core 权限错误（`str(e) in _PERMISSION_MSGS`）——core 文案变更即静默失效，权限错误降级为普通 Exception（status 403 分类丢失变 500），无失效信号。与 nodejs-store m-1、rust-store m-3 同源，建议三端联动（core 侧 CoreError 枚举 + 结构化错误码） | DRY / 脆弱契约 | core 错误结构化（错误码字段）；Host 侧改按错误码匹配 | ✅ 已整改（第 2 轮，前缀方案） |
| m-2 | src/py_store/__init__.py:189-190 | `_create_indexes_if_needed` 对 `db_of_schema` 的 `except Exception: continue` 过宽（有 noqa 标注说明是刻意的，但吞掉的不只是「source 未配置」，还包括 namespace 校验 fail-fast 错误），索引静默缺失且无反馈事件（对比：索引创建失败有 stderr 输出） | fail-fast 一致性 | 按错误类型收窄捕获；或统一走 `feedback.emit` | ✅ 已整改（第 2 轮） |
| m-3 | src/py_store/__init__.py:170-171 | `Store.__getattr__` 直接 `_store_map[name]`——**拼错属性抛 KeyError 而非 AttributeError**，违反 Python 属性协议：`hasattr(store, 'quer')` 向上炸 KeyError、`getattr(store, 'typo', None)` 的默认值失效、`try/except AttributeError` 捕获不到 | Python 数据模型协议 | `try: return _store_map[name] except KeyError: raise AttributeError(name) from None` | ✅ 已整改（第 2 轮） |
| m-4 | src/py_store/crud/query.py:98-99 | 联邦查询逐源**串行**取数（for-await），`plan['sources']` 各单元相互独立，可 `asyncio.gather` 并行，源多时延迟线性叠加 | CISQ 性能 | 改 `asyncio.gather`（merge 阶段仍同步；保持任一源失败整体失败语义） | ✅ 已整改（第 2 轮） |
| m-5 | src/py_store/types.py 全模块 | **死代码**：`TYPE_MAP`/`get_default`/`is_numeric`/`is_primitive` 全仓库零引用（默认值填充已下沉 core） | 死代码（SonarQube RSPEC-3923 类） | 删除；若保留作公共工具需在 README 标注并补测试 | ✅ 已整改（第 2 轮，已删除） |
| m-6 | src/py_store/__init__.py:37-92,95-171 | Store 显式方法与 `_store_map` **双轨并存**：17 个方法显式定义、40+ 条目走 `__getattr__` 动态查找，同类 API 两处维护（新增方法需两处同步，漏一处行为不一致）；且动态路径让 IDE 补全/静态检查全部失效 | DRY / API 一致性 | 收敛为单轨：要么全显式（类方法），要么全 `_store_map` + `__dir__` 补全支持 | ✅ 已整改（第 5 轮：删除 `_store_map` 与 `__getattr__`，Store 全显式——CRUD 保留带类型标注的显式类方法，驼峰别名与其余 API 以类属性显式绑定实现函数；单一实现、IDE 补全/静态检查可用） |
| m-7 | src/py_store/datasource.py:29 | 模块级全局 `_connections` 可变单例：全局状态，`set_connections` 整体替换，测试/多租户隔离靠调用方自觉（与 nodejs-store m-4 同源对齐项） | Clean Architecture | 与 node 侧一起评估 `scoped_connections`（contextvars 变体） | 已评估（第 6 轮）：与 nodejs-store m-4 同源、两端对齐的**模块级单例**是既定设计；改 contextvars 作用域属架构级重构且无实际痛点驱动——**维持现状** |
| m-8 | pyproject.toml | 无 ruff / mypy 配置（`[tool.*]` 全空）：风格一致性当前靠人工纪律（实测一致性很好），无自动化守护 | 工程基线 | 加 ruff（含 isort）+ mypy（宽松起步），规则从现有风格反推 | ✅ 已整改（第 4 轮：pyproject 接入 `[tool.ruff]`（E/F/W/I/B/RUF，E501 与 RUF001-003 中文全角标点豁免——中文注释为既定风格；刻意不启 SIM/UP/FURB 改写类规则避免大规模重排）与 `[tool.mypy]`（宽松起步：ignore_missing_imports + check_untyped_defs）；存量违规清零——导入排序/陈旧 noqa 自动修复、executors/introspect 显式再导出、B904 异常链 `from e`、B905 zip strict=True（DB-API 行宽保证，fail-fast）、E731 lambda→def、6 处最小类型注解；工具入 dev extras（`pip install -e .[dev]`）。实测 ruff 0 违规、mypy 0 错误、pytest 83/83 通过 |
| m-9 | pyproject.toml | pytest 无任何配置：无 `[tool.pytest.ini_options]`、pythonpath、asyncio-mode 声明——本轮实测需手工 `$env:PYTHONPATH='src'` + `LOCAL_CORE=1` 才能跑（运行口诀只存在于测试文件 docstring 内），新环境可重复性差 | ISTQB 可重复性（Repeatable） | pyproject 补 `[tool.pytest.ini_options] pythonpath=["src"]`；dev extras 收拢 pytest/驱动依赖；README 写明测试命令 | ✅ 已整改（第 2 轮） |
| m-10 | README.md | **README 无开发/测试章节**（全文无 test 字样）：如何跑测试、LOCAL_CORE 开发兜底、e2e 外部库要求（MYSQL_URI/PG_URI/MONGO_URI 与自动 skip 行为）均未文档化 | ISO 25010 可理解性 | README 补「Development」章节（测试/构建/LOCAL_CORE 说明） | ✅ 已整改（第 2 轮） |

### I Info

| 编号 | 定位 | 问题 | 标准出处 | 修复建议 | 状态 |
|------|------|------|----------|----------|------|
| I-1 | src/py_store/schema.py:144-146 | `set_allow_user_pipeline` 默认放行用户 `$pipeline`：开关存在但默认「允许」，纵深防御需宿主主动关闭（与 nodejs-store I-1 同源）。默认方向与 fail-secure 相反 | OWASP ASVS | 评估默认关闭、宿主显式 opt-in | 已评估（第 6 轮）：默认放行是三端一致的**向后兼容既定语义**，改默认值属 breaking change 并破坏 parity——**维持现状**，由宿主显式 `set_allow_user_pipeline(False)` 关闭 |
| I-2 | src/py_store/crud/*.py（route_override 贯穿） | `route_override` 允许覆盖 source/namespace 定位，无来源校验——宿主透传用户输入即可跨源路由。属宿主职责且 docstring 已注明「权限/计算列仍按结构 schema 判定」，但未文档化「禁止透传用户输入」红线 | CWE-639 类 | docstring 与 README 显式声明为受信服务端参数 | ✅ 已整改（第 6 轮：README「Multi-tenant route override」与 `crud/query.py::query` docstring 补**受信服务端参数、禁止透传用户输入**红线（CWE-639）；不加校验代码——跨源路由是合法能力，校验属宿主职责） |
| I-3 | src/py_store/permission.py:56-59 vs crud/query.py:35-36 | 异步判别方式两处不一致：`run_as_internal` 用 `inspect.iscoroutinefunction`（且 `import inspect` 在函数体内），`_finalize` 用 `inspect.isawaitable`——后者对 partial/wrapped 协程更稳。风格统一项 | 惯用法一致性 | 统一用 `isawaitable`；`import inspect` 提到模块顶部 | ✅ 已整改（第 6 轮：`run_as_internal` 改「先调用后 `isawaitable(out)` 判定」（对 partial/包装协程不漏判），与 `_finalize` 统一；`import inspect` 提到模块顶部） |
| I-4 | src/py_store/executors/mysql.py:41-61 | `_to_pyformat` 手写字符扫描做 `?`→`%s` 占位符转换——**Host 层唯一「理解 SQL 文本」的例外面**（其余所有 SQL 处理都在 core）。注释充分（跳过引号内 `?`，仅替换占位符、不拼 SQL），边界依赖「core 方言引号成对规范」这一隐含不变式；core 引号规则若变（如 `''` 双写转义）此处需同步 | 设计边界显式化 | 在 core 方言文档记录「占位符不得出现在字符串字面量内」不变式；或 core 直接产出 `%s` paramstyle 变体 | 已评估（第 6 轮）：让 core 直出 `%s` paramstyle 属**改跨端契约**的大改动，为消除一个已注释充分的例外面不划算——**维持现状**（该转换已注释成文，隔离在单一函数内） |
| I-5 | tests/test_py_store.py（mock 驱动） | mock 的 findOneAndUpdate 用简化合并语义，与真实驱动有偏差（与 nodejs-store I-3 同源） | 测试替身保真度 | mock 特判 `$set`/`$inc`/`$unset` | 已评估（第 6 轮）：真实后端 e2e 已兜底写路径语义，提升 mock 保真度收益低于其成本——**维持现状** |
| I-6 | src/py_store/__pycache__/ | 本地残留旧结构缓存（`computes`/`pipeline`/`crud` 顶层模块 .pyc，对应拆包前结构）——已被 .gitignore 忽略未入库，仅本地工作区卫生 | — | 本地清理 `__pycache__` 即可 | 已评估（第 6 轮）：已被 `.gitignore` 忽略、未入库，属本地工作区卫生而非仓库缺陷——**无需仓库变更**（本地可随时清理） |

### 范围外发现

无（GQL 解析 / 权限 / 计算列 / SQL 生成等纯逻辑问题归 rust-store 评测范围，已在该仓库报告中记录）。

## 四、亮点（+3.5）

1. **+1.5 跨语言同构契约测试**（tests/test_host_contract.py）：与 nodejs-store 共享 `rust-store/fixtures/host/*.json` fixture 对拍 `resolve_placeholders`/`_truthy`/`_new_id_pool`/回调桥四个契约件——「双端语义一致」是可执行测试而非口号。
2. **+1.0 fail-fast 下推拦截 + 统一反馈通道**（datasource.py:159-187、feedback.py）：core 标记 `unsupported` 时显式抛 `PushdownUnsupportedError`（先于执行器检查——命令不可安全下推时报下推不支持而非「执行器未接入」，检查顺序讲究），degraded 与拒绝事件统一走 `feedback.emit`，全链路「允许拦截，禁止静默失守」。
3. **+0.5 原生模块生产加载防护 + 平台细节防御**（core.py）：生产只从 pip 依赖加载；开发兜底 `LOCAL_CORE=1` **且** 非.production 双开关；datasource.py:136-145 明确记录 PyMongo `Database.__getattr__` 兜底陷阱（`db.kind` 返回 Collection 而非报错）故只认 Mapping 判 SQL 源——把驱动怪癖转化为显式防御并成文。
4. **+0.5 防拖库与 e2e 工程化**：`query_with_count` pageSize 硬上限 5000；test_real_backends_e2e.py 一次覆盖四库真实驱动（外部库不可达自动 skip，不阻塞回归），asyncmy 池/单连接双形态兼容（acquire contextmanager）。

## 五、需运行验证项

| 项 | 验证步骤 | 验证结果（复评时填） |
|----|----------|---------------------|
| M-1 碰撞复现 | `insert_many` 单次传入 1,000+ 无 `_id` 文档（同毫秒），观察主键冲突概率 | 待验证 |
| update 原生操作符白名单 | 对未注册字段调 `update(schema, cond, {'$set': {'hacker': 1}})`，确认 core 侧字段过滤是否拦截 `$set` 内新键 | 待验证 |
| 真实四库 e2e | 本机启动 MySQL/PG/Mongo 后重跑 `pytest tests/test_real_backends_e2e.py -v`（本轮以自动 skip 路径通过） | 待验证 |
| 行/分支覆盖率 | `pytest --cov=py_store --cov-report=term`（需装 pytest-cov；core 覆盖率归 rust-store） | 待验证 |

## 六、改进建议（按优先级排序）

1. **本次必须**（下一发版前）：
   - M-1 ID 随机源换 `secrets`（与 nodejs-store 同步修，双端一致）
   - M-3 query_one 下推 limit 1
   - m-3 `__getattr__` 改抛 AttributeError（一行级改动，消除协议破坏）
   - m-1 权限错误映射结构化（与 rust-store CoreError 联动）
2. **短期跟进**（1-2 迭代）：
   - M-2 SQL 路径事务包裹 + 「mutation 非原子」信任边界文档化
   - m-2 索引初始化 catch 收窄；m-4 联邦并行取数
   - m-5 删除 types.py 死代码；m-6 Store API 收敛单轨
   - m-8/m-9/m-10 ruff + pytest 配置 + README 开发章节
3. **长期**：
   - I-1 `$pipeline` 默认值策略评估；m-7 `scoped_connections`（contextvars 变体）评估；I-4 core 方言不变式成文

## 七、复评记录（第 2 轮 · 定向）

> 复评时间：2026-09-12（第 1 轮「本次必须」+ 短期跟进部分项闭环后）
> 复评方式：**定向复评** —— 仅回补已闭环项扣分，未做全量重扫；回归由 `python -m pytest`（78/78 通过，e2e 外部库不可达自动 skip）兜底
> 本轮闭环：M-1 / M-3 / m-1 / m-2 / m-3 / m-4 / m-5 / m-9 / m-10

### 闭环项验证

| 编号 | 修复内容 | 验证证据 |
|------|----------|----------|
| M-1 | id.py 随机源 `random.choice`(4位) → `secrets.choice`(8位 base36，约 41 bit 熵)，消除同毫秒批量碰撞与可预测性 | pytest 78/78（insert_many 填充 ID 用例） |
| M-3 | query_one 走 core `plan_query_one`：未显式 `$limit` 时下推 `$limit(1)`，对齐 PyMongo `find_one` 语义 | pytest（query_one 用例）+ core 对拍 |
| m-1 | 权限错误映射改按 `ERR_PERMISSION:` 稳定前缀识别（core 侧新增哨兵前缀，联动 rust-store m-3 部分缓解），core 文案变更不再静默失效 | pytest（权限用例） |
| m-2 | 索引初始化 catch 收窄：datasource 新增 `has_connection` 先行软跳过未配置源（移除 `except Exception: continue` 宽捕获），namespace 校验等 fail-fast 错误恢复上抛 | pytest（init/索引用例） |
| m-3 | `Store.__getattr__` KeyError → `raise AttributeError(name) from None`，hasattr/getattr 协议恢复 | pytest 全量（store 属性访问路径） |
| m-4 | 联邦查询逐源串行 for-await → `asyncio.gather` 并行（merge 阶段仍同步，任一源失败整体失败语义不变） | pytest（联邦用例） |
| m-5 | 删除 types.py 死代码（`TYPE_MAP`/`get_default` 等全仓库零引用已确认） | pytest 全量（导入链无残留） |
| m-9 | pyproject 补 `[tool.pytest.ini_options]`（testpaths / pythonpath=["src"] / addopts）+ dev extras（pytest>=8） | `LOCAL_CORE=1` 下 `python -m pytest` **零手工 PYTHONPATH** 复跑通过 |
| m-10 | README 补「Development」章节：测试命令、LOCAL_CORE 开发兜底、e2e 自动 skip 行为、Host 层架构边界 | — |

### 回补后评分

| # | 维度 | 第 1 轮 | 第 2 轮 | 回补依据 |
|---|------|--------|--------|----------|
| 1 | 功能正确性 | 12.0 | **14.0** | M-1 +1.5（批量主键冲突根因消除）、M-3 +0.5 |
| 2 | 可靠性 | 7.5 | **8.5** | m-2 +0.5、m-3 +0.5（M-2 无事务未闭环，主扣分保留） |
| 3 | 安全性 | 14.8 | **15.0** | M-1 +0.2（ID 可预测性回补） |
| 4 | 性能效率 | 7.5 | **9.0** | M-3 +1.0（limit 下推）、m-4 +0.5（联邦并行） |
| 5 | 可维护性 | 13.5 | **14.5** | m-1 +0.5（前缀匹配）、m-5 +0.5（死代码清除） |
| 6 | 可读性与规范 | 9.4 | 9.4 | 不变 |
| 7 | 测试质量 | 8.9 | **9.4** | m-9 +0.5（pytest 配置可重复性） |
| 8 | 文档与可理解性 | 4.0 | **5.0** | m-10 +1.0（README 开发章节） |
| 9 | 架构与设计 | 10.0 | 10.0 | 不变 |
| — | 小计 | 87.1 | **94.8** | |
| + | 亮点加分 | +3.5 | +3.5 | |
| — | **总分** | **91** | **98（S 卓越）** | 定向复评口径 |

### 遗留项（第 3 轮候选）

- M-2 多步写入事务/补偿（SQL 路径事务包裹 + 「mutation 非原子」信任边界文档化）—— 首位
- m-6 Store API 收敛单轨（全显式 vs `_store_map`+`__dir__` 二选一）；m-8 ruff + mypy 接入
- I 级备查项：I-1 `$pipeline` 默认值策略、m-7 `scoped_connections`、I-4 core 方言不变式成文

### 第 3 轮 · 定向复评（2026-09-12）

> 闭环：**M-2 多步写入事务化**（与 nodejs-store 同方案、同语义对齐）
>
> - 执行器事务化：asyncmy（显式 `BEGIN` + `commit/rollback`，对 autocommit 任意配置确定成立）、
>   asyncpg（pool → `acquire()` 专用连接 + `conn.transaction()`）、aiosqlite（显式 `BEGIN` + commit/rollback）；
>   整个 plan 固定在同一连接上执行（池路径语句顺序与连接一致性同步修正），多语句 plan 本身即事务原子；
> - 步骤序列事务化：datasource 新增 `run_in_transaction(source, fn)`（contextvars 作用域连接覆盖），
>   mutation 父子步骤、remove 归档+删除在**单一 SQL 源**时整体落同连接同事务，任一步失败整体回滚；
>   Mongo 源 / 跨源步骤按原样顺序执行（README 新增「Transaction boundary」显式声明非原子信任边界，绝不静默假装已事务化）；
> - 归档幂等：remove 归档命令携带 `upsertById`（core 侧产出），Mongo 逐条 `replace_one(upsert)`，
>   SQL 由 dialect `ON CONFLICT/ON DUPLICATE/INSERT OR REPLACE` 承接 —— 「归档成功但删除失败」的重试不再整批失败。
>
> 验证：`python -m pytest` 78/78 全绿（含 MySQL/PG/Mongo 真实库 e2e + sqlite，事务路径实测）。
> 评分影响：维度 2 可靠性 +1.0（8.5 → 9.5，主扣分 M-2 关闭）——
> 小计 95.8 + 亮点 3.5 = **99（S 卓越，定向复评口径）**；m-6/m-8 保留扣分不变。

### 第 4 / 5 轮 · 定向复评（2026-09-12）

> 第 4 轮闭环：**m-8 工程基线**（`[tool.ruff]` E/F/W/I/B/RUF + `[tool.mypy]` 宽松起步接入，
> 存量违规清零；详见 m-8 行状态）。
>
> 第 5 轮闭环：**m-6 Store API 收敛单轨**（全显式类方法，消除双轨）。
>
> - `_store_map`（40+ 动态条目）与 `Store.__getattr__` **整体删除**：拼错属性不再经动态回退，
>   直接走 Python 默认 `AttributeError`（m-3 的协议修复改由结构保证，而非依赖 try/except 兜底）；
> - CRUD 保留带类型标注的显式类方法；驼峰别名（`queryOne`/`queryWithCount`/`queryFederated`/
>   `insertMany`/`updateMany`/`syncSchema`/`buildPipeline`）在类体内 `= 同名蛇形方法`
>   指向**同一函数对象**（单一实现，非两处维护）；其余 API（schema 管理 / 权限上下文 /
>   `$pipeline` 与上下文开关 / 反馈通道 / 数据源连接）以 `staticmethod(实现函数)` 显式绑定，
>   杜绝实例化后的 `self` 注入；
> - 全属性静态可见：IDE 补全与静态检查恢复，新增 API 只需定义一次（对齐 nodejs-store 全显式 Store 风格）。
>
> 验证：`python -m ruff check src tests` 全通过、`python -m mypy src/py_store` 0 错误、
> `LOCAL_CORE=1 python -m pytest` **83/83 全绿**（含改写的 `test_store_camel_and_snake_aliases`：
> 断言驼峰/蛇形 `__func__` 为同一实现，并校验 `hasattr(store, 'quer') is False`）。
> 评分影响：维度 5 可维护性 +0.5（14.5 → 15.0 封顶，m-6 关闭）——
> 小计 96.3 + 亮点 3.5 = **99.8 → 100（S 卓越，定向复评口径）**。

### 第 6 轮 · 收尾评估（2026-09-12）

> 结论：**剩余 Info / 备查项不做「为消分而改」**，仅落地零风险小项后收尾。
>
> - 已落地（I-3）：`run_as_internal` 统一为「先调用后 `isawaitable(out)` 判定」，与
>   `crud/query.py::_finalize` 一致（对 partial/包装协程不漏判）；`import inspect` 提到模块顶部。
> - 已落地（I-2）：`route_override` 受信参数红线补入 README 与 `crud/query.py::query` docstring
>   （禁止透传用户输入，CWE-639）；**不加校验代码**——跨源路由是合法能力，校验属宿主职责。
> - 已评估维持现状：I-1（默认放行是三端 parity 的既定语义，改默认值属 breaking change）、
>   I-4（core 直出 `%s` 属改跨端契约，为消一个已注释例外面不划算）、I-5（真实后端 e2e 已兜底）、
>   I-6（`.gitignore` 已忽略的本地缓存，非仓库缺陷）、m-7（模块级单例为与 nodejs-store 对齐的既定设计）。
> - 判定原则：属「既定设计取舍」或「重复防线」的项不做变更，避免引入回归风险。
>
> 验证：`python -m ruff check src tests` 全通过、`python -m mypy src/py_store` 0 错误、
> `LOCAL_CORE=1 python -m pytest` **83/83** 全绿。
> 评分影响：无（I 级不在扣分口径内；问题清单仅余 I-1/I-4/I-5/I-6 与 m-7，均为「已评估维持现状」）。

### 收尾结论

- **Blocker / Critical / Major：全部清零。**
- **Minor：m-1~m-6 / m-8~m-10 已整改；m-7 已评估维持现状（含成文理由）。**
- **Info：仅余 I-1 / I-4 / I-5 / I-6，均已评估并维持现状。**
- 无剩余需修复的动作项。

---

### 第 7 轮 · 最终全量评测（2026-09-12）

#### 评测口径（与第 2~6 轮「定向复评口径」的区别）

- 第 2~6 轮为**定向复评口径**：只复评已登记问题项（M-x / m-x / I-x）的闭环情况，未登记项沿用前轮结论，不做全量重扫 → 分数单调收敛至 100。
- 本轮为**项目评审 · 全量口径**：不预设问题清单，对九个维度重新做「全量脚本核查 + 抽样深度核查」，逐维度独立打分；历史问题仅作对比参考，**不继承其分数**。
- 范围：整个 `py-store` 仓库当前状态（排除 `.venv/`、`__pycache__/`、`.pytest_cache/`、`*.egg-info`）。
- 语言 / 栈：Python 3.10+，PyO3 绑定（`rust-store-py`）+ Rust core，pytest + ruff + mypy。

#### 证据采集（实测，2026-09-12）

| 项 | 命令 | 结果 |
|---|---|---|
| 静态检查 | `python -m ruff check src tests` | **All checks passed!**（exit 0） |
| 类型检查 | `python -m mypy src/py_store` | **Success: no issues found in 22 source files**（exit 0） |
| 单元 / 契约 | `$env:LOCAL_CORE='1'; python -m pytest -q` | **83 passed**（串行稳定；≥11 次重复全绿） |
| 覆盖率 | `python -m pytest --cov=py_store` | **88%**（955 stmts / 115 miss） |
| 规模 | — | src 22 个 `.py` / 2122 行；tests 6 个 `.py` / 1870 行；`def` 138 |
| 注解 / 豁免 | — | `# type: ignore` src=tests=**0**；`# noqa` src=0；TODO/FIXME src=0；裸 `except:` src=0 |
| 环境 | — | Python 3.14.4 / ruff 0.16.6 / mypy 2.3.1 / pytest 9.1.1（满足 `requires-python>=3.10` 与 dev extras） |

> **稳定性核查（本轮关键新证据）**：**串行**运行 83/83 全绿（反复验证）；**两个 pytest 进程并发**（含与 `--cov` 会话并发）→ `52 passed, 31 errors`（100% 复现）；跨仓库并发（`py-store` 与 `nodejs-store` e2e 同名库）→ `assert 4 == 2` / `assert 2 == 1` 偶发污染。根因见 M-4。

#### 九维评分表

| 维度 | 满分 | 得分 | 得分率 | 主要依据（本轮重新核查） |
|---|---|---|---|---|
| 1 功能正确性 | 15 | 15.0 | 100% | `query_one` 下推 `$limit(1)`；`_generate_id` 用 `secrets.choice`；`mongo_db` 双形态严格校验；`_kind_of` 只认 Mapping（避 PyMongo `__getattr__` 陷阱）；`_to_pyformat` 跳过引号内 `?`；SQLite `zip(strict=True)`；`sync.py` 的 `dict(d, datasource=/namespace=)` 实测为「覆盖同名键」而非 `TypeError`（`dict({'namespace':'x'}, namespace='y') → {'namespace':'y'}`）；ruff/mypy 0 告警。**无新缺陷** |
| 2 可靠性 | 10 | 10.0 | 100% | single SQL source 事务化（`run_in_transaction`）、归档 upsertById 幂等、`PushdownUnsupportedError` + feedback 事件、`_dev_fallback` 需 `LOCAL_CORE=1` 且非 production；Mongo 多步非原子已成文设计取舍（README「事务边界」）。**无新产品缺陷** |
| 3 安全性 | 15 | 15.0 | 100% | 全路径参数化（`%s`/`$n`/`?`）；MySQL introspect `base.replace` 仅替换**内部常量模板**（值仍走参数）、SQLite `_quote` 引号转义且标识符源自 `sqlite_master`（非用户输入）；`route_override` 受信红线已文档化（README + docstring，CWE-639，即历史 I-2）；fail-secure `set_require_context`；`secrets` 生成 ID；无硬编码密钥 |
| 4 性能效率 | 10 | 10.0 | 100% | `$limit(1)` 下推；`query_federated` `asyncio.gather` 并行；pageSize 上限 5000；连接池复用；归档 upsertById 逐条 `replace_one` 为幂等取舍（`executors/mongo.py:31-34` 注释成文），非缺陷 |
| 5 可维护性 | 15 | 15.0 | 100% | 单文件最大 241 行（`datasource.py`）< 300 阈值；无 `type: ignore`/`noqa`/TODO/FIXME/裸 `except`；mypy `check_untyped_defs` 开启；覆盖率 88% |
| 6 可读性与规范 | 10 | 10.0 | 100% | ruff（E/F/W/I/B/RUF）0 违规；snake/camel 为**同一函数对象**别名 + 其余 `staticmethod` 显式绑定（无 `__getattr__` 动态查找）；命名与 `nodejs-store` 对齐 |
| 7 测试质量 | 10 | **6.9** | 69% | 83 用例 / 88% 覆盖 / 四后端 e2e + Host 契约对拍；**新发现隔离与门禁缺陷**（M-4、m-11、m-12、I-7 → 合计 −3.1） |
| 8 文档与可理解性 | 5 | 5.0 | 100% | README 255 行，API 与代码逐条核对（`store.set_feedback_sink` / `store.set_allow_user_pipeline` 均存在）；CHANGELOG 1.0.0 含破坏性变更 + 迁移指引；失败/降级/路由红线/事务边界均成文 |
| 9 架构与设计 | 10 | 10.0 | 100% | Rust 单核心 + 双宿主同构（core-py / core-node）；Host 薄层（驱动 IO + 回调 + 占位符）；`rust-store/fixtures/host` 跨语言契约对拍；`(source, namespace, collection)` 三元组定位且唯一性 fail-fast |

#### 总分

- Σ 维度分 = **96.9**
- 亮点加分 = **+3.5**（Rust 单核心双宿主同构 / Host 跨语言契约 fixture 对拍 / 多后端同一定位三元组 / fail-secure 开关 / feedback 降级通道）
- 原始 = 100.4 → 按规则**封顶** → **总分 100 / 等级 S 卓越**

> 透明性说明：因「封顶 100」规则，本轮在测试维度扣掉的 3.1 分未体现在最终分上；问题清单原样保留，供排期整改。

#### 分级问题清单

**Major**

| 编号 | 位置 | 问题 | 标准出处 | 扣分 | 修复建议 | 状态 |
|---|---|---|---|---|---|---|
| **M-4** | `tests/test_federation_e2e.py:28-34,84,128,148,175-179`；`tests/test_real_backends_e2e.py:38-93`（对拍 `nodejs-store/tests/federation-e2e.test.js:28-34,56,92,111`） | e2e 套件硬编码**固定共享外部库/表**（`mongo_store_e2e` 的 `fed_users`/`fed_orders` 等），无进程级 / 仓库级隔离。两个 pytest 进程并发（或 py-store 与 nodejs-store 同名库同时跑）即 `52 passed, 31 errors`（100% 复现）；跨仓库 `_reset()` 清表竞态导致 `assert 4 == 2` / `assert 2 == 1` 偶发污染 | ISTQB F.I.R.S.T（Independent / Repeatable / Self-validating）；ISO 25010 可靠性·可测试性（7.3） | −2 | 库名/表名加仓库+进程后缀（如 `mongo_store_py_e2e_${pid}`）或随机命名空间，`_reset()` 后校验；禁止跨仓共享固定库 | 新增 |

**Minor**

| 编号 | 位置 | 问题 | 标准出处 | 扣分 | 修复建议 | 状态 |
|---|---|---|---|---|---|---|
| m-11 | `tests/test_federation_e2e.py:182-202`；`tests/test_real_backends_e2e.py:299-319` | 模块级 `asyncio.new_event_loop()` + `asyncio.set_event_loop()` 叠加进程级可变全局单例（`schema._schemas`、`datasource._connections`），造成测试间隐式顺序耦合，无法进程内并行（pytest-xdist / 线程并行） | ISTQB 测试独立性；SonarQube「全局可变状态」 | −0.5 | 改用 `pytest-asyncio`/`anyio` 或 function-scoped loop；提供 registry/connections 重置 fixture | 新增 |
| m-12 | `.github/workflows/release-pypi.yml:30-62` | 发布流水线仅 `build` + `publish`，无 ruff / mypy / pytest 质量门禁，发版前无自动化校验 | DevOps 质量门禁；ISO 25010 可维护性 | −0.5 | 增加 `test` job（ruff + mypy + pytest --cov）并作为 `publish` 的 `needs` | 新增 |

**Info**

| 编号 | 位置 | 问题 | 标准出处 | 扣分 | 修复建议 | 状态 |
|---|---|---|---|---|---|---|
| I-7 | `tests/test_host_contract.py:26-33` | 依赖仓库外 fixture `../../rust-store/fixtures/host/*.json`，文件缺失时 `open()` 抛 `FileNotFoundError` 而非 `pytest.skip`；单仓 clone / 单包 CI 场景硬失败 | ISTQB 可重复性 | −0.1 | 路径不存在时 `pytest.skip('缺少 rust-store fixture')` | 新增 |

> 历史遗留（I-1 / I-4 / I-5 / I-6、m-7）：已于第 6 轮评估「维持现状（既定设计取舍，含成文理由）」，本轮复核结论不变，不重复扣分。

#### 与前轮（100 分定向复评口径）的对比

| 轮次 | 口径 | 总分 | 主要变化 |
|---|---|---|---|
| 第 1 轮 | 项目评审（首评） | 91 | 基线 + M/m/I 清单 |
| 第 2~5 轮 | 定向复评 | 98 → 99 → 100 | 逐项闭环 M-1/M-2/M-3、m-1~m-10、m-6、m-8 |
| 第 6 轮 | 收尾评估 | 100 | I-2/I-3 落地；其余维持现状 |
| **第 7 轮** | **项目评审（全量）** | **100（S）** | 独立复评：**产品代码（src/）无新缺陷**；**新增 1 Major + 2 Minor + 1 Info，全部集中在测试工程维度**（前几轮定向口径未覆盖） |

差异说明：本轮得分与前轮同为 100，但**并非沿用**——Σ 维度分实为 96.9（测试维度 −3.1），经亮点加分与封顶规则后为 100。差异来源即口径：定向复评只看已登记项闭环，全量评审才暴露 e2e 隔离与 CI 门禁缺口。

#### 结论

- **Blocker / Critical：清零。**
- **Major：新增 1 条（M-4 测试隔离）**，属测试工程质量问题而非运行时代码缺陷——仅在并发 / 跨仓并行跑测试时触发，不影响 `pip install storepy` 的运行时行为；建议排期整改（库名隔离 + CI 门禁）。
- Minor：m-11 / m-12（新增）；历史 m-1~m-10 已闭环，m-7 维持现状。
- Info：I-7（新增）；历史 I-1 / I-4 / I-5 / I-6 维持现状（已评估）。
- 产品代码 **src/ 本轮未发现新增正确性 / 安全性 / 性能 / 架构缺陷**；封顶后 **总分 100（S 卓越）**。
