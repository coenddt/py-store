# py-store 不足清单修复 执行文档

> 日期: 2026-09-26
> 设计依据: 本会话「py-store 不足清单（基线 v2.1.0 / rust-store v2.1.1，2026-09-26 取证）」；代码取证 `py-store/src/py_store/{schema.py,ddl.py,executors/{sqlite,mysql,postgres}.py,datasource.py}`、`rust-store/core/src/schema/registry.rs:48-55,164-166,395-417`；本机实测见 §1
> 性质: 代码执行文档（AI 照此执行）
> 范围: **py-store 本仓库**（Host 侧契约修复 + 新增 CI + 补测试，零 parity 税）；`rust core` 级问题与下游适配项一律转入 §9「处置对照」，本轮不执行
> 不在范围: 修改 `rust-store/core`、`core-py`、`core-node`；下游业务项目的适配层 / Alembic / fail-secure 落地

## 1. 目标（验收标准）

1. **SQL 写路径提交契约**：`sqlite` / `mysql` 执行器的 `exec_`（非事务路径）在 plan 成功后显式提交，失败回滚后上抛；新增「落盘 + 跨连接读回」回归用例通过（证明写入不丢）。
2. **归档表不再重复**：`store.register({...})` 后 `store.list()` 无重复名；`generate_ddl(backend)` 无重复 `CREATE TABLE`。
   - 基线实测（2026-09-26，本机 `PYTHONPATH=src`）：
     `store.list()` → `['User', 'UserDeleted', 'UserDeleted']`；`store.generate_ddl('sqlite')` 输出**两条** `CREATE TABLE "users_deleted"`。
3. **纵深防御告警**：`schema.list()` / `ddl.generate()` 若仍检出重复（上游失守），去重同时 `feedback.emit` 一条告警（同签名只告警一次），禁静默。
4. **CI 门禁**：新增 `.github/workflows/ci.yml`，`push(main)` / `pull_request` / `workflow_dispatch` 触发；起真实 MySQL/PostgreSQL/MongoDB 容器；跑 ruff + mypy + pytest；**语句覆盖率门禁 ≥90%**，另单独出具分支覆盖率口径。
5. **覆盖率口径**：语句覆盖率 ≥90%（本机基线 90%），分支覆盖率单独出具（本机基线 88%）。
6. **stress 纳入 lint**：`ruff check src tests stress` 零告警（修复 `stress/stress.py` 现有 7 处，实测统计 `1×I001 + 6×RUF100`，7 处全 `--fix` 可修）。
7. **无回归**：py 全量 pytest 无新增失败；`mypy src/py_store` 通过。

## 2. 涉及端 × 角色

| 端 | 是否涉及 | 角色 | 说明 |
|---|---|---|---|
| py-store（Host） | 是 | 数据层 | 执行器提交契约、schema/ddl 去重、CI、测试、覆盖率 |
| rust-store core | 否 | — | **不碰**（零 parity 税）；core 级问题见 §9 受限项 |
| nodejs-store（镜像端） | 否 | — | 本次仅 py 侧；node 侧同类问题另案（`nodejs-store/.github/workflows/ci.yml` 已存在，可作 CI 蓝本） |
| 下游业务项目 | 否 | — | 适配项（适配层 / Alembic / fail-secure）见 §9，不在本仓库执行 |

## 3. 迁移与移除清单（动手清单）

### 3.1 新增清单
| 新增项 | 所在文件/模型 | 说明 |
|---|---|---|
| CI 工作流 | `py-store/.github/workflows/ci.yml` | push/PR 质量门禁（ruff+mypy+pytest，语句覆盖 ≥90%） |
| 回归测试 | `py-store/tests/test_sql_commit_and_registry.py` | 提交契约（落盘跨连接读回 / 失败回滚）+ 注册唯一性（含去重告警） |

### 3.2 修改清单
| 修改项 | 文件 | 由 → 到 |
|---|---|---|
| `exec_` 显式提交 | `py-store/src/py_store/executors/sqlite.py` | `return await run_stmts(plan)` → try/rollback + `await db.commit()` |
| `exec_` 显式提交 | `py-store/src/py_store/executors/mysql.py` | 仅 `run_stmts` → try/rollback + `await conn.commit()` |
| 归档表不再重复注册 core | `py-store/src/py_store/schema.py` | `register(archive_defn)` 递归 → 直接写 `_schemas['<Name>Deleted']` 镜像 |
| `list` 顺序去重 + 告警 | `py-store/src/py_store/schema.py` | `return core.list()` → 顺序去重 + `feedback.emit('schemaDuplicateName')` |
| `generate` 表名去重 + 告警 | `py-store/src/py_store/ddl.py` | 逐表 join → 按 `collection` 去重 + `feedback.emit('ddlDuplicateTable')` |
| dev 依赖 + 覆盖率配置 | `py-store/pyproject.toml` | `dev=[pytest,ruff,mypy]` → 追加 `pytest-cov>=5`；新增 `[tool.coverage.run]` / `[tool.coverage.report]` |
| 覆盖率产物移出版本库 | `py-store/.gitignore` | 新增 `.coverage` 条目（配套 `git rm --cached .coverage`：该文件为历史误提交且被跟踪） |
| ruff 范围纳入 stress | `py-store/.github/workflows/release-pypi.yml` | `ruff check src tests` → `ruff check src tests stress` |
| stress 7 处 lint | `py-store/stress/stress.py` | 7 处告警 → `ruff check stress --fix` 后零告警 |
| 覆盖率 margin | `py-store/tests/*` | 按 `term-missing` 补分支断言（见步骤 7） |
| README / CHANGELOG 落档 | `py-store/README.md`、`README.zh-CN.md`、`CHANGELOG.md` | 补提交契约与归档派生两条说明 + 版本记录 |

### 3.3 删除清单
| 删除项 | 位置 | 原因 | 替代 |
|---|---|---|---|
| （无） | — | — | — |

## 4. 详细执行契约（代码优先）

### 4.1 py-store：`executors/sqlite.py` — `exec_` 显式提交（唯一默认非 autocommit 的驱动）

```python
    async def exec_(plan):
        """非事务路径：plan 成功后**显式提交**（aiosqlite 默认非 autocommit，
        不提交则仅当前连接可见 —— 落盘场景会丢数据/读到幻象，且无任何告警）；
        失败则回滚后上抛，绝不提交半截写入"""
        try:
            out = await run_stmts(plan)
        except BaseException:
            await db.rollback()
            raise
        await db.commit()
        return out
```

要点：
- 只改 `exec_`；`with_transaction` 路径不受影响（事务内 `datasource._exec_of` 取到的是 `run_stmts` 包装，非 `exec_`，见 §8.2）。
- `with_transaction` 开头的 `await db.commit()`（清理遗留隐式事务）保留不动。

### 4.2 py-store：`executors/mysql.py` — `exec_` 显式提交（驱动无关，池 autocommit 下幂等）

```python
    async def exec_(plan):
        """非事务路径：整个 plan 固定在同一连接执行，成功后显式 commit
        （池 autocommit=True 时为幂等 no-op；单连接 autocommit=False 时杜绝「只执行不提交」）；
        失败则 rollback 后上抛，绝不提交半截写入"""
        async with acquire(driver) as conn:
            try:
                out = await run_stmts(conn, plan)
            except BaseException:
                await conn.rollback()
                raise
            await conn.commit()
            return out
```

要点：`asyncmy` 连接同时具备 `commit()` / `rollback()`；`postgres`（asyncpg）**不改逻辑**——无显式事务块时每次 `fetch/execute` 均隐式 autocommit，写入已提交（见 §8.1）。

### 4.3 py-store：`schema.py` — 归档表镜像直写（不再重复注册 core）+ `list` 去重告警

顶部 import 追加：

```python
from .core import core
from .feedback import emit as _emit_feedback
```

`register` 全量替换为：

```python
def register(defn):
    """注册一个 schema（core 注册 + Host 侧元数据镜像）

    归档表 `<Name>Deleted` 由 core 在 register 内**自动派生并注册**
    （rust-store/core/src/schema/registry.rs:52-55）；Host 只补 Host 侧镜像，
    **不再调用 core.register** —— 否则同名条目二次进入 core.order，使 list()/
    generate_ddl() 出现重复表（基线实测 list=['User','UserDeleted','UserDeleted']）。
    """
    core.register(_to_core_defn(defn))

    # 计算列回调：fn → core 回调桥；asyncFn → Host 侧映射
    computes = {}
    for key, val in (defn.get('computes') or {}).items():
        fn_ref = val.get('fnRef') or key
        if val.get('fn'):
            core.set_fn(fn_ref, val['fn'])
        if val.get('asyncFn'):
            _async_fns[fn_ref] = val['asyncFn']
        computes[key] = {'fnRef': fn_ref}

    _schemas[defn['name']] = {
        'name': defn['name'],
        'collection': defn.get('collection') or defn['name'],
        'namespace': defn.get('namespace') or None,
        'idPrefix': defn.get('idPrefix') or '',
        'timestamps': defn.get('timestamps') is not False,
        'timestampUnit': 's' if defn.get('timestamps') == 's' else (
            None if defn.get('timestamps') is False else 'ms'),
        'fields': defn.get('fields') or {},
        'relations': defn.get('relations') or {},
        'computes': computes,
        'indexes': defn.get('indexes') or [],
        'read': defn.get('read'),
        'write': defn.get('write'),
        'datasource': defn.get('datasource'),
    }

    # 归档表镜像（形状对齐 core archive_defn：registry.rs:395-417）
    # —— 字段 + deletedAt、collection=<c>_deleted、idPrefix=''、timestamps 默认 true
    if not defn.get('_isArchive') and not defn['name'].endswith('Deleted'):
        _schemas[f"{defn['name']}Deleted"] = {
            'name': f"{defn['name']}Deleted",
            'collection': f"{defn.get('collection') or defn['name']}_deleted",
            'namespace': defn.get('namespace') or None,
            'idPrefix': '',
            'timestamps': True,
            'timestampUnit': 'ms',
            'fields': {**(defn.get('fields') or {}), 'deletedAt': {'type': 'number'}},
            'relations': {},
            'computes': {},
            'indexes': defn.get('indexes') or [],
            'read': None,
            'write': None,
            'datasource': defn.get('datasource'),
        }

    return _schemas[defn['name']]
```

新增模块级告警去重签名集合，并替换 `list`：

```python
# 去重告警签名（同一重复形态只告警一次，避免 list() 高频调用刷屏）
_dup_signatures: set = set()


def list():
    """所有已注册 schema 名称（core 侧，含归档表，按注册顺序；同名只保留首次出现）

    去重是纵深防御的第二层：一旦检出重复即说明上游（register/core）失守，
    去重同时 emit 告警（同签名只告警一次），禁静默。
    """
    names = core.list()
    seen = set()
    out = []
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        out.append(name)
    if len(out) != len(names):
        sig = tuple(names)
        if sig not in _dup_signatures:
            _dup_signatures.add(sig)
            _emit_feedback({
                'type': 'schema_duplicate_name',
                'code': 'schemaDuplicateName',
                'layer': 'host',
                'message': f'schema 注册表存在重复名（多 {len(names) - len(out)} 条），已顺序去重',
                'hint': ('上游注册逻辑失守（core.order 同名两次）；'
                         '核查 register 是否重复调用 core.register，或 core.register 未对同名去重'),
                'names': names,
            })
    return out
```

### 4.4 py-store：`ddl.py` — `generate` 按表名去重 + 告警

```python
def generate(backend, names=None):
    """生成 DDL 文本（多表以空行分隔）；backend ∈ mysql/postgres/sqlite

    按表名去重：同名表只出一次 CREATE TABLE（防御 core 注册表出现重复名 ——
    上游失守即告警，禁静默）。
    """
    if backend not in _BACKENDS:
        raise ValueError(f'DDL 生成：不支持的后端 {backend!r}（支持 {list(_BACKENDS)}）')
    targets = list(names) if names else list(_list_schema())
    blocks = []
    seen_tables = set()
    dup = []
    for n in targets:
        defn = _get_schema(n)
        table = defn.get('collection') or n
        if table in seen_tables:
            dup.append(table)
            continue
        seen_tables.add(table)
        blocks.append(_create_table(defn, backend))
    if dup:
        _emit_feedback({
            'type': 'ddl_duplicate_table',
            'code': 'ddlDuplicateTable',
            'layer': 'host',
            'message': f'DDL 生成：表 {sorted(set(dup))} 重复注册，已去重',
            'hint': 'schema 注册表出现重复名（见 schemaDuplicateName 告警）；修复注册侧根因',
            'backend': backend,
        })
    return '\n\n'.join(blocks)
```

### 4.5 py-store：新增 `tests/test_sql_commit_and_registry.py`

```python
"""修复回归：① SQL 写路径提交契约（落盘 + 跨连接读回 / 失败回滚）
          ② 归档表注册唯一性（list/DDL 无重复 + 去重告警）

依据执行文档：doc/execution/2026/09/已完成-py-store不足清单修复-执行.md §4.5
运行：PYTHONPATH=src python -m pytest tests/test_sql_commit_and_registry.py -q
"""

import asyncio

import aiosqlite
import pytest

from py_store import datasource, executors, feedback, store
from py_store import schema as schema_mod


@pytest.fixture(autouse=True)
def _isolate_globals():
    datasource.set_connections({})
    feedback.set_sink(None)
    yield
    datasource.set_connections({})
    feedback.set_sink(None)


def _run(coro):
    return asyncio.run(coro)


# ─── ① SQL 写路径提交契约 ────────────────────────────────────

def test_sqlite_write_persists_across_connections(tmp_path):
    """写经 store.execute_raw 落盘后，**新连接**必须读到（证明 exec_ 已提交）"""
    db_path = tmp_path / 'persist.db'

    async def scenario():
        db = await aiosqlite.connect(db_path)
        datasource.set_connections({'default': executors.create_connection('sqlite', db)})
        await store.execute_raw(
            'default', 'CREATE TABLE t (_id TEXT PRIMARY KEY, v TEXT)', is_write=True)
        await store.execute_raw(
            'default', "INSERT INTO t (_id, v) VALUES ('1', 'a')", is_write=True)
        await db.close()

        db2 = await aiosqlite.connect(db_path)
        try:
            cur = await db2.execute('SELECT v FROM t WHERE _id = ?', ('1',))
            rows = await cur.fetchall()
            await cur.close()
        finally:
            await db2.close()
        return rows

    assert _run(scenario()) == [('a',)]


def test_sqlite_plan_failure_rolls_back(tmp_path):
    """plan 第 2 条语句失败 → 第 1 条已写入的行**不得**落盘（rollback 生效）"""
    db_path = tmp_path / 'rollback.db'

    async def scenario():
        db = await aiosqlite.connect(db_path)
        conn = executors.create_connection('sqlite', db)
        datasource.set_connections({'default': conn})
        await store.execute_raw('default', 'CREATE TABLE t (_id TEXT PRIMARY KEY)', is_write=True)

        with pytest.raises(aiosqlite.OperationalError):
            await conn['exec']({'stmts': [
                {'text': "INSERT INTO t (_id) VALUES ('x')"},
                {'text': 'THIS IS NOT SQL'},
            ]})
        await db.close()

        db2 = await aiosqlite.connect(db_path)
        try:
            cur = await db2.execute('SELECT COUNT(*) FROM t')
            n = (await cur.fetchone())[0]
            await cur.close()
        finally:
            await db2.close()
        return n

    assert _run(scenario()) == 0


# ─── ② 归档表注册唯一性 ─────────────────────────────────────

def test_register_does_not_duplicate_archive():
    store.register({
        'name': 'RegUniq', 'collection': 'reg_uniq', 'idPrefix': 'ru',
        'fields': {'_id': {'type': 'string'}, 'name': {'type': 'string'}},
    })
    names = store.list()
    assert names.count('RegUniqDeleted') == 1
    assert len(names) == len(set(names))


def test_generate_ddl_has_no_duplicate_table():
    store.register({
        'name': 'RegDdl', 'collection': 'reg_ddl', 'idPrefix': 'rd',
        'fields': {'_id': {'type': 'string'}},
    })
    sql = store.generate_ddl('sqlite', ['RegDdl', 'RegDdlDeleted'])
    assert sql.count('CREATE TABLE "reg_ddl"') == 1
    assert sql.count('CREATE TABLE "reg_ddl_deleted"') == 1


def test_registry_dedupe_emits_feedback():
    """防御网：core 注册表若仍出现重复名 → list() 去重并告警（禁静默）"""
    events = []
    feedback.set_sink(events.append)
    defn = {'name': 'RegDup', 'collection': 'reg_dup', 'idPrefix': 'rq',
            'fields': {'_id': {'type': 'string'}}}
    # 绕过 Host register（其已修），直调 core 两次制造重复
    schema_mod.core.register(defn)
    schema_mod.core.register(defn)

    assert store.list().count('RegDup') == 1
    assert any(e.get('code') == 'schemaDuplicateName' for e in events), events
```

### 4.6 py-store：新增 `.github/workflows/ci.yml`

```yaml
name: ci

# py-store 质量门禁（不足清单 #7 / #25）：
#   1. ruff（零告警，规则见 pyproject.toml [tool.ruff]）
#   2. mypy（见 [tool.mypy]）
#   3. pytest + 覆盖率门禁（语句 ≥90%；分支口径单独出具）
#
# 真实三库由 services 起容器，库名/账号与测试默认 URI 对齐（e2e/e2e123，库 mongo_store_e2e）；
# e2e 表名带随机 token 后缀，多实例互不干扰。
# core 走 PyPI 发布的 rust-store-py（随 `pip install -e .[dev,...]` 安装），无需 LOCAL_CORE。
on:
  push:
    branches: [main]
  pull_request:
  workflow_dispatch:

permissions:
  contents: read

jobs:
  lint-type-test:
    name: lint + type + test (coverage gate)
    runs-on: ubuntu-latest
    services:
      mongo:
        image: mongo:7
        ports:
          - 27017:27017
      mysql:
        image: mysql:8
        env:
          MYSQL_ROOT_PASSWORD: root
          MYSQL_USER: e2e
          MYSQL_PASSWORD: e2e123
          MYSQL_DATABASE: mongo_store_e2e
        ports:
          - 3306:3306
        options: >-
          --health-cmd="mysqladmin ping -h 127.0.0.1 -uroot -proot"
          --health-interval=10s
          --health-timeout=5s
          --health-retries=12
      postgres:
        image: postgres:16
        env:
          POSTGRES_USER: e2e
          POSTGRES_PASSWORD: e2e123
          POSTGRES_DB: mongo_store_e2e
        ports:
          - 5432:5432
        options: >-
          --health-cmd="pg_isready -h 127.0.0.1 -U e2e"
          --health-interval=10s
          --health-timeout=5s
          --health-retries=12
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      # 必须装全 DB extras：否则 e2e 因缺驱动全 skip → 覆盖率崩塌 → 门禁误红
      - name: Install with dev + db extras
        run: python -m pip install -e ".[dev,mysql,postgres,sqlite]"
      - name: Ruff check
        run: python -m ruff check src tests stress
      - name: Mypy check
        run: python -m mypy src/py_store
      - name: Pytest + coverage gate (statements >= 90)
        run: python -m pytest --cov=py_store --cov-report=term-missing --cov-fail-under=90
      - name: Branch coverage (informational)
        run: python -m pytest --cov=py_store --cov-branch --cov-report=term-missing --cov-fail-under=0
```

### 4.7 py-store：`pyproject.toml` — dev 依赖与覆盖率配置

`[project.optional-dependencies]` 的 `dev` 行：

```toml
dev = ["pytest>=8", "pytest-cov>=5", "ruff>=0.16", "mypy>=1.0"]
```

文件末尾追加：

```toml
# ── 覆盖率（门禁口径：语句 ≥90%，见 .github/workflows/ci.yml；分支单独出具） ──
[tool.coverage.run]
source = ["py_store"]
relative_files = true

[tool.coverage.report]
show_missing = true
exclude_lines = [
  "pragma: no cover",
  "if TYPE_CHECKING:",
]
```

### 4.8 py-store：`stress/stress.py` — 修复 7 处 lint

执行 `python -m ruff check stress --fix`，得到（实测）零告警；7 处明细：

```
stress\stress.py:28:1: I001 [*] Import block is un-sorted or un-formatted
stress\stress.py:28:73: RUF100 [*] Unused `noqa` directive (unused: `E402`)
stress\stress.py:30:19: RUF100 [*] Unused `noqa` directive (unused: `E402`)
stress\stress.py:31:17: RUF100 [*] Unused `noqa` directive (unused: `E402`)
stress\stress.py:32:17: RUF100 [*] Unused `noqa` directive (unused: `E402`)
stress\stress.py:33:39: RUF100 [*] Unused `noqa` directive (unused: `E402`)
stress\stress.py:261:29: RUF100 [*] Unused `noqa` directive (non-enabled: `BLE001`)
```

要点：ruff 对「`sys.path.insert` 前置的 import」不报 E402（pycodestyle 例外），故这些 `# noqa: E402` 属多余，`--fix` 直接删除；`# noqa: BLE001` 因规则未启用亦删除。勿手工保留。

## 5. 执行步骤（逐步骤状态，可中断续做）

**执行期间主文档只读：状态一律记录在外置账本 `tmp/execution-status-py-store不足清单修复.md`（步骤 | todo/doing/done | 完成时间 | 备注）；下表 `[x]` 由收尾动作一次性勾选，执行中不编辑本表。**

| 步骤 | 状态 | 详细说明 |
|---|---|---|
| 1. py：sqlite `exec_` 提交契约 | [x] 已完成 | **做什么**：`exec_` 改为 try/rollback + `await db.commit()`（见 §4.1） <br>**涉及文件**：`py-store/src/py_store/executors/sqlite.py` <br>**验证方式**：`PYTHONPATH=src` 手验落盘 + 新连接读回（第 6 步用例固化） |
| 2. py：mysql `exec_` 提交契约 | [x] 已完成 | **做什么**：`exec_` 加 try/rollback + `await conn.commit()`（见 §4.2） <br>**涉及文件**：`py-store/src/py_store/executors/mysql.py` <br>**验证方式**：`python -c "import py_store.executors.mysql"`；真实提交语义由 CI 真实 MySQL e2e 覆盖 |
| 3. py：归档表镜像不再重复注册 core | [x] 已完成 | **做什么**：`register` 去掉递归 `core.register(archive)`，改直写 `_schemas['<Name>Deleted']`（见 §4.3） <br>**涉及文件**：`py-store/src/py_store/schema.py` <br>**验证方式**：`store.register({...}); store.list()` 无重复名 |
| 4. py：`schema.list` 顺序去重 + 告警 | [x] 已完成 | **做什么**：`list()` 顺序去重；检出重复 emit `schemaDuplicateName`（同签名一次）（见 §4.3） <br>**涉及文件**：`py-store/src/py_store/schema.py` <br>**验证方式**：直调 `core.register` 两次同名 → `store.list()` 无重复且 sink 收到告警 |
| 5. py：`ddl.generate` 表名去重 + 告警 | [x] 已完成 | **做什么**：按 `collection` 去重并 emit `ddlDuplicateTable`（见 §4.4） <br>**涉及文件**：`py-store/src/py_store/ddl.py` <br>**验证方式**：`store.generate_ddl('sqlite')` 中 `CREATE TABLE "users_deleted"` 仅 1 次 |
| 6. py：新增提交/注册回归用例 | [x] 已完成 | **做什么**：新建 `tests/test_sql_commit_and_registry.py`（5 用例，见 §4.5） <br>**涉及文件**：`py-store/tests/test_sql_commit_and_registry.py` <br>**验证方式**：`PYTHONPATH=src python -m pytest tests/test_sql_commit_and_registry.py -q` 全绿 |
| 7. py：补齐覆盖 margin | [x] 已完成 | **做什么**：按 `--cov-report=term-missing` 补真实分支断言，优先缺口最大的模块（本机实测语句口径）：`core.py` 41%、`crud/query.py` 73%、`introspect/postgres.py` 73%、`sync.py` 81%、`executors/__init__.py` 84%、`executors/mysql.py` 83%；目标语句 ≥91%（给门禁留余量） <br>**涉及文件**：`py-store/tests/*`（按缺口定） <br>**验证方式**：`PYTHONPATH=src python -m pytest --cov=py_store --cov-report=term-missing -q` 语句 ≥91%（基线 90%） |
| 8. py：新增 CI 工作流 | [x] 已完成 | **做什么**：新建 `.github/workflows/ci.yml`（见 §4.6） <br>**涉及文件**：`py-store/.github/workflows/ci.yml` <br>**验证方式**：YAML 可解析；本地逐条跑等价命令全绿 |
| 9. py：dev 依赖 + 覆盖率配置 | [x] 已完成 | **做什么**：`pyproject.toml` dev 加 `pytest-cov>=5`；追加 `[tool.coverage.run]` / `[tool.coverage.report]`（见 §4.7） <br>**涉及文件**：`py-store/pyproject.toml` <br>**验证方式**：`python -m pytest --cov=py_store` 正常出表 |
| 10. py：stress 修复 7 处 lint | [x] 已完成 | **做什么**：`python -m ruff check stress --fix`（见 §4.8） <br>**涉及文件**：`py-store/stress/stress.py` <br>**验证方式**：`python -m ruff check stress` → All checks passed |
| 11. py：lint 范围纳入 stress | [x] 已完成 | **做什么**：`release-pypi.yml` 的 `ruff check src tests` → `ruff check src tests stress`（与第 8 步 ci.yml 一致） <br>**涉及文件**：`py-store/.github/workflows/release-pypi.yml` <br>**验证方式**：`python -m ruff check src tests stress` 零告警 |
| 12. 全量回归 | [x] 已完成 | **做什么**：跑 py 全量 pytest + ruff + mypy，确认无新增失败 <br>**涉及文件**：无改动 <br>**验证方式**：`PYTHONPATH=src python -m pytest -q`；`python -m ruff check src tests stress`；`python -m mypy src/py_store` |
| 13. README / CHANGELOG 落档 | [x] 已完成 | **做什么**：README 补「SQL 写路径提交契约（非事务路径显式提交）」与「归档表由 core 自动派生、Host 只补镜像不重复注册」；CHANGELOG 记本次修复 <br>**涉及文件**：`py-store/README.md`、`py-store/README.zh-CN.md`、`py-store/CHANGELOG.md` <br>**验证方式**：文字与实现一致，无悬空描述 |

### 步骤说明

- **git 提交**：本仓库即 `py-store/`（独立仓库，分支 `main`，工作树起始干净）。所有步骤在该仓库内提交，提交信息 `py-store不足清单修复 步骤N：<步骤名>`；**不推送、不切分支**；`git add <本步具体文件>`（禁 `git add -A` / `git add .`；账本不入库）。
- **边界**：`exec_` 只加提交/回滚，不改 SQL 生成、不改结果塑形；`register` 只改归档镜像落地方式，不改业务镜像字段。
- **兼容**：不改任何公开签名；`set_require_context` 等既有 API 不动。

## 6. 实施顺序（阶段依赖）

| 阶段 | 内容 | 依赖 |
|---|---|---|
| A | 契约修复（步骤 1–5） | 无 |
| B | 回归用例（步骤 6） | A |
| C | 覆盖 margin（步骤 7） | B |
| D | CI 与 lint 门禁（步骤 8–11） | C |
| E | 全量回归 + 落档（步骤 12–13） | D |

## 7. 禁止事项

❌ 禁止修改 `rust-store/core`、`core-py`、`core-node` 任何文件（零 parity 税）
❌ 禁止把 `exec_` 提交改为「依赖驱动默认 autocommit」或全局改 autocommit（必须显式 commit/rollback）
❌ 禁止在 `exec_` 失败时提交半截写入（必须 rollback 后上抛）
❌ 禁止在 `schema.register` 内再对归档表调用 `core.register`
❌ 禁止为过覆盖率门禁而删测试、加 `# pragma: no cover` 或下调 `--cov-fail-under`（必须补真实分支断言）
❌ 禁止改动 `storepy` / `py_store` / `py-store` 任一名称（PyPI 近似名规则）
❌ 禁止 `git add -A` / `git add .`；禁止提交 `tmp/` 账本
❌ 禁止手工保留 `stress.py` 的 `# noqa: E402` / `# noqa: BLE001`（实测均多余）

## 8. 注意事项

1. **PG 为何不改**：`asyncpg` 无显式事务块时每次语句独立隐式 autocommit（提交语义已满足）；`aiosqlite` 是唯一默认非 autocommit 的驱动，`asyncmy` 取决于池/连接配置（仓库自测池显式 `autocommit=True`，见 `tests/test_real_backends_e2e.py:191`）——故 `exec_` 改动覆盖 sqlite（必需）+ mysql（防御）。
2. **事务路径不受影响**：`datasource.run_in_transaction` 把事务连接以 `{kind, exec}` 覆盖注入 ContextVar，`_exec_of` 取到的是 `run_stmts` 包装（`datasource.py:194-200`），事务内不会走到 `exec_`，故新增的 commit 不会提前提交事务。
3. **归档镜像须与 core 同形**：Host 镜像字段/默认值须对齐 `registry.rs:395-417`（`deletedAt`、`collection=<c>_deleted`、`idPrefix=''`、timestamps 默认 true），否则 Host 元数据与 core 漂移，`ddl.generate` 出表不一致。
4. **去重是第二层防御**：根因（重复注册）已在步骤 3 修掉；`list()` / `generate()` 去重仅防御，命中即 emit 告警（禁静默失守）；同签名只告警一次。
5. **CI 必须装 DB extras**：`pip install -e ".[dev,mysql,postgres,sqlite]"`；否则 e2e 因缺驱动全 skip，覆盖率崩塌并使门禁误红。
6. **覆盖率口径**：本机实测（Python 3.14.4，驱动全装）语句 **90%**（1083 stmts / 107 miss）；语句+分支合并 **88%**（362 分支 / 55 miss）。门禁取语句口径 90%（`--cov-fail-under` 在未启用 branch 时即语句口径），分支口径由单独一步出具（非门禁）。
7. **金额/类型契约**：`executors/_values.py` 的 DECIMAL/NUMERIC 归一已修，本轮仅复核，不重复改（见 §9 #24）。

## 9. 清单条目处置对照（29 条 + 待核实 → 本轮执行 / 受限项 / 已核对）

| # | 清单条目 | 处置 | 依据 / 落点 |
|---|---|---|---|
| 1 | SQL 单条写命令不提交事务（P0） | **本轮执行** | 步骤 1–2；契约 §4.1–4.2 |
| 2 | 归档表被重复注册（P0） | **本轮执行** | 步骤 3–5；契约 §4.3–4.4；基线实测复现 |
| 3 | object/array 字段 SQL 后端不支持读写 | 受限项（core） | 需 core `dialect` 落 JSON 列（MySQL JSON/PG jsonb/SQLite TEXT+JSON1），本轮不碰 core |
| 4 | 关系谓词仅一级；数组/对象点号过滤被拒 | 受限项（core） | 需 core CTE/嵌套 EXISTS 下推；本轮不碰 core |
| 5 | 移除了 `$pipeline` 与 `store.aggregate()` | 受限项（core） | `pipeline/parse.rs:62-69` 显式拒绝；逃生舱属 core 设计 |
| 6 | `route_override` 无来源校验（CWE-639 面） | 受限项（core） | 需 core 加来源签名/受限注入；本轮不碰 core |
| 7 | 无 CI 测试工作流，质量门禁为零 | **本轮执行** | 步骤 8；新增 `ci.yml`（`release-pypi.yml` 仅在 tag/dispatch 跑门禁，push/PR 无） |
| 8 | 版本节奏带破坏性变更 | 下游 | 项目内适配层隔离 API，见下文「下游适配建议」 |
| 9 | schema 是运行时 dict，无静态类型与校验 | 下游 | 下游用 Pydantic 做业务校验，见「下游适配建议」 |
| 10 | 分发名 `storepy` ≠ import `py_store` ≠ 仓库名 `py-store` | **已核对（无需改）** | `README.md:18/150/519-520` 已标注；分发名受 PyPI 近似名规则限制（`release-pypi.yml:13-14` 备注） |
| 11 | 无迁移/DDL 引擎 | 下游 | 接 Alembic；py-store 只做运行时访问层 |
| 12 | SQL 后端索引仅元数据 | 受限项（设计内） | 铁律 6：SQL 后端不建索引，`ddl.py` 明确不生成 `CREATE INDEX` |
| 13 | 无 identity map / 变更跟踪 / 懒加载 | 受限项（设计内） | 薄数据层定位，非 ORM |
| 14 | 无 session 级工作单元 / savepoint | 受限项（设计内） | 事务仅单 SQL 源（`datasource.run_in_transaction`） |
| 15 | Mongo 多步写非原子 | 受限项（设计内） | 需 replica set；文档已述 |
| 16 | 无悲观锁 API | **已缓解** | `store.execute_raw`（事务内）可 `SELECT ... FOR UPDATE`（`datasource.py:248-268`） |
| 17 | 无查询构建器链（GQL 为字符串） | 受限项（能力缺口） | 属新增能力，非修复 |
| 18 | 后端仅 4 种；Mongo 跨库联邦上限 10 万行 | 受限项（能力缺口） | 属新增能力，非修复 |
| 19 | 权限默认 fail-open | **已具备开关** | `schema.set_require_context` / `store.set_require_context` 已有；下游须启动即开启（见「下游适配建议」） |
| 20 | 权限错误靠字符串前缀 `ERR_PERMISSION:` 映射 | 受限项（core） | 需 core 出结构化错误码（`crud/exec.py:24-27` 现按前缀映射） |
| 21 | 角色为字符串、未归一默认放行 | 受限项（core） | 需 core 显式枚举角色 |
| 22 | 大结果集读慢（FFI 逐行 `restore_rows`） | 受限项（core） | 需 core 批量编码/一次 FFI；`executors/sqlite.py:43` 现为逐计划调用 |
| 23 | 批量写吞吐偏低 | 受限项（core） | 需 core 多值 INSERT / 批量绑定 |
| 24 | 绑定层类型契约脆弱（DECIMAL/NUMERIC） | **已修（本轮复核）** | `executors/_values.py` 已归一 |
| 25 | 覆盖率 88% 未达 90%，无分支口径 | **本轮执行** | 步骤 7–9；门禁 90% + 分支口径单独出具 |
| 26 | E2E 用固定表名 + 破坏性 DDL，多进程互踩 | **已修复（本轮核对）** | `tests/test_real_backends_e2e.py:33`、`test_federation_e2e.py:32` 已用随机 `E2E_TOKEN` 后缀，逐实例只建/清自己的表 |
| 27 | 测试用 `:memory:` 掩盖「不提交」缺陷 | **本轮执行** | 步骤 6：新增「落盘 + 跨连接读回」用例 |
| 28 | 缺事务失败注入 / 边界极值 / 超深超批量载荷 | **部分本轮** | 步骤 6 覆盖「事务失败注入（回滚）」；超深/超批量载荷属受限项（后续专项） |
| 29 | `stress/stress.py` 游离于 lint 范围（7 处） | **本轮执行** | 步骤 10–11 |

### 待核实（本轮不涉及，改造前需实测）
- **D-07**：`$limit`/`$skip` 非数值是否校验——源码层未见校验（core `pipeline/util.rs`），需实测。
- **D-03**：无 `idPrefix` 且无 `_id` 时的最终行为（core `command/mutate/mod.rs` 要求 `idPrefix` 非空）。
- **MySQL/PostgreSQL 提交语义**：本轮由 CI 真实库 e2e 覆盖（步骤 8）；性能绝对值不测。
- **下游项目 MySQL 池是否显式 `autocommit=True`**：由下游自测。

### 下游适配建议（不属于本仓库执行，供「另一项目」参考路线）
1. 锁版本 + 加 DataLayer 适配层，业务不直连 GQL/API；
2. 启动即 `set_require_context(True)`，内部任务走 `run_as_internal`；
3. 所有 SQL 源统一走 `transaction()` 或确保 autocommit，杜绝静默不落盘（本仓库已修 `exec_`，下游仍建议显式事务）；
4. 迁移/DDL 交给 Alembic，py-store 只做运行时访问层；
5. 性能分层：报表大结果读与批量 ETL 走原生 SQL / SQLAlchemy Core，高频点查 + 权限 + AI 问数走 py-store；
6. 自建 CI 门禁并补齐测试缺口（本仓库 `ci.yml` 可作蓝本）。
