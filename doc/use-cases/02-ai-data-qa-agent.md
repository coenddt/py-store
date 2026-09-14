# AI data-QA agent

## The problem

You are building an agent that answers natural-language questions over your product data ("how are courses distributed by status?"). A natural-language → GQL layer produces the query string and the params dict, but you do not want to blindly execute whatever the model produced: an unsupported or malformed query should be caught *before* it hits the database, and degraded paths should be observable instead of silently returning partial results.

## Why py-store

- `store.build_pipeline(gql, params)` compiles GQL to the command plan **without executing it**, so you can validate and inspect the planned query shape first and then hand the exact same GQL to `store.query`.
- Degraded / non-pushdownable paths emit structured feedback events (`federation_degraded`, `sql_pushdown_unsupported`) instead of failing silently.
- `store.set_feedback_sink(...)` lets the host take over that channel, so a data-QA service can feed events back into its own loop (retry, downgrade, or report to the model).
- The command plan is deterministic JSON, which makes it easy to show the user *what will run*, or to cache and audit it.

## Walkthrough

```python
from py_store import init, store

# A natural-language -> GQL layer (your own, or a skill such as `text-to-query`)
# produces the GQL string and the params dict. This library compiles and plans them.
def nl_to_gql(question: str) -> tuple[str, dict]:
    ...
    return (
        "Course($condition:@c0,$group:@g0,$having:@h0,$sort:@s0,$limit:@l0)"
        "{ status, n, total }",
        {
            "c0": {"status": {"$ne": "deleted"}},
            "g0": {"by": ["status"],
                   "agg": {"n": {"$count": "*"}, "total": {"$sum": "price"}}},
            "h0": {"n": {"$gt": 1}},
            "s0": {"total": -1},
            "l0": 20,
        },
    )

# Take over the feedback channel first, so degraded paths are observable.
events = []

def sink(event):
    events.append(event)              # or forward to your LLM loop / alerting
    print("store feedback:", event)

store.set_feedback_sink(sink)

await init({"default": {"kind": "postgres", "exec": exec}})

question = "how are courses distributed by status?"
gql, params = nl_to_gql(question)

# 1) Compile and validate: no execution, no permission/compute application.
plan = store.build_pipeline(gql, params)
# plan -> {"tokens", "ast", "pipeline", "projection"}

# Your agent can inspect, reject or repair the plan here before running anything.

# 2) Run the exact same GQL.
rows = await store.query(gql, params)

# 3) Degraded paths surface as feedback events, not silent partial results.
for event in events:
    if event.get("code") in ("federation_degraded", "sql_pushdown_unsupported"):
        ...                           # re-plan, degrade gracefully, or tell the user
```

## Pitfalls

- **`build_pipeline` plans only.** It returns the compiled plan with no execution and without applying permissions or computed columns — do not treat it as a dry-run of the *result*.
- **A rejected SQL pushdown raises *and* emits.** `PushdownUnsupportedError` (a `RuntimeError`) is thrown and a `sql_pushdown_unsupported` event is emitted through the sink. Catch it if you want to re-run that segment on a Mongo source.
- **There is no raw aggregation escape hatch.** `$pipeline` passthrough and `store.aggregate()` were removed; a GQL containing `$pipeline` fails explicitly. Steer the model toward `$condition` / `$group` / `$having` / relations instead.
- **Boundary rules fail loudly.** Filtering on array fields, whole object fields or object dot-paths is rejected on every backend, and relation predicates support only one level. Surface those errors back to the model rather than retrying blindly.

## See also

- [README — Feedback events](../../README.md#feedback-events)
- [README — Aggregation](../../README.md#aggregation)
- [README — Schema reference](../../README.md#schema-reference)
