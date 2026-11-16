"""向量回填（VR-10）：按 source_hash 只重算变化项。

纯宿主编排：query（分页）→ 算摘要 → 比对 → embed → update（向量与 hash 同写）。
摘要口径逐字节对齐 nodejs-store/src/reindex.js（见执行文档 06 §4.1）。
"""

import hashlib
import inspect
import time

from . import feedback
from . import schema as schema_mod
from .crud import triggers as trigger_exec

DEFAULT_PAGE = 200
MAX_PAGE = 1000
SEP = "\u001e"


def hash_text(source_fields, doc):
    """按「字段名:字节长度:值」逐字段拼接（字段名排序，None 记空串）后 SHA-256，得到源内容指纹"""
    parts = []
    for f in sorted(source_fields):
        v = doc.get(f)
        s = "" if v is None else str(v)
        parts.append(f"{f}:{len(s.encode('utf-8'))}:{s}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _meta(schema_name):
    """取 schema 定义；不存在则抛 ERR_REINDEX_SCHEMA"""
    if not schema_mod.has(schema_name):
        raise ValueError(f"ERR_REINDEX_SCHEMA:未知 schema {schema_name}")
    return schema_mod.get(schema_name)


def _opts(m, opts):
    """校验并补全回填选项：field 须为 vector 字段、hashField 须为 string、sourceFields 须为非空 string 数组、embed 须可调用、pageSize 限 1..MAX_PAGE；filter/force/dryRun/routeOverride 填默认值"""
    o = dict(opts or {})
    f = m["fields"].get(o.get("field"))
    if not f or f.get("type") != "vector":
        raise ValueError(
            f"ERR_REINDEX_FIELD:{o.get('field')} 不是 {m['name']} 的 vector 字段"
        )
    h = m["fields"].get(o.get("hashField"))
    if not h or h.get("type") != "string":
        raise ValueError(
            f"ERR_REINDEX_HASH_FIELD:{o.get('hashField')} 须为 string 字段"
        )
    src = o.get("sourceFields")
    if not isinstance(src, list) or not src:
        raise ValueError("ERR_REINDEX_SOURCE:sourceFields 须为非空数组")
    for s in src:
        sf = m["fields"].get(s)
        if not sf or sf.get("type") != "string":
            raise ValueError(
                f"ERR_REINDEX_SOURCE:{s} 须为 string 字段（摘要口径要求，见 §4.1）"
            )
    if not callable(o.get("embed")):
        raise ValueError("ERR_REINDEX_EMBED:embed 须为可调用对象")
    page_size = o.get("pageSize", DEFAULT_PAGE)
    if (
        not isinstance(page_size, int)
        or isinstance(page_size, bool)
        or page_size < 1
        or page_size > MAX_PAGE
    ):
        raise ValueError(f"ERR_REINDEX_PAGE:pageSize 须为 1..{MAX_PAGE} 的整数")
    o["pageSize"] = page_size
    o.setdefault("filter", {})
    o.setdefault("force", False)
    o.setdefault("dryRun", False)
    o.setdefault("routeOverride", None)
    return o


def _text(source_fields, doc):
    """把源字段按序以分隔符 SEP 拼成待嵌入文本（None 记空串）"""
    return SEP.join(
        "" if doc.get(f) is None else str(doc.get(f)) for f in source_fields
    )


async def _embed_and_write(store, m, o, todo, dim):
    """对 todo 行调 embed（同步或协程）生成向量，逐条校验维度后 update 写回向量与源 hash；返回写入条数"""
    texts = [_text(o["sourceFields"], r) for r in todo]
    vecs = o["embed"](texts)
    if inspect.isawaitable(vecs):
        vecs = await vecs
    if not isinstance(vecs, list) or len(vecs) != len(texts):
        got = len(vecs) if isinstance(vecs, list) else "非数组"
        raise ValueError(f"ERR_REINDEX_EMBED:embed 返回 {got} 条，期望 {len(texts)} 条")
    written = 0
    for i, row in enumerate(todo):
        v = vecs[i]
        if not isinstance(v, list) or len(v) != dim:
            got = len(v) if isinstance(v, list) else "非数组"
            raise ValueError(f"ERR_REINDEX_EMBED:第 {i} 条维度 {got}，期望 {dim}")
        await store.update(
            m["name"],
            {"_id": row["_id"]},
            {
                o["field"]: v,
                o["hashField"]: hash_text(o["sourceFields"], row),
            },
        )
        written += 1
    return written


async def reindex_vectors(
    store,
    schema_name,
    *,
    field,
    hashField,
    sourceFields,
    embed,
    filter=None,
    pageSize=DEFAULT_PAGE,
    force=False,
    dryRun=False,
    routeOverride=None,
):
    """向量回填主入口：按 _id 游标分页扫描目标 schema，hash 与源内容一致的跳过，其余 embed 后写回；force 全量重算、dryRun 只统计不落库、routeOverride 路由覆盖，返回 scanned/embedded/skipped/pending/pages/elapsedMs 统计"""
    started = time.monotonic()
    m = _meta(schema_name)
    o = _opts(
        m,
        {
            "field": field,
            "hashField": hashField,
            "sourceFields": sourceFields,
            "embed": embed,
            "filter": filter,
            "pageSize": pageSize,
            "force": force,
            "dryRun": dryRun,
            "routeOverride": routeOverride,
        },
    )
    dim = m["fields"][field].get("dim")
    proj = ", ".join(["_id", *sourceFields, hashField])
    out = {
        "schema": m["name"],
        "field": field,
        "scanned": 0,
        "embedded": 0,
        "skipped": 0,
        "pending": 0,
        "pages": 0,
        "dryRun": dryRun is True,
        "elapsedMs": 0,
    }

    anchor = None
    while True:
        cond = o["filter"]
        if anchor is not None:
            cond = (
                {"$and": [o["filter"], {"_id": {"$gt": anchor}}]}
                if o["filter"]
                else {"_id": {"$gt": anchor}}
            )
        gql = f"{m['name']}($condition:@c,$sort:@s,$limit:@l){{ {proj} }}"
        rows = await store.query(
            gql, {"c": cond, "s": {"_id": 1}, "l": o["pageSize"]}, o["routeOverride"]
        )
        page = rows if isinstance(rows, list) else []
        out["pages"] += 1
        out["scanned"] += len(page)

        todo = []
        for r in page:
            if not o["force"] and r.get(hashField) == hash_text(sourceFields, r):
                out["skipped"] += 1
            else:
                todo.append(r)
        out["pending"] += len(todo)

        if todo and not o["dryRun"]:
            out["embedded"] += await _embed_and_write(store, m, o, todo, dim)
            feedback.emit(
                {
                    "type": "vector_reindex",
                    "code": "vectorReindex",
                    "layer": "host",
                    "message": f"{m['name']}.{field} 回填 {out['embedded']}/{out['scanned']}"
                    f"（第 {out['pages']} 页）",
                    "schema": m["name"],
                    "field": field,
                    "page": out["pages"],
                }
            )

        if len(page) < o["pageSize"]:
            break
        anchor = page[-1].get("_id")
        if anchor is None:
            raise ValueError("ERR_REINDEX_ANCHOR:分页锚点缺失（投影须含 _id）")

    out["elapsedMs"] = int((time.monotonic() - started) * 1000)
    return out


async def reindex_one(store, schema_name, rid, opts):
    """单条回填：按 _id 取行，hash 一致即跳过（返回 embedded=0, skipped=1），否则 embed 写回；记录不存在抛 ERR_REINDEX_NOT_FOUND"""
    m = _meta(schema_name)
    o = _opts(m, opts)
    dim = m["fields"][o["field"]].get("dim")
    proj = ", ".join(["_id", *o["sourceFields"], o["hashField"]])
    row = await store.query_one(
        f"{m['name']}($condition:@c){{ {proj} }}",
        {"c": {"_id": rid}},
        o["routeOverride"],
    )
    if not row:
        raise ValueError(f"ERR_REINDEX_NOT_FOUND:{m['name']} 无 _id={rid}")
    if not o["force"] and row.get(o["hashField"]) == hash_text(o["sourceFields"], row):
        return {"embedded": 0, "skipped": 1}
    n = await _embed_and_write(store, m, o, [row], dim)
    return {"embedded": n, "skipped": 0}


def register_vector_reindex(cfg):
    """把回填注册为触发器函数 ``<schema>.vectorReindex``（校验 cfg 后固定单条回填选项），返回函数引用名"""
    m = _meta(cfg.get("schema"))
    o = _opts(m, cfg)
    fn_ref = f"{m['name']}.vectorReindex"

    async def _impl(args, _ctx, host):
        """触发器实现：取 args._id（缺失抛 ERR_REINDEX_TRIGGER），用注册时固化的选项对该条回填"""
        rid = (args or {}).get("_id")
        if rid is None or rid == "":
            raise ValueError(f"ERR_REINDEX_TRIGGER:{fn_ref} 缺少 args._id")
        return await reindex_one(host["store"], m["name"], rid, o)

    trigger_exec.set_trigger_fn(fn_ref, _impl)
    return fn_ref
