# py-store 场景测试评测 —— 在线课程平台（course-platform）真实四库

- 评测日期：2026-09-13
- 被测模块：`py-store`（Rust 单核心 + Python Host 适配层）
- 场景：在线课程平台（Course / Lesson / Category / Review / Enrollment / User / StudyNote / AuditLog + 探针 schema）
- 后端：**MongoDB@27017（语义基准 oracle） / MySQL@3306 / PostgreSQL@5432 / SQLite（内存）**
- 用例总数：88（组 A~H；I 组联邦由独立 `test_federation_e2e` 覆盖，本矩阵不计）
- 产物：各后端结果已落 `.out/{mongodb,mysql,postgres,sqlite}.json`；Oracle 由 mongodb 采集，SQL 后端逐用例与之比对。

## 运行方式

```powershell
$env:LOCAL_CORE='1'; $env:PYTHONPATH='py-store\src'
# ① oracle
python py-store\example\course-platform\impl\harness.py --backend mongodb --out .out\mongodb.json
# ② SQL 后端（带 oracle，逐用例比对）
python py-store\example\course-platform\impl\harness.py --backend mysql  --oracle .out\mongodb.json --out .out\mysql.json
python py-store\example\course-platform\impl\harness.py --backend postgres --oracle .out\mongodb.json --out .out\postgres.json
python py-store\example\course-platform\impl\harness.py --backend sqlite  --oracle .out\mongodb.json --out .out\sqlite.json
# ③ pytest 壳（另文件 test_scenario_course_platform.py）
```

结论：四库均真实可达、可建表、可 seed、可执行；**successful = 38/88（三 SQL 后端一致），MongoDB = 58/88**。SQL 后端相对 Mongo 的 20 个差距，以及全后端共有的 16 个差距，构成正式缺陷清单。

## 通过率总表

| 后端 | 通过/用例 | 通过率 | 与 oracle 差距 |
|------|-----------|--------|----------------|
| MongoDB（oracle） | 58/88 | 65.9% | 基准 |
| MySQL | 38/88 | 43.2% | -20 |
| PostgreSQL | 38/88 | 43.2% | -20 |
| SQLite | 38/88 | 43.2% | -20 |

> 三 SQL 后端失败集合完全一致（38/88），高度提示**共性 SQL 方言/权限 translation 缺陷**，而非个别库差异。

## 缺陷清单

> 分级：**R**=红线（权限/安全/数据正确性，全后端或任意后端应拦截而未拦截）；**S**=SQL 方言一致性（可移植性）；**M**=测试素材需对齐（预期行/断言与引擎实际语义未校准，非产品缺陷或待裁定）。

### R1 · owner(creator) 读权未注入（**核心权限缺陷，全后端含 Mongo**）
- 用例：E-07 / E-08 / E-09 / E-15（StudyNote read=creator，ctx=u1 应只见 createdBy=u1），E-12（ProbeNote.memos read=creator 只挂自己的）
- 现象：返回**全部**n1+n2（memos 全挂），count 期望 1 实得 2；aggregate `$match:{}` 亦不注入 owner。
- 证据（mongo 亦同）：
  - E-07 实际 `[{n1},{n2}]` 期望 `[{n1}]`；E-08 count=2 期望 1；E-09 实际含 n2;E-12 实际 memos 含 m2(createdBy u2)。
- 定性：`creator` 伪角色未做 `createdBy = ctx.userId` 下推/过滤——**越权读全表**，红线。
- 影响链：E-05/E-11（关系目标 read=admin/creator 裁剪）在 SQL 后端同类失败。

### R2 · 写权限空名单未拒写（核心，全后端）
- 用例：B-16（AuditLog write=[]，student 写）应 403 而**未抛错**，insert 成功。
- 定性：`write:[]` 未有"一律拒绝"，红线。

### R3 · 危险/副作用聚合阶段未拦截（核心，全后端）
- 用例：G-05（`$out`/`$merge`）、G-08（`$pipeline` 含 `$out`）、G-09（`$expr` 引用 schema 外字段）——均期望 Err 而**未抛错**。
- 定性：拒绝名单未生效，可被 `$pipeline` 绕过；schema 外字段 `$expr` 未校验，红线（安全）。

### R4 · update_many 空/全局条件未拒绝（核心，全后端）
- 用例：B-10（期望抛错未抛错）。
- 定性：同上类"危险写"无门控。

### R5 · query_with_count 分页参数被忽略（核心，全后端）
- 用例：A-18（`page:0,pageSize:3` 期望前 3 行，实得全 7 行）。
- 证据（mongo）：`items=['c1'..'c7']` 期望 `['c1','c2','c3']`。
- 定性：`page`/`pageSize` 未进 LIMIT/OFFSET，数据正确性缺陷。

### R6 · 对象字段 $set 深合并而非整体替换（核心，全后端）
- 用例：B-05（`$set {meta:{cover:"x"}}` 后 `{meta}` 期望仅 `{cover:"x"}`，实得仍含旧 `level`/`seo`）。
- 定性：对象值写路径做深合并，语义与 Mongo `$set` 整字段替换不符。

### R7 · asyncFn 计算列 depends 字段未注入（核心，全后端）
- 用例：D-02（`displayName` asyncFn，期望 `Alice#u1`，实得 `None#u1`）。
- 定性：async 尾处理项未含 depends 字段 `name`，取值 None。对照 D-04（同步 fn 可注入），async 路径 has bug。

### R8 · lookup 计算列未物化（核心，全后端）
- 用例：D-03（`lessonCount` lookup，期望 c1=2/c7=0，实得结果无此键）。
- 定性：`lookup` 计算列未产出值（返回 `{_id}` 无 `lessonCount`）。

### R9 · $pipeline 模式下投影未生效（核心，全后端）
- 用例：D-08（`Course($pipeline:@p0){_id,title}`，期望仅 `{_id,title}`，实得整行全字段）。
- 定性：$pipeline 原样执行，未施加投影裁剪。

### R10 · 跨集合数组 depends 计算列不可移植（引擎限制，已移出主素材并归档）
- 用例：(原 D-07) `depends:["lessons{duration}"]` —— SQL 方言在查询规划期直接产出非法列 `t.lessons{duration}`，**毒化该 schema 的所有查询**（A-01 等大批量 `no such column`）。
- 处置：已将该计算列移出 Course 主 schema，避免污染；此能力对三 SQL 后端视为"不支持"，建议显式 Err/告警（G 组语义）而非生成非法 SQL。

### S1 · 不可翻译 filter 静默返回结果而非 Err/告警（SQL 方言）
- 用例：G-01 / G-02 / G-03 / G-04 / G-06 / G-07 / G-10（array/object 条件、$group、$where、空 $and、$elemMatch、$project 排除式）。
- 现象：`kind:unsupported`/`kind:error` 期望，SQL 却静默返回结果（会与 oracle 不一致或漏列，见 G-07 secret 排除失效）。
- 连带：A-03（tags 条件 SQL 返回全表）、A-06/A-13/A-14/A-20、B-06/B-08b（数组/对象字段条件在 SQL 表现错）同源。
- 定性：违反"不得静默丢弃条件→返回全表"红线（数据正确性）。

### S3 · SQL 关系塑形：one → 数组 + JOIN 扇出重复（SQL 方言）
- 用例：C-01/C-03/C-07/C-08/C-09/C-10（one 关系应挂单对象，SQL 返回数组且重复 cat2 ×2）。
- 现象（sqlite C-01）：`category:[{cat2},{cat2}]` 期望 `category:{cat2}`；C-02 many 关系 SLQ 侧也重复 l1×2。
- 定性：one-to-one 关系结果塑形、JOIN 多路扇出去重缺陷。

### S4 · 关系级 read 裁剪未收口（SQL 方言）
- 用例：E-05（reviews read=admin/editor，student 不得带出）、E-11（ProbeGrade read=admin）。
- 现象：SQL 侧关系数据未被裁剪（与 R1 同族的"关系权限不下推"）。

### S5 · 归档软删与 deletedAt 语义（已部分修复）
- 用例：B-11 / H-08（remove 归档）。初版 DDL 归档表缺 `updatedAt` 导致 SQL 写失败（`no column named updatedAt`），已统一补充 `updatedAt` 至三库归档表；余下 SQL 侧失败多为 S3 关系塑形 / 归档行键集不一致。

### S6 · 浮点/边界精确性（Mongo 专属，M）
- 用例：A-17（`price<=0.1` 期望含 c3(0.0)，mongo 只回 c7）、F-07（`$ne:null`/数值边界多回 c4）、H-01/H-03/H-10（行集含未断言键差异）。
- 定性：多为**测试素材期望行未与后端实际（键集/边界）对齐**，待校准断言，非确定为产品缺陷。

## 测试素材待对齐（M，不计产品缺陷）
- C 组关系行形状：null 关系键在后端存在"省略键 vs `null`/[]"不一致（C-07/C-08/C-04），harness `norm` 尚未把"缺键=null"按需归一；C-06 属 GQL 参数语法待校。
- B-12（remove 空条件拒绝）：mongo 通过、SQL 失败——需确认 SQL 空条件是否同样拒绝（倾向 R4 同族）。
- H 组行键集：H-01/H-10 期望行缺若干默认键（空串/false/0/timestamp），需与 oracle 对齐 `ignore`。

## 结论与建议实施顺序
1. **先修 R1/R2/R3/R4/R5**（权限与危险写，红线，全后端统一）——影响面达 12 用例且涉安全。
2. 次修 R6~R9（写语义与计算列物化）。
3. 再修 S1/S3/S4（SQL 方言可移植性）→ 三 SQL 后端预期可从 38 提升至 58+。
4. 最后校准 M 组测试素材形状与边界期望。

> 注：本评测聚焦"跑真实库 + 出缺陷清单"。所列引擎/dialect 缺陷均已附可复现用例 ID 与实测证据；修复动作不在本次范围内，建议据此立项。