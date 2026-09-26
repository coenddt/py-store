# course-platform 场景矩阵 · 多后端对拍报告（2026-09-14）

## 一、环境与后端可达性

- `mongodb`：可达，通过 90/90
- `postgres`：可达，通过 90/90
- `mysql`：可达，通过 90/90
- `sqlite`：可达，通过 90/90

> 本报告只出证据，不修实现。判定规则：SQL 结果集与 Mongo(oracle) 逐行相等或显式 Err/unsupported+告警；静默不一致判缺陷。

## 二、覆盖度表（A~J 组）

| 组 | 覆盖数 | 后端 | 通过 | 失败 | skip(不可达) |
|---|----|----|----|----|----|
| A | 13 | mongodb,postgres,mysql,sqlite | 52 | 0 | - |
| B | 13 | mongodb,postgres,mysql,sqlite | 52 | 0 | - |
| C | 10 | mongodb,postgres,mysql,sqlite | 40 | 0 | - |
| D | 7 | mongodb,postgres,mysql,sqlite | 28 | 0 | - |
| E | 15 | mongodb,postgres,mysql,sqlite | 60 | 0 | - |
| F | 8 | mongodb,postgres,mysql,sqlite | 32 | 0 | - |
| G | 3 | mongodb,postgres,mysql,sqlite | 12 | 0 | - |
| H | 10 | mongodb,postgres,mysql,sqlite | 40 | 0 | - |
| J | 11 | mongodb,postgres,mysql,sqlite | 44 | 0 | - |

未覆盖组：**I(联邦)** —— 本场景为单源 harness（每个后端独立进程、`default` 源），无法起双可写源；跨源联邦由 `tests/test_federation_e2e.py` 单独覆盖（I-01..I-08 对应矩阵）。

## 三、缺陷清单（按 静默失真 > 越权 > 其它 排序）

无失败用例。

## 四、与环境变量

- 可复跑：`$env:LOCAL_CORE=1; $env:PYTHONPATH=py-store/src; python -m pytest py-store/tests/test_scenario_course_platform.py -q`
- 后端可达性可用 `MYSQL_URI` / `PG_URI` / `MONGO_URI` 覆盖。

