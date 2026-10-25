# py-store

**One data layer for MongoDB, MySQL, SQLite, PostgreSQL and local disk in Python asyncio — define models as pure JSON, query them with a MongoDB-style GQL tree syntax, and get role-based access control, computed columns and soft-delete out of the box.**

![PyPI version](https://img.shields.io/pypi/v/storepy)
![license](https://img.shields.io/pypi/l/storepy)
![python versions](https://img.shields.io/pypi/pyversions/storepy)
![backends](https://img.shields.io/badge/backends-MongoDB%20%7C%20MySQL%20%7C%20SQLite%20%7C%20PostgreSQL%20%7C%20Local-blue)
![query dialect](https://img.shields.io/badge/query%20dialect-GQL%20(MongoDB--flavoured)-green)

`py-store` lets a Python service talk to MongoDB (native aggregation), MySQL, PostgreSQL, SQLite and local disk through a **single schema definition and a single query dialect**. Nested relations compile to **one native query per backend** — you never hand-write `$lookup` or raw SQL.

> Also looking for the Node.js version? See [`nodejs-store`](https://github.com/coenddt/nodejs-store) (npm `nodejs-store`). Both are thin hosts over the shared Rust engine [`rust-store`](https://github.com/coenddt/rust-store).
> 中文文档见 [README.zh-CN.md](README.zh-CN.md)。

**Documentation site:** <https://coenddt.github.io/py-store/> — every scenario walkthrough with runnable code and the engine's exact limits, one indexable page per scenario.

**Install:** the distribution name is `storepy`; the import package is `py_store`.

```bash
pip install storepy
```

```python
from py_store import init, store
```

---

## Table of contents

- [What it is](#what-it-is)
- [When to use it](#when-to-use-it)
- [When not to use it](#when-not-to-use-it)
- [How it compares](#how-it-compares)
- [Installation](#installation)
- [Quick start](#quick-start)
- [Supported backends](#supported-backends)
- [Features](#features)
- [GQL tree queries](#gql-syntax)
- [Aggregation](#aggregation)
- [Query & write API](#query--write-api)
- [Multi-datasource connections](#multi-datasource-connections)
- [Permission context](#permission-context)
- [Feedback events](#feedback-events)
- [Schema reference](#schema-reference)
- [Vector search](#vector-search)
- [Transactions](#transaction-boundary)
- [Triggers](#triggers)
- [Transactional capabilities](#transactional-capabilities)
- [Scenario examples](#scenario-examples)
- [Performance](#performance)
- [FAQ](#faq)
- [Related projects](#related-projects)
- [Framework usage contract](#framework-usage-contract)

---

## What it is

A lightweight, backend-agnostic data layer for Python asyncio. You describe your models once as pure JSON (`fields`, `relations`, `computes`, `indexes`, `read`/`write` role whitelists). From that description the library derives:

- **command planning** (GQL → Mongo command JSON) — executed by the Rust core `rust-store-py`,
- **dialect translation** (command JSON → parameterized SQL) for MySQL / PostgreSQL / SQLite,
- **permission checks** (schema-level + field-level read/write, owner-condition injection),
- **computed columns**, **soft-delete archives**, and **result rehydration** (flat JOIN rows → nested documents).

MongoDB is the *primary dialect*: queries are written in a MongoDB-flavoured GQL, and the three relational backends adapt to it. That is what makes one schema portable across a document store and three relational stores.

### How it relates to nodejs-store and rust-store

```
                 ┌──────────────────────────────┐
   Node.js  ──▶  │  nodejs-store (npm, host)    │ ─┐
                 └──────────────────────────────┘  │  rust-store-node (napi-rs)
                                                   ▼
                                     ┌───────────────────────────────┐
                                     │ rust-store/core (pure logic)  │
                                     │ GQL · permissions · computes  │
                                     │ command planning · dialects   │
                                     └───────────────────────────────┘
                                                   ▲
                 ┌──────────────────────────────┐  │  rust-store-py (PyO3)
   Python   ──▶  │  py-store (pip, host)        │ ─┘
                 └──────────────────────────────┘
```

The **Rust core** owns GQL parsing, permission checks, computed columns, command planning and SQL dialect translation — it never touches a database. The **hosts** (`py-store`, `nodejs-store`) own driver IO, callbacks and placeholder substitution. Behaviour therefore cannot drift between Python and Node.js: there is only one implementation.

## When to use it

Reach for `py-store` when any of these describe your situation:

- **One codebase, several databases.** You ship the same service against MongoDB in dev and PostgreSQL in production (or per-tenant), and you don't want two data-access layers.
- **You need nested / relational reads without writing `$lookup` or JOINs.** Order → items, Course → lessons, User → orders — all expressed once in the schema and resolved in a single query.
- **You are building an admin backend, FastAPI service or internal CRUD API** and want schema-driven CRUD, soft-delete, computed columns and role checks without a full ORM.
- **You need row-level / field-level access control.** Whitelists per role, `guest` can never write, `creator` ownership is checked against `doc.createdBy`, and owner conditions are injected automatically into queries.
- **You are building an AI / natural-language data-QA layer.** The library was designed with AI query hosts in mind: `store.build_pipeline(...)` exposes the planned query without executing it, and degraded / non-pushdownable paths emit structured feedback events instead of failing silently.
- **You are migrating between MongoDB and SQL** and want to keep one query syntax during the transition.
- **Multi-tenant SaaS.** One schema definition, N tenants: locate a schema by `(source, database, schema, collection)` and re-target any query or write at execution time with a `{"source", "database", "schema"}` override.

Typical concrete scenarios (see [`doc/use-cases/`](doc/use-cases/) for full walkthroughs):

| Scenario | Why py-store fits |
| --- | --- |
| Multi-tenant SaaS with per-tenant schema/database | `database` / `schema` per tenant + runtime route override, one schema |
| FastAPI / admin backend | Schema-driven CRUD, soft-delete, computed columns, RBAC |
| MongoDB today, PostgreSQL tomorrow | Same GQL + same schema, only the datasource changes |
| AI data-QA / text-to-query agent | Plan-only `build_pipeline`, deterministic command JSON, feedback events |
| Mixed SQL + Mongo in one product | Cross-source queries with native SQL pushdown and Mongo in-memory federation |
| Audit-friendly CRUD | Every schema auto-gets a `<Model>Deleted` archive table/collection |

## When not to use it

Being explicit about the boundary saves you time:

- **You want a full ORM with a migration engine (Alembic, Django migrations).** `py-store` is a *data layer*, not a full migration tool. It can **read** a SQL backend's physical structure (`sync_schema` → introspection) but it never invents DDL on its own. It does ship a whitelist migration **orchestrator** — `store.migrate()` diffs two def versions, renders per-dialect SQL and applies it transactionally with a `__schemaMigration` version trail (see "Schema migration (orchestrator)") — and an optional `generate_ddl()` that renders `CREATE TABLE` text from your registered schemas (pure text, it never connects to the database by itself). Beyond the whitelist (add table/column/index, type widening), pair it with your migration tool of choice.
- **You want a Pydantic-model-centric ORM.** Schemas here are runtime JSON dicts, giving you cross-language parity (the same schema runs in Python and Node.js) rather than Pydantic type validation.
- **You only ever use one database and rarely join.** A plain driver (or a single-database ODM/ORM) will be simpler.
- **You need raw aggregation escape hatches.** `$pipeline` passthrough was deliberately removed; `store.aggregate()` exists only as a strictly **opt-in** alias (disabled by default). Use `$condition` / `$group` / `$having` / relations; anything that cannot be safely translated fails **explicitly** rather than silently.

## How it compares

General positioning, not a benchmark — always verify against each tool's current docs.

| | py-store | SQLAlchemy | Beanie / Motor | Tortoise ORM | SQLModel | Django ORM |
| --- | --- | --- | --- | --- | --- | --- |
| Primary shape | JSON schema + GQL data layer | SQL toolkit + ORM | Async MongoDB ODM / driver | Async ORM | Pydantic + SQLAlchemy | ORM bundled with Django |
| Backends | MongoDB, MySQL, SQLite, PostgreSQL, local | PostgreSQL, MySQL, SQLite, Oracle, MSSQL | MongoDB | PostgreSQL, MySQL, SQLite, Oracle, MSSQL | PostgreSQL, MySQL, SQLite, … | PostgreSQL, MySQL, SQLite, Oracle |
| One query dialect across Mongo **and** SQL | ✅ (MongoDB-flavoured GQL) | ➖ (SQL only) | ➖ (Mongo only) | ➖ (SQL only) | ➖ (SQL only) | ➖ (SQL only) |
| Nested relation reads in one query | ✅ declarative relations → `$lookup` / `JOIN` | ⚠️ manual `selectinload`/joins | ✅ `Link`/`fetch_links` | ✅ `prefetch_related` | ⚠️ via SQLAlchemy | ✅ `prefetch_related` |
| Built-in role / field-level RBAC + owner injection | ✅ | ➖ | ➖ | ➖ | ➖ | ➖ (permissions are app-level) |
| Read-time computed columns (sync / async / relation-agg) | ✅ | ➖ (hybrid properties) | ➖ | ➖ | ➖ | ➖ |
| Soft-delete archive table auto-provisioned | ✅ | ➖ | ➖ | ➖ | ➖ | ➖ |
| Migration / DDL engine | ➖ (introspection read-only; whitelist `migrate()` orchestrator; optional `generate_ddl` text) | ✅ (Alembic) | ➖ | ✅ (Aerich) | ✅ (Alembic) | ✅ |
| Framework coupling | none (asyncio) | none | none | none | none | Django |
| Shared native core across Python & Node | ✅ (Rust `rust-store`) | ➖ | ➖ | ➖ | ➖ | ➖ |

### How it differs from specific libraries

Positioning only, based on those projects' public documentation at the time of writing — verify against your own requirements.

- **vs SQLAlchemy / SQLModel / Django ORM** — all SQL-only and model-class-centric: they do not target MongoDB, and none of them ships schema-declared role/field access control or read-time computed columns. `py-store` compiles one GQL to native MongoDB aggregation or to parameterized SQL.
- **vs Beanie / Motor** — MongoDB-only. `py-store` uses the same MongoDB-flavoured query style but the identical query also runs on MySQL, SQLite and PostgreSQL.
- **vs Tortoise ORM / pyloquent** — async Python ORMs over SQL backends, with model classes and (in Tortoise's case) a migration tool. `py-store` has no inline migration engine — introspection only reads physical structure, `generate_ddl()` only *renders* `CREATE TABLE` text, and `migrate()` orchestrates whitelist-only changes (add column/index/table, widening) with a version trail — and describes models as plain dicts, which is exactly what makes a schema portable to the Node.js host.
- **vs `nodejs-store`** — the same engine and the same GQL, in JavaScript. Use whichever host matches your service; schemas and query semantics are interchangeable.

Short version: use an ORM when you want **model classes, Pydantic validation and migrations**; use `py-store` when you want **one runtime schema + one query dialect spanning MongoDB and SQL**, with RBAC and computed columns built in.

## Installation

```bash
pip install storepy
```

> The distribution name is `storepy`; the import package is `py_store`:
> `from py_store import init, store`.

Requires Python 3.10+ and one supported backend (MongoDB / MySQL / SQLite / PostgreSQL / local disk).

Optional driver extras:

```bash
pip install "storepy[mysql]"     # asyncmy
pip install "storepy[postgres]"  # asyncpg
pip install "storepy[sqlite]"    # aiosqlite
```

## Quick start

```python
from pymongo import AsyncMongoClient
from py_store import init, store

client = AsyncMongoClient("mongodb://localhost:27017")
await init(client["mydb"])   # idempotently creates indexes for registered schemas

# Register a schema (pure JSON)
store.register({
    "name": "Post",                  # model name used in GQL
    "collection": "posts",           # optional, defaults to name
    "idPrefix": "PT",                # string _id: prefix + base36 timestamp + random
    "fields": {
        "title": {"type": "string", "default": ""},
        "status": {"type": "string", "default": "draft"},
        "tags": {"type": "array", "default": []},
    },
    "computes": {
        "statusLabel": {"type": "string", "depends": ["status"],
                        "fn": lambda doc: doc["status"].upper()},
    },
    "indexes": [{"keys": {"status": 1, "createdAt": -1}}],
})

# Write — only user data; defaults are filled on read
doc = await store.insert("Post", {"title": "Hello"})

# Query — GQL tree syntax, values referenced from params via @key
items = await store.query(
    "Post($condition:@c0,$sort:@s1,$limit:@l) { title, status, statusLabel }",
    {"c0": {"status": "draft"}, "s1": {"createdAt": -1}, "l": 20},
)
```

The same schema and the same query run unchanged against PostgreSQL — only the `init()` datasource changes:

```python
await init({"default": {"kind": "postgres", "exec": exec}})
items = await store.query("Post($condition:@c0) { title, status }", {"c0": {"status": "draft"}})
```

## Supported backends

| Backend | Notes |
| --- | --- |
| MongoDB | native aggregation pipeline (`find`/`aggregate`/`$lookup`), PyMongo `AsyncMongoClient` (pymongo >= 4.9) |
| MySQL | parameterized SQL, `information_schema` introspection (`asyncmy`) |
| SQLite | parameterized SQL, `sqlite_master` + `PRAGMA` introspection (`aiosqlite`) |
| PostgreSQL | parameterized SQL (`$n`), `RETURNING` support (`asyncpg`) |
| local (local disk) | collections persisted as JSON files, evaluated directly by the core's local evaluator (no SQL, no driver); see [Local disk data source (local)](#local-disk-data-source-local) |

GQL tree queries compile to a single native query per backend (except the local source — commands are evaluated directly by the core's local evaluator) — never hand-write `$lookup` or raw SQL again.

### Local disk data source (local)

Zero external services, zero native DB engine: collections are persisted as JSON files and commands are evaluated directly inside the Rust core's local evaluator (semantics match the MongoDB driver).

```python
from py_store import init, local

await init({'default': local.connect({'dir': './data/store'})})
```

- **Usage**: `local.connect({'dir': …})` returns a connection descriptor (`kind: 'local'`); pass it to `await init()` like any Mongo/SQL connection.
- **On-disk format**: `<dir>/<physical collection>.json` (an array of documents); writes go through a `.tmp` temp file and an atomic `rename`.
- **Transactions**: snapshot isolation — during a transaction reads/writes hit an in-memory snapshot; `commit()` persists the whole directory, `rollback()` discards it. Atomic by construction.
- **Guardrails**: hard cap of 100,000 documents per collection (explicit error, never silent truncation); no indexes (sequential scan only) — declaring `schema.indexes` emits a `local_indexes_ignored` feedback event.
- **Concurrency limit**: single process only — in-process writes are serialized by a directory lock; cross-process concurrency is outside the v1 guarantee.

## Features

- **Pure JSON schemas, zero code** — a model is just a dict: fields, relations, computes, indexes.
- **GQL tree queries → one native query** — nested relations resolve in a single query; never hand-write `$lookup` again.
- **Normalized aggregation** — root-level `$group` / `$having` and relation aggregate predicates (semi/anti-join) in the same GQL, pushed down to all five backends.
- **Read-time defaults & computed columns** — writes store only user data; reads fill defaults and run `fn` / `asyncFn` / relation-`agg` computes.
- **Smart mutation** — `mutation()` auto-detects upsert by `_id` + unique index and recursively fills relation children.
- **Soft-delete built in** — every schema auto-registers a `<Model>Deleted` archive collection/table; `remove()` archives before deleting.
- **Permission context** — `ContextVar`-based roles (`super_admin`/`admin`/`guest`/`creator`...), schema/field-level read/write whitelists, automatic owner-condition injection.
- **Multi-datasource & multi-tenant** — locate a schema by `(source, database, schema, collection)`; re-target per request with a route override.
- **Async-first, Rust core** — built on PyMongo's `AsyncMongoClient` and a shared Rust core with SQL dialects.
- **Relation predicates in mutations** — filter `update` / `remove` by related-table fields, pushed down to all five backends (previously a silent no-op on MongoDB).
- **Autoincrement primary keys** — declare `_id` as `{"type": "int", "strategy": "autoincrement"}` for database-assigned integer IDs, with explicit errors where autoincrement is impossible.
- **Index DDL** — `schema.indexes` compiles to real `CREATE [UNIQUE] INDEX` statements (per backend, byte-identical); the generator still only emits text.

## GQL syntax

```text
Model($condition:@c0,$sort:@s1,$skip:@sk,$limit:@l1) {
  field1, field2, obj.subField,
  Relation($condition:@c2,$sort:@s3,$limit:@l2) { f3, Nested { f4 } }
}
```

- Values come from the params dict: `{"c0": {...}, "s1": {...}}`.
- Object sub-fields use dot notation; relations are declared in the schema (`type: "many" | "one"`) and resolved automatically — **do not hand-write `$lookup`**.
- `many` relations return lists (`[]` when empty); `one` relations merge into the parent document (`None` when missing).
- Relation-level `$sort`/`$skip`/`$limit` are **per-parent top-N** (each parent gets its own window; translated to a window function on SQL).

> **Breaking change**: user `$pipeline` passthrough was removed (raw aggregation escape hatch). A GQL containing `$pipeline` now fails explicitly instead of being silently ignored. `store.aggregate()` returns as a strictly **opt-in** alias (disabled by default, see "Transactions and raw SQL") — the default behavior is unchanged.

## Aggregation

Normalized aggregation lives **inside GQL** — no separate API, no raw pipeline.

**Root-level `$group` + `$having`** (GROUP BY / HAVING):

```python
rows = await store.query(
    "Course($condition:@c0,$group:@g0,$having:@h0,$sort:@s0,$limit:@l0){ status, n, total }",
    {
        "c0": {"status": {"$ne": "deleted"}},
        "g0": {"by": ["status"], "agg": {"n": {"$count": "*"}, "total": {"$sum": "price"}}},
        "h0": {"n": {"$gt": 1}},
        "s0": {"total": -1},
        "l0": 20,
    },
)
```

- Whitelisted operators: `$count` / `$sum` / `$avg` / `$min` / `$max`.
- Fixed execution order: `$condition` (WHERE) → `$group` (GROUP BY) → `$having` (HAVING) → `$sort` → `$skip`/`$limit` → projection.
- Omit `by` (or pass `[]`) for a single all-table group; the empty-input case still returns one row (`$count` → `0`, others → `None`).

**Relation aggregate predicates (semi / anti-join)** — filter parents by an aggregate over a relation, without fanning out:

```python
await store.query("Product($condition:@c0,$sort:@s0){ _id, name }", {
    "c0": {
        "$and": [
            {"status": "onSale"},
            {"orders": {"$count": {"$gt": 3}}},                                   # has > 3 orders
            {"$not": {"orders": {"$sum": {"$of": "amount", "$gt": 10000}}}},      # not a whale
        ],
    },
    "s0": {"name": 1},
})
```

Translates to `EXISTS` / `NOT EXISTS` on SQL and `$lookup` + `$match` on MongoDB.

**Relation-rolling computed columns** — declare once in the schema, request by name:

```python
"computes": {
    "itemCount": {"type": "int", "agg": {"$count": "items"}},       # 0 when empty
    "itemsTotal": {"type": "float", "agg": {"$sum": "items.qty"}},  # None when empty
}
```

## Query & write API

```python
items  = await store.query(gql, params)              # list[dict]
one    = await store.query_one(gql, params)          # dict | None
page   = await store.query_with_count(gql, params)   # {'items','total','hasMore','page','pageSize'} (pageSize capped at 5000)
exists = await store.exists("Post", {"_id": pid})
n      = await store.count("Post", {"status": "active"})

doc    = await store.insert("Post", {...})           # auto _id / createdAt / updatedAt
docs   = await store.insert_many("Post", [{...}, ...])
await store.update("Post", {"_id": pid}, {"status": "live"})     # plain fields → $set
await store.update("Post", {"_id": pid}, {"$inc": {"views": 1}}) # '$'-prefixed keys pass through as operators
await store.update_many("Post", {"type": t}, {"status": "live"})
r      = await store.remove("Post", {"_id": pid})    # archives to <collection>_deleted first
await store.mutation("Post", {...})                  # smart upsert + recursive relation children
await store.upsert("Post", {"code": "A1"}, {...})    # explicit-condition upsert (no relation handling)
```

Notes:

- `None` values are stripped before persisting; `_id` cannot be changed via `update`.
- `createdAt`/`updatedAt` are framework-maintained — do not set them manually. Unit follows the schema's `timestamps` setting: milliseconds by default, or seconds when `timestamps: "s"`.
- `update_many` / `remove` with an **empty condition** (`{}`, `None`, `{"$and": []}`) is rejected outright — it never falls through to a full-table write.
- Snake-case API (the only naming, no camelCase aliases): `query_one`, `insert_many`, `update_many`, `build_pipeline`, ...

### Transactions and raw SQL

```python
async def transfer():
    rows = await store.execute_raw(
        "default", "SELECT * FROM accounts WHERE _id = ? FOR UPDATE", [acc_id])
    await store.execute_raw(
        "default", "UPDATE accounts SET balance = ? WHERE _id = ?", [new_balance, acc_id],
        is_write=True)

await store.transaction("default", transfer)
```

- `store.transaction(source, fn)` opens a transaction scope on one source: every `execute_raw` / CRUD call inside `fn` lands on that source's transaction connection, with `commit` / `rollback` as one unit (reuses the internal `run_in_transaction`). Mongo sources are probed at runtime (replica set / sharded) and wrapped in a session transaction; on standalone or probe failure `fn` runs as-is and emits `mongo_transaction_unsupported` (`deployment: standalone|unknown`) — it never pretends to be atomic. Executors without `with_transaction` also run `fn` as-is and emit a `transaction_not_atomic` feedback event (degradation is allowed, silent pretence is not). A nested same-source transaction opens a savepoint (an inner failure rolls back only that scope); without savepoint primitives it degrades by joining the outer transaction and emits `nested_savepoint_unsupported`.
- `store.execute_raw(source, sql, params=None, is_write=None)` runs raw SQL, compiled by the core `raw_stmt_compile`. Two styles selected by the `params` type: **positional** (list/tuple/None) passes the SQL through as-is with native placeholders (`?` for MySQL / SQLite, `$1..$n` for PostgreSQL); **named** (dict) compiles `:name` tokens in the SQL into dialect placeholders (same-name reuse, `::` casts / quotes / comments kept intact; missing or unused names raise `RawSqlError`). SQL sources only — a Mongo source raises `RawSqlError` (`py_store.RawSqlError` / `store.RawSqlError`).
- When `is_write` is omitted it is inferred from the SQL's first word (SELECT / WITH / EXPLAIN / SHOW / PRAGMA / TABLE count as reads, everything else as a write — defaulting to write is the safe direction); passing it explicitly overrides the inference. Returns `{"rows", "affectedRows"}`: rows for reads, the affected-row count for writes.
- `store.execute_native(source, collection, pipeline=None, options=None)` runs a native aggregation pipeline on a Mongo source (the Mongo counterpart of the SQL-side `execute_raw` escape hatch): `pipeline` is a native aggregation pipeline, `options` uses driver-native keys (`allowDiskUse` / `batchSize` / `hint` / `maxTimeMS`..., no host-side whitelist). Inside a transaction / session the session is injected automatically (owned by the transaction; `options.session` cannot override it); resolution always follows the read path, so `$merge` / `$out` write stages require you to open a transaction yourself. Mongo sources only — a SQL source raises `NativeCommandError` pointing to `execute_raw`; the MongoClient form requires a schema-declared `database`. Returns `{"rows"}`.
- `store.aggregate(schema_name, pipeline=None, options=None)` is the schema-name twin of `execute_native`: it resolves `datasource` / `collection` from the registered schema metadata and delegates to the same channel. Strictly **opt-in** — disabled by default (a product boundary, not an oversight); enable it per-init with `init(connections, {"allowNativeAggregate": True})` (every `init` resets it — omitting the key turns it back off). Disabled calls raise `RuntimeError` with `code = "ERR_NATIVE_AGGREGATE_DISABLED"`; an unregistered schema raises `code = "ERR_AGGREGATE_SCHEMA"`. When enabled it behaves exactly like the native driver: **no GQL permission filtering, no compute rehydration**, and `$merge` / `$out` write stages still require you to open a transaction yourself. Returns `{"rows"}`.
- **The non-transactional path commits explicitly**: on SQL sources, a write plan that runs outside `store.transaction` is committed by the executor (`commit` on success; `rollback` then re-raise on failure). `aiosqlite` is not autocommit by default, so without that commit the write would be visible only on the current connection while `execute_raw` still reported success — a silent data-loss hazard. Multi-statement writes that must be atomic as a group belong inside `store.transaction`.

### Concurrency primitives

Declarative task-claiming and compare-and-swap on top of the same plan → Mongo-command pipeline — the patterns that previously required a raw-SQL escape hatch (`FOR UPDATE SKIP LOCKED` via `execute_raw`) now have first-class methods, so outbox dispatching and leader election no longer need bare SQL:

```python
got = await store.claim("Job", {"status": "pending", "runAt": {"$lte": now}},
    {"claimedBy": "worker-1", "status": "claimed", "leaseUntil": now + 30_000},
    limit=10, sort=[["runAt", 1]])     # returns the claimed rows (their latest values)

matched = await store.compare_and_set("Leader",
    {"_id": "leader-1", "term": 7},    # current-state guard: the CAS condition
    {"term": 8, "holder": "node-a"})   # the write to apply when it matches

await store.renew_lease("Leader",
    {"_id": "leader-1", "holder": "node-a"},   # ownership condition: only the holder renews
    {"leaseUntil": now + 30_000})
```

- `store.claim(schema_name, filter, update, *, limit=1, sort=None, route_override=None)` atomically claims up to `limit` rows matching `filter` and applies `update` to exactly those rows in one transaction (`atomic_commands`: every command lands on the same connection, in order). Returns the claimed rows. PostgreSQL uses `SELECT … FOR UPDATE SKIP LOCKED` so concurrent claimers skip locked rows instead of blocking; SQLite compiles the same plan into a single `UPDATE … RETURNING`; **MySQL has no `SKIP LOCKED`-equivalent in one statement for this plan, so the core compiles it as two steps on one connection** (`SELECT _id … FOR UPDATE` → `UPDATE _id IN (…)`), still atomic within the transaction but contenders queue on the lock rather than skipping.
- `store.compare_and_set(schema_name, filter, update, *, route_override=None)` applies `update` only when `filter` still matches the current state; it returns `{"matched", "modified"}` (both counts). **MySQL matched semantics**: the core pre-reads the matching rows with a locking `SELECT` and drives the `UPDATE` by the returned primary keys, so `matched` is the number of pre-read rows — not MySQL's default "rows changed" count. `renew_lease` is `compare_and_set` by another name: the lease-ownership condition (`holder` / `claimedBy`) is part of `filter`, supplied by the caller.
- Lease bookkeeping fields (`claimedBy` / `leaseUntil` / …) are **caller-owned schema fields**, not built-ins: declare them on the schema, and expiry/cleanup is a plain query on your side.
- SQL sources only. A Mongo source raises with code `concurrencyPrimitivesSqlOnly` — Mongo has no `SKIP LOCKED`-style claim primitive, and faking one with `find_one_and_update` loops would not be atomic under concurrency (explicit refusal, no silent degradation).

### Session (Unit of Work)

```python
async with store.session() as s:
    await s.insert("Order", {...})
    await s.update("Account", cond, {...})
    await s.execute_raw("pg_main", "SELECT ... FOR UPDATE", [1])
```

- Inside a session, **every command on the same SQL source lands on one transaction connection**: the session commits once on exit, and rolls back as one unit on any exception.
- **Lazy transaction start**: a session with no commands never checks out a connection.
- **Cross-source writes fail closed**: if a session writes to ≥2 datasources, it rolls everything back and raises `NonAtomicWriteError` on exit (no distributed transaction — it never commits a half-done unit of work).
- Mongo sources are probed at runtime (replica set / sharded) and made transactional; on standalone or probe failure they run as-is (non-atomic) and emit one `mongo_transaction_unsupported` (`deployment: standalone|unknown`) feedback event.
- Sessions nest: an inner scope opens a savepoint (`SAVEPOINT sp_<n>`) on the outer transaction and, on exit, `RELEASE`s it (success) or `ROLLBACK TO`s and releases it (failure) — **an inner failure rolls back only the inner scope while the outer one continues**. When the transaction handle has no savepoint primitives, the nested scope degrades by joining the outer one and emits one `nested_savepoint_unsupported` feedback event.

### DDL generation

```python
sql = store.generate_ddl("mysql")                       # every registered model
sql = store.generate_ddl("postgres", ["Course", "CourseDeleted"])
```

`store.generate_ddl(backend, names=None)` maps one registered schema def to one `CREATE TABLE` — the inverse of `sync_schema()`, which only *reads*. The generator is **pure text**: it never connects to, or writes to, the database (iron rule 6 still holds).

- Only scalar fields become columns; `object` / `array` fields do not.
- Every table gets the `__present` sentinel column; `timestamps` models also get `createdAt` / `updatedAt`; the `<collection>_deleted` archive table is generated like any other registered def.
- The `<Name>Deleted` archive def is **derived by the Rust core when the model is registered**; the Python host only mirrors it (`collection` `<c>_deleted`, the `deletedAt` field, empty `idPrefix`) and never re-registers it into the core. So `schema.list()` and `generate_ddl()` contain each archive table exactly once; if a duplicate ever appears (upstream regression), they deduplicate and emit a `schemaDuplicateName` / `ddlDuplicateTable` feedback event instead of silently emitting duplicate `CREATE TABLE`s.
- No `CREATE INDEX` is emitted — SQL backends keep indexes as metadata only.
- MySQL `__present` is `VARCHAR(255)`; a schema whose present-token string would overflow emits a `ddlPresentOverflow` feedback event rather than failing silently.

### Schema migration (orchestrator)

`store.migrate(backend=..., source=..., defs=..., old_defs=..., driver=..., tenant=None, env=None)` orchestrates the whitelist migration physically: diff two def versions → per-dialect SQL (`generate_migration`) → execute in one transaction per table → record a version row in the built-in `__schemaMigration` table.

```python
r = await store.migrate(
    backend="sqlite", source="default",
    defs={"Note": note_v2},        # {name: defn}, same shape as persist_def
    old_defs=None,                 # None → introspect (needs `driver`: the native driver instance,
                                   #        same convention as sync_schema — the connection view does
                                   #        not expose it); or pass old_defs explicitly to skip it
    driver=db,
)
# r = {"applied": [{"name", "from", "to", "version"}], "skipped": [{"name", "reason"}]}
```

- **Idempotent** — the latest `__schemaMigration` row for the same `(tenant, env, backend, name)` is hash-compared (SHA-1 of the stable-serialized defn) *before* diffing; an identical rerun is reported as `skipped: [{"name", "reason": "unchanged"}]` and touches nothing.
- **Whitelist-only** — add table / add column / add index / type widening. Anything else raises `MigrationUnsupported` (a `MetaDefError` subclass) whose message carries a fixed two-step recipe: *"白名单外变更请分两版走：先加新列并回填，确认切换后再于下一版删除旧列"* — no auto-execution, no auto-rollback.
- **Atomic per table** — the SQL statements and the version row commit or roll back together; a mid-way failure leaves no orphan version row. A multi-table batch is *not* one cross-table transaction — reconcile by `applied` / `skipped` and re-run.
- **Diff runs in the physical namespace** — introspected keys are physical (`view_count`, snake_case on SQL backends) and physical names are not reversible, so the target defn is translated through the same single-point physical-name function before diffing; introspection-inferred granularity (e.g. SQLite `INTEGER` → `number`) is aligned so a re-run is never mis-read as a change. Type widening (`int→long` etc.) produces DDL on MySQL/PostgreSQL; on SQLite numeric columns have no physical difference, so it reports unchanged.

## Multi-datasource connections

A schema is located by `(source, database, schema (PostgreSQL only), collection)` — the tuple
is globally unique across the registry (duplicate registration raises instead of silently
mis-routing). The definition file itself carries no location; `source` / `database` / `schema`
are resolved from the definitions directory layout plus the connection config.

<!-- SPEC:LOCATION:BEGIN -->
### Location: directory semantics + connection config (definitions carry no location)

A schema definition file contains no location fields (no `source` / `database` / `schema`; `namespace` is removed). Location is resolved from the definition directory layout plus the connection config:

- Under the definitions root `<defs-root>/`: the first directory level is the `database`; PostgreSQL adds a second level for `schema` (Mongo / MySQL / SQLite have no such level); deeper levels are free-form and flattened at load time (no hierarchy semantics).
- The connection config (`store.config.json`) declares `sources` (`kind` + `databases`) and `defs`; `kind` decides whether that database directory is read one level deeper for `schema`.
- Location fields are `source` / `database` / `schema` (PG only) / `collection`; the word `namespace` is removed.
- Same-named schemas: exactly one primary (no `replica`); the rest declare `{ "name": "...", "replica": true }`, add only a link, and must not repeat the structure. Zero or two-or-more primaries is an error.
- A duplicated `name` within one load batch is an error and the service does not start; re-loading the same `name` across versions bumps its version by 1.
- Writes are synchronized within a single connection, across the primary plus all links, in one transaction; a write spanning a cross-connection link is explicitly rejected or degraded with a feedback event (never silent).
<!-- SPEC:LOCATION:END -->

```python
# Multiple Mongo servers: one source per connection
await init({"mongo_main": db, "pg_a": {"kind": "postgres", "exec": exec}})

# SQL cross-database / PG-schema joins are pushed down natively ("db_a"."t" JOIN "db_b"."t");
# only Mongo cross-db relations fall back to in-memory federation.
```

**Multi-tenant route override** — one schema definition, N tenants. Any query/write accepts
a `{ "source", "database", "schema" }` override that re-targets commands at execution time
(permissions and computed columns still follow the structural schema):

```python
await store.query('User($condition:@c0){...}', params, {"database": "tenant_42"})
await store.insert("Order", data, {"source": "pg_cluster", "schema": "tenant_7"})
```

**`route_override` is a trusted server-side parameter** — it carries no origin check, so
forwarding user-controlled input into it lets a caller re-target another tenant's
`source`/`database`/`schema` (CWE-639 authorization-bypass surface). Never pass raw request data here.

Legacy single-db usage (`init(db)` + schema without location) is unchanged: commands carry
`source: "default"` with the connection's default `database` / `schema`.

## Permission context

```python
# Set once per request (in router/dependency layer)
store.set_context({"userId": uid, "roles": ["editor"]})

# Internal/cron jobs — bypass permission checks
await store.run_as_internal(lambda: store.remove("Post", {"_id": pid}))
```

- `super_admin`/`admin`/`internal` roles pass everything; other roles are checked against schema-level and field-level `read`/`write` whitelists; `guest` can never write.
- `creator` is a pseudo-role resolved by `doc.createdBy == ctx.userId`; schemas granting it automatically get owner conditions injected on queries and ownership checks on update/remove.
- No context set → permission checks disabled (backward compatible).
- Denied access raises `store.PermissionError` (with `status = 403`).

### Fail-secure mode (opt-in)

"No context" can mean both *system call* and *caller forgot the context* — by default the
latter silently passes every check (fail-open, kept for backward compatibility). For
security-sensitive hosts, enable the context requirement once at startup:

```python
store.set_require_context(True)
# now every query/write without a context raises `ERR_NO_CONTEXT:...`
# internal jobs must be explicit:
await store.run_as_internal(lambda: store.remove("Post", {"_id": pid}))
```

`run_as_internal` marks the call as `{"internal": True}`, which is semantically distinct
from a missing context and always passes. `set_require_context(False)` restores the default.

## Feedback events

Degraded / pushdown-rejection paths never fail silently — they emit a structured event:

```python
{type, code, layer, message, hint, ...}   # federation_degraded / sql_pushdown_unsupported / ...
```

- Default sink prints to stderr; hosts (e.g. AI data-QA services) can take over for automated feedback loops:

```python
store.set_feedback_sink(lambda event: log.warning("store feedback: %s", event))
```

- SQL pushdown rejection raises `PushdownUnsupportedError` (a `RuntimeError`) **and** emits the event; catch it to re-run that segment on a Mongo source.

## Schema reference

```python
{
    "name": "Order",
    "collection": "orders",
    "idPrefix": "OD",
    "timestamps": True,                # True (ms, default) | False | "ms" | "s" (seconds); auto-maintain createdAt/updatedAt
    "fields": {
        "_id": "string",                                        # shorthand
        "title": {"type": "string", "default": ""},
        "meta": {"type": "object", "default": {}, "fields": {...}},  # nested object fields
        "bidders": {"type": "array", "default": []},            # array field — inline agg target
    },
    "relations": {
        "items": {"model": "OrderItem", "type": "many",
                  "localField": "_id", "foreignField": "orderId"},
    },
    "computes": {
        "total": {"type": "float", "depends": ["amount"], "fn": lambda d: d["amount"] * 1.1},
        "itemCount": {"type": "int", "agg": {"$count": "items"}},                 # relation agg
        "biddersCount": {"type": "int", "depends": ["bidders"], "agg": {"$size": "bidders"}},
        "maxBid": {"type": "float", "depends": ["bidders"], "agg": {"$max": "bidders.amount"}},
        "bidderCount": {"type": "int", "depends": ["bidders"], "agg": {"$countDistinct": "bidders.bidderId"}},
    },
    "indexes": [
        {"keys": {"status": 1}},
        {"keys": {"code": 1}, "options": {"unique": True}},
    ],
    "read": ["editor", "viewer"],      # optional schema-level role whitelists
    "write": ["editor"],
    "ownerKey": "_id",                 # optional ownership key for the `creator` pseudo-role; default "createdBy"
}
```

Types: `string | int | long | float | double | boolean | array | object | date | any`.

Boundary rules worth knowing up front (all **fail explicitly**, never silently degrade):

- Filtering on array fields directly, on a whole object field, or on object dot-paths is rejected on every backend — model cross-entity semantics as `relations` instead.
- Relation predicates support **one level** of relation; paths like `orders.items.price` are rejected.
- An unreadable relation is an error, not a silent `False`.

### Inline array aggregation

An `agg` whose first path segment is a declared **array field** (not a relation) aggregates
over the document's own array — compiled into the same pipeline, no extra query phase:

| Operator | Path | Empty array / missing field |
|---|---|---|
| `$size` | the array field itself | `0` |
| `$sum` | `array.element` | `0` |
| `$avg` / `$max` / `$min` | `array.element` | `null` |
| `$countDistinct` | `array.element` | `0` — counts distinct non-null values |

Rules (violations fail at registration, never silently degrade):

- `depends` must include the first path segment field.
- One level of array element only: `bidders.amount` is valid; nesting further into arrays is rejected.
- `$count` stays relation-only — use `$size` for arrays.
- Available on MongoDB and the local (in-process) evaluator; SQL backends reject it explicitly.

<!-- SPEC:NAMING-STYLE:BEGIN -->
### Naming: freeform definitions, system-directed translation

Definitions (`collection`, fields, referenced relation fields, computed-column keys, `fnRef` values, index names) may use any style; the engine translates them to the target style. Contract keys (`fnRef`, `localField`, `foreignField`, `asyncFn`, `type`, ...) and the schema `name` are never translated.

| Target | Style | Example (`orderTotal`) |
|---|---|---|
| MySQL / PostgreSQL / SQLite (physical) | snake_case | `order_total` |
| MongoDB (physical) | camelCase | `orderTotal` |
| Node.js / Java / C# / Rust (code; computed columns follow) | camelCase | `orderTotal` |
| Go (code; computed columns follow) | PascalCase (must be exported) | `OrderTotal` |
| Python (code; computed columns follow) | snake_case | `order_total` |

Canonicalization (single implementation `core::naming`, re-exported by the bindings; hosts must not re-implement it): split on `_`, `-`, `.`, space and at lower/digit-to-upper boundaries; a trailing uppercase in a run followed by a lowercase starts the next token (`HTTPServer` -> `[http, server]`, `userID` -> `[user, id]`); digits stay inside a token (`order2Items` -> `[order2, items]`). Reassembly: snake = `t1_t2`, camel = `t1T2`, pascal = `T1T2`.

Two logical names in one schema that canonicalize equal (`orderTotal` vs `order_total`), or a name that canonicalizes onto a reserved contract key (e.g. `fnref`), is an error `ERR_NAME_CONFLICT:` and the service does not start (never silently overwritten).
<!-- SPEC:NAMING-STYLE:END -->

<!-- SPEC:FNREF:BEGIN -->
### Computed columns: `fnRef` binding by composite name + canonical match

Computed columns live at the schema top level, `computes: { <key>: { type, fn | asyncFn | agg, fnRef?, depends?, read? } }` (`fn` / `asyncFn` / `agg` are mutually exclusive).

- The logical `fnRef` defaults to `<schema.name>.<computed-column key>` (generated, never hand-written); since `name` is globally unique, the `fnRef` is globally unique too.
- Host implementations bind by canonicalization: both the implementation's name in the host language style and the schema's logical `fnRef` are canonicalized to token sequences and compared. So Node's `orderAmountLabel` and Python's `order_amount_label` bind to the same logical computed column.
- Reusing one implementation across schemas: write an explicit shared name (e.g. `"fnRef": "common.moneyLabel"`); naming goes from required to optional.
- Every declared `fnRef` must have an implementation, otherwise the service fails to start with `ERR_FN_MISSING`.
<!-- SPEC:FNREF:END -->

## Vector search

Vectors are ordinary field values with a declared dimension and metric: you produce the
embedding, the store persists it, ranks by it and returns the score. No embedding model is
bundled.

### Schema declaration

```python
{
  'name': 'Artwork',
  'collection': 'artworks',
  'fields': {
    'title': {'type': 'string', 'default': ''},
    'caption': {'type': 'string', 'default': ''},
    'embedding': {'type': 'vector', 'dim': 1024, 'metric': 'cosine'},  # 'cosine' | 'l2' | 'ip'
    'embeddingHash': {'type': 'string', 'default': ''},                # optional: reindex digest
  },
  'indexes': [
    # Declares intent for a native approximate index (pgvector / Atlas Search).
    {'keys': {'embedding': 'vector'}, 'options': {'metric': 'cosine', 'method': 'hnsw'}},
  ],
}
```

- `type: 'vector'` requires an integer `dim` and one of `metric: 'cosine' | 'l2' | 'ip'`;
  an invalid `dim` or `metric` fails at registration time, not at query time.
- Two different ceilings apply. **Storage** accepts up to 16000 dimensions. **Native indexing**
  is lower and backend-specific: pgvector HNSW / IVFFlat accepts up to 2000, MongoDB Atlas
  `vectorSearch` up to 4096. DDL generation rejects an index above the backend ceiling instead
  of silently skipping it.
- A vector index item is a single field: `{'keys': {<field>: 'vector'}, 'options': {...}}`.
  Options are `metric`, `method` (`hnsw` | `ivfflat` | `flat`) and, for Atlas, `indexName`.
- Rows whose vector is `None` (including pre-migration rows) are simply not scored.

### Query operator

```python
rows = await store.query(
    'Artwork($condition:@c){ _id, title, _score }',
    {'c': {'embedding': {'$near': {'$vector': [0.12, -0.03], '$limit': 10}}}},
)
```

- `$near` takes exactly three keys: `$vector` (required query vector), `$limit` (required
  TopK, positive integer) and `$minScore` (optional floor on `_score`). Any other key is rejected.
- `metric` is fixed by the field declaration; it is not a `$near` parameter. All metrics are
  normalized to "higher is more similar": `cosine` = cosine similarity, `ip` = inner product,
  `l2` = `1 / (1 + euclidean distance)`.
- Filters live next to the operator under the same `$condition`, e.g.
  `{'status': 'public', 'embedding': {'$near': {...}}}`.
- `_score` is opt-in: project it explicitly. A query without `$near` never returns a `_score` key.
- A vector whose length differs from `dim`, an all-zero query vector and a missing `$limit` are
  errors on every backend.

### Degradation semantics

`(await store.capabilities())['vector']` is the static capability report:
`{supported, native, backend, metrics, maxDim}`.

- `native: True` means the target can run an approximate index (pgvector installed, or a declared
  Atlas Search vector index). `native` is a **static** property of the target; it never changes
  because one query had to fall back.
- Without a native index — MySQL, SQLite without `vec0`, self-hosted MongoDB without an Atlas
  Search index, and the embedded `local` / `memory` carriers — the query still runs as an
  **exact application-side scan**, and the host records a `vector_degraded` degradation event.
  Degradation is observable, never silent.
- PostgreSQL with a filter (anything besides `$near` under `$condition`) falls back to an exact
  scan: a pgvector approximate index is post-filtering and would break ACL / TopK correctness.
- Read degradation three ways: `py_store.degraded.begin()` … `store.degraded_status()` for a
  request scope, the `vector_degraded` event on the feedback channel, and the
  `x-store-degraded` response header in `store-api`. Events carry
  `type / code / layer / backend / field / metric / message / hint`.

### Migration

- Existing tables are never altered implicitly. Add the column explicitly and re-register the
  field: `ddl.generate_migration(...)` emits `ALTER TABLE ... ADD COLUMN embedding vector(1024)`
  (a `DEFAULT` on a vector column is rejected; existing rows stay `NULL` until backfilled).
- Changing `dim` or `metric` of an existing field is not a supported migration — rebuild the
  column and its index on purpose.
- Backfill with
  `await store.reindex_vectors(schema, field=..., hashField=..., sourceFields=..., embed=...)`:
  it pages by `_id` (anchored, no `skip`), digests `sourceFields` with `sha256`, and only
  re-embeds rows whose digest changed. `force=True` re-embeds everything; `dryRun=True` reports
  without calling `embed`. The vector and the digest are written in the same update.
- For incremental embedding, `store.register_vector_reindex(cfg)` installs the
  `<Schema>.vectorReindex` trigger so one row can be refreshed on demand.
- DDL generation only prints the index statement; the store never executes it. On big tables run
  it out of band (`CREATE INDEX CONCURRENTLY` on PostgreSQL).

## Transaction boundary

| Scenario | Atomicity |
|---|---|
| Single-command API (`insert` / `insert_many` / `update_many` / `upsert` / `remove` / `count` / `exists`) | Naturally atomic within one SQL source (a single statement); single documents are atomic on Mongo |
| `store.transaction(source, fn)` | Atomic within one SQL source: every command in the scope shares one connection and one transaction; nested same-source scopes use a savepoint (an inner failure rolls back only that scope) |
| `store.session(...)` | Atomic **across multiple calls** on one SQL source inside the session; cross-source writes are rejected explicitly (`NonAtomicWriteError`) |
| Cross-source multi-write without a session | Not atomic (no 2PC / Saga), executed datasource by datasource, and declares `nonAtomic` via the feedback channel (event `non_atomic_write`, with the sources) |
| Multi-step writes on Mongo | replica set / sharded: atomic on a single Mongo source (session transaction); standalone: non-atomic and explicitly declares `mongo_transaction_unsupported` |

- **Mongo sources**: made transactional inside a session according to the runtime probe; non-transactable ones (standalone / probe failure) run as-is and emit a `mongo_transaction_unsupported` feedback event (`deployment: standalone|unknown`) (degradation is allowed, silent pretence is not).
- **SQL executors without `open_transaction`**: commands run as-is inside a session and emit a `session_not_atomic` feedback event (degradation is allowed, silent pretence is not).
- **SQL executors without `with_transaction`**: commands run as-is inside a transaction scope (or the top-level atomic envelope) and emit a `transaction_not_atomic` feedback event (symmetric with `session_not_atomic`; degradation is allowed, silent pretence is not).
- **Archive idempotency**: `remove` archives with upsert-by-`_id` semantics, so a retry after partial failure no longer fails on duplicate `_id`.
- Read consistency: only multiple reads inside an explicit session share one transaction connection; reads outside a session do not open an extra transaction.
- **Cross-source writes (no session)**: a single write call touching ≥2 datasources **cannot be atomic**; it runs sequentially and emits one `non_atomic_write` feedback event (`code: nonAtomic`, with the source list) — degradation is allowed, silence is not. Converge writes onto a single source, or wrap them in `store.session()` (which fails closed on cross-source writes).

## Triggers

Declarative trigger chains on a schema: write events (`insert` / `update` / `remove`) are expanded by the Rust core at plan time into an ordered list of side-effect steps (`plan.triggers`) that the host runs in sequence inside the same atomic envelope as the source write. The declaration form (the `triggers` event keys plus the field table) and the placeholder grammar are defined by the "Triggers" section of the rust-store README; the core's expanded output is byte-for-byte identical across hosts (golden guard: `rust-store/fixtures/triggers/cases.json`).

**Callback wiring** (`py_store/crud/triggers.py`):

```python
async def _grant_points(args, ctx, host):
    await host['store'].update('User', {'_id': args['userId']}, {'$inc': {'points': args['amount']}})

store.set_trigger_fn('grantPoints', _grant_points)
store.assert_trigger_fns_covered(defns)  # startup: a declared fnRef without an implementation ⇒ ERR_TRIGGER_FN_MISSING
```

**Execution semantics**:

- **Hit test** — an `update` event first checks that the `onFields` values actually changed (a structural deep compare, no-op suppression — an unchanged value does not fire) → then the `when` guard (`eq/ne/gt/gte/lt/lte/in/and/or/not`); `insert` has no `before` and skips the field-level check; `remove` uses the archived (pre-delete) document as root. System fields (`createdAt` / `updatedAt` / `deletedAt`) are excluded from the trigger probe projection, so their changes never fire.
- **Placeholders** — `{{root.*}}` (post-change value) / `{{before.*}}` (pre-change value) / `{{now}}` undergo whole-value substitution only; embedding a placeholder inside a string raises `ERR_TRIGGER_PLACEHOLDER` — no silent drift.
- **De-duplication** — within one top-level call, `(step.name, _id)` runs at most once.
- **Callbacks** — `impl(args, ctx, {'store': store})`; `store.*` inside the callback hits the current transaction connection → the same transaction as the source write.
- **Scheduled (cron)** — the `schedule` event is enumerated by the host scheduler plugin (`py_store/scheduler/__init__.py`) and reuses the same trigger chain; only `{{now}}` is allowed as a placeholder.

**Boundaries**: single-source real transaction / a cross-source write declares `nonAtomic` / cross-source writes inside `store.session()` fail closed (see [Transaction boundary](#transaction-boundary)); **no cascading** (a trigger write does not fire further triggers); `update_many` does not support field-level triggers (explicitly refused, no silent degradation); the command step `op: "upsert"` is a registration-time `Err`.

## Resource storage (named credential pools)

Content-addressed blob storage beside the CRUD path: `resource.put / open / remove / url` fan bytes out to a provider registry (`local` directory, `s3`; register more via `registerProvider` / `store.register_resource_provider`) while metadata lands in three plain CRUD schemas (`Resource` / `ResourceLocation` / `ResourceBinding`).

```python
from py_store import resource

resource.configure({"store": store, "pools": {
    "sys": {"providers": [{"kind": "s3", "options": {...sys creds...}}]},
    "biz": {"providers": [{"kind": "s3", "options": {...biz creds...}}]},
}})
out = await resource.put(bytes=data, file_name="a.pdf", pool="biz")
```

- **Named credential pools**: the `pools` config shape builds one isolated provider set per name — the same `kind` can appear in two pools with different credentials without overwriting each other. Every accessor takes `pool=...` (`put` / `open` / `remove` / `url`); `pool=None` means the default pool, and an unknown pool name raises `ValueError` (no silent fallback to the default pool). `resource.pool_names()` lists the configured pools.
- **Backward compatible**: the legacy `{"providers": [...]}` shape is exactly the default pool `"default"` — existing config keeps working unchanged. The two shapes are mutually exclusive: passing both `providers` and `pools` raises `ValueError` (no silent merge). `schema` / `fields` / `store` / `url` / `sign` stay global and cannot be set per pool (a per-pool occurrence raises).
- **`resource.create_provider(kind, options=None)` is a stable public API**: it returns `{"kind", "put", "get", "remove", "exists"}` (callables) or raises `ValueError` on an unknown kind. Services that previously reached into the internal factory and built their own cache (e.g. manager-side storage adapters) should migrate to this export plus the `pool=` parameter — the module-level `_pool` variable remains only as a default-pool alias and is no longer the supported surface.

## Transactional capabilities

Capabilities aimed at transactional workloads (orders, inventory — write contention plus
complex reads). Full details, semantics and the explicit-error list:
**[doc/transaction-capabilities.md](doc/transaction-capabilities.md)** ·
[中文](doc/transaction-capabilities.zh-CN.md).

- **Relation predicates in mutations** — `update_many('Inventory', {'product': {'category': 'meat'}}, {'$inc': {'stock': 10}})`: condition keys matching a declared relation become a semi/anti-join, normalized into a preCommand (aggregate fetching `_id`s) plus `_id $in`.
- **`$group by` one-relation paths** — `by: ['product.category']` compiles to `$lookup`+`$unwind` (Mongo) / `LEFT JOIN` (SQL); `many` paths fail explicitly (fan-out breaks count semantics).
- **Autoincrement PKs** — `_id: {'type': 'int', 'strategy': 'autoincrement'}`; PG/SQLite read back via `INSERT…RETURNING`, MySQL via last-insert-id; MongoDB and `insert_many` fail explicitly with `AUTOINCREMENT_NOT_SUPPORTED` (no silent ObjectId substitution).
- **Index DDL** — `schema.indexes` (MongoDB shape) → `CREATE [UNIQUE] INDEX idx_<table>_<cols>` in `ddl.generate`, byte-identical across MySQL/PostgreSQL/SQLite.
- **Declarative migration** — `ddl.diff_defs(old, new)` + `ddl.generate_migration(backend, old, new)`: whitelist-only (add table/column/index, type widening), per-dialect SQL, pure functions; destructive changes fail with `MIGRATION_UNSUPPORTED`.

## Workflow orchestration (first batch)

Express "orchestration of multi-step data operations" as data: a workflow definition (defn) is pure
JSON isomorphic to a schema defn, and each run is persisted to the built-in schema `__workflowRun`
(queryable with plain GQL — zero new observability endpoints). Execution generalizes the existing
mutation step-sequence mechanism: linear steps + per-step `when` guards + fail-fast. The engine
lives in the host layer (`py_store/workflow.py`), core unchanged; aligned with
`nodejs-store/src/workflow.js` (byte-identical outputs guarded by parity anchor tests).

```python
from py_store import workflow

workflow.register({
    'name': 'placeOrder',
    'run': ['admin', 'ops'],              # three-tier whitelists read/write/run (run falls back to write)
    'steps': [
        {'op': 'query', 'as': 'inv',
         'gql': 'Inventory($condition:@c0){_id, stock}',
         'params': {'c0': {'productId': '{{input.productId}}', 'warehouse': '{{input.warehouse}}'}}},
        {'op': 'fail', 'when': {'exists': '{{inv._id}}', 'is': None}, 'message': '库存记录不存在'},
        {'op': 'fail', 'when': {'lt': '{{inv.stock}}', 'than': '{{input.qty}}'}, 'message': '库存不足'},
        {'op': 'mutation', 'model': 'Inventory',
         'data': {'_id': '{{inv._id}}', 'stock': '{{dec:{{inv.stock}},{{input.qty}}}}'}},
    ],
})

run = await workflow.run('placeOrder', {'productId': 'p1', 'warehouse': 'w1', 'qty': 30})
# run['status'] ∈ succeeded | failed | rejected | drySucceeded | dryFailed
# Uniform contract: business failures never raise; the error lives in run['error']
# (set only on failure; always null on success — never `||`-masked downstream)
```

- **Step whitelist** (three kinds; anything else fails registration with `WORKFLOW_UNSUPPORTED`):
  `query` (result must be unique — >1 row is an explicit error), `mutation` (store.mutation /
  upsert), `fail` (explicit business assertion). Optional `when` guards (exists / is / eq / ne /
  lt / lte / gt / gte) record `skipped` explicitly — never silently skipped.
- **Placeholders**: `{{input.<path>}}`, `{{<as>.<path>}}` (forward references only),
  `{{dec:<a>,<b>}}`; full-string replacement keeps the value type. No placeholders inside gql
  (bind via params — injection safety); no array-index path segments.
- **Permissions**: three-tier role whitelists embedded in the defn (same RBAC semantics: admin /
  super_admin bypass, guest denied, internal bypass); runs inherit the caller's Context and every
  step goes through core permission checks — no superuser. `require_context(true)` rejects
  context-less runs (fail-secure wins over dry-run); rejected runs are persisted for audit.
- **Atomicity**: a single-source run is atomic across steps (outer `run_atomic` wraps the whole
  loop, inner mutations nest into it); multi-source / prescan-failed runs execute sequentially and
  emit feedback events (`workflow_non_atomic` / `workflow_prescan_failed`) — never silent. The run
  record (running → terminal) is committed outside the business transaction so failed runs stay
  queryable after rollback.
- **dry-run**: `workflow.run(name, input, dry_run=True)` — query steps execute for real (read-only
  safe); mutation / fail are recorded as `wouldRun` (`drySucceeded | dryFailed`).
- **Run persistence**: `__workflowRun` is bootstrapped on import (idempotent); SQL backends need a
  one-time `ddl.generate(backend, ['__workflowRun'])` (Mongo creates the collection on first write).
  Its `write` whitelist is explicitly empty (GQL tampering with run audit is rejected by R2).

### Explicitly not in the first batch (detected → error; boundaries shipped with the same weight as features)

| Not supported | Why | Escape hatch |
|---|---|---|
| Loops / parallel / sub-workflows / human approval | DAG & wait semantics explode; linear + `when` covers the first batch | orchestrate in host code via the store API |
| Auto compensation (Saga) / auto retry | Inverse-operation burden; steps have no automatic idempotency | inspect run records and handle explicitly |
| Per-step host callbacks | Arbitrary code breaks whitelist governance | schema computes (read) / host code (write) |
| Workflow defn persistence / hot reload | Depends on schema versioning (next on the roadmap) | defn stays code-side JSON + register, like schemas today |
| Timers / event triggers | Scheduling is a resident-IO concern, orthogonal to pure orchestration | call `run` from the application layer |
| Placeholders inside gql / array-index paths | Injection surface / per-row iteration semantics | params binding / host-code orchestration |

## Scenario examples

The reference sections above describe each capability on its own. The examples below put the same
library to work in a concrete business situation — each is copy-runnable against the backends
listed in [Supported backends](#supported-backends).

### 1. Define a schema as plain Python and query it right away

**Scenario.** You are adding a `Post` model to a small service and do not want to hand-write DDL,
an ODM class, or a repository layer. The model is data, not code.

```python
from pymongo import AsyncMongoClient
from py_store import init, store

await init(AsyncMongoClient("mongodb://localhost:27017")["mydb"])

store.register({
    "name": "Post",
    "collection": "posts",
    "idPrefix": "PT",
    "fields": {
        "title":  {"type": "string", "default": ""},
        "status": {"type": "string", "default": "draft"},
        "tags":   {"type": "array",  "default": []},
    },
})

await store.insert("Post", {"title": "Hello"})
items = await store.query(
    "Post($condition:@c0,$sort:@s1,$limit:@l) { title, status }",
    {"c0": {"status": "draft"}, "s1": {"createdAt": -1}, "l": 20},
)
```

**Result.** `items` is `[{"_id": "PT…", "title": "Hello", "status": "draft"}]` — you never wrote
`status`; it is filled by the field `default` on read.

**Why it wins.** The model is a plain dict, so there is no class hierarchy, no metaclass and no
import-time registration dance — `register()` plus `init()` and the model exists. String `_id`s are
generated for you (`idPrefix` + base36 timestamp + random), and because the very same dict is a valid
schema for the Node.js host ([`nodejs-store`](https://github.com/coenddt/nodejs-store)), a schema
written here is not locked to Python.

### 2. One schema, swap the backend without touching business code

**Scenario.** You develop against a local SQLite file and deploy on PostgreSQL. You do not want two
query dialects or a rewrite at deploy time.

```python
import aiosqlite
from py_store import executors, init, store

# local dev: a SQLite file
db = await aiosqlite.connect("dev.db")
await init({"default": executors.create_connection("sqlite", db)})
```

```python
import asyncpg
from py_store import executors, init, store

# production: a PostgreSQL pool
pool = await asyncpg.create_pool(dsn="postgres://user:pass@host:5432/db")
await init({"default": executors.create_connection("postgres", pool)})
```

The business code that follows is identical in both environments:

```python
items = await store.query(
    "Post($condition:@c0) { title, status }",
    {"c0": {"status": "draft"}},
)
```

**Result.** Both environments return the same document shape; only the two datasource lines differ.

**Why it wins.** GQL is compiled per backend by the shared Rust core — native aggregation on MongoDB,
parameterized SQL on SQLite/PostgreSQL/MySQL. Your business code holds no SQL string and no dialect
branch, so "change the database" is a change to the connection, not to the query. The `local` disk
datasource (see [example 7](#7-local-disk-datasource-for-tests-and-offline-caches)) slots in the same
way with `local.connect(...)`.

### 3. Fetch a course with its lessons, category and reviews in one query (no N+1)

**Scenario.** A course detail page needs the course, its category, its lesson list and its reviews.
The classic bug is N+1: one query for the course, then one more per relation.

```python
course = await store.query_one(
    "Course($condition:@c0){ _id, title, "
    "  category { _id, name }, "
    "  lessons { _id, title, seq }, "
    "  reviews { _id, score } }",
    {"c0": {"_id": "c1"}},
)
```

**Result.** One document with the relations merged in:

```python
{"_id": "c1", "title": "Python入门",
 "category": {"_id": "cat2", "name": "后端"},
 "lessons": [{"_id": "l1", "title": "安装", "seq": 1}, {"_id": "l2", "title": "入门", "seq": 2}],
 "reviews": [{"_id": "r1", "score": 5}, {"_id": "r2", "score": 4}]}
```

`one` relations merge into the parent document, `many` relations become lists (`[]` when empty, and a
missing `one` is `None`).

**Why it wins.** Relations are declared once in the schema, so the selection set alone tells the engine
to resolve them: MongoDB compiles to `$lookup`, SQL to `JOIN`, in a single query per backend. There is
no ORM `selectinload` / `prefetch_related` to remember and no `$lookup` to hand-write, and a missing
relation is `None`/`[]` rather than a crash. Child lists can even be sorted and paginated per parent:
`lessons($sort:@s0,$limit:@l0){ _id, title }`.

### 4. Row-level data access for an org / multi-tenant model

**Scenario.** Online study notes: a student must only read and write their own notes, an audit log is
admin-only, and a `secret` course field is admin-only. You want this enforced in the data layer, not
re-implemented in every handler.

```python
store.register({
    "name": "StudyNote", "collection": "study_notes",
    "read": ["creator", "admin"], "write": ["creator", "admin"],
    "fields": {"_id": {"type": "string"}, "title": {"type": "string"},
               "createdBy": {"type": "string"}},
    "relations": {},
})
```

```python
# set once per request, in the router / dependency layer
store.set_context({"userId": "u1", "roles": ["student"]})

# only u1's notes come back — the owner condition is injected automatically
mine = await store.query("StudyNote($condition:@c0){ _id, title }", {"c0": {"title": "笔记A"}})
```

**Result.** `mine == [{"_id": "n1", "title": "笔记A"}]`. Another user's note is never returned, and
asking for it explicitly returns `[]` — the owner condition is ANDed with your condition, never
replaces it.

**Why it wins.** `creator` is a pseudo-role resolved from `doc.createdBy == ctx.userId`, so ownership
is a data fact rather than a per-handler check — schemas that grant it get owner conditions and
ownership checks injected on the read *and* write paths (including `count`/`exists`). Field-level
whitelists work the same way: a `student` requesting a field marked `read: ["admin"]` simply does not
get the field, while an explicitly requested, unreadable **relation** raises `store.PermissionError`
(`status = 403`) instead of a silent `False`. Privileged roles (`super_admin`/`admin`/`internal`) pass
everything; `guest` can never write.

### 5. Computed columns reused at zero cost on the Python side

**Scenario.** A course list shows `revenue = price * enrolledCount` and `lessonCount = number of
lessons`. You want both computed at read time, with no denormalized columns to keep in sync.

```python
store.register({
    "name": "Course", "collection": "courses",
    "fields": {"_id": {"type": "string"}, "price": {"type": "float"},
               "enrolledCount": {"type": "int"}},
    "relations": {
        "lessons": {"model": "Lesson", "type": "many",
                    "localField": "_id", "foreignField": "courseId"},
    },
    "computes": {
        # sync compute over a single document
        "revenue": {"type": "float", "depends": ["price", "enrolledCount"],
                    "fn": lambda d: (d.get("price") or 0) * (d.get("enrolledCount") or 0)},
        # relation aggregate: count of the related lessons
        "lessonCount": {"type": "int", "agg": {"$count": "lessons"}},
        # async compute over the whole result page (async def fn(items, ctx) -> None)
        "displayName": {"type": "string", "depends": ["name"], "asyncFn": my_display_name},
    },
})
```

```python
rows = await store.query(
    "Course($condition:@c0){ _id, price, revenue, lessonCount }",
    {"c0": {"_id": "c2"}},
)
```

**Result.** `rows == [{"_id": "c2", "price": 100.5, "revenue": 301.5, "lessonCount": 2}]`. You never
requested `enrolledCount`, yet `revenue` is correct — `depends` pulled the input in — and it does not
leak into the output.

**Why it wins.** Dependency fields are auto-injected for the computation and auto-removed from the
projection, so the wire shows exactly what you asked for. A `fn` is one callback over a row; an
`asyncFn` is one batched pass over the whole result (`async def fn(items, ctx)`); an `agg` reuses the
same relation-aggregate machinery as query filters, so "count of lessons" needs no hand-written join.
Reuse across schemas is name-based: give the column an explicit `"fnRef": "common.moneyLabel"` and both
Python's `common_money_label` and Node's `commonMoneyLabel` bind to the same logical column (binding is
by canonical name; a `fnRef` with no implementation aborts startup with `ERR_FN_MISSING`).

### 6. Aggregation that replaces a hand-rolled SQL report

**Scenario.** A dashboard needs "courses grouped by status, with a count and a price sum, keeping only
groups that have more than one course". Instead of one SQL query per metric, ask once.

```python
rows = await store.query(
    "Course($condition:@c0,$group:@g0,$having:@h0,$sort:@s0,$limit:@l0){ status, n, total }",
    {
        "c0": {"status": {"$ne": "deleted"}},
        "g0": {"by": ["status"], "agg": {"n": {"$count": "*"}, "total": {"$sum": "price"}}},
        "h0": {"n": {"$gt": 1}},
        "s0": {"total": -1},
        "l0": 20,
    },
)
```

**Result.** `[{"status": "published", "n": 5, "total": 1000000000000110.8}]` — a
`GROUP BY status ... HAVING count(*) > 1`, ordered by the summed price.

**Why it wins.** `$group` / `$having` are part of GQL, so the same call runs native aggregation on
MongoDB and a parameterized `GROUP BY ... HAVING` on SQL — no separate reporting SQL, no dialect
branches. The execution order is fixed and documented (`WHERE → GROUP BY → HAVING → sort → skip/limit`),
the operator whitelist is `$count` / `$sum` / `$avg` / `$min` / `$max`, and omitting `by` (or passing
`[]`) yields a single whole-table row. Parents can also be filtered by a child aggregate without
fan-out — `{"orders": {"$count": {"$gt": 3}}}` becomes `EXISTS` / `NOT EXISTS` on SQL and never
duplicates the parent row.

### 7. Local disk datasource for tests and offline caches

**Scenario.** You want the same data-layer code in CI or a sandbox with no database to install, and a
durable cache that survives a restart — with zero services.

```python
from py_store import init, local, store

await init({"default": local.connect({"dir": "./data/store"})})

store.register({"name": "User", "collection": "users",
               "fields": {"_id": {"type": "string"}, "name": {"type": "string"}},
               "relations": {}})
await store.insert("User", {"name": "Ada"})
rows = await store.query("User{ _id, name }")
```

**Result.** `rows == [{"_id": "…", "name": "Ada"}]`, and `./data/store/users.json` now holds a JSON
array of the documents.

**Why it wins.** `local.connect({"dir": …})` returns an ordinary connection descriptor, so it drops
into the same `init()` and every GQL/CRUD call works unchanged — no SQL engine, no driver (the `local`
source is evaluated by the core's local evaluator with MongoDB-matching semantics). Writes go through a
`.tmp` file plus an atomic rename, and `store.transaction` / `store.session` get snapshot isolation
that is atomic by construction. The guardrails are explicit rather than silent: a hard cap of 100,000
documents per collection, no indexes (declaring `schema.indexes` emits a `local_indexes_ignored`
feedback event), and single-process only.

### 8. Workflow orchestration: chain multi-step business actions

**Scenario.** "Place an order" is: read the stock, fail if it is missing, fail if it is insufficient,
then decrement. You want the steps and their guards expressed as data, executed fail-fast, and
auditable afterwards.

```python
from py_store import workflow

workflow.register({
    "name": "placeOrder",
    "run": ["admin", "ops"],
    "steps": [
        {"op": "query", "as": "inv",
         "gql": "Inventory($condition:@c0){_id, stock}",
         "params": {"c0": {"productId": "{{input.productId}}", "warehouse": "{{input.warehouse}}"}}},
        {"op": "fail", "when": {"exists": "{{inv._id}}", "is": None}, "message": "库存记录不存在"},
        {"op": "fail", "when": {"lt": "{{inv.stock}}", "than": "{{input.qty}}"}, "message": "库存不足"},
        {"op": "mutation", "model": "Inventory",
         "data": {"_id": "{{inv._id}}", "stock": "{{dec:{{inv.stock}},{{input.qty}}}}"}},
    ],
})

run = await workflow.run("placeOrder", {"productId": "p1", "warehouse": "w1", "qty": 30})
```

**Result.** `run["status"]` is one of `succeeded | failed | rejected | drySucceeded | dryFailed`;
business failures do not raise — the reason is in `run["error"]` (null on success). The run is
persisted to the built-in `__workflowRun` schema, so it is queryable with plain GQL.

**Why it wins.** The definition is pure JSON isomorphic to a schema defn and is validated statically at
`register()`: an unknown step type, a backward placeholder reference, a `when` with a missing operand,
or a `gql` with an embedded placeholder all fail at registration (`WORKFLOW_UNSUPPORTED`) rather than at
run time. Steps are a whitelist (`query` / `mutation` / `fail`), placeholders are `{{input.x}}`,
`{{step.field}}` and `{{dec:a,b}}`, and every step runs under the caller's permission context — there is
no super-identity. A single-source run is atomic end-to-end; a `fail` guard records the run as `failed`
with its `stepIndex`, never silently skipped. `workflow.run(name, input, dry_run=True)` executes the
reads for real and marks writes as `wouldRun`.

### 9. Fail-secure mode: one switch closes every open fallback

**Scenario.** You are going to production and cannot rely on "the caller always remembered to set the
context". You want the safe default to be the enforced default.

```python
from py_store import secure

# once, at startup
secure.secure_mode(admin_roles=["admin"])
```

**Result.** With no context set, every query/write now raises `ERR_NO_CONTEXT` instead of passing
(fail-open → fail-secure). Background jobs must opt in explicitly:

```python
await store.run_as_internal(lambda: store.remove("Post", {"_id": pid}))   # still passes
```

Unconfigured schemas are denied to *everyone* (admin included) until a whitelist is declared, and
registering a new definition without a context is refused. `secure.relax_mode()` restores the
permissive defaults; `secure.is_secure()` reports the current state.

**Why it wins.** `secure_mode()` flips three previously-independent switches together (require-context,
unconfigured-roles-closed, meta-policy-closed), so you cannot turn on one and forget the other two.
`run_as_internal` is *semantically distinct* from a missing context, which is exactly what lets system
jobs keep working while forgotten contexts fail closed — the two cases that "no context" used to
conflate. Degraded paths stay loud, too: a `set_feedback_sink` still receives structured events instead
of a failure going quiet.

### 10. Multi-datasource: MySQL + Mongo behind one store, one federated GQL

**Scenario.** Your users live in MongoDB, your orders live in MySQL, and a profile page needs both.
You do not want two clients, two query languages and a hand-written join in the middle.

```python
import asyncmy
from pymongo import AsyncMongoClient
from py_store import executors, init, store

await init({
    "mongo_main": AsyncMongoClient("mongodb://mongo-host:27017")["shop"],
    "mysql_main": executors.create_connection(
        "mysql", await asyncmy.create_pool(host="mysql-host", user="app",
                                           password="…", db="shop", autocommit=True)),
})

# each def pins its home source; the relation is cross-source
store.register({
    "name": "User", "collection": "users", "datasource": "mongo_main",
    "fields": {"_id": {"type": "string"}, "name": {"type": "string"}},
    "relations": {
        "orders": {"model": "Order", "type": "many",
                   "localField": "_id", "foreignField": "userId"}},
})
store.register({
    "name": "Order", "collection": "orders", "datasource": "mysql_main",
    "fields": {"_id": {"type": "string"}, "userId": {"type": "string"},
               "code": {"type": "string"}, "amount": {"type": "float"}},
    "relations": {},
})

rows = await store.query_federated(
    "User($condition:@c0){ _id, name, orders { code, amount } }",
    {"c0": {"name": "ada"}},
)
```

**Result.** Each user comes back with its MySQL orders nested in the same document — the store
fetches per source, joins in memory, and rehydrates the tree:

```python
[{"_id": "u1", "name": "ada", "orders": [{"code": "A1", "amount": 30.0}, ...]}]
```

**Why it wins.** A schema is located by `(source, database, collection)` and each def carries its
`datasource`, so one registry covers every backend. SQL cross-database / PG-schema joins are pushed
down natively (`"db_a"."t" JOIN "db_b"."t"`); Mongo cross-source relations run through the federated
path (`query_federated`) — and when a segment cannot be pushed down you get a structured
`federation_degraded` event, not a silent slowdown. This exact topology (Mongo users → MySQL orders,
plus a PG side) is what the repo's own four-database stress test runs 8 concurrent workers against
(see [Performance](#performance)).

### 11. Transaction + raw SQL: a transfer that cannot half-land

**Scenario.** Money moves between two accounts: lock the source row, check the balance, debit one
side, credit the other, and append an audit ledger row. Any failure must roll back everything.

```python
async def transfer(amount, from_id, to_id):
    rows = await store.execute_raw(
        "pg_main", "SELECT balance FROM accounts WHERE _id = $1 FOR UPDATE", [from_id])
    if rows["rows"][0]["balance"] < amount:
        raise ValueError("insufficient funds")        # rolls the whole scope back

    await store.execute_raw(
        "pg_main", "UPDATE accounts SET balance = balance - $1 WHERE _id = $2",
        [amount, from_id], is_write=True)
    await store.execute_raw(
        "pg_main", "UPDATE accounts SET balance = balance + $1 WHERE _id = $2",
        [amount, to_id], is_write=True)
    await store.insert("Ledger", {"fromId": from_id, "toId": to_id, "amount": amount})
    #  ↑ a plain CRUD call — it lands on the same transaction connection

await store.transaction("pg_main", lambda: transfer(30, "acc_a", "acc_b"))
```

**Result.** All four statements commit together or not at all. `execute_raw` returns
`{"rows", "affectedRows"}` (rows for reads, the affected count for writes), and
`store.transaction(source, fn)` reuses one connection for everything inside `fn` — raw SQL and
CRUD alike.

**Why it wins.** Raw SQL is compiled by the core's `raw_stmt_compile` (named `:name` params or
positional `?`/`$1`), so escaping and dialect details stay out of your code while the escape hatch
stays fully native (`FOR UPDATE` included). Mongo sources are probed at runtime (replica set /
sharded) and wrapped in a session transaction; standalone Mongo or an executor without transaction
primitives runs `fn` as-is and emits `mongo_transaction_unsupported` / `transaction_not_atomic` —
degradation is announced, never silently pretended. Nested same-source scopes become savepoints: an
inner failure rolls back only that scope.

### 12. Session (Unit of Work): many writes, one commit, fail closed across sources

**Scenario.** "Create an order" touches three tables: insert the order, decrement stock, append an
audit row. You want all of it to behave as one unit of work without hand-managing a connection.

```python
async with store.session() as s:
    order = await s.insert("Order", {"userId": "u1", "amount": 30})
    await s.update("Inventory", {"_id": "sku1"}, {"$inc": {"stock": -1}})
    await s.insert("AuditLog", {"kind": "order", "ref": order["_id"]})
# exits → one commit; any exception → one rollback
```

**Result.** Every command on the same SQL source lands on one transaction connection; the session
commits once on exit and rolls back as one unit on any exception. A session with no commands never
checks out a connection (lazy start).

**Why it wins.** Sessions nest with savepoints (`SAVEPOINT sp_<n>`), so an inner failure rolls back
only the inner scope while the outer continues. The dangerous case is handled fail-closed: a session
writing to ≥2 datasources rolls everything back and raises `NonAtomicWriteError` — there is no
distributed transaction to silently half-commit. Executors without the primitives emit
`session_not_atomic` instead of pretending.

### 13. DDL generation: schema in, CREATE TABLE text out

**Scenario.** Your models are registered in code and the DBA (or Alembic) now needs the physical
tables. You want a migration starting point without a second source of truth.

```python
sql = store.generate_ddl("postgres")                           # every registered model
sql = store.generate_ddl("sqlite", ["Order", "OrderDeleted"])  # just these two
```

**Result.** Plain `CREATE TABLE` text — never executed, never written to the database — generated
from the registered defs:

```sql
CREATE TABLE "bench_orders" (
  "_id" TEXT PRIMARY KEY,
  "user_id" TEXT,
  "amount" REAL,
  "qty" INTEGER,
  "status" TEXT,
  "__present" TEXT
);
```

**Why it wins.** `generate_ddl(backend, names)` is the inverse of `sync_schema()` (which only
*reads*). Freeform field names are translated per backend (`userId` → `user_id` on SQL, `userId` on
Mongo), every table gets the `__present` sentinel column, and the `<Model>Deleted` archive table is
generated exactly once alongside its model. No `CREATE INDEX` is emitted (SQL backends keep indexes
as metadata), and edge cases fail loudly (`ddlPresentOverflow`) rather than silently.

### 14. Triggers: a declarative audit trail inside the same transaction

**Scenario.** Every insert, balance-changing update and delete on `Order` must leave an audit row —
and if the audit write fails, the business write must not land either.

```python
DEFS = [{
    "name": "Order", "collection": "orders", "idPrefix": "OD",
    "fields": {"_id": {"type": "string"}, "amount": {"type": "float"},
               "status": {"type": "string"}},
    "triggers": {
        "insert": [{"name": "aud_ins", "into": "AuditLog", "op": "insert",
                    "data": {"_id": "{{now}}", "kind": "ins",
                             "ref": "{{root._id}}", "amount": "{{root.amount}}"}}],
        "update": [{"name": "aud_upd", "onFields": ["amount"], "into": "AuditLog",
                    "op": "insert",
                    "data": {"_id": "{{now}}", "kind": "upd",
                             "ref": "{{root._id}}", "amount": "{{root.amount}}"}}],
        "remove": [{"name": "aud_del", "fnRef": "auditRemove",
                    "args": {"id": "{{before._id}}"}}],
    },
}, {
    "name": "AuditLog", "collection": "audit_logs", "idPrefix": "AL",
    "fields": {"_id": {"type": "string"}, "kind": {"type": "string"},
               "ref": {"type": "string"}, "amount": {"type": "float"}},
}]

for d in DEFS:
    store.register(d)

async def audit_remove(args, ctx, host):
    await host["store"].insert("AuditLog", {"kind": "del", "ref": args["id"]})

store.set_trigger_fn("auditRemove", audit_remove)
store.assert_trigger_fns_covered(DEFS)   # startup: a declared fnRef without an impl ⇒ ERR_TRIGGER_FN_MISSING
```

**Result.** A write event is expanded by the Rust core at plan time into an ordered trigger chain
that the host runs inside the same atomic envelope as the source write: `insert` and
`update(amount)` leave command-written audit rows; removing an order runs the `auditRemove` callback
with the pre-delete document (`{{before._id}}`).

**Why it wins.** The chain is data, declared on the schema. `update` events fire only when the
watched fields actually changed (structural deep compare — an unchanged value never fires);
`{{root.*}}` / `{{before.*}}` / `{{now}}` are whole-value substitutions (embedding one inside a
string raises `ERR_TRIGGER_PLACEHOLDER`); duplicates `(step.name, _id)` run once; callbacks receive
`(args, ctx, {"store": store})` and any `store.*` inside them hits the same transaction connection.
A failing callback or command step rolls the whole write back — and there is no cascading: a trigger
write never fires further triggers.

### 15. Soft delete archive: remove() that keeps the audit trail

**Scenario.** Compliance wants deletes to keep an audit trail: the row disappears from every list,
but the archive stays queryable — including *when* it was deleted.

```python
r = await store.remove("Order", {"_id": "o1"})
# r == {"deletedCount": 1, "archivedCount": 1}

archived = await store.query("OrderDeleted{ _id, amount, deletedAt }")
```

**Result.** The document is copied into the auto-derived `OrderDeleted` archive (with `deletedAt`),
then deleted from `orders`. The archive is a normal registered def — plain GQL works on it, and it
shows up in `generate_ddl()` exactly once.

**Why it wins.** Every registered model gets its `<Model>Deleted` archive collection/table for free,
derived by the Rust core at registration. Archive writes are upsert-by-`_id`, so re-creating the
same `_id` never collides and a retry after partial failure idempotently re-archives. An
empty-condition `remove` is rejected outright — a full-table delete cannot be triggered by
forgetting a filter.

**Opting out.** A schema that must not keep an archive passes `"archive": False` at registration —
the `<Model>Deleted` archive def is then never derived (DDL / migrations skip it too), and
`remove()` deletes without archiving (`archivedCount == 0`, return shape unchanged). For one-shot
control pass `remove(cond, None, {"archive": False})` to skip the archive for that call only;
later default calls archive as usual.

### 16. FastAPI: per-request permission context in one dependency

**Scenario.** An internal admin API: paginated list, create, soft-delete. Permissions must be
enforced at the data layer for every endpoint, not re-written per route.

```python
from fastapi import Depends, FastAPI, Request
from py_store import init, store

app = FastAPI()

@app.on_event("startup")
async def startup():
    await init({"default": mongo_db})
    store.register({
        "name": "Article", "collection": "articles", "idPrefix": "AR",
        "fields": {"_id": {"type": "string"}, "title": {"type": "string", "default": ""},
                   "body": {"type": "string", "default": ""},
                   "status": {"type": "string", "default": "draft"},
                   "authorId": {"type": "string", "default": ""}},
        "read": ["editor", "viewer", "creator"],
        "write": ["editor", "creator"],
    })

async def current_user(request: Request) -> dict:
    user = {"userId": request.headers["x-user-id"],
            "roles": request.headers.getlist("x-role")}
    store.set_context(user)          # one dependency, whole request covered
    return user

@app.get("/articles")
async def list_articles(page: int = 0, page_size: int = 50,
                        user: dict = Depends(current_user)):
    return await store.query_with_count(
        "Article($sort:@s1){ _id, title, status }",
        {"s1": {"createdAt": -1}, "page": page, "pageSize": page_size})
    # → {"items", "total", "hasMore", "page", "pageSize"}   (pageSize capped at 5000)

@app.delete("/articles/{article_id}")
async def delete_article(article_id: str, user: dict = Depends(current_user)):
    return await store.remove("Article", {"_id": article_id})   # archives first
```

**Result.** Role checks (schema/field whitelists, `creator` ownership) run on every store call with
no per-route code; `delete` leaves an auditable archive row.

**Why it wins.** The permission context lives in a `ContextVar`, so setting it in an `async`
dependency covers the whole request path (a sync dependency running in a thread pool would not).
A missing context is fail-open by default for backward compatibility; flip
`store.set_require_context(True)` at startup and forgotten contexts raise `ERR_NO_CONTEXT` instead
of passing. Empty-condition writes (`update_many` / `remove` with `{}`) are rejected outright, so a
missing filter can never become a full-table write. The full walkthrough lives in
[doc/use-cases/04-fastapi-admin-backend.md](doc/use-cases/04-fastapi-admin-backend.md).

## Performance

Numbers below come from two sources: a micro-benchmark run on the machine writing this README
(SQLite, no services needed — the query/write patterns are copy-runnable) and the repo's own
four-database stress test.

### Micro-benchmark (SQLite, measured locally)

Measured **2026-10-15**, Python 3.14.4, Windows amd64, file-backed SQLite via `aiosqlite`, py-store
on the Rust core (`rust-store-py`). Each figure is the median of 4 runs after discarding one warm-up
run. Seed data: 200 users; the order table grows to 30,000 rows across the write benchmarks.

| Operation | Data volume | Median | Throughput / comparison |
|---|---|---|---|
| `insert_many` (10 batches × 1,000) | 10,000 rows | 392 ms | **≈ 25,500 rows/s** |
| single `insert` in a loop | 1,000 rows | 3,439 ms | ≈ 290 rows/s → batching is ≈ 88× faster |
| GQL tree query — 1 call, relations nested | 100 users + 15,000 child rows | 517 ms | 1 call |
| hand-written N+1 — same data | 100 users + 15,000 child rows | 718 ms | 101 calls → **1.4× slower** |
| `$group` pushed down to SQL | 10,000 rows | 15 ms | 1 call |
| fetch-all + Python-side groupby | 10,000 rows | 600 ms | **41× slower** |
| computed column (read-time `fn`) | 2,000 rows | 29 ms | ≈ equal to a manual loop (29 ms) |

How to read this:

- **Aggregation is the big win.** `$group` compiles to a `GROUP BY` executed *inside* SQLite; the
  manual alternative pays to move all 10,000 rows over the driver and re-aggregate them in Python.
- **The tree query wins on call count, not just latency.** One round trip versus 101. Against a
  local SQLite file each extra call is cheap, so the wall-clock gap is 1.4×; over a network to
  MySQL/PostgreSQL/MongoDB each of those 101 calls costs an RTT and the gap widens accordingly.
- **Batching is the biggest single lever.** The same rows written as one `insert_many` per 1,000 are
  ~88× faster than 1,000 single inserts — each single insert pays planning plus its own commit.
- **Computed columns are honest, not magic.** The `fn` runs per row on the Python side, so it
  matches a hand-written loop while removing the bookkeeping you would otherwise maintain by hand
  (dependency injection, projection cleanup).

### Four-database stress test (recorded in this repo)

`stress/stress.py` (in this repo) drives MongoDB + MySQL + PostgreSQL + SQLite simultaneously —
8 workers × 200 rounds × 5 ops each (a 3-source federated query, a SQLite write, a PostgreSQL
count, a Mongo query, a MySQL `$inc` update). The recorded run in
[doc/2026-09-12-压测报告.md](doc/2026-09-12-压测报告.md) reports: **8,000 operations, 0 errors,
824 QPS**, federated-query p50 **19.3 ms** / p95 24.2 ms. (To be verified: not re-run on the machine
writing this README — it needs running MySQL/PG/Mongo services; treat these as the repo's recorded
figures, reproducible with the command in that report.)

### Where the time goes (design notes)

- **The Rust core does the planning.** GQL parsing, permission checks, computed-column planning,
  command planning and SQL dialect translation all run in Rust (`rust-store-py` via PyO3); the
  Python host crosses the FFI once per command and then does driver IO — one boundary crossing per
  command, not per row.
- **Pushdown over shipping data.** `JOIN` / `$lookup`, `GROUP BY` / `HAVING`, `EXISTS` /
  `NOT EXISTS` semi/anti-joins, and per-parent child sorting/paging are compiled into one statement
  per backend — the work happens where the data lives.
- **Read-time rehydration.** Flat JOIN rows are reassembled into nested documents once per result
  set; a missing `one` relation becomes `None`, an empty `many` becomes `[]` — no extra queries.
- **Known ceilings.** Python-side work is real: `fn` computed columns run per row, `asyncFn` per
  page, and cross-source Mongo federation joins in Python memory. The `local` disk source caps at
  100,000 documents per collection and keeps no indexes. None of these numbers are claims about
  your hardware — rerun the micro-benchmark (it needs nothing but `aiosqlite`) before treating them
  as a budget.

## FAQ

**How do I use one schema for both MongoDB and PostgreSQL in Python?**
Define the schema once as a dict, call `init()` with your datasource(s), and run the same GQL against either. MongoDB uses native aggregation; MySQL/PostgreSQL/SQLite get parameterized SQL. See [Quick start](#quick-start).

**How do I query nested / related data without writing `$lookup` or JOINs?**
Declare the relation in `relations` (`{"model", "type": "many" | "one", "localField", "foreignField"}`) and reference the relation name inside the GQL selection set. It becomes `$lookup` on Mongo and a `JOIN` on SQL, returned as nested documents.

**Does it support GROUP BY / COUNT / SUM / AVG?**
Yes — normalized aggregation is part of GQL: root-level `$group` / `$having` and relation aggregate predicates. See [Aggregation](#aggregation).

**Can I filter parents by an aggregate of their children ("products with more than 3 orders")?**
Yes — relation aggregate predicates implement semi/anti-join without fanning out; SQL uses `EXISTS`/`NOT EXISTS`.

**How do I implement row-level permissions?**
Use `store.set_context({"userId": ..., "roles": [...]})` plus schema-level `read`/`write` whitelists. The `creator` pseudo-role adds automatic ownership checks and owner-condition injection (ownership key defaults to `createdBy`; override per schema with `ownerKey`, e.g. `"ownerKey": "_id"` when documents are owned by their own `_id`). `guest` can never write. Turn on `set_require_context(True)` for fail-secure behaviour.

**How do I do soft delete?**
Every registered model automatically gets a `<Model>Deleted` archive collection/table. `store.remove()` archives the document first, then deletes it; re-creating the same `_id` does not collide because the archive write is upsert-by-`_id`.

**Is it usable for multi-tenant applications?**
Yes. Locate a schema by `(source, database, schema, collection)` and pass a `{"source", "database", "schema"}` route override per request. Treat `route_override` as trusted server-side input only.

**Does it run migrations?**
`sync_schema()` only *reads* physical structure via introspection (introspect → merge overlay → register). For applying schema changes there is a whitelist **orchestrator**: `store.migrate()` diffs two def versions, renders per-dialect SQL and applies it transactionally per table with a `__schemaMigration` version trail — whitelist-only (add table/column/index, type widening); anything destructive raises `MigrationUnsupported` with a two-step recipe instead (see "Schema migration (orchestrator)"). Beyond the whitelist, schema changes remain your migration tool's job (Alembic, etc.). If you want a starting point, `store.generate_ddl(backend)` renders `CREATE TABLE` text from the registered schemas — pure text generation: it never runs or writes DDL by itself.

**Can I see the generated query without running it?**
Yes — `store.build_pipeline(gql, params)` returns the compiled plan with no execution and no permission/compute application.

**What happens when SQL pushdown isn't possible?**
The command raises `PushdownUnsupportedError` **and** emits a structured feedback event (`sql_pushdown_unsupported`) through `set_feedback_sink`. Cross-source pagination/sort degradations emit `federation_degraded` events. Nothing fails silently.

**How is it related to nodejs-store and rust-store?**
`rust-store` is the shared Rust engine (GQL parsing, permissions, computed columns, command planning, SQL dialect translation — pure logic, no IO). `py-store` (pip `storepy`) and [`nodejs-store`](https://github.com/coenddt/nodejs-store) are thin hosts in front of it: they own driver IO, callbacks and placeholder substitution. Same schemas, same GQL, same semantics in Python and Node.

**Why is the pip package called `storepy` and the import `py_store`?**
The distribution name on PyPI is `storepy`; the importable package is `py_store`. Install with `pip install storepy`, then `from py_store import init, store`.

## Development

```bash
# run the full suite from the repo root (e2e cases auto-skip when MySQL/PG/Mongo are unreachable)
$env:PYTHONPATH='py-store/src'; python -m pytest py-store/tests/ -q

# against custom backends
$env:MYSQL_URI='mysql://user:pass@host:3306/db'; $env:PG_URI='postgres://user:pass@host:5432/db'; $env:MONGO_URI='mongodb://host:27017/db'
```

- Unit/contract suites (`test_py_store.py`, `test_host_contract.py`, `test_multi_datasource.py`) need no external services.
- The Rust core (`GQL parsing / planning / dialect`) lives in `../rust-store/core` and is consumed via the `rust-store-py` binding — pure logic never lives in this repo.
- `src/py_store/` is a thin Host layer: driver IO, callbacks, placeholder substitution. Keep it that way.

## Related projects

- [`nodejs-store`](https://github.com/coenddt/nodejs-store) — the Node.js twin (npm `nodejs-store`, camelCase API).
- [`rust-store`](https://github.com/coenddt/rust-store) — the shared Rust core and its `rust-store-node` / `rust-store-py` bindings.
- `text-to-query` — a skill that turns natural-language questions into GQL + params for this data layer.

## Framework usage contract

- Definition layer (data): models / permissions / workflows / interfaces are always pure JSON — publishable, rollbackable, hot-reloadable.
- Callback layer (code): datasource IO, computed-column implementations, external calls, transactions — declared via `fnRef` and injected at startup; not serializable, must never be persisted.
- Observability layer: every degradation / interception / fallback event lands in `__feedback` (queryable via GQL) — never silent.

## License

[MIT](LICENSE)
