# course-platform 场景矩阵 · 多后端对拍报告（2026-09-13）

## 一、环境与后端可达性

- `mongodb`：可达，通过 88/88
- `postgres`：可达，通过 64/88
- `mysql`：可达，通过 65/88
- `sqlite`：可达，通过 65/88

> 本报告只出证据，不修实现。判定规则：SQL 结果集与 Mongo(oracle) 逐行相等或显式 Err/unsupported+告警；静默不一致判缺陷。

## 二、覆盖度表（A~H 组）

| 组 | 覆盖数 | 后端 | 通过 | 失败 | skip(不可达) |
|---|----|----|----|----|----|
| A | 13 | mongodb,postgres,mysql,sqlite | 43 | 9 | - |
| B | 13 | mongodb,postgres,mysql,sqlite | 39 | 13 | - |
| C | 10 | mongodb,postgres,mysql,sqlite | 19 | 21 | - |
| D | 8 | mongodb,postgres,mysql,sqlite | 29 | 3 | - |
| E | 16 | mongodb,postgres,mysql,sqlite | 58 | 6 | - |
| F | 8 | mongodb,postgres,mysql,sqlite | 32 | 0 | - |
| G | 10 | mongodb,postgres,mysql,sqlite | 31 | 9 | - |
| H | 10 | mongodb,postgres,mysql,sqlite | 31 | 9 | - |

未覆盖组：**I(联邦)** —— 本场景为单源 harness（每个后端独立进程、`default` 源），无法起双可写源；跨源联邦由 `tests/test_federation_e2e.py` 单独覆盖（I-01..I-08 对应矩阵）。

## 三、缺陷清单（按 静默失真 > 越权 > 其它 排序）

- **A-13** `[postgres]` group=A：行集不一致
  实际: [{"_id": "c1"}]
  期望: [{"_id": "c1", "tags": ["python", "db"]}]
- **A-14** `[postgres]` group=A：行集不一致
  实际: [{"_id": "c1"}]
  期望: [{"_id": "c1", "meta": {"cover": "c1.png", "level": "beginner", "seo": {"title": "看Python", "desc": "从零开始"}}}]
- **A-20** `[postgres]` group=A：期望抛错但未抛错
- **B-05** `[postgres]` group=B：行集不一致
  实际: [{"_id": "c1"}]
  期望: [{"_id": "c1", "meta": {"cover": "x"}}]
- **B-06** `[postgres]` group=B：行集不一致
  实际: [{"_id": "c1"}]
  期望: [{"_id": "c1", "tags": ["a", "b"]}]
- **B-08b** `[postgres]` group=B：行集不一致
  实际: [{"_id": "c1"}]
  期望: [{"_id": "c1", "tags": ["python", "db", "x"]}]
- **B-11** `[postgres]` group=B：执行报错: column "createdAt" of relation "courses_deleted" does not exist
- **B-11** `[postgres]` group=B：行集不一致
  实际: []
  期望: [{"_id": "c1"}]
- **B-13** `[postgres]` group=B：count=0 期望 1
- **B-13** `[postgres]` group=B：count=0 期望 1
- **B-13** `[postgres]` group=B：行集不一致
  实际: []
  期望: [{"_id": "c9", "title": "补title"}]
- **C-01** `[postgres]` group=C：执行报错: SQL 后端暂不支持的聚合阶段 $unwind：拒绝静默忽略后返回未聚合的原始行
- **C-03** `[postgres]` group=C：执行报错: SQL 后端暂不支持的聚合阶段 $unwind：拒绝静默忽略后返回未聚合的原始行
- **C-05** `[postgres]` group=C：行集不一致
  实际: [{"_id": "c1", "lessons": [{"_id": "l1", "parent": null, "title": "安装"}, {"_id": "l2", "parent": null, "title": "入门"}]}]
  期望: [{"_id": "c1", "lessons": [{"_id": "l1", "title": "安装", "parent": null}, {"_id": "l2", "title": "入门", "parent": {"_id": "l1", "title": "安装"}}]}]
- **C-06** `[postgres]` group=C：执行报错: SQL 下推不支持（postgres）: childLimit；$lookup 关系 lessons 含子 limit/skip（每父 top-N），需窗口函数或 LATERAL，暂未下推（_unsupported:childLimit）
- **C-07** `[postgres]` group=C：执行报错: SQL 后端暂不支持的聚合阶段 $unwind：拒绝静默忽略后返回未聚合的原始行
- **C-08** `[postgres]` group=C：执行报错: SQL 后端暂不支持的聚合阶段 $unwind：拒绝静默忽略后返回未聚合的原始行
- **C-09** `[postgres]` group=C：执行报错: SQL 后端暂不支持的聚合阶段 $unwind：拒绝静默忽略后返回未聚合的原始行
- **D-03** `[postgres]` group=D：执行报错: SQL 后端暂不支持的聚合阶段 $addFields：拒绝静默忽略后返回未聚合的原始行
- **D-03** `[postgres]` group=D：执行报错: SQL 后端暂不支持的聚合阶段 $addFields：拒绝静默忽略后返回未聚合的原始行
- **E-05** `[postgres]` group=E：行集不一致
  实际: [{"_id": "c1", "lessons": [{"_id": "l1"}, {"_id": "l1"}, {"_id": "l2"}, {"_id": "l2"}]}]
  期望: [{"_id": "c1", "lessons": [{"_id": "l1"}, {"_id": "l2"}]}]
- **E-12** `[postgres]` group=E：行集不一致
  实际: [{"_id": "PN1", "label": "pn", "memos": [{"body": "mine", "createdBy": "u1"}, {"body": "other", "createdBy": "u2"}]}]
  期望: [{"_id": "PN1", "label": "pn", "memos": [{"body": "mine", "createdBy": "u1"}]}]
- **G-03** `[postgres]` group=G：行集不一致
  实际: [{"_id": "c7", "categoryId": "cat3", "createdAt": 1789304448881, "createdBy": "u1", "enrolledCount": 0, "price": 0.1, "rating": 1.1, "secret": "SECRET-7", "status": "published", "summary": "base", "title": "基础数学", "updatedAt": 1789304448881}]
  期望: []
- **G-06** `[postgres]` group=G：期望抛错但未抛错
- **G-07** `[postgres]` group=G：行集不一致
  实际: [{"_id": "c1", "categoryId": "cat2", "createdAt": 1789304449023, "createdBy": "u1", "enrolledCount": 0, "price": 9.9, "rating": 4.5, "secret": "SECRET-1", "status": "published", "summary": "必学", "title": "Python入门", "updatedAt": 1789304449023}]
  期望: [{"_id": "c1", "categoryId": "cat2", "createdAt": 1789304445508, "createdBy": "u1", "enrolledCount": 0, "meta": {"cover": "c1.png", "level": "beginner", "seo": {"desc": "从零开始", "title": "看Python"}}, "price": 9.9, "rating": 4.5, "status": "published", "summary": "必学", "tags": ["python", "db"], "title": "Python入门", "updatedAt": 1789304445508}]
- **H-07** `[postgres]` group=H：执行报错: SQL 后端暂不支持的聚合阶段 $unwind：拒绝静默忽略后返回未聚合的原始行
- **H-08** `[postgres]` group=H：执行报错: column "createdAt" of relation "courses_deleted" does not exist
- **H-08** `[postgres]` group=H：行集不一致
  实际: []
  期望: [{"_id": "c1"}]
- **H-10** `[postgres]` group=H：行集不一致
  实际: [{"_id": "c7"}]
  期望: [{"_id": "c7", "tags": ["x", "y"], "meta": {"cover": "z", "seo": {"title": "深", "desc": "深描"}}}]
- **A-13** `[mysql]` group=A：行集不一致
  实际: [{"_id": "c1"}]
  期望: [{"_id": "c1", "tags": ["python", "db"]}]
- **A-14** `[mysql]` group=A：行集不一致
  实际: [{"_id": "c1"}]
  期望: [{"_id": "c1", "meta": {"cover": "c1.png", "level": "beginner", "seo": {"title": "看Python", "desc": "从零开始"}}}]
- **A-20** `[mysql]` group=A：期望抛错但未抛错
- **B-05** `[mysql]` group=B：行集不一致
  实际: [{"_id": "c1"}]
  期望: [{"_id": "c1", "meta": {"cover": "x"}}]
- **B-06** `[mysql]` group=B：行集不一致
  实际: [{"_id": "c1"}]
  期望: [{"_id": "c1", "tags": ["a", "b"]}]
- **B-08b** `[mysql]` group=B：行集不一致
  实际: [{"_id": "c1"}]
  期望: [{"_id": "c1", "tags": ["python", "db", "x"]}]
- **B-11** `[mysql]` group=B：执行报错: (1054, "Unknown column 'createdAt' in 'field list'")
- **B-11** `[mysql]` group=B：行集不一致
  实际: []
  期望: [{"_id": "c1"}]
- **C-01** `[mysql]` group=C：执行报错: SQL 后端暂不支持的聚合阶段 $unwind：拒绝静默忽略后返回未聚合的原始行
- **C-03** `[mysql]` group=C：执行报错: SQL 后端暂不支持的聚合阶段 $unwind：拒绝静默忽略后返回未聚合的原始行
- **C-05** `[mysql]` group=C：行集不一致
  实际: [{"_id": "c1", "lessons": [{"_id": "l1", "parent": null, "title": "安装"}, {"_id": "l2", "parent": null, "title": "入门"}]}]
  期望: [{"_id": "c1", "lessons": [{"_id": "l1", "title": "安装", "parent": null}, {"_id": "l2", "title": "入门", "parent": {"_id": "l1", "title": "安装"}}]}]
- **C-06** `[mysql]` group=C：执行报错: SQL 下推不支持（mysql）: childLimit；$lookup 关系 lessons 含子 limit/skip（每父 top-N），需窗口函数或 LATERAL，暂未下推（_unsupported:childLimit）
- **C-07** `[mysql]` group=C：执行报错: SQL 后端暂不支持的聚合阶段 $unwind：拒绝静默忽略后返回未聚合的原始行
- **C-08** `[mysql]` group=C：执行报错: SQL 后端暂不支持的聚合阶段 $unwind：拒绝静默忽略后返回未聚合的原始行
- **C-09** `[mysql]` group=C：执行报错: SQL 后端暂不支持的聚合阶段 $unwind：拒绝静默忽略后返回未聚合的原始行
- **D-03** `[mysql]` group=D：执行报错: SQL 后端暂不支持的聚合阶段 $addFields：拒绝静默忽略后返回未聚合的原始行
- **D-03** `[mysql]` group=D：执行报错: SQL 后端暂不支持的聚合阶段 $addFields：拒绝静默忽略后返回未聚合的原始行
- **E-05** `[mysql]` group=E：行集不一致
  实际: [{"_id": "c1", "lessons": [{"_id": "l1"}, {"_id": "l1"}, {"_id": "l2"}, {"_id": "l2"}]}]
  期望: [{"_id": "c1", "lessons": [{"_id": "l1"}, {"_id": "l2"}]}]
- **E-12** `[mysql]` group=E：行集不一致
  实际: [{"_id": "PN1", "label": "pn", "memos": [{"body": "mine", "createdBy": "u1"}, {"body": "other", "createdBy": "u2"}]}]
  期望: [{"_id": "PN1", "label": "pn", "memos": [{"body": "mine", "createdBy": "u1"}]}]
- **G-03** `[mysql]` group=G：行集不一致
  实际: [{"_id": "c7", "categoryId": "cat3", "createdAt": 1789304456114, "createdBy": "u1", "enrolledCount": 0, "price": 0.1, "rating": 1.1, "secret": "SECRET-7", "status": "published", "summary": "base", "title": "基础数学", "updatedAt": 1789304456114}]
  期望: []
- **G-06** `[mysql]` group=G：期望抛错但未抛错
- **G-07** `[mysql]` group=G：行集不一致
  实际: [{"_id": "c1", "categoryId": "cat2", "createdAt": 1789304456427, "createdBy": "u1", "enrolledCount": 0, "price": 9.9, "rating": 4.5, "secret": "SECRET-1", "status": "published", "summary": "必学", "title": "Python入门", "updatedAt": 1789304456427}]
  期望: [{"_id": "c1", "categoryId": "cat2", "createdAt": 1789304445508, "createdBy": "u1", "enrolledCount": 0, "meta": {"cover": "c1.png", "level": "beginner", "seo": {"desc": "从零开始", "title": "看Python"}}, "price": 9.9, "rating": 4.5, "status": "published", "summary": "必学", "tags": ["python", "db"], "title": "Python入门", "updatedAt": 1789304445508}]
- **H-07** `[mysql]` group=H：执行报错: SQL 后端暂不支持的聚合阶段 $unwind：拒绝静默忽略后返回未聚合的原始行
- **H-08** `[mysql]` group=H：执行报错: (1054, "Unknown column 'createdAt' in 'field list'")
- **H-08** `[mysql]` group=H：行集不一致
  实际: []
  期望: [{"_id": "c1"}]
- **H-10** `[mysql]` group=H：行集不一致
  实际: [{"_id": "c7"}]
  期望: [{"_id": "c7", "tags": ["x", "y"], "meta": {"cover": "z", "seo": {"title": "深", "desc": "深描"}}}]
- **A-13** `[sqlite]` group=A：行集不一致
  实际: [{"_id": "c1"}]
  期望: [{"_id": "c1", "tags": ["python", "db"]}]
- **A-14** `[sqlite]` group=A：行集不一致
  实际: [{"_id": "c1"}]
  期望: [{"_id": "c1", "meta": {"cover": "c1.png", "level": "beginner", "seo": {"title": "看Python", "desc": "从零开始"}}}]
- **A-20** `[sqlite]` group=A：期望抛错但未抛错
- **B-05** `[sqlite]` group=B：行集不一致
  实际: [{"_id": "c1"}]
  期望: [{"_id": "c1", "meta": {"cover": "x"}}]
- **B-06** `[sqlite]` group=B：行集不一致
  实际: [{"_id": "c1"}]
  期望: [{"_id": "c1", "tags": ["a", "b"]}]
- **B-08b** `[sqlite]` group=B：行集不一致
  实际: [{"_id": "c1"}]
  期望: [{"_id": "c1", "tags": ["python", "db", "x"]}]
- **B-11** `[sqlite]` group=B：执行报错: table courses_deleted has no column named createdAt
- **B-11** `[sqlite]` group=B：行集不一致
  实际: []
  期望: [{"_id": "c1"}]
- **C-01** `[sqlite]` group=C：执行报错: SQL 后端暂不支持的聚合阶段 $unwind：拒绝静默忽略后返回未聚合的原始行
- **C-03** `[sqlite]` group=C：执行报错: SQL 后端暂不支持的聚合阶段 $unwind：拒绝静默忽略后返回未聚合的原始行
- **C-05** `[sqlite]` group=C：行集不一致
  实际: [{"_id": "c1", "lessons": [{"_id": "l1", "parent": null, "title": "安装"}, {"_id": "l2", "parent": null, "title": "入门"}]}]
  期望: [{"_id": "c1", "lessons": [{"_id": "l1", "title": "安装", "parent": null}, {"_id": "l2", "title": "入门", "parent": {"_id": "l1", "title": "安装"}}]}]
- **C-06** `[sqlite]` group=C：执行报错: SQL 下推不支持（sqlite）: childLimit；$lookup 关系 lessons 含子 limit/skip（每父 top-N），需窗口函数或 LATERAL，暂未下推（_unsupported:childLimit）
- **C-07** `[sqlite]` group=C：执行报错: SQL 后端暂不支持的聚合阶段 $unwind：拒绝静默忽略后返回未聚合的原始行
- **C-08** `[sqlite]` group=C：执行报错: SQL 后端暂不支持的聚合阶段 $unwind：拒绝静默忽略后返回未聚合的原始行
- **C-09** `[sqlite]` group=C：执行报错: SQL 后端暂不支持的聚合阶段 $unwind：拒绝静默忽略后返回未聚合的原始行
- **D-03** `[sqlite]` group=D：执行报错: SQL 后端暂不支持的聚合阶段 $addFields：拒绝静默忽略后返回未聚合的原始行
- **D-03** `[sqlite]` group=D：执行报错: SQL 后端暂不支持的聚合阶段 $addFields：拒绝静默忽略后返回未聚合的原始行
- **E-05** `[sqlite]` group=E：行集不一致
  实际: [{"_id": "c1", "lessons": [{"_id": "l1"}, {"_id": "l1"}, {"_id": "l2"}, {"_id": "l2"}]}]
  期望: [{"_id": "c1", "lessons": [{"_id": "l1"}, {"_id": "l2"}]}]
- **E-12** `[sqlite]` group=E：行集不一致
  实际: [{"_id": "PN1", "label": "pn", "memos": [{"body": "mine", "createdBy": "u1"}, {"body": "other", "createdBy": "u2"}]}]
  期望: [{"_id": "PN1", "label": "pn", "memos": [{"body": "mine", "createdBy": "u1"}]}]
- **G-03** `[sqlite]` group=G：行集不一致
  实际: [{"_id": "c7", "categoryId": "cat3", "createdAt": 1789304458766, "createdBy": "u1", "enrolledCount": 0, "price": 0.1, "rating": 1.1, "secret": "SECRET-7", "status": "published", "summary": "base", "title": "基础数学", "updatedAt": 1789304458766}]
  期望: []
- **G-06** `[sqlite]` group=G：期望抛错但未抛错
- **G-07** `[sqlite]` group=G：行集不一致
  实际: [{"_id": "c1", "categoryId": "cat2", "createdAt": 1789304458828, "createdBy": "u1", "enrolledCount": 0, "price": 9.9, "rating": 4.5, "secret": "SECRET-1", "status": "published", "summary": "必学", "title": "Python入门", "updatedAt": 1789304458828}]
  期望: [{"_id": "c1", "categoryId": "cat2", "createdAt": 1789304445508, "createdBy": "u1", "enrolledCount": 0, "meta": {"cover": "c1.png", "level": "beginner", "seo": {"desc": "从零开始", "title": "看Python"}}, "price": 9.9, "rating": 4.5, "status": "published", "summary": "必学", "tags": ["python", "db"], "title": "Python入门", "updatedAt": 1789304445508}]
- **H-07** `[sqlite]` group=H：执行报错: SQL 后端暂不支持的聚合阶段 $unwind：拒绝静默忽略后返回未聚合的原始行
- **H-08** `[sqlite]` group=H：执行报错: table courses_deleted has no column named createdAt
- **H-08** `[sqlite]` group=H：行集不一致
  实际: []
  期望: [{"_id": "c1"}]
- **H-10** `[sqlite]` group=H：行集不一致
  实际: [{"_id": "c7"}]
  期望: [{"_id": "c7", "tags": ["x", "y"], "meta": {"cover": "z", "seo": {"title": "深", "desc": "深描"}}}]

## 四、与环境变量

- 可复跑：`$env:LOCAL_CORE=1; $env:PYTHONPATH=py-store/src; python -m pytest py-store/tests/test_scenario_course_platform.py -q`
- 后端可达性可用 `MYSQL_URI` / `PG_URI` / `MONGO_URI` 覆盖。

