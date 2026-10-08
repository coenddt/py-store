# course-platform 场景矩阵 · 多后端对拍报告（2026-10-08）

## 一、环境与后端可达性

- `sqlite`：可达，通过 96/101
- `mongodb`：**skip** —— MongoDB 不可达（mongodb://127.0.0.1:27017/mongo_store_e2e）: 127.0.0.1:27017: [WinError 1225] 远程计算机拒绝网络连接。 (configured timeouts: socketTimeoutMS: 20000.0ms, connectTimeoutMS: 20000.0ms), Timeout: 3.0s, Topology Description: <TopologyDescription id: 6ac70a401866ef4711980f41, topology_type: Unknown, servers: [<ServerDescription ('127.0.0.1', 27017) server_type: Unknown, rtt: None, error=AutoReconnect('127.0.0.1:27017: [WinError 1225] 远程计算机拒绝网络连接。 (configured timeouts: socketTimeoutMS: 20000.0ms, connectTimeoutMS: 20000.0ms)')>]>
- `postgres`：**skip** —— PostgreSQL 不可达（postgres://e2e:e2e123@127.0.0.1:5432/mongo_store_e2e）: [WinError 1225] 远程计算机拒绝网络连接。
- `mysql`：**skip** —— MySQL 不可达（mysql://e2e:e2e123@127.0.0.1:3306/mongo_store_e2e?charset=utf8mb4）: (2003, "Can't connect to MySQL server on '127.0.0.1' ([WinError 1225] 远程计算机拒绝网络连接。)")

> 本报告只出证据，不修实现。判定规则：SQL 结果集与 Mongo(oracle) 逐行相等或显式 Err/unsupported+告警；静默不一致判缺陷。

## 二、覆盖度表（A~J 组）

| 组 | 覆盖数 | 后端 | 通过 | 失败 | skip(不可达) |
|---|----|----|----|----|----|
| A | 15 | sqlite | 14 | 1 | - |
| B | 13 | sqlite | 13 | 0 | - |
| C | 10 | sqlite | 10 | 0 | - |
| D | 7 | sqlite | 7 | 0 | - |
| E | 15 | sqlite | 15 | 0 | - |
| F | 8 | sqlite | 8 | 0 | - |
| G | 7 | sqlite | 7 | 0 | - |
| H | 10 | sqlite | 7 | 3 | - |
| J | 16 | sqlite | 15 | 1 | - |

未覆盖组：**I(联邦)** —— 本场景为单源 harness（每个后端独立进程、`default` 源），无法起双可写源；跨源联邦由 `tests/test_federation_e2e.py` 单独覆盖（I-01..I-08 对应矩阵）。

## 三、缺陷清单（按 静默失真 > 越权 > 其它 排序）

- **A-08** `[sqlite]` group=A：缺少期望行集且无 mongo oracle
- **H-02** `[sqlite]` group=H：缺少期望行集且无 mongo oracle
- **H-04** `[sqlite]` group=H：缺少期望行集且无 mongo oracle
- **H-05** `[sqlite]` group=H：缺少期望行集且无 mongo oracle
- **J-11** `[sqlite]` group=J：缺少期望行集且无 mongo oracle
- **J-11** `[sqlite]` group=J：缺少期望行集且无 mongo oracle

## 四、与环境变量

- 可复跑：`$env:LOCAL_CORE=1; $env:PYTHONPATH=py-store/src; python -m pytest py-store/tests/test_scenario_course_platform.py -q`
- 后端可达性可用 `MYSQL_URI` / `PG_URI` / `MONGO_URI` 覆盖。

