---
title: "py-store documentation"
description: "Documentation for py-store — one JSON schema and one MongoDB-style GQL query dialect for MongoDB, MySQL, SQLite and PostgreSQL in Python asyncio."
---

# py-store

**One data layer for MongoDB, MySQL, SQLite and PostgreSQL in Python asyncio — define models as pure JSON, query them with a MongoDB-style GQL tree syntax, and get role-based access control, computed columns and soft-delete out of the box.**

`py-store` lets a Python service talk to MongoDB (native aggregation), MySQL, PostgreSQL and SQLite through a **single schema definition and a single query dialect**. Nested relations compile to **one native query per backend** — you never hand-write `$lookup` or raw SQL.

```bash
pip install storepy
```

The distribution name is `storepy`; the import package is `py_store`.

## Scenario walkthroughs

Six end-to-end walkthroughs, each with runnable code, the mistakes people make, and the exact limits of the engine:

| Scenario | What it covers |
| --- | --- |
| [01 — Multi-tenant SaaS](use-cases/01-multi-tenant-saas.html) | One schema serving N tenants: bind models to `(source, namespace, collection)` and re-target per request with a trusted route override. |
| [02 — AI data-QA agent](use-cases/02-ai-data-qa-agent.html) | Compile and validate a GQL plan with `build_pipeline` before executing it; capture degraded paths with `set_feedback_sink`. |
| [03 — MongoDB → PostgreSQL migration](use-cases/03-mongodb-to-postgres-migration.html) | The same schema and the same GQL against two backends — only the `init()` datasource changes. |
| [04 — FastAPI admin backend](use-cases/04-fastapi-admin-backend.html) | Schema-driven CRUD with computed columns, soft-delete archives, role whitelists and paginated reads. |
| [05 — Async ETL / batch writes](use-cases/05-async-etl-batch-writes.html) | Page a source and write batches into another database through one schema — idempotent upsert keys, per-source transactions, bounded memory. |
| [06 — Migrating from SQLAlchemy or Beanie](use-cases/06-migrating-from-sqlalchemy-or-beanie.html) | Concept mapping from model classes plus sessions to JSON schemas plus GQL, with one schema and one dialect across MongoDB and SQL. |

## Documentation

- [Full README](readme.html) — architecture, API reference, GQL capabilities, permission model, gotchas.
- [Use-cases index](use-cases/) — the walkthrough list above with summaries.

## Related projects

- [`nodejs-store`](https://github.com/coenddt/nodejs-store) — the Node.js host of the same engine (npm `nodejs-store`).
- [`rust-store`](https://github.com/coenddt/rust-store) — the shared Rust core: GQL parsing, permissions, computed columns, command planning and SQL dialect translation.

## Links

- PyPI: [storepy](https://pypi.org/project/storepy/)
- Source: [github.com/coenddt/py-store](https://github.com/coenddt/py-store)
- Machine-readable summary: [llms.txt](llms.txt)
