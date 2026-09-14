# MongoDB → PostgreSQL migration

## The problem

Your service started on MongoDB and now needs to run on PostgreSQL — in production, per customer, or on both at once during a transition. Rewriting every data-access call site into SQL is expensive, and keeping two query layers in sync is worse.

## Why py-store

- The schema is pure JSON and backend-agnostic, so one definition serves MongoDB and PostgreSQL.
- The same GQL string runs unchanged against either backend — only the `init()` datasource changes. MongoDB uses native aggregation; PostgreSQL gets parameterized SQL.
- `sync_schema(backend, driver, overlay=[...])` pulls a SQL backend's physical structure into the registry (introspect → merge overlay → register) when you need to align against an existing database.
- Nested relations compile to one native query per backend (`$lookup` on Mongo, `JOIN` on SQL) without hand-writing either.

## Walkthrough

```python
from pymongo import AsyncMongoClient
from py_store import init, store

# The schema is backend-agnostic: register it once, then point init() at any backend.
store.register({
    "name": "OrderItem",
    "collection": "order_items",
    "fields": {
        "_id": "string",
        "orderId": {"type": "string", "default": ""},
        "sku": {"type": "string", "default": ""},
        "qty": {"type": "int", "default": 1},
    },
})

order_schema = {
    "name": "Order",
    "collection": "orders",
    "idPrefix": "OD",
    "fields": {
        "_id": "string",
        "customerId": {"type": "string", "default": ""},
        "amount": {"type": "float", "default": 0},
        "status": {"type": "string", "default": "open"},
    },
    "relations": {
        "items": {"model": "OrderItem", "type": "many",
                  "localField": "_id", "foreignField": "orderId"},
    },
    "computes": {
        "itemCount": {"type": "int", "agg": {"$count": "items"}},
    },
    "indexes": [{"keys": {"status": 1, "createdAt": -1}}],
}
store.register(order_schema)

GQL = "Order($condition:@c0,$sort:@s1,$limit:@l) { _id, customerId, amount, itemCount }"
PARAMS = {"c0": {"status": "open"}, "s1": {"createdAt": -1}, "l": 20}

# --- Phase 1: MongoDB (native aggregation + $lookup) ---
mongo = AsyncMongoClient("mongodb://localhost:27017")
await init(mongo["mydb"])
mongo_rows = await store.query(GQL, PARAMS)

# --- Phase 2: PostgreSQL — same schema, same GQL, only the datasource changes ---
await init({"default": {"kind": "postgres", "exec": exec}})
pg_rows = await store.query(GQL, PARAMS)          # parameterized SQL + JOIN

# Optionally align against an existing physical database first.
# sync_schema only READS structure: introspect -> merge overlay -> register.
# Signature: sync_schema(backend, driver, introspect_options=None, overlay=None,
#                        datasource=None, namespace=None, register_defs=True)
defs = await store.sync_schema(
    "postgres",
    driver,                  # prefer a read-only account
    overlay=[order_schema],  # local schemaJSON merged on top (permissions / computes)
)
```

## Pitfalls

- **`sync_schema` is read-only.** It introspects a SQL backend's physical structure and never writes DDL back — there is no migration engine. Schema changes and DDL are your migration tool's job (Alembic, etc.).
- **`indexes` are metadata for SQL backends.** `init()` creates indexes for Mongo sources; SQL backends are not given indexes — the schema keeps them as metadata only.
- **Mongo multi-step writes are not atomic across steps.** Single-document writes are atomic, but multi-step `mutation` and `remove` run sequentially (Mongo transactions require a replica set). Cross-source steps also run sequentially by design.
- **A single SQL source is transaction-wrapped.** Within one SQL source, `mutation` parent-child step sequences and `remove` (archive + delete) run inside one driver transaction; any step failure rolls the whole sequence back.
- **Not every query is pushdownable everywhere.** A rejected SQL pushdown raises `PushdownUnsupportedError` **and** emits `sql_pushdown_unsupported`; Mongo cross-db relations degrade to in-memory federation with a `federation_degraded` event.

## See also

- [README — Supported backends](../../README.md#supported-backends)
- [README — Transaction boundary](../../README.md#transaction-boundary)
- [01 — Multi-tenant SaaS](01-multi-tenant-saas.md)
