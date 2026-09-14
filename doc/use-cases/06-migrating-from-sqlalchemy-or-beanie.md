# Migrating from SQLAlchemy (or Beanie)

## The problem

Your team already has SQLAlchemy / SQLModel models, and possibly Beanie over Motor for MongoDB, and now the same service has to run against MongoDB *and* a relational database — per tenant, per environment, or during a migration. Keeping a declarative model class and a Beanie `Document` in sync means two schemas, two query builders and two sets of behaviours to keep aligned.

## Why py-store

- One model definition, written as a plain dict rather than a class, drives MongoDB, PostgreSQL, MySQL and SQLite.
- One query dialect: MongoDB-flavoured GQL compiles to native aggregation on Mongo and to parameterized SQL on the relational backends, so the same call site does not branch on backend.
- Nested relations are declared once in `relations` and requested by name inside the selection set — there is no per-query `selectinload` / `fetch_links` decision to make.
- Role whitelists, computed columns and soft-delete archives are declared in the same schema instead of being re-implemented per backend.

## Concept mapping

| SQLAlchemy / SQLModel | Beanie / Motor | py-store |
| --- | --- | --- |
| `create_engine()` / async engine | `AsyncIOMotorClient` | `init(connections)` — a `{name: connection}` map |
| `sessionmaker()` / `Session` | `AsyncIOMotorDatabase` | no session object; the schema is located by `(source, namespace, collection)` on each call |
| declarative model / `SQLModel` class | `Document` subclass | `store.register({...})` — a runtime JSON dict |
| `select(Order).where(...)` | `Order.find(...)` | `store.query(gql, params)` — GQL tree |
| `session.get(Order, id)` / `.one_or_none()` | `Document.get(id)` | `store.query_one(gql, params)` |
| `relationship()` | `Link` | a `relations` entry (`model`, `type`, `localField`, `foreignField`) |
| `.options(selectinload(Order.items))` | `fetch_links=True` | reference the relation name inside the GQL selection set |
| `session.add(obj)` + `commit()` | `doc.insert()` | `store.insert` / `store.mutation` / `store.upsert` |
| `bulk_insert_mappings()` | `insert_many()` | `store.insert_many(schema, docs)` |
| `func.count()` + `group_by()` | aggregation pipeline | GQL `$group` / `$having`; relation aggregate predicates |
| Alembic migration | (no equivalent) | read-only `sync_schema` introspection |
| `session.begin()` | `client.start_session()` | per-source driver transaction (single SQL source only) |

## Walkthrough

Before — SQLAlchemy 2.0, one class per table, one session per unit of work:

```python
class OrderItem(Base):
    __tablename__ = "order_items"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"))
    sku: Mapped[str]
    qty: Mapped[int]

class Order(Base):
    __tablename__ = "orders"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    status: Mapped[str]
    amount: Mapped[float]
    items: Mapped[list[OrderItem]] = relationship()

async with async_session() as session:
    orders = (await session.execute(
        select(Order).where(Order.status == "open").options(selectinload(Order.items))
    )).scalars().all()
```

After — py-store, one schema definition and one query, on either backend:

```python
from pymongo import AsyncMongoClient
from py_store import init, store
from py_store import executors

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
store.register({
    "name": "Order",
    "collection": "orders",
    "idPrefix": "OD",
    "fields": {
        "_id": "string",
        "status": {"type": "string", "default": "draft"},
        "amount": {"type": "float", "default": 0},
    },
    "relations": {
        "items": {"model": "OrderItem", "type": "many",
                  "localField": "_id", "foreignField": "orderId"},
    },
})

GQL = "Order($condition:@c0){ _id, status, amount, items { sku, qty } }"
PARAMS = {"c0": {"status": "open"}}

# MongoDB: native aggregation + $lookup
await init(AsyncMongoClient("mongodb://localhost:27017")["mydb"])
orders = await store.query(GQL, PARAMS)

# PostgreSQL: same schema, same GQL — parameterized SQL + JOIN
await init({"default": executors.create_connection("postgres", pg_pool)})
orders = await store.query(GQL, PARAMS)
```

When you point py-store at an existing physical database, `sync_schema(backend, driver, introspect_options=None, overlay=None, datasource=None, namespace=None, register_defs=True)` reads its structure into the registry (introspect → merge overlay → register). The `overlay` argument is where you pass your local schema dicts so that permissions and computed columns — which introspection cannot see — survive the merge.

## What you lose

Being explicit about the gap saves a rewrite later:

- **No migrations and no DDL engine.** `sync_schema` only *reads* physical structure; it never writes DDL back. Alembic (or Aerich) stays in your stack for schema changes, and on SQL targets your tool is also what creates the physical indexes and unique constraints — py-store's `indexes` are metadata on SQL backends.
- **No static typing or model validation.** Schemas are runtime dicts: no `Mapped[str]`, no editor/mypy completion, no constructor validation, and defaults are filled in at *read* time rather than on a Python object. Beanie/SQLModel users lose the Pydantic layer specifically.
- **A different transaction story.** There is no session-scoped unit of work and no savepoints. A driver transaction is opened for you only around a `mutation` step sequence and `remove` (archive + delete) on a *single SQL source*; Mongo multi-step writes run sequentially and are not atomic across steps, and cross-source steps cannot be atomic at all. If your current code depends on `async with session.begin()` spanning arbitrary statements, that pattern does not translate.
- **No identity map, change tracking or lazy loading.** You call `insert` / `update` / `mutation` explicitly, and reads return plain dicts — there is no `obj.items` that loads on attribute access, and relations come back only if you asked for them in the selection set.
- **No query-builder chaining and no raw escape hatch.** GQL is a string plus a params dict; the user `$pipeline` passthrough and `store.aggregate()` were removed. A combination that cannot be safely translated fails explicitly (`PushdownUnsupportedError`, plus a `sql_pushdown_unsupported` feedback event) rather than falling back to raw SQL.
- **No locking API.** There is no `SELECT … FOR UPDATE` / pessimistic-lock wrapper in the data layer; model that at the application level if you need it.

## See also

- [README — When not to use it](../../README.md#when-not-to-use-it)
- [README — How it compares](../../README.md#how-it-compares)
- [README — Transaction boundary](../../README.md#transaction-boundary)
- [03 — MongoDB → PostgreSQL migration](03-mongodb-to-postgres-migration.md)
