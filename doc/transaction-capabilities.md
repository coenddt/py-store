# Transaction capabilities

Scope: what is and is not atomic in py-store, and how non-atomic paths surface. Degradation is
allowed; silent pretence of atomicity is not.

## Write boundary

- **Within one connection / datasource** — a write that touches several databases (Mongo) or
  several PostgreSQL schemas is synchronized in **one transaction on that connection**. On Mongo
  this requires a **replica set** (or sharded cluster); a standalone deployment cannot run
  multi-document transactions and is declared non-atomic.
- **Across connections** — py-store has **no cross-source transaction** (no 2PC / Saga). A write
  that spans a cross-connection link is **explicitly rejected or degraded with a feedback event**
  — it is **never silent**:
  - writing to ≥2 sources inside `store.session(...)` fail-closes: everything is rolled back
    first, then `NonAtomicWriteError` is raised;
  - writing to ≥2 sources without a session runs source by source and emits a `non_atomic_write`
    feedback event (`code: nonAtomic`, with the sources involved);
  - a Mongo source that cannot transact (standalone / probe failure) runs as-is and emits
    `mongo_transaction_unsupported` (`deployment: standalone|unknown`).
- **Fix** — converge the write onto a single source, or put it inside `store.session()` and keep
  every command on one source (one connection = one transaction).

## Same-named schemas and links

- A write is synchronized within one connection across the **primary schema plus all its
  `replica: true` links**, in one transaction.
- A write that reaches a **cross-connection link** falls back to the rule above: explicitly
  rejected or degraded with a feedback event.

## Read consistency

- Only multiple reads inside an explicit session share one transaction connection; reads outside
  a session do not open an extra transaction.
