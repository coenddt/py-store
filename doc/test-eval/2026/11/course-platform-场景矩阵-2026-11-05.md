# course-platform 场景矩阵 · 多后端对拍报告（2026-11-05）

## 一、环境与后端可达性

- `mongodb`：可达，通过 101/101
- `sqlite`：可达，通过 101/101
- `postgres`：**skip** —— 缺少 asyncpg: No module named 'asyncpg'
- `mysql`：**skip** —— 缺少 asyncmy: No module named 'asyncmy'

> 本报告只出证据，不修实现。判定规则：SQL 结果集与 Mongo(oracle) 逐行相等或显式 Err/unsupported+告警；静默不一致判缺陷。

## 二、覆盖度表（A~J 组）

| 组 | 覆盖数 | 后端 | 通过 | 失败 | skip(不可达) |
|---|----|----|----|----|----|
| A | 15 | mongodb,sqlite | 30 | 0 | - |
| B | 13 | mongodb,sqlite | 26 | 0 | - |
| C | 10 | mongodb,sqlite | 20 | 0 | - |
| D | 7 | mongodb,sqlite | 14 | 0 | - |
| E | 15 | mongodb,sqlite | 30 | 0 | - |
| F | 8 | mongodb,sqlite | 16 | 0 | - |
| G | 7 | mongodb,sqlite | 14 | 0 | - |
| H | 10 | mongodb,sqlite | 20 | 0 | - |
| J | 16 | mongodb,sqlite | 32 | 0 | - |

未覆盖组：**I(联邦)** —— 本场景为单源 harness（每个后端独立进程、`default` 源），无法起双可写源；跨源联邦由 `tests/test_federation_e2e.py` 单独覆盖（I-01..I-08 对应矩阵）。

## 三、缺陷清单（按 静默失真 > 越权 > 其它 排序）

无失败用例。

## 四、与环境变量

- 可复跑：`$env:LOCAL_CORE=1; $env:PYTHONPATH=py-store/src; python -m pytest py-store/tests/test_scenario_course_platform.py -q`
- 后端可达性可用 `MYSQL_URI` / `PG_URI` / `MONGO_URI` 覆盖。

