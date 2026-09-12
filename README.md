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

## GQL syntax

```text
Model($condition:@c0,$sort:@s1,$skip:@sk,$limit:@l1) {
  field1, field2, obj.subField,
  Relation($condition:@c2,$sort:@s3,$limit:@l2) { f3, Nested { f4 } }
}
```

- Values come from the params dict: `{"c0": {...}, "s1": {...}}`.
- Object sub-fields use dot notation; relations are declared in the schema (`type: "many" | "one"`) and resolved automatically — **do not hand-write `$lookup`**.
- `$pipeline` passes a raw aggregation through as-is (no compute/defaults/permission trimming) — use with care; prefer `store.aggregate(model, pipeline)` for group/sum needs.

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
- `createdAt`/`updatedAt` (ms) are framework-maintained — do not set them manually.
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

## Schema reference

```python
{
    "name": "Order",
    "collection": "orders",
    "idPrefix": "OD",
    "timestamps": True,                # default: auto-maintain createdAt/updatedAt (ms)
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

## License

[MIT](LICENSE)
