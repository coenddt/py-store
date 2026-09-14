# FastAPI admin backend

## The problem

You need an internal admin API: list/search endpoints with pagination, create/update/delete, per-role permissions, a few computed display columns, and a soft-delete archive for auditing. A full ORM means model classes, migrations and a lot of boilerplate; a bare driver means hand-writing all of the access control and pagination yourself.

## Why py-store

- Schema-driven CRUD: register a JSON schema, then `insert` / `update` / `remove` / `query` with no model classes.
- Read-time computed columns (`fn` / `asyncFn` / relation `agg`) give you display values without denormalizing.
- Built-in RBAC: schema-level and field-level `read` / `write` whitelists, plus the `creator` pseudo-role for ownership (`doc.createdBy == ctx.userId`).
- Soft delete is automatic — every schema gets a `<Model>Deleted` archive collection/table, and `remove()` archives before deleting.
- `query_with_count` returns items plus total and pagination metadata in one call, with `pageSize` capped at 5000.

## Walkthrough

```python
from fastapi import Depends, FastAPI, Request
from py_store import init, store

app = FastAPI()

@app.on_event("startup")
async def startup():
    await init({"default": mongo_db})
    store.register({
        "name": "Article",
        "collection": "articles",
        "idPrefix": "AR",
        "timestamps": True,
        "fields": {
            "_id": "string",
            "title": {"type": "string", "default": ""},
            "body": {"type": "string", "default": ""},
            "status": {"type": "string", "default": "draft"},
            "authorId": {"type": "string", "default": ""},
        },
        "computes": {
            "titleUpper": {"type": "string", "depends": ["title"],
                           "fn": lambda d: d["title"].upper()},
        },
        # schema-level role whitelists; `creator` is resolved per document
        "read": ["editor", "viewer", "creator"],
        "write": ["editor", "creator"],
        "indexes": [{"keys": {"status": 1, "createdAt": -1}}],
    })

# One dependency sets the permission context for the whole request flow.
async def current_user(request: Request) -> dict:
    user = {"userId": request.headers["x-user-id"],
            "roles": request.headers.getlist("x-role")}
    store.set_context(user)            # roles drive schema/field whitelist checks
    return user

@app.get("/articles")
async def list_articles(page: int = 0, page_size: int = 50,
                        user: dict = Depends(current_user)):
    gql = "Article($condition:@c0,$sort:@s1) { _id, title, status, titleUpper }"
    params = {"c0": {}, "s1": {"createdAt": -1}, "page": page, "pageSize": page_size}
    return await store.query_with_count(gql, params)
    # -> {"items", "total", "hasMore", "page", "pageSize"}   (pageSize capped at 5000)

@app.post("/articles")
async def create_article(payload: dict, user: dict = Depends(current_user)):
    return await store.insert("Article", {**payload, "authorId": user["userId"]})

@app.delete("/articles/{article_id}")
async def delete_article(article_id: str, user: dict = Depends(current_user)):
    # archives to <collection>_deleted first, then deletes the original document
    return await store.remove("Article", {"_id": article_id})
```

Soft delete is transparent: the archive collection `<collection>_deleted` is auto-registered per schema, and re-creating the same `_id` does not collide because the archive write is upsert-by-`_id`.

## Pitfalls

- **Set the context on the same async path as the call.** The permission context lives in a `ContextVar`, so set it in an `async` dependency (or a helper wrapping the store call) — a sync dependency running in a thread pool will not propagate it to the endpoint.
- **Empty-condition writes are rejected.** `update_many` / `remove` with `{}`, `None` or `{"$and": []}` is refused outright; it never falls through to a full-table write. Require an explicit filter from the request.
- **Without a context, checks are disabled.** By default, a missing context means fail-open (no permission check). Enable `store.set_require_context(True)` at startup for fail-secure behaviour, and wrap internal/cron work in `store.run_as_internal(...)`.
- **`createdAt` / `updatedAt` are framework-maintained.** Do not set them from the payload; the unit follows the schema's `timestamps` setting (`"s"` for seconds).
- **`pageSize` is capped at 5000.** Larger values are clamped, so build your UI pagination around that ceiling.

## See also

- [README — Query & write API](../../README.md#query--write-api)
- [README — Permission context](../../README.md#permission-context)
- [02 — AI data-QA agent](02-ai-data-qa-agent.md)
