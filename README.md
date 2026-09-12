# py-store

A lightweight multi-backend data layer for Python asyncio apps — define your models as pure JSON schemas, query with GQL tree syntax, and get role-based access control out of the box. One unified MongoDB-style dialect runs on **MongoDB, MySQL, SQLite and PostgreSQL**.

## Supported backends

| Backend | Notes |
| --- | --- |
| MongoDB | native aggregation pipeline (`find`/`aggregate`/`$lookup`) |
| MySQL | parameterized SQL, `information_schema` introspection |
| SQLite | parameterized SQL, `sqlite_master` + `PRAGMA` introspection |
| PostgreSQL | parameterized SQL (`$n`), `RETURNING` support |

GQL tree queries compile to a single native query per backend — never hand-write `$lookup` or raw SQL again.

## Features

- **Pure JSON schemas, zero code** — a model is just a dict: fields, relations, computes, indexes.
- **Read-time defaults & computed columns** — writes store only user data; reads fill defaults and run `fn`/`asyncFn` computes.
- **GQL tree queries → one native query** — nested relations resolve in a single query; never hand-write `$lookup` again.
- **Smart mutation** — `mutation()` auto-detects upsert by `_id` + unique index and recursively fills relation children.
- **Soft-delete built in** — every schema auto-registers a `<Model>Deleted` archive collection/table; `remove()` archives before deleting.
- **Permission context** — ContextVar-based roles (`super_admin`/`admin`/`guest`/`creator`...), schema/field-level read/write whitelists, automatic owner-condition injection.
- **Async-first** — built on PyMongo's `AsyncMongoClient` (pymongo >= 4.9).

## Installation

```bash
pip install storepy
```

> The distribution name is `storepy`; the import package is `py_store`:
> `from py_store import init, store`.

Requires Python 3.10+ and one supported backend (MongoDB / MySQL / SQLite / PostgreSQL).

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

## GQL syntax

```text
Model($condition:@c0,$sort:@s1,$skip:@sk,$limit:@l1) {
  field1, field2, obj.subField,
  Relation($condition:@c2,$sort:@s3,$limit:@l2) { f3, Nested { f4 } }
}
```

- Values come from the params dict: `{"c0": {...}, "s1": {...}}`.
- Object sub-fields use dot notation; relations are declared in the schema (`type: "many" | "one"`) and resolved automatically — **do not hand-write `$lookup`**.
- `$pipeline` passes a raw aggregation through as-is (no compute/defaults/permission trimming) — use with care; prefer `store.aggregate(model, pipeline)` for group/sum needs. AI/agent hosts can hard-disable it via `store.set_allow_user_pipeline(False)` (registry-level guard; all plan paths then reject `$pipeline` explicitly).

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
rows  = await store.aggregate("Post", pipeline)      # native aggregation
```

Notes:

- `None` values are stripped before persisting; `_id` cannot be changed via `update`.
- `createdAt`/`updatedAt` are framework-maintained — do not set them manually. Unit follows the schema's `timestamps` setting: milliseconds by default, or seconds when `timestamps: "s"`.
- Snake-case aliases available: `query_one`, `insert_many`, `update_many`, `parse_gql`, `build_pipeline`, ...

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
        "itemCount": {"type": "int", "lookup": {"$size": {"$ifNull": ["$items", []]}}},
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

## Transaction boundary

- **Single SQL source**: `mutation` parent-child step sequences and `remove` (archive + delete) run inside one driver transaction on one checked-out connection — any step failure rolls back the whole sequence.
- **Each SQL write command** is itself atomic: multi-statement plans (e.g. MySQL write + readback) are transaction-wrapped in the executor.
- **Mongo sources**: single-document writes are atomic; multi-step `mutation` and `remove` execute sequentially and are **not** atomic across steps (Mongo transactions require a replica set). If your consistency requirement spans steps on Mongo, either use an SQL source for those models or add application-level compensation.
- **Archive idempotency**: `remove` archives with upsert-by-`_id` semantics, so a retry after partial failure no longer fails on duplicate `_id`.
- **Cross-source steps** (parent and child bound to different datasources) cannot be atomic — they run sequentially by design.

## License

[MIT](LICENSE)
