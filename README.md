# py-store

**One data layer for MongoDB, MySQL, SQLite and PostgreSQL in Python asyncio — define models as pure JSON, query them with a MongoDB-style GQL tree syntax, and get role-based access control, computed columns and soft-delete out of the box.**

![PyPI version](https://img.shields.io/pypi/v/storepy)
![license](https://img.shields.io/pypi/l/storepy)
![python versions](https://img.shields.io/pypi/pyversions/storepy)
![backends](https://img.shields.io/badge/backends-MongoDB%20%7C%20MySQL%20%7C%20SQLite%20%7C%20PostgreSQL-blue)
![query dialect](https://img.shields.io/badge/query%20dialect-GQL%20(MongoDB--flavoured)-green)

`py-store` lets a Python service talk to MongoDB (native aggregation), MySQL, PostgreSQL and SQLite through a **single schema definition and a single query dialect**. Nested relations compile to **one native query per backend** — you never hand-write `$lookup` or raw SQL.

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
- [Transactions](#transaction-boundary)
- [FAQ](#faq)
- [Related projects](#related-projects)

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
- **Multi-tenant SaaS.** One schema definition, N tenants: bind a schema to `(source, namespace, collection)` and re-target any query or write at execution time with a `{"source", "namespace"}` override.

Typical concrete scenarios (see [`doc/use-cases/`](doc/use-cases/) for full walkthroughs):

| Scenario | Why py-store fits |
| --- | --- |
| Multi-tenant SaaS with per-tenant schema/database | `namespace` per tenant + runtime route override, one schema |
| FastAPI / admin backend | Schema-driven CRUD, soft-delete, computed columns, RBAC |
| MongoDB today, PostgreSQL tomorrow | Same GQL + same schema, only the datasource changes |
| AI data-QA / text-to-query agent | Plan-only `build_pipeline`, deterministic command JSON, feedback events |
| Mixed SQL + Mongo in one product | Cross-source queries with native SQL pushdown and Mongo in-memory federation |
| Audit-friendly CRUD | Every schema auto-gets a `<Model>Deleted` archive table/collection |

## When not to use it

Being explicit about the boundary saves you time:

- **You want a full ORM with a migration engine (Alembic, Django migrations).** `py-store` is a *data layer*, not a migration tool. It can **read** a SQL backend's physical structure (`sync_schema` → introspection) but it never writes DDL back.
- **You want a Pydantic-model-centric ORM.** Schemas here are runtime JSON dicts, giving you cross-language parity (the same schema runs in Python and Node.js) rather than Pydantic type validation.
- **You only ever use one database and rarely join.** A plain driver (or a single-database ODM/ORM) will be simpler.
- **You need raw aggregation escape hatches.** `$pipeline` passthrough and `store.aggregate()` were deliberately removed. Use `$condition` / `$group` / `$having` / relations; anything that cannot be safely translated fails **explicitly** rather than silently.

## How it compares

General positioning, not a benchmark — always verify against each tool's current docs.

| | py-store | SQLAlchemy | Beanie / Motor | Tortoise ORM | SQLModel | Django ORM |
| --- | --- | --- | --- | --- | --- | --- |
| Primary shape | JSON schema + GQL data layer | SQL toolkit + ORM | Async MongoDB ODM / driver | Async ORM | Pydantic + SQLAlchemy | ORM bundled with Django |
| Backends | MongoDB, MySQL, SQLite, PostgreSQL | PostgreSQL, MySQL, SQLite, Oracle, MSSQL | MongoDB | PostgreSQL, MySQL, SQLite, Oracle, MSSQL | PostgreSQL, MySQL, SQLite, … | PostgreSQL, MySQL, SQLite, Oracle |
| One query dialect across Mongo **and** SQL | ✅ (MongoDB-flavoured GQL) | ➖ (SQL only) | ➖ (Mongo only) | ➖ (SQL only) | ➖ (SQL only) | ➖ (SQL only) |
| Nested relation reads in one query | ✅ declarative relations → `$lookup` / `JOIN` | ⚠️ manual `selectinload`/joins | ✅ `Link`/`fetch_links` | ✅ `prefetch_related` | ⚠️ via SQLAlchemy | ✅ `prefetch_related` |
| Built-in role / field-level RBAC + owner injection | ✅ | ➖ | ➖ | ➖ | ➖ | ➖ (permissions are app-level) |
| Read-time computed columns (sync / async / relation-agg) | ✅ | ➖ (hybrid properties) | ➖ | ➖ | ➖ | ➖ |
| Soft-delete archive table auto-provisioned | ✅ | ➖ | ➖ | ➖ | ➖ | ➖ |
| Migration / DDL engine | ➖ (introspection read-only) | ✅ (Alembic) | ➖ | ✅ (Aerich) | ✅ (Alembic) | ✅ |
| Framework coupling | none (asyncio) | none | none | none | none | Django |
| Shared native core across Python & Node | ✅ (Rust `rust-store`) | ➖ | ➖ | ➖ | ➖ | ➖ |

### How it differs from specific libraries

Positioning only, based on those projects' public documentation at the time of writing — verify against your own requirements.

- **vs SQLAlchemy / SQLModel / Django ORM** — all SQL-only and model-class-centric: they do not target MongoDB, and none of them ships schema-declared role/field access control or read-time computed columns. `py-store` compiles one GQL to native MongoDB aggregation or to parameterized SQL.
- **vs Beanie / Motor** — MongoDB-only. `py-store` uses the same MongoDB-flavoured query style but the identical query also runs on MySQL, SQLite and PostgreSQL.
- **vs Tortoise ORM / pyloquent** — async Python ORMs over SQL backends, with model classes and (in Tortoise's case) a migration tool. `py-store` has no migration engine — introspection reads physical structure only — and describes models as plain dicts, which is exactly what makes a schema portable to the Node.js host.
- **vs `nodejs-store`** — the same engine and the same GQL, in JavaScript. Use whichever host matches your service; schemas and query semantics are interchangeable.

Short version: use an ORM when you want **model classes, Pydantic validation and migrations**; use `py-store` when you want **one runtime schema + one query dialect spanning MongoDB and SQL**, with RBAC and computed columns built in.

## Installation

```bash
pip install storepy
```

> The distribution name is `storepy`; the import package is `py_store`:
> `from py_store import init, store`.

Requires Python 3.10+ and one supported backend (MongoDB / MySQL / SQLite / PostgreSQL).

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

GQL tree queries compile to a single native query per backend — never hand-write `$lookup` or raw SQL again.

## Features

- **Pure JSON schemas, zero code** — a model is just a dict: fields, relations, computes, indexes.
- **GQL tree queries → one native query** — nested relations resolve in a single query; never hand-write `$lookup` again.
- **Normalized aggregation** — root-level `$group` / `$having` and relation aggregate predicates (semi/anti-join) in the same GQL, pushed down to all four backends.
- **Read-time defaults & computed columns** — writes store only user data; reads fill defaults and run `fn` / `asyncFn` / relation-`agg` computes.
- **Smart mutation** — `mutation()` auto-detects upsert by `_id` + unique index and recursively fills relation children.
- **Soft-delete built in** — every schema auto-registers a `<Model>Deleted` archive collection/table; `remove()` archives before deleting.
- **Permission context** — `ContextVar`-based roles (`super_admin`/`admin`/`guest`/`creator`...), schema/field-level read/write whitelists, automatic owner-condition injection.
- **Multi-datasource & multi-tenant** — locate a schema by `(source, namespace, collection)`; re-target per request with a route override.
- **Async-first, Rust core** — built on PyMongo's `AsyncMongoClient` and a shared Rust core with SQL dialects.

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

> **Breaking change**: user `$pipeline` passthrough and `store.aggregate()` were removed (raw aggregation escape hatch). A GQL containing `$pipeline` now fails explicitly instead of being silently ignored.

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
- Snake-case aliases available: `query_one`, `insert_many`, `update_many`, `build_pipeline`, ...

## Multi-datasource connections

Every schema is located by the triple `(source, namespace, collection)` — the triple must be
globally unique across the registry (duplicate registration raises instead of silently
mis-routing).

- `source` — connection key in `init({...})` (default `"default"`).
- `namespace` — database/schema inside the connection: Mongo db name, PG schema,
  MySQL database, SQLite attached db. Optional; `None` = connection default.
- `collection` — table/collection name.

```python
# Multiple Mongo servers: one source per connection
await init({"mongo_main": db, "pg_a": {"kind": "postgres", "exec": exec}})

# Same MongoClient serving multiple databases: declare namespace (db name)
await init({"cluster": client})
store.register({"name": "User", "collection": "users", "datasource": "cluster",
                "namespace": "tenant_42", ...})

# SQL cross-namespace joins are pushed down natively ("ns_a"."t" JOIN "ns_b"."t");
# only Mongo cross-db relations fall back to in-memory federation.
```

**Multi-tenant route override** — one schema definition, N tenants. Any query/write accepts
a `{ "source", "namespace" }` override that re-targets commands at execution time
(permissions and computed columns still follow the structural schema):

```python
await store.query('User($condition:@c0){...}', params, {"namespace": "tenant_42"})
await store.insert("Order", data, {"source": "pg_cluster", "namespace": "tenant_7"})
```

**`route_override` is a trusted server-side parameter** — it carries no origin check, so
forwarding user-controlled input into it lets a caller re-target another tenant's
`source`/`namespace` (CWE-639 authorization-bypass surface). Never pass raw request data here.

Legacy single-db usage (`init(db)` + schema without `datasource`/`namespace`) is unchanged:
commands carry `source: "default"`, `namespace: None`.

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
    },
    "relations": {
        "items": {"model": "OrderItem", "type": "many",
                  "localField": "_id", "foreignField": "orderId"},
    },
    "computes": {
        "total": {"type": "float", "depends": ["amount"], "fn": lambda d: d["amount"] * 1.1},
        "itemCount": {"type": "int", "agg": {"$count": "items"}},
    },
    "indexes": [
        {"keys": {"status": 1}},
        {"keys": {"code": 1}, "options": {"unique": True}},
    ],
    "read": ["editor", "viewer"],      # optional schema-level role whitelists
    "write": ["editor"],
}
```

Types: `string | int | long | float | double | boolean | array | object | date | any`.

Boundary rules worth knowing up front (all **fail explicitly**, never silently degrade):

- Filtering on array fields directly, on a whole object field, or on object dot-paths is rejected on every backend — model cross-entity semantics as `relations` instead.
- Relation predicates support **one level** of relation; paths like `orders.items.price` are rejected.
- An unreadable relation is an error, not a silent `False`.

## Transaction boundary

- **Single SQL source**: `mutation` parent-child step sequences and `remove` (archive + delete) run inside one driver transaction on one checked-out connection — any step failure rolls back the whole sequence.
- **Each SQL write command** is itself atomic: multi-statement plans (e.g. MySQL write + readback) are transaction-wrapped in the executor.
- **Mongo sources**: single-document writes are atomic; multi-step `mutation` and `remove` execute sequentially and are **not** atomic across steps (Mongo transactions require a replica set). If your consistency requirement spans steps on Mongo, either use an SQL source for those models or add application-level compensation.
- **Archive idempotency**: `remove` archives with upsert-by-`_id` semantics, so a retry after partial failure no longer fails on duplicate `_id`.
- **Cross-source steps** (parent and child bound to different datasources) cannot be atomic — they run sequentially by design.

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
Use `store.set_context({"userId": ..., "roles": [...]})` plus schema-level `read`/`write` whitelists. The `creator` pseudo-role adds automatic ownership checks and owner-condition injection. `guest` can never write. Turn on `set_require_context(True)` for fail-secure behaviour.

**How do I do soft delete?**
Every registered model automatically gets a `<Model>Deleted` archive collection/table. `store.remove()` archives the document first, then deletes it; re-creating the same `_id` does not collide because the archive write is upsert-by-`_id`.

**Is it usable for multi-tenant applications?**
Yes. Bind a schema to `(source, namespace, collection)` and pass a `{"source", "namespace"}` route override per request. Treat `route_override` as trusted server-side input only.

**Does it run migrations?**
No. `sync_schema()` only *reads* physical structure via introspection (introspect → merge overlay → register). Schema changes / DDL are your migration tool's job (Alembic, etc.).

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

## License

[MIT](LICENSE)
