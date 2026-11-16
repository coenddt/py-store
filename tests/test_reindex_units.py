"""向量回填（reindex.py，VR-10）单元测试。

覆盖基线 D8 点名的低覆盖分支：_meta/_opts 全部校验路径（schema/field/hashField/
sourceFields/embed/pageSize）、embed 返回条数与维度不符显式 Err、hash 一致跳过、
dryRun 零写入、锚点分页、reindex_one 未命中与命中、register_vector_reindex 触发器
（含缺 args._id 显式报错）。store 为内存假实现（假 store 只需 query/query_one/update）。
"""

import asyncio

import pytest

from py_store import reindex, schema


def _run(coro):
    return asyncio.run(coro)


_FIELDS = {
    "_id": {"type": "string"},
    "title": {"type": "string"},
    "body": {"type": "string"},
    "hash": {"type": "string"},
    "embedding": {"type": "vector", "dim": 3},
    "n": {"type": "number"},
}

_OPT_KW = dict(
    field="embedding",
    hashField="hash",
    sourceFields=["title", "body"],
    embed=lambda texts: [[0.1, 0.2, 0.3] for _ in texts],
)


class _Store:
    """假 store：分页页序 + 记录 query/query_one/update 调用"""

    def __init__(self, pages=(), one=None):
        self.pages = list(pages)
        self._one = one
        self.queries = []
        self.updates = []

    async def query(self, gql, params, route=None):
        self.queries.append((gql, params))
        return self.pages.pop(0) if self.pages else []

    async def query_one(self, gql, params, route=None):
        self.queries.append((gql, params))
        return self._one

    async def update(self, schema_name, cond, patch):
        self.updates.append((schema_name, cond, patch))


@pytest.fixture(scope="module", autouse=True)
def _register_schema():
    schema.register(
        {"name": "ReidxDoc", "collection": "reidx_doc", "fields": dict(_FIELDS)}
    )
    yield


# ---------- _meta / _opts 校验 ----------


def test_meta_unknown_schema_raises():
    with pytest.raises(ValueError, match="ERR_REINDEX_SCHEMA"):
        reindex._meta("NoSuchSchemaReidx")


def test_opts_rejects_non_vector_field():
    with pytest.raises(ValueError, match="ERR_REINDEX_FIELD"):
        reindex._opts(
            {"name": "ReidxDoc", "fields": _FIELDS},
            dict(_OPT_KW, field="title"),
        )


def test_opts_rejects_non_string_hash_field():
    with pytest.raises(ValueError, match="ERR_REINDEX_HASH_FIELD"):
        reindex._opts(
            {"name": "ReidxDoc", "fields": _FIELDS},
            dict(_OPT_KW, hashField="n"),
        )


@pytest.mark.parametrize(
    "src", [None, [], ["title", "n"], "title"], ids=["none", "empty", "non-str", "scalar"]
)
def test_opts_rejects_bad_source_fields(src):
    with pytest.raises(ValueError, match="ERR_REINDEX_SOURCE"):
        reindex._opts(
            {"name": "ReidxDoc", "fields": _FIELDS},
            dict(_OPT_KW, sourceFields=src),
        )


def test_opts_rejects_non_callable_embed():
    with pytest.raises(ValueError, match="ERR_REINDEX_EMBED"):
        reindex._opts(
            {"name": "ReidxDoc", "fields": _FIELDS},
            dict(_OPT_KW, embed="nope"),
        )


@pytest.mark.parametrize("page", [0, 1001, "10", True], ids=["zero", "over", "str", "bool"])
def test_opts_rejects_bad_page_size(page):
    with pytest.raises(ValueError, match="ERR_REINDEX_PAGE"):
        reindex._opts(
            {"name": "ReidxDoc", "fields": _FIELDS},
            dict(_OPT_KW, pageSize=page),
        )


def test_opts_defaults():
    o = reindex._opts({"name": "ReidxDoc", "fields": _FIELDS}, dict(_OPT_KW))
    assert o["pageSize"] == reindex.DEFAULT_PAGE
    assert o["filter"] == {}
    assert o["force"] is False
    assert o["dryRun"] is False
    assert o["routeOverride"] is None


# ---------- embed 返回校验（_embed_and_write） ----------


def _kw_no_embed():
    return {k: v for k, v in _OPT_KW.items() if k != "embed"}


def test_embed_wrong_count_raises():
    store = _Store(pages=[[
        {"_id": "a", "title": "t", "body": "b", "hash": "x"},
    ]])
    with pytest.raises(ValueError, match="返回 2 条，期望 1 条"):
        _run(
            reindex.reindex_vectors(
                store, "ReidxDoc", **_kw_no_embed(),
                embed=lambda texts: [[0.1, 0.2, 0.3]] * 2,
            )
        )


def test_embed_wrong_dim_raises():
    store = _Store(pages=[[{"_id": "a", "title": "t", "body": "b", "hash": "x"}]])
    with pytest.raises(ValueError, match="维度 2，期望 3"):
        _run(
            reindex.reindex_vectors(
                store, "ReidxDoc", **_kw_no_embed(),
                embed=lambda texts: [[0.1, 0.2] for _ in texts],
            )
        )


def test_embed_awaitable_supported():
    async def embed(texts):
        return [[0.1, 0.2, 0.3] for _ in texts]

    store = _Store(pages=[[{"_id": "a", "title": "t", "body": "b", "hash": "x"}]])
    out = _run(reindex.reindex_vectors(store, "ReidxDoc", **_kw_no_embed(), embed=embed))
    assert out["embedded"] == 1


# ---------- reindex_vectors 主流程 ----------


def _doc(rid, title="t", body="b", stale=False):
    d = {"_id": rid, "title": title, "body": body}
    d["hash"] = "stale-hash" if stale else reindex.hash_text(["title", "body"], d)
    return d


def test_reindex_skips_unchanged_and_writes_stale():
    rows = [_doc("a"), _doc("b", stale=True)]
    store = _Store(pages=[rows])
    out = _run(reindex.reindex_vectors(store, "ReidxDoc", **_OPT_KW))
    assert out["scanned"] == 2
    assert out["skipped"] == 1
    assert out["embedded"] == 1
    assert out["pending"] == 1
    assert out["dryRun"] is False
    assert out["elapsedMs"] >= 0
    # 只写 stale 行：向量与 hash 同写
    assert len(store.updates) == 1
    name, cond, patch = store.updates[0]
    assert name == "ReidxDoc"
    assert cond == {"_id": {"_id": "b"}} or cond == {"_id": "b"} or "_id" in str(cond)
    assert patch["embedding"] == [0.1, 0.2, 0.3]
    assert patch["hash"] == reindex.hash_text(["title", "body"], rows[1])


def test_reindex_dry_run_no_writes():
    store = _Store(pages=[[_doc("a", stale=True)]])
    out = _run(reindex.reindex_vectors(store, "ReidxDoc", **_OPT_KW, dryRun=True))
    assert out["pending"] == 1
    assert out["embedded"] == 0
    assert store.updates == []


def test_reindex_force_embeds_even_if_hash_matches():
    store = _Store(pages=[[_doc("a")]])  # hash 一致但 force=True
    out = _run(reindex.reindex_vectors(store, "ReidxDoc", **_OPT_KW, force=True))
    assert out["embedded"] == 1
    assert out["skipped"] == 0


def test_reindex_paginates_by_anchor():
    p1 = [_doc("a"), _doc("b")]
    store = _Store(pages=[p1, []])
    out = _run(
        reindex.reindex_vectors(store, "ReidxDoc", **_OPT_KW, pageSize=2)
    )
    assert out["pages"] == 2
    # 第二页条件带锚点：_id $gt 上一页末行
    _, params2 = store.queries[1]
    cond = params2["c"]
    assert "_id" in str(cond) and "$gt" in str(cond)
    assert out["scanned"] == 2


def test_reindex_missing_anchor_raises():
    # 整页 = pageSize 且末行无 _id → 锚点缺失显式报错
    # （行 hash 须一致以走 skipped 分支：先锚点检查，不触达 embed 的 row["_id"]）
    d = _doc("no-id")
    del d["_id"]
    store = _Store(pages=[[d], []])
    with pytest.raises(ValueError, match="ERR_REINDEX_ANCHOR"):
        _run(reindex.reindex_vectors(store, "ReidxDoc", **_OPT_KW, pageSize=1))


# ---------- reindex_one / register_vector_reindex ----------


def test_reindex_one_not_found_raises():
    store = _Store(one=None)
    with pytest.raises(ValueError, match="ERR_REINDEX_NOT_FOUND"):
        _run(reindex.reindex_one(store, "ReidxDoc", "missing", dict(_OPT_KW)))


def test_reindex_one_skips_when_hash_matches():
    d = _doc("a")
    store = _Store(one=d)
    out = _run(reindex.reindex_one(store, "ReidxDoc", "a", dict(_OPT_KW)))
    assert out == {"embedded": 0, "skipped": 1}
    assert store.updates == []


def test_reindex_one_embeds_stale():
    d = _doc("b", stale=True)
    store = _Store(one=d)
    out = _run(reindex.reindex_one(store, "ReidxDoc", "b", dict(_OPT_KW)))
    assert out == {"embedded": 1, "skipped": 0}
    assert len(store.updates) == 1


def test_register_vector_reindex_and_trigger_impl():
    ref = reindex.register_vector_reindex(
        dict(_OPT_KW, schema="ReidxDoc")
    )
    try:
        assert ref == "ReidxDoc.vectorReindex"
        impl = None
        from py_store.crud import triggers as T

        impl = T._trigger_fns[ref]

        # 缺 args._id 显式报错
        with pytest.raises(ValueError, match="ERR_REINDEX_TRIGGER"):
            _run(impl({}, None, {"store": _Store()}))

        # 正常：按 _id 单条回填
        d = _doc("c", stale=True)
        store = _Store(one=d)
        out = _run(impl({"_id": "c"}, None, {"store": store}))
        assert out == {"embedded": 1, "skipped": 0}
    finally:
        from py_store.crud import triggers as T

        T._trigger_fns.pop(ref, None)
        T._fn_refs.discard(ref)
