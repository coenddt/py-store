# py-store 测试评测报告

> 本报告由 `.trae/skills/test-evaluation` SKILL 流程产出：九维检查项清单 → 全量实跑 → 达成度判定 → 缺陷定级 → 评分。
> 证据根目录（下称 `E`）= `f:\独立开发者\项目\mongo-store\tmp\test-eval\`，全部原始日志落盘于 `E*.log / E*.out`。

## 0. 元信息

| 项 | 值 |
|---|---|
| 评测对象 | `py-store`（mongo-store 多后端数据层 Python 薄 Host），分发名 **storepy**，版本 **1.0.0**，commit **92571c1** |
| 评测范围 | 全仓（`src/py_store` 全模块 + `tests/` + `stress/` + `.github/workflows/` + `README.md` / `pyproject.toml`），不改动被测代码 |
| 评测维度 | 九维（功能 / 边界 / 组合 / 交互 / 压力 / 兼容 / 安全 / 回归 / 测试工程） |
| 评测环境 | Windows（本机）；**Python 3.14.4**；MySQL 8.0.42 / PostgreSQL 16.4 / MongoDB 8.0.12 / SQLite 3.50.4；Node v25.1.0（core 探针经 core-node 直驱）；原生核心 `rust-store-py` 1.0.0（与 `rust-store/core` 同源，`LOCAL_CORE=1` 本地兜底） |
| 实测执行 | 约 20 条命令 / 探针（83 项 pytest 全量 ×5 轮、31 项真实库 E2E 参数化、23 项宿主补充探针、51 项 core 探针、4 进程并发验证、2 档压测 ×3 轮、覆盖率 / lint / mypy / audit）；日志见 §7；评测日期 **2026-09-12**（实跑），报告定稿 2026-09-13 |
| 评测标准 | ISO/IEC/IEEE 29119-1..4、ISO/IEC 25010/25023、ISTQB CTFL 4.0、OWASP Top 10 2021 / ASVS / WSTG、MITRE CWE Top 25、F.I.R.S.T、SonarQube Quality Gate、DORA |
| 本轮总分 | **64.2 / 100（C 合格）** |

> **架构前提**：py-store 为薄 Host，查询/权限/管道规划等核心逻辑全部来自共享的 Rust 单核心（经 `rust-store-py` 绑定）。因此 core 级缺陷（D-01/D-02/D-03/D-06/D-07/D-08）在本仓同样成立，本报告对其**负连带责任**。

## 1. 执行摘要

- **一句话结论**：全量 83 项测试五连跑零 flaky、四库真实 E2E 全绿、语句覆盖 88%、mypy 22 文件全过、README 测试与事务边界文档三仓最完备——宿主工程质量良好；但**仓库没有任何 CI 测试工作流**（仅 release-pypi.yml，门禁完全缺失），叠加共享核心两条 Critical（`$where` 条件静默丢弃、GQL 关系带参+子选择集解析失败），判「合格」。
- **最严重问题（≤3 条）**：
  1. **D-02（C）**：`$where` 恶意载荷使查询条件**静默丢弃**，SQLite 与 MongoDB 双路径均返回全量数据（`rows=85`）且无告警事件——结果静默错误（§4）。
  2. **D-01（C）**：README 记载的「关系带参 + 子选择集」GQL 语法解析失败（`期望 id(undefined) 实际 p({)`），与 Node 侧表现一致（§4）。
  3. **D-11（M）**：`tests/test_real_backends_e2e.py` 固定表名 + 破坏性 DDL，两进程并发执行时 32 个用例互踩报 `Table 'my_posts' already exists`，E2E 不具备并行安全性（§4）。
- **最突出亮点（≤3 条）**：
  1. 测试文档三仓最完备：README **Development** 章节（运行命令、外部库不可达自动 skip 语义、「thin Host…Keep it that way」架构约束）+ **Transaction boundary** 五条事务边界契约（`README.md:231-251`）。
  2. 83 项测试 ×5 轮复跑全部通过（1.42–1.51 s），**flaky 率 = 0**（`Epy-repeat5.log`）；单文件独立执行 4/4 exit 0（`Epy-independence.log`）。
  3. 负向用例成体系：require-context 五态、多数据源冲突注册拒绝、`exec_sql` 下推不支持显式报错并发射反馈事件、权限字段级白名单（`tests/test_require_context.py` / `test_multi_datasource.py` 等）。
- **与上一轮对比**：仓库内既有 `doc/2026-09-12-测试报告.md` 与 `doc/code-review/2026/09/已完成-py-store评测报告.md`。本轮为**首次九维实跑评测**，不可直接比对分数；性能项与既有压测基线（824.35 QPS）受评测环境干扰不可比（见 §3.5 注）。

## 2. 评分卡

| # | 维度 | 满分 | 达成率 R | 缺陷扣分 P | 维度分 | 主要失分原因 |
|---|------|------|----------|------------|--------|--------------|
| 1 | 功能正确性 | 15 | 0.833 | 5.5 | **7.00** | D-01（C，−5）；幂等性缺回归（m，−0.5）；F3/F5/F7 仅部分覆盖 |
| 2 | 边界值与极值 | 12 | 0.636 | 0.5 | **7.14** | `$limit` 非法类型未校验（m，−0.5）；上限+1、宿主层字符串/深度/时间极值无用例 |
| 3 | 组合与等价类 | 10 | 0.800 | 0 | **8.00** | 状态迁移/类型组合/异常叠加/剪裁说明仅部分覆盖 |
| 4 | 交互与集成 | 12 | 0.875 | 2.0 | **8.50** | D-03（M，−2，绑定序列化边界）；I9 并发隔离未通过；I6/I7 部分覆盖 |
| 5 | 压力与性能 | 12 | 0.750 | 0 | **9.00** | 无 p99、无分段/浸泡、无性能门禁（S10=0，无 CI）；吞吐基线对照受评测环境干扰不可归因（§3.5 注） |
| 6 | 兼容性 | 10 | 0.727 | 0 | **7.27** | 运行时/平台/后端版本单点验证；无 CI → 兼容矩阵无法自动化（M9=0） |
| 7 | 安全测试 | 12 | 0.833 | 5.5 | **4.50** | D-02（C，−5）；联邦上限错误文案错乱（m，−0.5）；X5 部分覆盖 |
| 8 | 回归与质量门禁 | 9 | 0.500 | 0 | **4.50** | **无 CI 测试工作流（G3=0，G4=0，G9=0）**；覆盖率 88% 未达 90 且无分支口径（G5=0.5）；发布前不跑测试（G6=0.5） |
| 9 | 测试工程与可复现性 | 8 | 0.846 | 2.0 | **4.77** | D-11（M，−2，并行安全/数据隔离）；T1 独立性仅部分验证 |
| — | 小计 | 100 | — | 15.5 | **60.68** | |
| — | 亮点加分 | ≤5 | — | — | **+3.50** | 黄金对拍 +2；负向体系 +1.5 |
| — | **总分** | 100 | — | — | **64.2** | |

等级：**C 合格**；否决项核查：**无**（V1–V5 均未命中，详见 §3.7）。
> 等级约束核对：本仓存在 2 条 Critical（维度 1、7）→ 按 §6 等级映射，「B 良好」要求 `C ≤ 1` 不成立；本轮 64.2 落在 C 区间，判 C 自洽。

## 3. 维度明细

### 3.1 功能正确性（满分 15）

| # | 检查项 | w | d | 加权 | 实测证据（命令/日志） | 说明 |
|---|--------|---|---|------|----------------------|------|
| F1 | 读路径闭环 | 2 | 1.0 | 2.0 | `Epy-pytest-all.log`（`crud.query/query_one/query_with_count/exists/count` 全绿）；`Epar-py-a.out` 31 项 E2E 参数化全过 | 断言具体字段值与条数 |
| F2 | 写路径闭环 | 2 | 1.0 | 2.0 | `test_crud_insert/update/update_many/remove/mutation/upsert` 全套 + 写后回读；E2E 同套四后端 | 含 set/raw 两模式与归档删除 |
| F3 | GQL/查询语义 | 2 | 0.5 | 1.0 | 正向：`test_federation_e2e.py` 跨源嵌套+计算列、单源同形；反向：**D-01**（`Edefect-d01-recheck-py.log` V3/V4 ERR；探针 B-07 `位置 15`） | 关系「带参+子选择集」不可用 |
| F4 | 写入派生语义 | 1 | 1.0 | 1.0 | `test_crud_insert_autoid_timestamp` + `test_schema_timestamp_unit_seconds_insert / _ms_default / _rejects_invalid_timestamps` | 秒/毫秒两态 + 非法拒绝 |
| F5 | 事务与多步原子性 | 1 | 0.5 | 0.5 | README **Transaction boundary** 五条（`README.md:245-251`）；`test_crud_remove_archives`（归档+删除终态） | 契约有文档；无失败注入用例 |
| F6 | 错误语义 | 1 | 1.0 | 1.0 | `Epy-probe-bcise.log:9`（PermissionError: 无写入权限）、`:10`（数据源未配置: not_registered_src…）；`B-06`（UNIQUE constraint failed: probe_lite_items_py._id）；`test_crud_update_empty_set_raises` | 断言具体错误类型/信息 |
| F7 | 幂等性 | 1 | 0.5 | 0.5 | `test_crud_upsert_by_condition / _generates_id`；归档 upsert-by-`_id` 幂等（README） | 重复 register 同名 schema 产生重复条目（**D-06**，core 继承） |
| F8 | 真实依赖下的正确性 | 1 | 1.0 | 1.0 | `Epy-pytest-all.log`：83 passed、**0 skipped**（真实 MySQL/PG/Mongo 可达，SQLite 内存库） | 四后端全 CRUD 实跑 |
| F9 | 跨实现一致性 | 1 | 1.0 | 1.0 | `Erust-verify-fixtures.log`：Rust core / core-node / **core-py** 三侧复算 **3/3 一致**（core-py 即本仓依赖的原生核心） | 冻结黄金基准 |

- **R** = Σ(w×d)/Σw = 10.0 / 12 = **0.833**；**P** = 5.0（D-01）+ 0.5（D-06）= **5.5**；**维度分** = 15×0.833 − 5.5 = **7.00**
- 本维度缺陷：D-01（C）、D-06（m）

### 3.2 边界值与极值（满分 12）

| # | 检查项 | w | d | 加权 | 实测证据 | 说明 |
|---|--------|---|---|------|----------|------|
| B1 | 数值边界 | 1 | 0.5 | 0.5 | `Epy-probe-bcise.log:6,22`（`$limit 1e9` 截断 rows=4；重复 `_id` 报 UNIQUE 约束） | `$limit="abc"` 未被校验（**D-07**，core 继承） |
| B2 | 空与缺省 | 2 | 1.0 | 2.0 | `Epy-probe-bcise.log:2,12`（空串/0 写入回读，`title=''`、`views=0` 断言；sqlite+mongo 双路径） | 回读字段断言具体值 |
| B3 | 集合长度边界 | 1 | 0.5 | 0.5 | `test_crud_insert_many_empty / _fills_ids` | 批量上限（超限批量）无用例 |
| B4 | 分页与游标边界 | 2 | 0.5 | 1.0 | `test_crud_query_with_count_pagesize_cap`（5000 上限断言）；`Epy-probe-bcise.log:6`（1e9 截断） | 宿主层缺「上限+1」显式断言（core 探针 B-21/B-22 已验 clamp） |
| B5 | 字符串边界 | 1 | 0.5 | 0.5 | core 探针 B-09（1MiB 保真）/ B-10（emoji/中文/零宽/组合字符保真）通过（`Erust-probe-bcise.log`） | 宿主层无字符串极值用例，仅 core 级证据 |
| B6 | 时间边界 | 1 | 0.5 | 0.5 | `test_schema_timestamp_unit_seconds_insert / _ms_default / register_rejects_invalid_timestamps` | epoch / 负时间戳 / 时区未覆盖 |
| B7 | 结构深度边界 | 1 | 0.5 | 0.5 | core 探针 B-06（深度 10 通过）；宿主层无深度用例 | 探针 B-07/B-08 深度用例受 D-01/构造缺陷阻断（§8） |
| B8 | 状态与资源边界 | 1 | 0.5 | 0.5 | `Epy-probe-bcise.log:10`（routeOverride 指向未注册 source 明确报错） | 超时 / 断连 / 重试 / 超长标识符未覆盖 |
| B9 | 边界断言质量 | 1 | 1.0 | 1.0 | `B-06` 断言具体约束错误串；`X-02` 断言完整错误消息 | 断言具体值/错误，非「不崩溃」 |

- **R** = 7.0 / 11 = **0.636**；**P** = 0.5（D-07）；**维度分** = 12×0.636 − 0.5 = **7.14**
- 本维度缺陷：D-07（m）

### 3.3 组合与等价类（满分 10）

| # | 检查项 | w | d | 加权 | 实测证据 | 说明 |
|---|--------|---|---|------|----------|------|
| C1 | 等价类划分显式性 | 1 | 1.0 | 1.0 | `test_real_backends_e2e.py:452-502`：8 用例 × `@pytest.mark.parametrize('ctx', CRUD_CTXS, ids=_IDS)`，划分维度可直接读出 | |
| C2 | 多参数组合覆盖 | 2 | 1.0 | 2.0 | 7 CRUD × 4 后端 + syncSchema × 3 = 31 参数化组合（`Epar-py-a.out` 全过）；探针 sqlite×mongo 双路径 | |
| C3 | 权限/规则决策表 | 2 | 1.0 | 2.0 | core 探针 `Erust-probe-bcise.log:27-41`：5 角色 × 读/写 + `requireContext × ctx × 读/写` 8 例决策表；本仓 `test_perm_schema_read_write / _owner_condition / _scoped_roles` | 含拒绝分支与具体报错 |
| C4 | 状态迁移组合 | 1 | 0.5 | 0.5 | `Epy-probe-bcise.log:23`（remove 后 `left=0 archived=1` 终态） | 缺非法迁移（对已删记录再更新/再删）负向用例 |
| C5 | 类型组合 | 1 | 0.5 | 0.5 | string / number 两型覆盖（B-01/B-02） | bool / date / object / null 型 × 后端组合不足 |
| C6 | 配置/开关组合 | 1 | 1.0 | 1.0 | `test_require_context.py` 五态（默认关 fail-open / 开且无 ctx 阻断 / system 放行 / user 放行 / 关恢复）；`test_store_pipeline_switch_blocks_and_restores` | 开关两态 × 读写全组合 |
| C7 | 异常组合 | 1 | 0.5 | 0.5 | `test_pushdown_unsupported_error_feedback` + `test_exec_sql_unsupported_raises_and_emits`（反馈事件通道） | 多失败点叠加（写失败+回滚失败）无 |
| C8 | 组合剪裁说明 | 1 | 0.5 | 0.5 | `pyproject.toml` pytest 分层配置 + README Development 说明 skip 语义 | 无显式组合策略/剪裁文档 |

- **R** = 8.0 / 10 = **0.800**；**P** = 0；**维度分** = **8.00**
- 本维度缺陷：无

### 3.4 交互与集成（满分 12）

| # | 检查项 | w | d | 加权 | 实测证据 | 说明 |
|---|--------|---|---|------|----------|------|
| I1 | 测试分层结构 | 2 | 1.0 | 2.0 | 单元/契约 50 项（test_py_store 32 + multi_datasource 9 + require_context 5 + host_contract 4）+ E2E 10 项（real_backends 8 + federation 2），共 83 collected | 分层清晰，未失衡 |
| I2 | 契约测试 | 2 | 1.0 | 2.0 | `test_host_contract.py`：`resolve_placeholders / truthy / new_id_pool / callback_bridge`；黄金对拍 3/3 | 有共享 fixture 消费链路 |
| I3 | 全链路集成 | 1 | 1.0 | 1.0 | `Epy-probe-bcise.log:23`（insert→remove→主表/归档表终态）；`test_federation_e2e.py` 跨源嵌套+计算列最终值断言 | |
| I4 | 真实依赖集成 | 2 | 1.0 | 2.0 | `Epy-pytest-all.log` 83 passed 0 skipped：MySQL / PostgreSQL / MongoDB 真实实例全 CRUD | 连接信息见 §0 环境 |
| I5 | 异步与回调交互 | 1 | 1.0 | 1.0 | `test_callback_bridge_fn_and_asyncfn`；`test_feedback_sink_and_default_stderr` | fn + asyncFn 双形态 |
| I6 | 事务与连接交互 | 1 | 0.5 | 0.5 | README 事务边界五条（单 SQL 源命令序列单连接事务内可回滚等） | 文档有、失败注入无；连接泄漏未验证 |
| I7 | 测试替身保真度 | 1 | 0.5 | 0.5 | 测试以真实库为主、mock 少 | **D-03** 证明「绑定序列化边界」无替身/断言覆盖（`Eprobe-py-iso2.log`：`TypeError: 不支持的 Python 类型`） |
| I8 | 跨进程/并发交互 | 1 | 1.0 | 1.0 | `Epy-probe-bcise.log:7,8`（Promise 式 30 并发读写一致、50 并发 ID 唯一）；`Emultiprocess-verify.log`（4 进程 160 条：唯一性/无丢失/分布 3/3 PASS，py-a/py-b 各 40） | |
| I9 | E2E 数据隔离与可重复 | 1 | 0.5 | 0.5 | `Epar-py-a.out`（进程 A 31 项全过）/ `Epar-py-b.out`（进程 B 32 ERROR） | 有并发实跑证据但**未通过**（**D-11**） |

- **R** = 10.5 / 12 = **0.875**；**P** = 2.0（D-03）；**维度分** = 12×0.875 − 2.0 = **8.50**
- 本维度缺陷：D-03（M）；D-11 归属维度 9，避免重复扣分

### 3.5 压力与性能（满分 12）

| # | 检查项 | w | d | 加权 | 实测证据 | 说明 |
|---|--------|---|---|------|----------|------|
| S1 | 压测资产可运行 | 2 | 1.0 | 2.0 | `Epy-stress-8x200-rerun.log` / `Estress-smoke-tier.log`（`stress/stress.py` 输出 `[RESULT]` JSON） | 模型含联邦链路（federation_ops 分项） |
| S2 | 负载档位 | 1 | 1.0 | 1.0 | 冒烟 1×10（298.59）+ 负载 8×200 三轮（286.13 / 293.98 / 307.84） | 两档 × 多轮 |
| S3 | 指标完整性 | 1 | 1.0 | 1.0 | `qps / errors / errors_by_type / all_ops{p50,p95,max,avg} / federation_ops{...}` | 缺 p99 |
| S4 | 错误率 | 2 | 1.0 | 2.0 | 全部轮次 `errors=0`（8000 ops ×3） | 无错误可归因 |
| S5 | 吞吐基线对照 | 1 | 0.5 | 0.5 | 历史基线 **824.35 QPS**（`doc/2026-09-12-压测报告.md:36` @ 2026-09-12，本轮未复跑）→ 本轮 **286.13–307.84**；同轮对标 nodejs-store 560.62 / rust-core 279.57 | 评测时段机器被外部任务占满（CPU 100%、磁盘近满，用户确认）→ 与基线差异**不可归因于产品**，仅记录实测值 |
| S6 | 尾延迟分析 | 1 | 0.5 | 0.5 | `all_ops p50=25.16 / p95=39.45 / max=118.59 ms`；联邦 `p50=34.03 / p95=44.63` | 无 p99；max/avg≈4.6 分布紧凑 |
| S7 | 瓶颈定位 | 1 | 0.5 | 0.5 | 既有报告归因「吞吐瓶颈在本机单实例数据库侧」（`doc/2026-09-12-压测报告.md:63`） | 无 CPU/内存/事件循环观测证据 |
| S8 | 稳定性与泄漏 | 1 | 0.5 | 0.5 | 三轮独立 8×200 稳定（286–308 QPS） | 无前/中/后分段或浸泡 |
| S9 | 压测可信度 | 1 | 1.0 | 1.0 | 原始 `[RESULT]` 行落盘、命令可复现、表/库隔离（suffix `py`） | |
| S10 | 性能门禁 | 1 | 0.0 | 0.0 | 无 CI → `stress/stress.py` 不可能进流水线 | 无阈值断言 |

**吞吐对照表**

| 档位 | 规模 | QPS | errors | p50 (ms) | p95 (ms) | max (ms) |
|---|---|---|---|---|---|---|
| 冒烟 | 1×10 | 298.59 | 0 | 1.08 | 12.10 | 14.04 |
| 负载 | 8×200 | 286.13 / 293.98 / 307.84（三轮） | 0 | 24.61–26.71 | 39.45–45.76 | 118.59–349.77 |
| 历史基线 | 8×200 | **824.35**（来源：`doc/2026-09-12-压测报告.md:36` @ 2026-09-12，**本轮未复跑**） | — | — | — | — |

> 注：评测时段机器被外部任务占满（CPU 100%、磁盘近满，用户确认），三仓吞吐与历史基线的差异不作产品缺陷计分（见 §6 / §8）。

- **R** = 9.0 / 12 = **0.750**；**P** = 0；**维度分** = **9.00**
- 本维度缺陷：无

### 3.6 兼容性（满分 10）

| # | 检查项 | w | d | 加权 | 实测证据 | 说明 |
|---|--------|---|---|------|----------|------|
| M1 | 多后端等价性 | 2 | 1.0 | 2.0 | `Epar-py-a.out`：同一套 E2E 断言跑 MySQL/PG/Mongo/SQLite 四后端全绿 | 声明后端全部实跑 |
| M2 | 方言差异专项 | 2 | 1.0 | 2.0 | `test_real_backends_e2e.py:353`「PG/SQLite RETURNING，MySQL 两段编排」+ `:372` `$inc` | ≥2 项方言特性专项断言 |
| M3 | 多语言绑定等价性 | 1 | 1.0 | 1.0 | `Erust-verify-fixtures.log` 3/3 一致（含 core-py） | |
| M4 | 运行时版本兼容 | 1 | 0.5 | 0.5 | `requires-python >= 3.10`；本地实测 3.14.4 + release-pypi 用 3.12 构建 | 无版本矩阵 |
| M5 | 平台兼容 | 1 | 0.5 | 0.5 | `release-pypi.yml` 构建 manylinux/musllinux/windows/macos 四组 wheel；**测试**仅 Windows 本机 | 构建矩阵宽 ≠ 测试矩阵 |
| M6 | 后端版本兼容 | 1 | 0.5 | 0.5 | `Eenv-versions.log`：MySQL 8.0.42 / PG 16.4 / Mongo 8.0.12 / SQLite 3.50.4 | 仅单版本，无范围声明 |
| M7 | 编码与排序规则兼容 | 1 | 0.5 | 0.5 | MySQL 连接串 `charset=utf8mb4`；core 探针 B-10 Unicode 保真 | 宿主层无 Unicode 专项用例 |
| M8 | 向后兼容 | 1 | 0.5 | 0.5 | `test_b9_legacy_single_db_defaults`（旧单库 API 默认行为） | 有 legacy 用例，无完整旧用法回归专项 |
| M9 | 兼容矩阵自动化 | 1 | 0.0 | 0.0 | 无 CI → 无 OS/版本/DB 矩阵 | 纯手工单点验证 |

- **R** = 8.0 / 11 = **0.727**；**P** = 0；**维度分** = **7.27**
- 本维度缺陷：无

### 3.7 安全测试（满分 12）

| # | 检查项 | w | d | 加权 | 实测证据 | 说明 |
|---|--------|---|---|------|----------|------|
| X1 | 注入防御 | 2 | 0.5 | 1.0 | 正向：`B-06`（重复 `_id` 约束拦截）、字段白名单（`test_perm_readable_and_writable_fields`）；反向：**D-02** `Epy-probe-bcise.log:11,21`（`$where` 载荷 → `rows=85` 全量返回，sqlite+mongo 双路径） | 条件被静默丢弃，注入防御不成立 |
| X2 | 权限矩阵 | 2 | 1.0 | 2.0 | `Epy-probe-bcise.log:9`（guest 写被拒：`PermissionError: 无写入权限`，越权数据未落库）+ core 探针 5 角色决策表 + `test_perm_scoped_roles` | 含拒绝分支与具体断言 |
| X3 | 越权防御 | 2 | 1.0 | 2.0 | `Epy-probe-bcise.log:10`（未注册 source 明确报错、不静默回落）；`test_perm_owner_condition`（owner 条件注入）；`Edefect-d02-perm.log` 证明 Mongo 原生路径带 owner 条件时 `$where` 未越权 | 水平/垂直越权均有负向验证 |
| X4 | fail-secure 默认 | 1 | 1.0 | 1.0 | `test_require_context.py` 五态：默认 fail-open 且**显式文档化**（README/pyproject 注释），开启后缺 ctx 阻断、system/user 放行、关闭恢复 | 开关两态均有用例 |
| X5 | 恶意载荷 | 1 | 0.5 | 0.5 | **D-02**：`$where` 未被拦截；超深 GQL / 超大批量未覆盖 | 仅 1 类载荷且未通过 |
| X6 | 敏感信息暴露 | 1 | 1.0 | 1.0 | 错误消息为业务语义（「数据源未配置: not_registered_src（请检查 init(connections) 与 schema 的 datasource 绑定）」），不含 SQL 原文/表结构 | |
| X7 | 资源耗尽防御 | 1 | 0.5 | 0.5 | `test_crud_query_with_count_pagesize_cap`（5000 上限断言）；联邦行数上限错误文案错乱（**D-08**，core 继承） | 上限存在但错误语义不符 |
| X8 | 凭据与配置安全 | 1 | 1.0 | 1.0 | 测试连接串 `e2e/e2e123` 仅指向本地测试库；`release-pypi.yml` 走 OIDC Trusted Publishing，仓库内无 token | 风险等级：低 |
| X9 | 依赖漏洞 | 1 | 1.0 | 1.0 | `Epy-pip-audit-full.log`：57 条命中**全部位于 8 个环境级包**（js2py/pillow/pip/pypdf/setuptools/torch/transformers/weasyprint）；项目直接依赖（pymongo/asyncmy/asyncpg/aiosqlite/rust-store-py/pytest/ruff/mypy，见 `Epy-store-deps.txt`）**0 命中** | 环境噪声已甄别，非项目缺陷 |

- **R** = 10.0 / 12 = **0.833**；**P** = 5.0（D-02）+ 0.5（D-08）= **5.5**；**维度分** = 12×0.833 − 5.5 = **4.50**
- 本维度缺陷：D-02（C）、D-08（m）
- **否决项核查**：V1 要求「越权读到他人数据」。nodejs 侧对照实验（`Edefect-d02-perm.log` / `Edefect-d02-v1-mongo.log`）实测带 owner 条件的攻击载荷**未**读到他人数据；本仓探针 X-03 场景为公共集合上「条件静默丢弃」，构成「结果静默错误」，按 §3 判 **C**，不构成 V1。

### 3.8 回归与质量门禁（满分 9）

| # | 检查项 | w | d | 加权 | 实测证据 | 说明 |
|---|--------|---|---|------|----------|------|
| G1 | 测试套件全绿 | 2 | 1.0 | 2.0 | `Epy-pytest-all.log` 83 passed / 0 failed（exit 0）；`Epy-repeat5.log` 5×exit 0 | 见 §6 并发例外说明 |
| G2 | 回归基线 | 1 | 1.0 | 1.0 | 共享核心冻结黄金基准（`rust-store/fixtures/`）+ `Erust-verify-fixtures.log` 3/3 复算——core-py 即本仓 `rust-store-py>=1.0.0` 依赖的同一产物；本仓另有 host 契约测试 4 项 | 基线在共享核心侧，消费链路成立 |
| G3 | 门禁完整性 | 2 | 0.0 | 0.0 | `Erepo-git-and-workflows.log`：**仓库仅有 `release-pypi.yml`，无任何 CI 测试工作流**——「测试靠自觉」 | 本仓最大失分项 |
| G4 | CI 与本地一致性 | 1 | 0.0 | 0.0 | 无 CI → 一致性无从建立 | |
| G5 | 覆盖率门槛 | 1 | 0.5 | 0.5 | `Epy-coverage.log`：TOTAL 955 stmts / 115 miss / **88%** | 未达 90%；分支覆盖率未单独出具 |
| G6 | 发布流程校验 | 1 | 0.5 | 0.5 | `release-pypi.yml`：build + OIDC publish；**发布前不跑测试** | 半覆盖 |
| G7 | 变更可追溯 | 1 | 1.0 | 1.0 | `CHANGELOG.md` 存在；git log 7 条全 Conventional Commits；tag `v1.0.0` ↔ `pyproject.toml` 1.0.0 | |
| G8 | 缺陷回归固化 | 1 | 0.5 | 0.5 | 历史修复伴随用例（`fix: exec_sql 先做下推安全检查…` → `test_exec_sql_unsupported_raises_and_emits`）；但本轮 D-01/D-02 无回归用例 | 半覆盖 |
| G9 | 失败阻断能力 | 1 | 0.0 | 0.0 | 无 CI → 无失败阻断载体 | |

- **R** = 5.5 / 11 = **0.500**；**P** = 0；**维度分** = **4.50**
- 本维度缺陷：无。**V4 注**：仓库既有 `doc/2026-09-12-测试报告.md` 声称「四库真实实例全部通过」——本轮实跑证实串行执行下 E2E 确实全绿（83 passed 0 skipped），宣称与事实相符，不构成 V4。

### 3.9 测试工程与可复现性（满分 8）

| # | 检查项 | w | d | 加权 | 实测证据 | 说明 |
|---|--------|---|---|------|----------|------|
| T1 | 独立性 | 2 | 0.5 | 1.0 | `Epy-independence.log`：`test_py_store.py`（32 点）/ `test_host_contract.py`（4）/ `test_multi_datasource.py`（9）/ `test_require_context.py`（5）单文件运行均 exit 0 | 另 2 文件（E2E/联邦）未单独验证 |
| T2 | 可重复性 | 2 | 1.0 | 2.0 | `Epy-repeat5.log`：5 次全量 `83 passed`（1.42–1.51 s） | flaky 率 = 0 |
| T3 | 并行安全 | 1 | 0.5 | 0.5 | `Epar-py-a.out`（进程 A 31 全过）/ `Epar-py-b.out`（进程 B 32 ERROR） | 有并发实跑但产生额外失败（**D-11**） |
| T4 | 数据隔离 | 1 | 0.5 | 0.5 | `tests/test_real_backends_e2e.py:36-40` 硬编码 `DROP TABLE IF EXISTS my_posts` + `CREATE TABLE my_posts` 等固定表名 | 隔离机制不成立（D-11 根因） |
| T5 | 可移植性 | 1 | 1.0 | 1.0 | 真实后端不可达自动 skip（`pyproject.toml:52` 注释 + README Development）；`LOCAL_CORE=1` 开发兜底亦文档化 | 实测 skip 机制存在（本轮库可达故全跑） |
| T6 | 可维护性 | 1 | 1.0 | 1.0 | 6 测试文件职责清晰；`mypy src` 22 文件通过；`ruff check src tests` 全过 | |
| T7 | 断言质量 | 2 | 1.0 | 2.0 | 抽查断言均为具体值/具体错误串；无永真断言、无被吞异常 | |
| T8 | 执行时长 | 1 | 1.0 | 1.0 | 全量 83 项 1.43 s（coverage 模式 2.03 s） | 远低于 5 min |
| T9 | 测试文档 | 1 | 1.0 | 1.0 | README **Development**（运行命令 / skip 语义 / 架构约束）+ **Transaction boundary** 五条（`README.md:231-251`） | 三仓最完备 |
| T10 | 覆盖广度 | 1 | 1.0 | 1.0 | 60 测试函数 / 6 文件 / 83 collected；语句覆盖 88% | 比例合理 |

- **R** = 11.0 / 13 = **0.846**；**P** = 2.0（D-11）；**维度分** = 8×0.846 − 2.0 = **4.77**
- 本维度缺陷：D-11（M）

## 4. 缺陷清单

| ID | 严重度 | 维度 | 标题 | 最小复现步骤 | 原始输出证据 | CWE/规则 | 影响面 | 修复建议 |
|----|--------|------|------|--------------|--------------|----------|--------|----------|
| D-01 | **C** | 1 | GQL「关系带参 + 子选择集」解析失败（共享核心） | `python tmp/test-eval/probes/_verify15.py`，观察 V3/V4 | `Edefect-d01-recheck-py.log`：`[ERR] V3 带参+选择集(README) :: 期望 id(undefined) 实际 p({) 位置 10`、`[ERR] V4 带condition+选择集 :: … 位置 10`；探针 B-07 同因（`位置 15`）；Node 侧 `Edefect-d01-recheck.log` 同判 | CWE-20 | 四面（core + 双绑定 + 双宿主）；README 记载语法不可用 | 修正 GQL 语法分析：关系名后「参数列表 + 选择集」需可同时出现；补正向回归用例 |
| D-02 | **C** | 7 | `$where` 恶意载荷使查询条件静默丢弃、返回全量数据 | `python tmp/test-eval/probes/probe-py.py`（X-03，sqlite 与 mongo 双路径） | `Epy-probe-bcise.log:11,21`：`$where 载荷导致全量返回，rows=85`；SQL 侧条件静默消失：`Edefect-d02-unsupported.log`（`SELECT t."_id", t."name" FROM "v_parent" t`，`unsupported=[]`） | CWE-89 / CWE-943 / OWASP A03:2021 | 四面；结果静默错误（调用方无法区分「无数据」与「条件被丢弃」） | 不支持的条件键应**显式报错**而非静默丢弃；对 `$where`/`$function` 等键建立拒绝名单；补负向回归用例 |
| D-03 | **M** | 4 | 无 `idPrefix` 且未显式给 `_id` 时生成 `_id=undefined`，跨绑定序列化崩溃 | Python 侧 `tmp/test-eval/probes/_verify_py2.py` | `Eprobe-py-iso2.log`：`TypeError: 不支持的 Python 类型`；Node 侧 `Eprobe-iso11.log` / `Edefect-d03-rust-binding.log` 同根因 | CWE-20（跨语言绑定序列化边界） | 四面（绑定层） | `_id` 缺失且无 `idPrefix` 时核心应显式报错或由绑定层统一兜底生成；补双向序列化契约用例 |
| D-11 | **M** | 9 | 真实后端 E2E 固定表名 + 破坏性 DDL，多进程并发互踩（与 nodejs-store 报告 D-05 同类，各自仓独立复现） | 两个进程同时 `python -m pytest tests/test_real_backends_e2e.py` | `Epar-py-b.out`：32 个 ERROR，全部 `asyncmy.errors.OperationalError: (1050, "Table 'my_posts' already exists")`，栈顶 `tests/test_real_backends_e2e.py:185 _setup_mysql`；对照 `Epar-py-a.out` 31 项全过 | —（测试可靠性） | 测试工程；CI 并行时会误报 | 表名按进程/随机后缀隔离；DDL 移入 fixture 生命周期；补并发 job |
| D-06 | m | 1 | 重复 register 同名 schema 静默覆盖且 `list()` 出现重复条目（core 继承） | `node tmp/test-eval/probes/probe-rust.cjs`（I-03） | `Erust-probe-bcise.log:72`：`list=4 second=ok`（重复注册后列表为 `["Dup","DupDeleted","Dup","DupDeleted"]`） | CWE-20 | 四面；Host 枚举 schema 时重复 | 同名覆盖时同步去重 `order`，或显式报错 |
| D-07 | m | 2 | `$limit` 非数值未校验，原样进入聚合管道（core 继承） | `probe-rust.cjs`（B-18） | `Erust-probe-bcise.log:68`：`{"$limit":"abc"}` 被原样放入 `pipeline` | CWE-20 | 四面 | 对 `$limit/$skip` 做类型与范围校验，非法值显式报错 |
| D-08 | m | 7 | 联邦结果超 `MAX_FEDERATION_ROWS` 时错误文案错乱（core 继承） | `probe-rust.cjs`（S-01） | `Erust-probe-bcise.log:75`：`第 0 个取数单元的结果必须是数组`（未提及行数上限） | CWE-703 | 四面；排障成本上升 | 超限时抛出含上限与实测行数的专用错误 |

**严重度统计**：B **0** / C **2** / M **2** / m **3** / I **0**

## 5. 测试覆盖矩阵（九维 × 现状）

| 维度 | 既有资产 | 本轮实跑 | 补充实跑 | 缺口结论 |
|------|----------|----------|----------|----------|
| 1 功能 | 60 函数 / 6 文件（83 collected） | 有 | 有（core 51 例 + 宿主 23 例） | 关系带参+子选择集、事务失败注入缺 |
| 2 边界 | 部分（crud 边界 + timestamp 三态 + pagesize cap） | 有 | 有（BVA 22 + 宿主 23 例） | 上限+1、宿主层字符串/深度极值缺 |
| 3 组合 | 参数化 E2E（4 后端 × 8 用例） | 有 | 有（决策表 15 例） | 状态迁移/异常叠加/剪裁说明缺 |
| 4 交互 | `real_backends_e2e` / `federation_e2e` / `host_contract` | 有 | 有（并发 30/50、4 进程） | 事务边界、替身保真度缺 |
| 5 压力 | `stress/stress.py` | 有（2 档 ×3 轮） | 有 | 无门禁/p99/浸泡；基线对照受环境干扰 |
| 6 兼容 | 四后端 E2E + 方言专项 + core parity | 有 | 有（版本实采） | 版本/平台矩阵缺，M9=0（无 CI） |
| 7 安全 | require-context 五态 / perm 系列 / exec_sql 拒绝 | 有 | 有（1 轮注入攻击） | `$where` 未拦截；超深/超大批量缺 |
| 8 回归门禁 | 仅 `release-pypi.yml`，**无 CI** | 有 | 有 | 门禁缺口为全仓最大短板 |
| 9 测试工程 | 6 文件 60 用例 + README 文档 | 有 | 有（独立性/重复 5 次/并发） | 并行互踩（D-11）；T1 部分验证 |

## 6. 受限清单（未执行的检查项）

| 检查项 | 未执行原因 | 建议补测方式 |
|--------|------------|--------------|
| 压力：尖峰 / 浸泡档 | 需用户同意长时占用机器 | 低负载 × ≥30 min 浸泡 + 尖峰跳变 |
| 压力：吞吐基线复测（S5） | **评测时段机器被外部任务占满（CPU 100%、磁盘近满，用户确认）**，与历史基线 824.35 的差异不可归因于产品 | 空闲时段复跑 `stress/stress.py --workers 8 --rounds 200` 后再对照 |
| 压力：p99 指标 | `stress/stress.py` 未输出 p99 | 脚本补 p99 分位 |
| 压力：性能门禁（S10） | 项目无 CI，无此设施 | 新建 CI 后增加 `stress` job 并设阈值断言 |
| 兼容：多运行时/多平台/后端版本矩阵（M4/M5/M6/M9） | 本机仅 1 组版本、1 平台；无 CI | 新建 CI 增加 OS × Python × DB 版本 matrix |
| 交互：事务失败注入（I6） | 项目无事务 API（单命令模型 + README 契约） | 以失败注入模拟多步写中断 |
| 安全：超深 GQL / 超大批量载荷（X5） | 无对应用例 | 新增深度/批量越界载荷用例 |
| 测试工程：E2E/联邦文件单文件独立性（T1） | 服务依赖型文件未单独执行验证 | 逐文件 `pytest tests/test_real_backends_e2e.py` 复验 |

## 7. 执行证据附录

> `E` = `f:\独立开发者\项目\mongo-store\tmp\test-eval\`

```
[维度 1/4/8] $ python -m pytest tests/ -q  （py-store，LOCAL_CORE=1，真实四库可达）
  结果：83 passed / 0 failed / 0 skipped，1.43 s（exit 0）
  日志：Epy-pytest-all.log

[维度 9/8] $ pytest --cov（覆盖率）
  结果：TOTAL 955 stmts / 115 miss / 88%；83 passed in 2.03s
  日志：Epy-coverage.log

[维度 8/9] $ python -m ruff check .  /  ruff check src tests  /  mypy src
  结果：全仓 7 errors（全部位于 stress\stress.py，见 §8）；src+tests All checks passed!；
        mypy Success: no issues found in 22 source files
  日志：Epy-lint.log

[维度 9] 单文件独立执行 ×4
  结果：test_py_store / test_host_contract / test_multi_datasource / test_require_context 均 exit=0
  日志：Epy-independence.log

[维度 9] $ python -m pytest tests/ -q ×5（重复性）
  结果：run 1..5 exit=0 :: 83 passed（1.45 / 1.51 / 1.46 / 1.46 / 1.42 s）
  日志：Epy-repeat5.log

[维度 2/4/7] $ python tmp/test-eval/probes/probe-py.py  （宿主层探针，真实 Mongo + SQLite）
  结果：total=23 pass=20 fail=3（X-03-sqlite / X-03-mongo / B-07）
  日志：Epy-probe-bcise.log

[维度 2/3/4/7] $ node tmp/test-eval/probes/probe-rust.cjs  （core 直驱，51 例，连带责任证据）
  结果：total=51 pass=40 fail=11
  日志：Erust-probe-bcise.log

[维度 4/9] 两进程并发跑真实后端 E2E
  结果：进程 A 31 项全过；进程 B 32 ERROR（Table 'my_posts' already exists）
  日志：Epar-py-a.out / Epar-py-b.out

[维度 4/9] 四进程并发（2×node + 2×py）
  结果：M1 唯一性 PASS / M2 无丢失 PASS / M3 分布 PASS（py-a/py-b 各 40 条）→ 3/3
  日志：Emultiprocess-verify.log

[维度 7] $ python -m pip_audit
  结果：Found 57 known vulnerabilities in 8 packages——全部为环境级包（js2py/pillow/pip/pypdf/
        setuptools/torch/transformers/weasyprint），项目直接依赖 0 命中
  日志：Epy-pip-audit-full.log / Epy-store-deps.txt

[维度 5] $ python stress/stress.py （8 workers × 200 rounds）
  结果：{"qps":293.98,"errors":0,"all_ops":{"p50_ms":24.61,"p95_ms":45.76,"max_ms":349.77}}
        （首轮 286.13；同期复跑 307.84——时段受外部负载干扰，见 §3.5 注）
  日志：Epy-stress-8x200-rerun.log / Epy-stress-8x200.log / Estress-8x200-idle-rerun.log

[维度 5] 冒烟档 1×10
  结果：{"qps":298.59,"errors":0}
  日志：Estress-smoke-tier.log

[维度 6] 环境与后端版本实采
  结果：python=3.14.4 / mysql=8.0.42 / postgres=16.4 / mongo=8.0.12 / sqlite=3.50.4 / node=v25.1.0
  日志：Eenv-versions.log

[维度 8] 仓库元信息与 CI/发布流水线
  结果：workflows 仅 release-pypi.yml（无 ci.yml）；git log 7 条；tags v1.0.0 / v0.1.0
  日志：Erepo-git-and-workflows.log

[维度 1/2/7] 缺陷最小复现复核
  结果：D-01（V3/V4 ERR，py 侧）、D-02（$where 条件静默丢弃）、D-03（TypeError: 不支持的 Python 类型）
  日志：Edefect-d01-recheck-py.log / Edefect-d02-perm.log / Eprobe-py-iso2.log
```

## 8. 范围外发现（不计分）

| 现象 | 位置 | 初判严重度 | 建议 |
|------|------|------------|------|
| `stress/stress.py` 游离于 lint 范围：`ruff check .` 报 7 errors（I001 + RUF100×6）全在该文件；`pyproject.toml` 刻意仅查 `src`/`tests` | `stress/stress.py:26-33,261` | I | 压测脚本纳入 lint（修复 7 处即可 `--fix`），或在 README 说明 lint 范围 |
| pip-audit 命中 57 条漏洞均为环境级包（js2py/pillow/pip/pypdf/setuptools/torch/transformers/weasyprint），与项目依赖无关 | `Epy-pip-audit-full.log` | I（非缺陷） | 以项目依赖清单为审计口径（lockfile 白名单方式） |
| 分发名 `storepy` 与仓库名 `py-store` 不一致（PyPI 名称校验所致，git log `0fabe6e`） | `pyproject.toml` `name` | I | README 标注分发名映射，避免安装困惑 |
| 评测时段机器被外部任务占满（CPU 100%、磁盘近满，用户确认）：三仓吞吐全部显著低于历史基线（py −62.7%），差异属环境干扰而非产品退化 | 全部压测日志（`E*-stress-*.log`） | I（环境） | 空闲时段复测后再与基线对比 |
| core 级设计内行为（角色字符串未归一化默认放行 / 未声明字段投影忽略 / register 缺 collection 回落 name / require_context fail-secure）经代码复核为设计内契约 | 见 nodejs-store 报告 §8 与 rust-store 报告 §8 | I | 同左（文档化契约） |
| Mongo 多步写非原子为显式文档化契约（需 replica set 方能事务） | README Transaction boundary | I（设计内） | 调用方需知晓；无需代码动作 |

## 9. 改进建议（按优先级）

| 优先级 | 建议 | 对应缺陷 | 预期收益 | 落地方式 |
|--------|------|----------|----------|----------|
| P0 | 共享核心：`$where`/`$function` 等不支持条件键改为**显式报错**，绝不静默丢弃 | D-02 | 消除「结果静默错误」，恢复注入防御可信度（三仓同步受益） | core `pipeline/plan` 校验 + 负向回归用例 |
| P0 | 共享核心：修正 GQL 语法，关系名后「参数 + 选择集」可同时出现 | D-01 | 恢复 README 记载能力 | core parser + 正向/反向用例 |
| P1 | **新建 CI 测试工作流**（pytest + ruff + mypy 三门禁；外部库用 service 容器） | G3/G4/G9 | 消除「测试靠自觉」，质量门禁从 0 到 1 | `.github/workflows/ci.yml` |
| P1 | 真实后端 E2E 表名按进程隔离，去掉破坏性 DDL | D-11 | 测试可并行、CI 可信 | `tests/test_real_backends_e2e.py` + fixture 生命周期 |
| P1 | `_id` 缺失且无 `idPrefix` 时显式报错或绑定层统一兜底 | D-03 | 消除跨绑定崩溃 | core + 双绑定契约用例 |
| P2 | 覆盖率门槛进 CI（语句 ≥90% + 分出口径） | G5 | 门禁量化（当前 88%） | `pyproject.toml` + CI `--cov-fail-under` |
| P2 | `$limit/$skip` 类型与范围校验；联邦上限专用错误 | D-07/D-08 | 边界输入可控、排障成本下降 | core 校验 + 用例 |
| P2 | `stress/stress.py` 纳入 lint；空闲时段复测吞吐并建立性能基线 | §8 | 全仓风格一致、性能可回归对比 | ruff `--fix` + 压测基线脚本 |

## 10. 复现指南

```powershell
# 环境前置：Windows；Python 3.14.4；MySQL 8.0.42 / PostgreSQL 16.4 / MongoDB 8.0.12 / SQLite 3.50.4
# 依赖安装（含开发 extras；原生核心经 rust-store-py，开发兜底需 LOCAL_CORE=1）
cd f:\独立开发者\项目\mongo-store\py-store
pip install -e ".[dev,mysql,postgres,sqlite]"
$env:LOCAL_CORE='1'

# 1. 全量测试（83 项；外部库不可达时 e2e 自动 skip）
$env:PYTHONPATH='src'; python -m pytest tests/ -q

# 2. 覆盖率
python -m pytest tests/ -q --cov=py_store --cov-report=term-missing

# 3. lint / 类型检查（与 pyproject 配置一致的范围）
python -m ruff check src tests
python -m mypy src

# 4. 压测（需四库可达；空闲时段运行以获得可比基线）
python stress/stress.py --workers 8 --rounds 200     # 负载档
python stress/stress.py --workers 1 --rounds 10      # 冒烟档

# 5. 补充探针
python f:\独立开发者\项目\mongo-store\tmp\test-eval\probes\probe-py.py
```

## 附录 A：标准对照

| 标准 | 本报告的使用位置 |
|------|------------------|
| ISO/IEC/IEEE 29119-1..4 | 九维框架、BVA/等价类/决策表设计（§3.2/3.3） |
| ISO/IEC 25010 / 25023 | 维度 1/5/6/7 的质量特性与度量 |
| ISTQB CTFL 4.0 | 维度 2/3 的边界值、等价类、状态迁移 |
| OWASP Top 10 2021 / ASVS / WSTG | 维度 7 攻击面（A03 注入） |
| MITRE CWE Top 25 | §4 各缺陷 CWE 归类 |
| F.I.R.S.T / Clean Tests | 维度 9 判据 |
| SonarQube Quality Gate / DORA | 维度 8 门禁与性能回归判据 |

## 附录 B：项目规则优先声明

- 本仓 `pyproject.toml:55-57` 注释表明 ruff 规则集为**刻意裁剪**（不启 SIM/UP/FURB 风格改写类规则、不跑 `ruff format`）——lint 达成度以项目配置范围（`src` + `tests`）为准，实测 `All checks passed!` 记达标；全仓 7 errors 归入 §8 观察项。
- 「e2e 用例在外部库不可达时自动 skip」为显式设计（`pyproject.toml:52` 注释 + README Development）——按 SKILL 依赖可达性降级规则处理（本轮库可达故全量实跑，未触发降级）。
- 测试命令口径以 README Development 为准（`PYTHONPATH=src` + `python -m pytest py-store/tests/ -q`），本轮按该口径实跑。
- 本仓为薄 Host，DB 执行由 executor 承担、核心逻辑由共享 Rust 核心承担；`I4 真实依赖集成` 判定以宿主真实库 E2E 为准，core 级检查项按连带责任引用 core 侧实测证据。
