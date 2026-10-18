"""向量回填（VR-10）：按 source_hash 只重算变化项。

纯宿主编排：query（分页）→ 算摘要 → 比对 → embed → update（向量与 hash 同写）。
摘要口径逐字节对齐 nodejs-store/src/reindex.js（见执行文档 06 §4.1）。
"""

import hashlib
import time
import inspect

from . import feedback
from . import schema as schema_mod
from .crud import triggers as trigger_exec

DEFAULT_PAGE = 200
MAX_PAGE = 1000
SEP = '\u001e'


def hash_text(source_fields, doc):
    parts = []
    for f in sorted(source_fields):
        v = doc.get(f)
        s = '' if v is None else str(v)
        parts.append(f"{f}:{len(s.encode('utf-8'))}:{s}")
    return hashlib.sha256('\n'.join(parts).encode('utf-8')).hexdigest()


def _meta(schema_name):
    if not schema_mod.has(schema_name):
        raise ValueError(f'ERR_REINDEX_SCHEMA:未知 schema {schema_name}')
    return schema_mod.get(schema_name)


def _opts(m, opts):
    o = dict(opts or {})
    f = m['fields'].get(o.get('field'))
    if not f or f.get('type') != 'vector':
        raise ValueError(f"ERR_REINDEX_FIELD:{o.get('field')} 不是 {m['name']} 的 vector 字段")
    h = m['fields'].get(o.get('hashField'))
    if not h or h.get('type') != 'string':
        raise ValueError(f"ERR_REINDEX_HASH_FIELD:{o.get('hashField')} 须为 string 字段")
    src = o.get('sourceFields')
    if not isinstance(src, list) or not src:
        raise ValueError('ERR_REINDEX_SOURCE:sourceFields 须为非空数组')
    for s in src:
        sf = m['fields'].get(s)
        if not sf or sf.get('type') != 'string':
            raise ValueError(f'ERR_REINDEX_SOURCE:{s} 须为 string 字段（摘要口径要求，见 §4.1）')
    if not callable(o.get('embed')):
        raise ValueError('ERR_REINDEX_EMBED:embed 须为可调用对象')
    page_size = o.get('pageSize', DEFAULT_PAGE)
    if not isinstance(page_size, int) or isinstance(page_size, bool) \
            or page_size < 1 or page_size > MAX_PAGE:
        raise ValueError(f'ERR_REINDEX_PAGE:pageSize 须为 1..{MAX_PAGE} 的整数')
    o['pageSize'] = page_size
    o.setdefault('filter', {})
    o.setdefault('force', False)
    o.setdefault('dryRun', False)
    o.setdefault('routeOverride', None)
    return o


def _text(source_fields, doc):
    return SEP.join('' if doc.get(f) is None else str(doc.get(f)) for f in source_fields)


async def _embed_and_write(store, m, o, todo, dim):
    texts = [_text(o['sourceFields'], r) for r in todo]
    vecs = o['embed'](texts)
    if inspect.isawaitable(vecs):
        vecs = await vecs
    if not isinstance(vecs, list) or len(vecs) != len(texts):
        got = len(vecs) if isinstance(vecs, list) else '非数组'
        raise ValueError(f'ERR_REINDEX_EMBED:embed 返回 {got} 条，期望 {len(texts)} 条')
    written = 0
    for i, row in enumerate(todo):
        v = vecs[i]
        if not isinstance(v, list) or len(v) != dim:
            got = len(v) if isinstance(v, list) else '非数组'
            raise ValueError(f'ERR_REINDEX_EMBED:第 {i} 条维度 {got}，期望 {dim}')
        await store.update(m['name'], {'_id': row['_id']}, {
            o['field']: v,
            o['hashField']: hash_text(o['sourceFields'], row),
        })
        written += 1
    return written


async def reindex_vectors(store, schema_name, *, field, hashField, sourceFields,
                          embed, filter=None, pageSize=DEFAULT_PAGE,
                          force=False, dryRun=False, routeOverride=None):
    started = time.monotonic()
    m = _meta(schema_name)
    o = _opts(m, {
        'field': field, 'hashField': hashField, 'sourceFields': sourceFields,
        'embed': embed, 'filter': filter, 'pageSize': pageSize,
        'force': force, 'dryRun': dryRun, 'routeOverride': routeOverride,
    })
    dim = m['fields'][field].get('dim')
    proj = ', '.join(['_id', *sourceFields, hashField])
    out = {
        'schema': m['name'], 'field': field, 'scanned': 0, 'embedded': 0,
        'skipped': 0, 'pending': 0, 'pages': 0,
        'dryRun': dryRun is True, 'elapsedMs': 0,
    }

    anchor = None
    while True:
        cond = o['filter']
        if anchor is not None:
            cond = ({'$and': [o['filter'], {'_id': {'$gt': anchor}}]}
                    if o['filter'] else {'_id': {'$gt': anchor}})
        gql = f"{m['name']}($condition:@c,$sort:@s,$limit:@l){{ {proj} }}"
        rows = await store.query(gql, {'c': cond, 's': {'_id': 1}, 'l': o['pageSize']},
                                 o['routeOverride'])
        page = rows if isinstance(rows, list) else []
        out['pages'] += 1
        out['scanned'] += len(page)

        todo = []
        for r in page:
            if not o['force'] and r.get(hashField) == hash_text(sourceFields, r):
                out['skipped'] += 1
            else:
                todo.append(r)
        out['pending'] += len(todo)

        if todo and not o['dryRun']:
            out['embedded'] += await _embed_and_write(store, m, o, todo, dim)
            feedback.emit({
                'type': 'vector_reindex', 'code': 'vectorReindex', 'layer': 'host',
                'message': f"{m['name']}.{field} 回填 {out['embedded']}/{out['scanned']}"
                           f"（第 {out['pages']} 页）",
                'schema': m['name'], 'field': field, 'page': out['pages'],
            })

        if len(page) < o['pageSize']:
            break
        anchor = page[-1].get('_id')
        if anchor is None:
            raise ValueError('ERR_REINDEX_ANCHOR:分页锚点缺失（投影须含 _id）')

    out['elapsedMs'] = int((time.monotonic() - started) * 1000)
    return out


async def reindex_one(store, schema_name, rid, opts):
    m = _meta(schema_name)
    o = _opts(m, opts)
    dim = m['fields'][o['field']].get('dim')
    proj = ', '.join(['_id', *o['sourceFields'], o['hashField']])
    row = await store.query_one(f"{m['name']}($condition:@c){{ {proj} }}",
                                {'c': {'_id': rid}}, o['routeOverride'])
    if not row:
        raise ValueError(f'ERR_REINDEX_NOT_FOUND:{m["name"]} 无 _id={rid}')
    if not o['force'] and row.get(o['hashField']) == hash_text(o['sourceFields'], row):
        return {'embedded': 0, 'skipped': 1}
    n = await _embed_and_write(store, m, o, [row], dim)
    return {'embedded': n, 'skipped': 0}


def register_vector_reindex(cfg):
    m = _meta(cfg.get('schema'))
    o = _opts(m, cfg)
    fn_ref = f"{m['name']}.vectorReindex"

    async def _impl(args, _ctx, host):
        rid = (args or {}).get('_id')
        if rid is None or rid == '':
            raise ValueError(f'ERR_REINDEX_TRIGGER:{fn_ref} 缺少 args._id')
        return await reindex_one(host['store'], m['name'], rid, o)

    trigger_exec.set_trigger_fn(fn_ref, _impl)
    return fn_ref
