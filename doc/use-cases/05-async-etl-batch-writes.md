# Async ETL and batch writes

## The problem

You have a recurring ingest job: rows land in one database (a staging table, an operational replica, a landing collection) and must be normalised and written into a second database that your read API and reports query. The source may be far larger than memory, the two sides may not even be the same engine, and the job will be killed and restarted — so a re-run must not duplicate rows or leave parent documents without their children.

## Why py-store

- **One schema definition, two endpoints.** A schema is located by the triple `(source, namespace, collection)`, and any read or write accepts a `{"source", "namespace"}` `route_override` that re-targets the command at execution time. The same `Order` definition reads from staging and writes into the target.
- **Batched writes.** `insert_many(schema, docs)` sends one batch; `mutation(schema, [docs])` writes an array; `upsert(schema, condition, data)` is an explicit-condition upsert; `update_many(schema, condition, data)` does set-based updates.
- **Idempotency by key.** `mutation` derives its upsert condition from `_id` or from a `unique` index whose keys are all present in the incoming document, so declaring a unique index on the natural key makes re-runs update instead of duplicate.
- **Bounded memory.** Page the source with `query_with_count(gql, {"page": …, "pageSize": …})` (or `$skip` / `$limit`) and write one page at a time; `pageSize` is capped at 5000.
- **Empty conditions are rejected.** `update_many` / `remove` with `{}`, `None` or `{"$and": []}` is refused outright — an ETL bug cannot turn into a full-table write.
- **`run_as_internal(...)`** marks a cron/worker call as internal so it bypasses request-level permission checks.

## Walkthrough

```python
from py_store import init, store
from py_store import executors

# Read side ("oltp") and write side ("dwh"); each may be SQL or Mongo.
await init({
    "oltp": executors.create_connection("postgres", oltp_pool),
    "dwh": executors.create_connection("postgres", dwh_pool),
})

# One definition of the written shape. The unique index on the natural key is
# what makes `mutation` upsert instead of duplicate on a re-run.
store.register({
    "name": "Order",
    "collection": "orders",
    "idPrefix": "OD",
    "datasource": "dwh",
    "namespace": "analytics",
    "fields": {
        "_id": "string",
        "externalId": {"type": "string", "default": ""},
        "customerId": {"type": "string", "default": ""},
        "amount": {"type": "float", "default": 0},
        "status": {"type": "string", "default": "open"},
    },
    "indexes": [{"keys": {"externalId": 1}, "options": {"unique": True}}],
})

GQL = "Order($condition:@c0,$sort:@s0){ _id, externalId, customerId, amount, status }"
PAGE, CHUNK = 1000, 250

async def run_etl():
    page = 0
    while True:
        # Read one page from staging: the route override re-targets the read
        # without touching the schema's registered binding.
        res = await store.query_with_count(
            GQL,
            {"c0": {"status": "open"}, "s0": {"_id": 1}, "page": page, "pageSize": PAGE},
            {"source": "oltp", "namespace": "public"},
        )
        rows = res["items"]
        if not rows:
            return

        for start in range(0, len(rows), CHUNK):
            chunk = rows[start:start + CHUNK]
            payload = [{"externalId": r["externalId"], "customerId": r["customerId"],
                        "amount": r["amount"], "status": r["status"]} for r in chunk]
            # Batch write; the job runs as an internal task (no request context).
            await store.run_as_internal(lambda p=payload: store.mutation("Order", p))

        if not res["hasMore"]:
            return
        page += 1
```

The read and the write target different sources, so they can never be one transaction (see *Transaction boundary*). The job is therefore built to be **re-runnable**: `mutation` upserts on the `externalId` unique index, so a chunk that fails part-way — the exception propagates out of `run_etl` — is simply reprocessed on the next run, and rows already written are updated rather than duplicated. If you want to resume from the middle instead of re-scanning from page 0, track the highest `_id` you completed yourself; there is no library-side checkpoint.

## Pitfalls

- **Writes are per-source only.** Within a single SQL source, a `mutation` step sequence and `remove` (archive + delete) run in one driver transaction on one connection. Mongo multi-step writes execute sequentially and are **not** atomic across steps (Mongo transactions require a replica set), and steps that span two sources cannot be atomic at all. A read-from-A / write-to-B pipeline is cross-source by definition — rely on idempotent keys, not on a distributed transaction.
- **Array-form `mutation` is item-by-item.** `mutation(schema, [d1, d2, …])` plans and executes each element separately; on a single SQL source each element's step sequence gets *its own* transaction, so the array is not one atomic unit. `insert_many` is the true single batched command — but it only inserts, it never upserts.
- **Always carry the full natural key.** `mutation` produces an upsert condition only from `_id`, or from a `unique` index whose keys are *all* present in the incoming document. If a key is missing, no upsert condition is built and the write degrades to a plain insert (which then fails on the physical constraint).
- **SQL `indexes` are metadata only.** `init()` creates indexes for Mongo sources; SQL backends are never given indexes. For an SQL target, the physical unique constraint (which `ON CONFLICT` / `ON DUPLICATE KEY UPDATE` relies on) must come from your own DDL tool.
- **`_id` and timestamps are framework-owned.** `createdAt` / `updatedAt` are maintained by the library, and `_id` cannot be changed by `update`. If you re-key rows during ingest, put the source id in your own field (`externalId` above), not in `_id`.
- **There is no cursor API; a page is materialised.** `query` / `query_with_count` return a Python list for the page you asked for. Keep `pageSize` (≤ 5000) and the chunk size modest so a single page plus its payload stays bounded.

## See also

- [README — Query & write API](../../README.md#query--write-api)
- [README — Transaction boundary](../../README.md#transaction-boundary)
- [01 — Multi-tenant SaaS](01-multi-tenant-saas.md)
- [03 — MongoDB → PostgreSQL migration](03-mongodb-to-postgres-migration.md)
