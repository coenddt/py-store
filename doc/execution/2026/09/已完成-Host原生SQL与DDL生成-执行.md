# Host 原生 SQL 与 DDL 生成 执行文档

> 日期: 2026-09-26
> 设计依据: 本会话取证结论（R1–R6）+ `py-store/README.md:107-142/444-451/502`、`rust-store/core/src/dialect/{mod.rs,write/insert.rs,ir.rs}`、`rust-store/core/src/schema/registry.rs:243-255,393-417`
> 性质: 代码执行文档（AI 照此执行）
> 范围: py-store + nodejs-store 两端 Host（**不碰 rust core**，零 parity 税）
> 不在范围: ③ 入口 Pydantic 校验（可选，未要求）；raw `$pipeline`（永不放开）

## 1. 目标（验收标准）

1. `store.transaction(source, fn)` + `store.execute_raw(source, sql, params, is_write)` 在 py-store 与 nodejs-store 均可公开调用；事务内 `execute_raw` 落到事务专用连接（可 `SELECT ... FOR UPDATE`），任一失败整体回滚。
2. 非 SQL 源（Mongo）调用 `execute_raw` **显式报错**（不静默）；无 `exec` 执行器时显式报错。
3. 新增 `ddl.generate(backend, names)`（py/node）从已注册 schema def 生成 MySQL/PG/SQLite 的 `CREATE TABLE` 文本；**两端对同一 schema 输出逐字节一致**（0 字节差异）。
4. 生成器契约：只对 scalar 建列、每表必建 `__present`、`timestamps !== false` 必补 `createdAt/updatedAt`、归档表 `<collection>_deleted` 逐表生成、**不生成 `CREATE INDEX`**。
5. 越界/异常显式处理：未知字段类型 → 抛错；MySQL `__present` 预估长度 > 255 → 走 `feedback.emit` 告警（不静默）。
6. 两端单测通过：py `pytest py-store/tests/test_raw_sql_and_ddl.py` 全绿；node `node --test nodejs-store/tests/raw-sql-ddl.test.js` 全绿。
7. 两端既有测试不回归：py 全量 pytest 无新增失败；node 既有 `npm test` 无新增失败。

## 2. 涉及端 × 角色

| 端 | 是否涉及 | 角色 | 说明 |
|---|---|---|---|
| py-store（Host） | 是 | 数据层 | 新增 `execute_raw` / `transaction` / `ddl` |
| nodejs-store（Host） | 是 | 数据层 | 同构镜像（保持两端「对齐」约定） |
| rust-store core | 否 | — | 不碰 core（零 parity 税） |
| 四后端驱动 | 否 | — | 复用既有执行器，仅 py-PG 判定补一处（见 4.2） |

## 3. 迁移与移除清单（动手清单）

### 3.1 新增清单
| 新增项 | 所在文件/模型 | 说明 |
|---|---|---|
| `RawSqlError` | `py-store/src/py_store/datasource.py` | 原生 SQL 入口显式错误类型 |
| `execute_raw(source, sql, params, is_write)` | `py-store/src/py_store/datasource.py` | Host 层原生 SQL（事务内可用） |
| `ddl` 模块（`generate`） | `py-store/src/py_store/ddl.py` | schema def → CREATE TABLE 文本 |
| `tests/test_raw_sql_and_ddl.py` | `py-store/tests/` | ① + ② 单测 |
| `RawSqlError` 类 | `nodejs-store/src/datasource.js` | 同 py |
| `executeRaw(source, sql, params, isWrite)` | `nodejs-store/src/datasource.js` | 同 py |
| `ddl.js` 模块（`generate`） | `nodejs-store/src/ddl.js` | 与 py **输出逐字节一致** |
| `tests/raw-sql-ddl.test.js` | `nodejs-store/tests/` | ① + ② 单测 |

### 3.2 修改清单
| 修改项 | 文件 | 由 → 到 |
|---|---|---|
| PG 取行判定 | `py-store/src/py_store/executors/postgres.py:34` | `if shape:` → `if shape or not stmt.get('isWrite'):` |
| Store 门面 | `py-store/src/py_store/__init__.py` | 新增 `transaction`/`execute_raw`/`generate_ddl`（+camel 别名）；导出 `ddl`、`RawSqlError` |
| Store 门面 | `nodejs-store/src/index.js` | 新增 `transaction`/`executeRaw`/`generateDdl`；`module.exports` 加 `ddl` |
| 数据源导出 | `nodejs-store/src/datasource.js` | `module.exports` 加 `executeRaw`、`RawSqlError` |

### 3.3 删除清单
| 删除项 | 位置 | 原因 | 替代 |
|---|---|---|---|
| （无） | — | — | — |

## 4. 详细执行契约（代码优先）

### 4.1 py-store：`datasource.py` 新增（追加到 `exec_sql` 之后）

```python
class RawSqlError(RuntimeError):
    """原生 SQL 入口的显式错误（非 SQL 源 / 执行器未接入）"""


async def execute_raw(source, sql, params=None, is_write=False):
    """
    在指定 SQL 源上执行原生 SQL（Host 层逃生口，绕开 core 的 dialect_translate）

      - 事务作用域内经 ``connection_for`` 落到事务专用连接 → 支持 SELECT ... FOR UPDATE；
      - 占位符沿用各后端原生风格（mysql/sqlite 用 ``?``，postgres 用 ``$1..$n``）；
      - 仅支持 SQL 源；Mongo 源显式报错（绝不静默）；
      - ``is_write=False`` 视为读（取行）；``True`` 视为写（取影响行数）。
      - 返回 ``{'rows': list|None, 'affectedRows': int}``。
    """
    conn = connection_for(source)
    if not isinstance(conn, Mapping):
        raise RawSqlError(
            f'数据源 {source} 不是 SQL 源（原生 SQL 入口仅支持 mysql/postgres/sqlite）')
    exec_fn = _exec_of(conn)
    if not callable(exec_fn):
        raise RawSqlError(
            f'SQL 数据源 {source}({_kind_of(conn)}) 的执行器未接入')
    stmt = {'text': sql, 'params': list(params or []), 'isWrite': bool(is_write)}
    out = await exec_fn({'stmts': [stmt]})
    return {'rows': out.get('rows'), 'affectedRows': int(out.get('affectedRows') or 0)}
```

### 4.2 py-store：`executors/postgres.py` 判定补一处

`run_stmts` 第 34 行，由：

```python
            if shape:
```
改为：
```python
            if shape or not stmt.get('isWrite'):
```
要点：core 的 SELECT 恒带 `rowShape`（`ir.rs:21-29` `SqlStmt::select`）、写语句恒 `isWrite=True`，故对 core 计划零行为变化；仅使「`isWrite=False` 且无 `rowShape`」的原生 SELECT 走 `fetch` 取行。

### 4.3 py-store：`__init__.py` Store 新增方法（插在 `build_pipeline` 之前）

```python
    async def transaction(self, source: str, fn) -> Any:
        """事务作用域：单 SQL 源「同连接 + 同事务」执行 fn（复用 datasource.run_in_transaction）

        fn 内 ``execute_raw`` / CRUD 均落到该源事务连接；Mongo 源/多源按原样执行（非原子，
        见 README「事务边界」），绝不假装已事务化。单源场景 source 传 'default'。
        """
        return await datasource.run_in_transaction(source, fn)

    async def execute_raw(self, source: str, sql: str, params: list | None = None,
                          is_write: bool = False) -> dict[str, Any]:
        """在指定 SQL 源执行原生 SQL（事务内可用；占位符按各后端原生风格）"""
        return await datasource.execute_raw(source, sql, params, is_write)

    def generate_ddl(self, backend: str, names: list | None = None) -> str:
        """从已注册 schema def 生成指定后端 DDL 文本（纯函数，不连库、不回写）"""
        return ddl.generate(backend, names)
```

并在「驼峰别名」区追加：

```python
    executeRaw = execute_raw
    generateDdl = generate_ddl
```

并：`from . import ddl`（加入既有 `from . import (...)` 块）；Store 内新增 `RawSqlError = datasource.RawSqlError`；模块级 `RawSqlError` 亦导出（`from .datasource import RawSqlError`）。

### 4.4 py-store：新增 `src/py_store/ddl.py`

```python
"""
DDL 生成（schema def → CREATE TABLE 文本；纯函数，不连库、不回写）

与 core 契约严格对齐（schema→DDL 单向映射）：
  - 只对 scalar 字段建列（object/array 不建列；同 core dialect::scalar_column）；
  - 每表必建 __present 哨兵列（形态 ,f1,f2,；同 core write/insert.rs::present_value）；
  - timestamps !== false → 追加 createdAt / updatedAt（同 core schema/registry.rs::add_timestamp_fields）；
  - 归档表 <collection>_deleted 由 registry 自动派生，本模块按已注册 def 逐表生成（不特判）；
  - 不生成 CREATE INDEX（SQL 后端不建索引，schema.indexes 仅元数据，铁律 6）。

生成器只产出文本、不执行 —— 不违反铁律 6（绝不写 DDL 回库）。
"""

from .feedback import emit as _emit_feedback
from .schema import get as _get_schema
from .schema import list as _list_schema

_BACKENDS = ('mysql', 'postgres', 'sqlite')

# schema 声明类型 → (mysql, postgres, sqlite) 列类型
_TYPES = {
    'string':   ('VARCHAR(255)', 'TEXT', 'TEXT'),
    'int':      ('INT', 'INTEGER', 'INTEGER'),
    'long':     ('BIGINT', 'BIGINT', 'INTEGER'),
    'number':   ('BIGINT', 'BIGINT', 'INTEGER'),
    'float':    ('DOUBLE', 'DOUBLE PRECISION', 'REAL'),
    'double':   ('DOUBLE', 'DOUBLE PRECISION', 'REAL'),
    'bool':     ('TINYINT(1)', 'BOOLEAN', 'INTEGER'),
    'boolean':  ('TINYINT(1)', 'BOOLEAN', 'INTEGER'),
    'datetime': ('BIGINT', 'BIGINT', 'INTEGER'),
    'date':     ('BIGINT', 'BIGINT', 'INTEGER'),
}
_NON_COLUMN = ('object', 'array')
_ID_TYPE = ('VARCHAR(64)', 'TEXT', 'TEXT')
_PRESENT_TYPE = ('VARCHAR(255)', 'TEXT', 'TEXT')
_TIMESTAMP_FIELDS = ('createdAt', 'updatedAt')
_MYSQL_PRESENT_MAX = 255


def _idx(backend):
    return _BACKENDS.index(backend)


def _q(backend, ident):
    """标识符引用（与 core dialect::Backend::quote_ident 一致：mysql 反引号，其余双引号）"""
    if backend == 'mysql':
        return '`' + ident.replace('`', '``') + '`'
    return '"' + ident.replace('"', '""') + '"'


def _declared_type(field_def):
    if isinstance(field_def, dict):
        return field_def.get('type')
    return field_def


def _columns(defn, backend):
    """返回 [(name, sql_type, pk)]，顺序：声明的标量字段 → timestamps → __present"""
    i = _idx(backend)
    cols = []
    fields = defn.get('fields') or {}
    for name, fdef in fields.items():
        ftype = _declared_type(fdef)
        if ftype in _NON_COLUMN:
            continue
        if name == '_id':
            cols.append((name, _ID_TYPE[i], True))
            continue
        if ftype not in _TYPES:
            raise ValueError(
                f'DDL 生成：字段 "{defn["name"]}.{name}" 类型 {ftype!r} 未知，'
                f'支持 {sorted(_TYPES)}')
        cols.append((name, _TYPES[ftype][i], False))
    if not any(c[2] for c in cols):
        raise ValueError(f'DDL 生成：schema "{defn["name"]}" 缺少 _id 字段')
    if defn.get('timestamps') is not False:
        for ts in _TIMESTAMP_FIELDS:
            if not any(c[0] == ts for c in cols):
                cols.append((ts, _TYPES['number'][i], False))
    cols.append(('__present', _PRESENT_TYPE[i], False))
    return cols


def _warn_present_overflow(defn, cols):
    """MySQL __present VARCHAR(255) 容量校验：超限即告警（不静默）"""
    length = sum(len(c[0]) for c in cols) + len(cols) + 1
    if length > _MYSQL_PRESENT_MAX:
        _emit_feedback({
            'type': 'ddl_present_overflow',
            'code': 'ddlPresentOverflow',
            'layer': 'host',
            'message': (f'表 {defn.get("collection")} 的 __present 预估长度 {length} '
                        f'超过 MySQL VARCHAR(255)'),
            'hint': ('为该表改用 TEXT 列，或减少标量字段；否则写入会被截断/报错，'
                     '导致 $eq:null / $exists 三态判定错误'),
            'schema': defn.get('name'),
        })


def _create_table(defn, backend):
    i = _idx(backend)
    table = defn.get('collection') or defn.get('name')
    cols = _columns(defn, backend)
    if backend == 'mysql':
        _warn_present_overflow(defn, cols)
    lines = []
    for name, ctype, pk in cols:
        if pk and backend == 'mysql':
            lines.append(f'  {_q(backend, name)} {ctype} NOT NULL')
        elif pk:
            lines.append(f'  {_q(backend, name)} {ctype} PRIMARY KEY')
        else:
            lines.append(f'  {_q(backend, name)} {ctype}')
    if backend == 'mysql':
        lines.append(f'  PRIMARY KEY ({_q(backend, "_id")})')
    return f'CREATE TABLE {_q(backend, table)} (\n' + ',\n'.join(lines) + '\n);'


def generate(backend, names=None):
    """生成 DDL 文本（多表以空行分隔）；backend ∈ mysql/postgres/sqlite"""
    if backend not in _BACKENDS:
        raise ValueError(f'DDL 生成：不支持的后端 {backend!r}（支持 {list(_BACKENDS)}）')
    targets = list(names) if names else list(_list_schema())
    return '\n\n'.join(_create_table(_get_schema(n), backend) for n in targets)
```

### 4.5 nodejs-store：`datasource.js` 新增

```js
/** 原生 SQL 入口的显式错误（非 SQL 源 / 执行器未接入） */
class RawSqlError extends Error {
  constructor(message) {
    super(message);
    this.name = 'RawSqlError';
  }
}

/**
 * 在指定 SQL 源上执行原生 SQL（Host 层逃生口，绕开 core 的 dialectTranslate）
 *
 *   - 事务作用域内经 `connectionFor` 落到事务专用连接 → 支持 SELECT ... FOR UPDATE；
 *   - 占位符沿用各后端原生风格（mysql/sqlite 用 `?`，postgres 用 `$1..$n`）；
 *   - 仅支持 SQL 源；Mongo 源显式报错（绝不静默）；
 *   - `isWrite=false` 视为读（取行）；`true` 视为写（取影响行数）。
 *   - 返回 `{ rows, affectedRows }`。
 */
async function executeRaw(source, sql, params = [], isWrite = false) {
  const conn = connectionFor(source);
  if (!isSqlConnection(conn)) {
    throw new RawSqlError(
      `数据源 ${source} 不是 SQL 源（原生 SQL 入口仅支持 mysql/postgres/sqlite）`,
    );
  }
  if (typeof conn.exec !== 'function') {
    throw new RawSqlError(`SQL 数据源 ${source}(${conn.kind}) 的执行器未接入`);
  }
  const stmt = { text: sql, params: Array.from(params || []), isWrite: Boolean(isWrite) };
  const out = await conn.exec({ stmts: [stmt] });
  return { rows: out.rows ?? null, affectedRows: Number(out.affectedRows || 0) };
}
```

`module.exports` 追加：`executeRaw`、`RawSqlError`。

### 4.6 nodejs-store：`index.js` Store 新增方法

```js
  // ── 事务 + 原生 SQL（复用 datasource.runInTransaction） ──
  /** 事务作用域：单 SQL 源「同连接 + 同事务」执行 fn；Mongo 源按原样执行（非原子） */
  async transaction(source, fn) {
    return datasource.runInTransaction(source, fn);
  }

  /** 在指定 SQL 源执行原生 SQL（事务内可用；占位符按各后端原生风格） */
  async executeRaw(source, sql, params, isWrite) {
    return datasource.executeRaw(source, sql, params, isWrite);
  }

  /** 从已注册 schema def 生成指定后端 DDL 文本（纯函数，不连库、不回写） */
  generateDdl(backend, names) {
    return ddl.generate(backend, names);
  }
```
`require` 区加 `const ddl = require('./ddl');`；`module.exports` 追加 `ddl`。

### 4.7 nodejs-store：新增 `src/ddl.js`

与 4.4 **逐行同构**（同 `_TYPES`、同 `_q`、同 `_columns`、同 `_create_table`、同 `generate`），差异仅语言语法：

```js
'use strict';

const { emit: _emitFeedback } = require('./feedback');
const schema = require('./schema');

const BACKENDS = ['mysql', 'postgres', 'sqlite'];
const TYPES = {
  string: ['VARCHAR(255)', 'TEXT', 'TEXT'],
  int: ['INT', 'INTEGER', 'INTEGER'],
  long: ['BIGINT', 'BIGINT', 'INTEGER'],
  number: ['BIGINT', 'BIGINT', 'INTEGER'],
  float: ['DOUBLE', 'DOUBLE PRECISION', 'REAL'],
  double: ['DOUBLE', 'DOUBLE PRECISION', 'REAL'],
  bool: ['TINYINT(1)', 'BOOLEAN', 'INTEGER'],
  boolean: ['TINYINT(1)', 'BOOLEAN', 'INTEGER'],
  datetime: ['BIGINT', 'BIGINT', 'INTEGER'],
  date: ['BIGINT', 'BIGINT', 'INTEGER'],
};
const NON_COLUMN = ['object', 'array'];
const ID_TYPE = ['VARCHAR(64)', 'TEXT', 'TEXT'];
const PRESENT_TYPE = ['VARCHAR(255)', 'TEXT', 'TEXT'];
const TIMESTAMP_FIELDS = ['createdAt', 'updatedAt'];
const MYSQL_PRESENT_MAX = 255;

const idx = (backend) => BACKENDS.indexOf(backend);

/** 标识符引用（与 core Backend::quoteIdent 一致：mysql 反引号，其余双引号） */
function q(backend, ident) {
  if (backend === 'mysql') return '`' + ident.replace(/`/g, '``') + '`';
  return '"' + ident.replace(/"/g, '""') + '"';
}

const declaredType = (fdef) => (fdef && typeof fdef === 'object' ? fdef.type : fdef);

function columns(defn, backend) {
  const i = idx(backend);
  const cols = [];
  const fields = defn.fields || {};
  for (const [name, fdef] of Object.entries(fields)) {
    const ftype = declaredType(fdef);
    if (NON_COLUMN.includes(ftype)) continue;
    if (name === '_id') { cols.push([name, ID_TYPE[i], true]); continue; }
    if (!(ftype in TYPES)) {
      throw new Error(
        `DDL 生成：字段 "${defn.name}.${name}" 类型 ${JSON.stringify(ftype)} 未知，支持 ${Object.keys(TYPES).sort()}`,
      );
    }
    cols.push([name, TYPES[ftype][i], false]);
  }
  if (!cols.some((c) => c[2])) throw new Error(`DDL 生成：schema "${defn.name}" 缺少 _id 字段`);
  if (defn.timestamps !== false) {
    for (const ts of TIMESTAMP_FIELDS) if (!cols.some((c) => c[0] === ts)) cols.push([ts, TYPES.number[i], false]);
  }
  cols.push(['__present', PRESENT_TYPE[i], false]);
  return cols;
}

function warnPresentOverflow(defn, cols) {
  const length = cols.reduce((n, c) => n + c[0].length, 0) + cols.length + 1;
  if (length > MYSQL_PRESENT_MAX) {
    _emitFeedback({
      type: 'ddl_present_overflow',
      code: 'ddlPresentOverflow',
      layer: 'host',
      message: `表 ${defn.collection} 的 __present 预估长度 ${length} 超过 MySQL VARCHAR(255)`,
      hint: '为该表改用 TEXT 列，或减少标量字段；否则写入会被截断/报错，导致 $eq:null / $exists 三态判定错误',
      schema: defn.name,
    });
  }
}

function createTable(defn, backend) {
  const table = defn.collection || defn.name;
  const cols = columns(defn, backend);
  if (backend === 'mysql') warnPresentOverflow(defn, cols);
  const lines = [];
  for (const [name, ctype, pk] of cols) {
    if (pk && backend === 'mysql') lines.push(`  ${q(backend, name)} ${ctype} NOT NULL`);
    else if (pk) lines.push(`  ${q(backend, name)} ${ctype} PRIMARY KEY`);
    else lines.push(`  ${q(backend, name)} ${ctype}`);
  }
  if (backend === 'mysql') lines.push(`  PRIMARY KEY (${q(backend, '_id')})`);
  return `CREATE TABLE ${q(backend, table)} (\n` + lines.join(',\n') + '\n);';
}

/** 生成 DDL 文本（多表以空行分隔）；backend ∈ mysql/postgres/sqlite */
function generate(backend, names) {
  if (!BACKENDS.includes(backend)) {
    throw new Error(`DDL 生成：不支持的后端 ${JSON.stringify(backend)}（支持 ${BACKENDS.join('/')}）`);
  }
  const targets = names && names.length ? Array.from(names) : schema.list();
  return targets.map((n) => createTable(schema.get(n), backend)).join('\n\n');
}

module.exports = { generate };
```

### 4.8 测试契约

**py `tests/test_raw_sql_and_ddl.py`**（不需要真实驱动，用假执行器描述符）：
- `test_execute_raw_builds_plan_and_returns_rows`：注册假 SQL 源 `{'kind':'sqlite','exec': fake}`，`store.execute_raw('db','SELECT 1',[],False)` → 断言 `fake` 收到 `{'stmts':[{'text':'SELECT 1','params':[],'isWrite':False}]}` 且返回 `{'rows': [...], 'affectedRows': 0}`。
- `test_execute_raw_mongo_source_raises`：源为假 Mongo（非 Mapping）→ 断言抛 `RawSqlError`。
- `test_transaction_wraps_and_rolls_back`：假执行器带 `with_transaction`，fn 内 raise → 断言 rollback 被调用且异常上抛；fn 内 `execute_raw` 落到事务连接。
- `test_ddl_scalar_only_and_present`：注册含 object/array 字段 schema → 断言生成文本**不含** object/array 子字段列、**含** `__present`、`_id ... PRIMARY KEY`、归档表 `_deleted`。
- `test_ddl_timestamps_and_no_index`：`timestamps` 默认 → 含 `createdAt`/`updatedAt`；全文**不含** `CREATE INDEX`。
- `test_ddl_unknown_type_raises`：字段 `{'type':'weird'}` → 断言 `ValueError`。
- `test_ddl_present_overflow_emits_feedback`：构造多长字段名 → 设置 feedback sink 捕获 → 断言收到 `ddlPresentOverflow`。

**node `tests/raw-sql-ddl.test.js`**（`node:test`，同构 7 用例）。

**跨端逐字节比对（第 12 步实测）**：对 `py-store/example/course-platform/schema.json` 逐 schema 生成，py 与 node 输出 `diff` 必须为 0 差异。

## 5. 执行步骤（逐步骤状态，可中断续做）

**执行期间主文档只读：状态一律记录在外置账本 `tmp/execution-status-Host原生SQL与DDL生成.md`（步骤 | todo/doing/done | 完成时间 | 备注）；下表 `[x]` 由收尾动作一次性勾选，执行中不编辑本表。**

| 步骤 | 状态 | 详细说明 |
|---|---|---|
| 1. py：datasource 新增 execute_raw | [x] 已完成 | **做什么**：在 `py-store/src/py_store/datasource.py` 追加 `RawSqlError` 与 `execute_raw`（见 4.1） <br>**涉及文件**：`py-store/src/py_store/datasource.py` <br>**验证方式**：`python -c "import py_store.datasource as d; print(hasattr(d,'execute_raw'), hasattr(d,'RawSqlError'))"`（PYTHONPATH=py-store/src） |
| 2. py：PG 执行器判定补 isWrite | [x] 已完成 | **做什么**：`executors/postgres.py:34` `if shape:` → `if shape or not stmt.get('isWrite'):`（见 4.2） <br>**涉及文件**：`py-store/src/py_store/executors/postgres.py` <br>**验证方式**：模块可导入；core 计划行为不变（SELECT 恒带 rowShape） |
| 3. py：Store 门面 | [x] 已完成 | **做什么**：`__init__.py` 加 `import ddl`、Store 加 `transaction`/`execute_raw`/`generate_ddl` + 别名 `executeRaw`/`generateDdl` + `RawSqlError`（见 4.3） <br>**涉及文件**：`py-store/src/py_store/__init__.py` <br>**验证方式**：`store` 实例具三方法；`py_store.RawSqlError` 可访问 |
| 4. py：新增 ddl.py | [x] 已完成 | **做什么**：新建 `src/py_store/ddl.py`（见 4.4） <br>**涉及文件**：`py-store/src/py_store/ddl.py` <br>**验证方式**：注册示例 schema 后 `store.generate_ddl('mysql')` 返回非空且含 `__present` |
| 5. py：单测 | [x] 已完成 | **做什么**：新建 `tests/test_raw_sql_and_ddl.py`（见 4.8 7 用例） <br>**涉及文件**：`py-store/tests/test_raw_sql_and_ddl.py` <br>**验证方式**：`$env:PYTHONPATH='py-store/src'; python -m pytest py-store/tests/test_raw_sql_and_ddl.py -q` 全绿 |
| 6. py：全量回归 | [x] 已完成 | **做什么**：跑 py 全量 pytest，确认无新增失败 <br>**涉及文件**：无改动 <br>**验证方式**：`$env:PYTHONPATH='py-store/src'; python -m pytest py-store/tests/ -q` |
| 7. node：datasource 新增 executeRaw | [x] 已完成 | **做什么**：`src/datasource.js` 追加 `RawSqlError` 与 `executeRaw`，导出之（见 4.5） <br>**涉及文件**：`nodejs-store/src/datasource.js` <br>**验证方式**：`node -e "const d=require('./src/datasource');console.log(typeof d.executeRaw, typeof d.RawSqlError)"` |
| 8. node：Store 门面 | [x] 已完成 | **做什么**：`src/index.js` 加 `require('./ddl')`、Store 加 `transaction`/`executeRaw`/`generateDdl`、导出 `ddl`（见 4.6） <br>**涉及文件**：`nodejs-store/src/index.js` <br>**验证方式**：`node -e "const s=require('./src').store;console.log(typeof s.transaction, typeof s.executeRaw, typeof s.generateDdl)"` |
| 9. node：新增 ddl.js | [x] 已完成 | **做什么**：新建 `src/ddl.js`（见 4.7） <br>**涉及文件**：`nodejs-store/src/ddl.js` <br>**验证方式**：同 4 步 |
| 10. node：单测 | [x] 已完成 | **做什么**：新建 `tests/raw-sql-ddl.test.js`（同构 7 用例） <br>**涉及文件**：`nodejs-store/tests/raw-sql-ddl.test.js` <br>**验证方式**：`node --test nodejs-store/tests/raw-sql-ddl.test.js` 全绿 |
| 11. node：既有回归 | [x] 已完成 | **做什么**：跑既有 node 测试（`npm test` 或 `node scripts/test.js`）确认无新增失败 <br>**涉及文件**：无改动 <br>**验证方式**：与改动前基线一致 |
| 12. 跨端逐字节比对 | [x] 已完成 | **做什么**：写临时比对脚本，对 course-platform schema.json 生成 py/node 输出并 diff <br>**涉及文件**：`tmp/`（临时，不入库） <br>**验证方式**：`diff` 输出为空（0 字节差异） |
| 13. README 落档 | [x] 已完成 | **做什么**：py/node README 的 API 清单补 `transaction`/`execute_raw`/`generate_ddl` 条目；「When not to use」的 DDL 表述补「可选生成器」一句 <br>**涉及文件**：`py-store/README.md`、`nodejs-store/README.md`（+ zh-CN 同步） <br>**验证方式**：文字与实现一致，无悬空 API |

### 步骤说明

- **git 提交**：py 步骤（1–6、13）在 `py-store/` 仓库提交；node 步骤（7–11、13）在 `nodejs-store/` 仓库提交；doc 随 py 提交。提交信息 `Host原生SQL与DDL生成 步骤N：<步骤名>`；**不推送、不切分支**。
- **边界**：`execute_raw` 只支持 SQL 源；`params` 缺省 `[]`；`is_write` 缺省 `False`。
- **兼容**：不改 core、不改既有函数签名；py-PG 判定改动对 core 计划零影响（已证）。

## 6. 实施顺序（阶段依赖）

| 阶段 | 内容 | 依赖 |
|---|---|---|
| A | py ①（步骤 1–3、5–6） | 无 |
| B | py ②（步骤 4–6） | A |
| C | node ①+②（步骤 7–11） | B（形态已定） |
| D | 跨端比对 + 文档（步骤 12–13） | C |

## 7. 禁止事项

❌ 禁止修改 `rust-store/core`、`core-py`、`core-node` 任何文件（零 parity 税）
❌ 禁止放开 raw `$pipeline` / `store.aggregate()`
❌ 禁止把 `execute_raw` 用于非 SQL 源而不报错（必须抛 `RawSqlError`）
❌ 禁止在 DDL 生成器中输出 `CREATE INDEX`（SQL 后端不建索引）
❌ 禁止在 DDL 生成器中**连库执行**或回写数据库（违反铁律 6）
❌ 禁止对未知字段类型静默兜底为 TEXT（必须抛错）
❌ 禁止 `__present` 超限时静默（必须 `feedback.emit`）
❌ 禁止用 `git add -A` / `git add .`；禁止提交 `tmp/` 账本
❌ 禁止把两端 DDL 生成器写成不同格式（必须逐字节一致）

## 8. 注意事项

1. **py-PG 判定改动是唯一触碰既有执行器的改动**：core 的 `SqlStmt::select` 恒带 `rowShape`、写语句恒 `isWrite=True`（`ir.rs:21-39`），故行为等价；改动只为让「无 rowShape 且 isWrite=False」的原生 SELECT 走 `fetch`。
2. **`__present` 令牌串长度 = Σlen(列名) + 列数 + 1**（core `present_value`：`"," + ",".join(keys) + ","`）；MySQL `VARCHAR(255)`，实测字段名均长 8 时 >28 列即超限。
3. **`timestamps` 默认 true**：core `add_timestamp_fields` 与 Host 镜像均默认补 `createdAt/updatedAt`；归档 def 亦默认 true。生成器须按 `defn.timestamps is not False` 判断（勿只读 `fields`）。
4. **归档表不特判**：Host 镜像已注册 `<Name>Deleted`（`collection=<c>_deleted`，含 `deletedAt`），生成器对 `schema.list()` 逐表生成即自动涵盖。
5. **`_id` 长度**：`len(idPrefix)+16`（base36 毫秒 8 位至 2059 年 + 8 位随机）；`VARCHAR(64)` 余量充足。
6. **占位符风格**：raw SQL 须按目标后端原生风格书写（mysql/sqlite `?`；postgres `$1..$n`），与 core 产出一致。
7. 两端生成器**必须逐字节一致**：任何格式（缩进、逗号、换行、引号）差异都算失败。
