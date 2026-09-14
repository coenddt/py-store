# Multi-tenant SaaS with per-tenant namespaces

## The problem

You run a SaaS product where every customer gets an isolated dataset. The schema is identical for all tenants — the same `Order`, `Customer` and `Invoice` models — but the data must live in a separate database (or schema) per tenant, so that one query can never leak across tenants and a single tenant can be moved, backed up or restored independently.

Duplicating the schema per tenant is unmaintainable, and opening a separate Mongo connection per tenant forces you to thread a connection object through every call site.

## Why py-store

- A schema is located by the triple `(source, namespace, collection)`. Bind each model once with `datasource` + `namespace`, and the same definition serves every tenant.
- Any query or write accepts a `{"source", "namespace"}` `route_override` that re-targets the command at execution time — you do not need a new store instance per tenant.
- Permissions and computed columns still follow the structural schema, so a tenant's identity only changes *where* the data lives, not *how* it is validated.
- SQL sources push cross-namespace joins down natively; Mongo cross-db relations fall back to in-memory federation, which the library handles for you.

## Walkthrough

```python
from pymongo import AsyncMongoClient
from py_store import init, store

# One MongoClient serves every tenant database; init maps a connection key -> connection.
client = AsyncMongoClient("mongodb://localhost:27017")
await init({"cluster": client})

# One schema, bound to a namespace. `datasource` picks the connection key,
# `namespace` picks the database inside it.
store.register({
    "name": "Order",
    "collection": "orders",
    "idPrefix": "OD",
    "datasource": "cluster",
    "namespace": "tenant_42",
    "fields": {
        "_id": "string",
        "customerId": {"type": "string", "default": ""},
        "amount": {"type": "float", "default": 0},
        "status": {"type": "string", "default": "open"},
    },
    "computes": {
        "amountLabel": {"type": "string", "depends": ["amount"],
                        "fn": lambda d: f"{d['amount']:.2f}"},
    },
    "indexes": [{"keys": {"customerId": 1, "createdAt": -1}}],
})

# The route override re-targets a query at another tenant without
# re-registering the schema or opening a new client.
# Resolve `tenant` from the authenticated session — never from raw request data.
tenant = "tenant_7"

orders = await store.query(
    "Order($condition:@c0,$sort:@s1,$limit:@l) { _id, customerId, amount, amountLabel }",
    {"c0": {"status": "open"}, "s1": {"createdAt": -1}, "l": 50},
    {"source": "cluster", "namespace": tenant},
)

# Writes take the same override.
await store.insert(
    "Order",
    {"customerId": cust_id, "amount": 199.0},
    {"source": "cluster", "namespace": tenant},
)
```

## Pitfalls

- **`route_override` is a trusted server-side parameter.** It carries no origin check, so forwarding user-controlled input into it lets a caller re-target another tenant's `source` / `namespace` — a CWE-639 authorization-bypass surface. Never pass raw request data here; resolve the namespace from your authenticated session first.
- **The `(source, namespace, collection)` triple must be globally unique.** Registering two schemas with the same triple raises instead of silently mis-routing.
- **`namespace` means different things per backend** — Mongo db name, PG schema, MySQL database, SQLite attached db. Pick the granularity your tenant-isolation model actually needs.
- **Cross-database relations are not free on Mongo.** SQL cross-namespace joins push down natively, but Mongo cross-db relations fall back to in-memory federation (a `federation_degraded` event may be emitted for degraded pagination/sort).

## See also

- [README — Multi-datasource connections](../../README.md#multi-datasource-connections)
- [README — Permission context](../../README.md#permission-context)
- [03 — MongoDB → PostgreSQL migration](03-mongodb-to-postgres-migration.md)
