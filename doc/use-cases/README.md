# py-store Use Cases

Scenario walkthroughs for py-store — each one maps a realistic business problem onto the documented py-store API, with runnable-looking code and the pitfalls that actually bite.

| Scenario | One-line description |
| --- | --- |
| [01 — Multi-tenant SaaS](01-multi-tenant-saas.md) | One schema serving N tenants: bind models to `(source, namespace, collection)` and re-target per request with a trusted route override. |
| [02 — AI data-QA agent](02-ai-data-qa-agent.md) | Compile and validate a GQL plan with `build_pipeline` before executing it, and capture degraded-path events with `set_feedback_sink`. |
| [03 — MongoDB → PostgreSQL migration](03-mongodb-to-postgres-migration.md) | The same schema and the same GQL against MongoDB then PostgreSQL — only the `init()` datasource changes. |
| [04 — FastAPI admin backend](04-fastapi-admin-backend.md) | Schema-driven CRUD with computed columns, soft-delete archives, role whitelists and paginated reads. |
| [05 — Async ETL / batch writes](05-async-etl-batch-writes.md) | Page a source and write batches into another database through one schema — idempotent upsert keys, per-source transactions, bounded memory. |
| [06 — Migrating from SQLAlchemy or Beanie](06-migrating-from-sqlalchemy-or-beanie.md) | Concept mapping from model classes + sessions to JSON schemas + GQL, with one schema and one dialect across MongoDB and SQL. |

Back to the main documentation: [../../README.md](../../README.md).
