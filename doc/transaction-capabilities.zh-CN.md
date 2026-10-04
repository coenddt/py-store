# 事务型能力

范围：py-store 中哪些操作原子、哪些不原子，以及非原子路径如何显式声明。允许降级，
禁止静默假装已原子。

## 写边界

- **同一连接 / 数据源内** —— 一次写入若跨多个数据库（Mongo）或多个 PostgreSQL schema，
  在**该连接的一个事务内**同步完成。Mongo 侧需 **replica set**（或 sharded cluster）；
  standalone 部署不支持多文档事务，按非原子声明。
- **跨连接** —— py-store **没有跨源事务**（无 2PC / Saga）。一次写入若跨连接链路，
  **显式拒绝，或降级并发出反馈事件** —— **绝不静默**：
  - 在 `store.session(...)` 内写 ≥2 个数据源时 fail-closed：先全部回滚，再抛
    `NonAtomicWriteError`；
  - 无会话写 ≥2 个数据源时按数据源顺序执行，并发出一条 `non_atomic_write` 反馈事件
    （`code: nonAtomic`，含涉及的数据源）；
  - 无法事务化的 Mongo 源（standalone / 探测失败）按原样执行，并发
    `mongo_transaction_unsupported`（`deployment: standalone|unknown`）。
- **收敛方式** —— 把写收敛到单一数据源，或放进 `store.session()` 并让所有命令落在同一
  数据源（一个连接 = 一个事务）。

## 同名 schema 与从链路

- 一次写入在同一连接内，跨**主 schema 及其全部 `replica: true` 从链路**，在一个事务内同步。
- 写入若触达**跨连接从链路**，回落到上一条规则：显式拒绝，或降级并发出反馈事件。

## 读一致性

- 只有显式会话内的多条读共享同一事务连接；会话外的读不额外开启事务。
