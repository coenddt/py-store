# py-store 修复文档 · 2026-09

> 依据：`rust-store/doc/fix-plan/2026-09/缺陷分层决策-共享核心修复主案.md`。
> 绝大多数缺陷归属 Rust 共享核心，本仓修完即继承，无需重复实现；本仓只承担**核心修复的宿主对准/去重 + 回归验证**。

## 本仓需动的部分

### 1. R5 · queryWithCount 宿主塑形去重（与 node 对齐）
- 位置：`src/py_store/crud/query.py`（`query_with_count` hasMore/page/pageSize 计算）。
- 现状：与 `nodejs-store/src/crud/query.js` 重复实现同一分页塑形。
- 动作：核心 `plan_query_with_count` 修复分页落库后，把宿主塑形收敛为薄封装（或改用核心返回包络），与 node 同构。**随机修核心一并做，避免双端各写一份。**

### 2. R7 · asyncFn 尾处理（与核心注入联动）
- 位置：`src/py_store/crud/query.py` post 尾处理 + `src/py_store/schema.py` `get_async_fn`。
- 动作：先用一个最小用例确认「核心注入的 depends 是否进入宿主 items」；若核心已注入而宿主取不到 → 本仓修；若核心未注入 → rust-store 修，本仓仅验证。
- 期望：D-02 `displayName` = `Alice#u1`，不再 `None#u1`。
- 注意：node `asyncFn` 尾处理为独立实现，若根因在宿主则两仓**各自对齐同一语义**（可抽公共约定文档，避免分叉）。

## 本仓回归验收
- `python -m pytest py-store/tests/test_scenario_course_platform.py -q`
- 四库结果（mongodb/mysql/postgres/sqlite）随核心 P0/P1 修复应从 38/58 → 追平 100% 对拍；缺键/形状对齐项单列测试素材。

## 不重复造轮子提醒
- 本仓不改任何 Rust 核心逻辑；核心修复到位后，无需在 py 侧补拍「已修实现」，只保留 `cases/*.json` 作为跨端行为契约（回归条），Node 亦可复用同一批用例语义。