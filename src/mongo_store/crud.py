"""
CRUD 操作

核心原则：
  - 写入时不补默认值（DB 存最少数据）
  - 读取时自动补默认值 + 执行简单计算列（计算列引擎在 computes.py）
  - lookup 计算列嵌入 aggregate 的 $addFields
  - asyncFn 计算列需显式请求
"""

import math
import random
import string
import time
from typing import Any

from pymongo import ReturnDocument

from .computes import (
    _merge_depends_into_ast,
    _run_async_fns,
    _strip_dep_injected,
    apply_defaults_and_computes,
    process_node,
)
from .permission import (
    PermissionError,
    can_read_schema,
    can_write_schema,
    evaluate,
    filter_writable_data,
    get_context,
    merge_owner_condition,
)
from .pipeline import build_pipeline, build_projection, parse_gql
from .schema import get as _get_schema
from .schema import has as _has_schema

_db: Any = None


def set_db(db):
    global _db
    _db = db


def _get_db():
    if _db is None:
        raise RuntimeError('MongoStore 未初始化，请先调用 init(db)')
    return _db


def _remove_undefined(obj):
    """剔除对象中的 None 值（原地修改），避免空值写入 DB"""
    for key in list(obj.keys()):
        if obj[key] is None:
            del obj[key]
    return obj


def _col(schema_name):
    s = _get_schema(schema_name)
    return _db[s['collection']]


# ─── GQL 查询 ──────────────────────────────────────────────


async def query(gql: str, params: dict | None = None) -> list[dict[str, Any]]:
    """
    GQL 查询（返回数组）

    支持的 params 键（通过 GQL 的 @key 引用）:
      $condition / $sort / $skip / $limit / $pipeline
    使用 $pipeline 时，框架不追加 compute 层、不补默认值、不裁剪，完全由用户控制。
    """
    params = params if params is not None else {}
    ctx = get_context()
    ast = parse_gql(gql)
    schema = _get_schema(ast['model'])

    # Schema 级读权限检查
    if ctx and not can_read_schema(schema, ctx):
        raise PermissionError('无访问权限')

    # 所有者条件注入（非 admin 用户只看自己的数据）
    cond_ref = ast['params'].get('condition')
    if ctx and cond_ref:
        cond_key = cond_ref[1:]
        params[cond_key] = merge_owner_condition(schema, ctx, params.get(cond_key))

    # $pipeline 模式 → 用户全权控制
    pipeline_ref = ast['params'].get('pipeline')
    has_pipeline = bool(pipeline_ref) and params.get(pipeline_ref[1:]) is not None

    # 注入 asyncFn 计算列的关系依赖
    if has_pipeline:
        inject_info: dict[str, dict[str, Any]] = {'relations': {}}
    else:
        inject_info = _merge_depends_into_ast(ast, schema)
    has_inject = len(inject_info['relations']) > 0

    pipeline = build_pipeline(ast, params)

    # 由 GQL 根字段 + fn 计算列 depends 驱动的投影，避免拉取整文档
    projection = None if has_pipeline else build_projection(ast, schema, ctx)

    coll = _col(ast['model'])

    return await _execute_pipeline(coll, pipeline, has_pipeline, projection, ast, schema, ctx,
                                   has_inject, inject_info)


async def _execute_pipeline(coll, pipeline, has_pipeline, projection, ast, schema, ctx,
                            has_inject, inject_info):
    """按 pipeline 形态选执行路径：纯 $match → find 快路径；$lookup+分页 → 两阶段；否则标准聚合"""
    # ── 纯 $match 无关联 → 用 find 性能更好 ──
    if not has_pipeline and len(pipeline) == 1 and '$match' in pipeline[0]:
        cursor = coll.find(pipeline[0]['$match'], projection) if projection else coll.find(pipeline[0]['$match'])
        items = await cursor.to_list(length=None)
        return await _postprocess(items, ast, schema, ctx, has_inject, inject_info)

    # ── 自动两阶段优化（根级别） ──
    # 当 pipeline 同时有 $lookup 和 $skip/$limit 时，先用轻量 pipeline 取分页 ID，
    # 再对少量 ID 做关联查询，避免全量 join 后被 $skip 丢弃。
    first_lookup_idx = next((i for i, st in enumerate(pipeline) if '$lookup' in st), -1)
    has_skip_limit = (not has_pipeline) and any('$skip' in st or '$limit' in st for st in pipeline)

    if first_lookup_idx >= 0 and has_skip_limit:
        return await _run_two_phase(coll, pipeline, projection, ast, schema, ctx, has_inject, inject_info)

    # ── 标准单阶段聚合 ──
    if not has_pipeline and projection:
        pipeline.append({'$project': projection})
    items = await (await coll.aggregate(pipeline)).to_list(length=None)

    # $pipeline 模式：直接返回原始结果
    if has_pipeline:
        return items

    # 标准 GQL 模式：递归处理每一条
    return await _postprocess(items, ast, schema, ctx, has_inject, inject_info)


async def query_one(gql: str, params: dict | None = None) -> dict[str, Any] | None:
    """GQL 查询（返回单条）"""
    items = await query(gql, params)
    return items[0] if items else None


def _resolve_page(ast, params):
    """解析分页参数（page/pageSize 或传统 $skip/$limit），pageSize 已含 5000 上限"""
    if 'page' in params or 'pageSize' in params:
        page = max(0, math.floor(Number(params['page']))) if params.get('page') is not None else 0
        page_size = Number(params['pageSize']) if params.get('pageSize') is not None else 50
    else:
        skip_ref = ast['params'].get('skip')
        limit_ref = ast['params'].get('limit')
        skip_val = params.get(skip_ref[1:]) if skip_ref else None
        limit_val = params.get(limit_ref[1:]) if limit_ref else None
        page = math.floor(Number(skip_val) / Number(limit_val)) if (skip_val is not None and limit_val) else 0
        page_size = Number(limit_val) if limit_val is not None else 50
    return page, min(page_size, 5000)


async def query_with_count(gql: str, params: dict | None = None) -> dict[str, Any]:
    """
    GQL 查询（返回 items + total + 分页元数据）

    支持两种分页参数方式：
      1. page/pageSize（推荐）— 自动计算 skip/limit，page 默认 0，pageSize 默认 50
      2. 传统 $skip/$limit — 从 GQL 参数推导 page/pageSize
    pageSize 上限 5000，防止拖库。
    """
    params = params if params is not None else {}
    ast = parse_gql(gql)
    schema = _get_schema(ast['model'])
    coll = _col(ast['model'])

    page, page_size = _resolve_page(ast, params)

    # 确保 GQL 实际使用上述分页值
    if ast['params'].get('skip'):
        params[ast['params']['skip'][1:]] = page * page_size
    if ast['params'].get('limit'):
        params[ast['params']['limit'][1:]] = page_size

    items = await query(gql, params)

    # ── 统计 total（忽略 skip/limit） ──
    count_filter: dict = {}
    cond_ref = ast['params'].get('condition')
    if cond_ref:
        count_filter = params.get(cond_ref[1:]) or {}

    # 权限：total 也应反映所有者条件
    ctx = get_context()
    if ctx:
        count_filter = merge_owner_condition(schema, ctx, count_filter)

    total = await coll.count_documents(count_filter)
    has_more = (page + 1) * page_size < total

    return {'items': items, 'total': total, 'hasMore': has_more, 'page': page, 'pageSize': page_size}


async def _postprocess(items, ast, schema, ctx, has_inject, inject_info):
    """标准 GQL 尾处理：递归裁剪 + asyncFn 计算列 + 移除注入的关系依赖"""
    for item in items:
        process_node(item, ast, schema, ctx)
    await _run_async_fns(items, schema, ctx)
    if has_inject:
        _strip_dep_injected(items, inject_info, schema)
    return items


async def _run_two_phase(coll, pipeline, projection, ast, schema, ctx, has_inject, inject_info):
    """自动两阶段优化：先取分页 ID，再对少量 ID 关联查询"""
    # 检查 sort 是否引用关联表字段（如 'bidders.amount'），是则退化为标准单阶段
    if _sorts_by_relation(pipeline):
        return await _run_standard(coll, pipeline, projection, ast, schema, ctx, has_inject,
                                   inject_info)

    first_lookup_idx = next((i for i, st in enumerate(pipeline) if '$lookup' in st), -1)

    # 阶段一：仅取分页后的 ID（无 $lookup，利用索引）
    id_pipeline = _paginate_id_pipeline(pipeline, first_lookup_idx)
    pl_sort = next((st for st in pipeline if '$sort' in st), None)

    id_docs = await (await coll.aggregate(id_pipeline)).to_list(length=None)
    if not id_docs:
        return []
    ids = [d['_id'] for d in id_docs]

    # 阶段二：仅对分页后的少量 ID 执行关联查询
    full_pipeline = [
        st for st in pipeline[first_lookup_idx:]
        if '$sort' not in st and '$skip' not in st and '$limit' not in st
    ]
    full_pipeline.insert(0, {'$match': {'_id': {'$in': ids}}})

    if projection:
        full_pipeline.append({'$project': projection})
    items = await (await coll.aggregate(full_pipeline)).to_list(length=None)

    # $in 查询不保证返回顺序，按阶段一 ids 的顺序重排，恢复正确排序
    items = _restore_sort_order(items, ids, pl_sort)

    return await _postprocess(items, ast, schema, ctx, has_inject, inject_info)


def _paginate_id_pipeline(pipeline, first_lookup_idx):
    """构建仅取分页 ID 的轻量 pipeline（保留 sort/skip/limit，投影仅 _id）"""
    id_pipeline = list(pipeline[:first_lookup_idx])
    for key in ('$sort', '$skip', '$limit'):
        st = next((s for s in pipeline if key in s), None)
        if st:
            id_pipeline.append(st)
    id_pipeline.append({'$project': {'_id': 1}})
    return id_pipeline


def _sorts_by_relation(pipeline):
    """pipeline 的 $sort 是否引用关联表点号字段（如 'bidders.amount'）"""
    sort_stage = next((st for st in pipeline if '$sort' in st), None)
    if not isinstance(sort_stage, dict):
        return False
    return any('.' in k for k in (sort_stage.get('$sort') or {}))


def _restore_sort_order(items, ids, pl_sort):
    """按阶段一 ids 顺序重排阶段二结果（有排序且多文档时）"""
    if not pl_sort or len(ids) <= 1:
        return items
    id_order = {str(_id): i for i, _id in enumerate(ids)}
    items.sort(key=lambda d: id_order.get(str(d['_id']), len(id_order)))
    return items


async def _run_standard(coll, pipeline, projection, ast, schema, ctx, has_inject, inject_info):
    """标准单阶段聚合（两阶段优化因 sort 引用关联字段而退化至此路径）"""
    if projection:
        pipeline.append({'$project': projection})
    items = await (await coll.aggregate(pipeline)).to_list(length=None)
    return await _postprocess(items, ast, schema, ctx, has_inject, inject_info)


def Number(val):
    """对齐 JS Number()：无法转换时返回 0（NaN→0 由调用处 || 兜底）"""
    try:
        return int(val)
    except (TypeError, ValueError):
        try:
            return float(val)
        except (TypeError, ValueError):
            return 0


# ─── 简单 CRUD ─────────────────────────────────────────────


def _generate_id(schema):
    """按 schema.idPrefix 生成唯一 ID（时间戳36进制 + 随机4位）"""
    ts = _to_base36(int(time.time() * 1000)).upper()
    rnd = ''.join(random.choices(string.ascii_lowercase + string.digits, k=4)).upper()
    return schema['idPrefix'] + ts + rnd


def _to_base36(n):
    digits = string.digits + string.ascii_lowercase
    if n == 0:
        return '0'
    out = []
    while n:
        n, r = divmod(n, 36)
        out.append(digits[r])
    return ''.join(reversed(out))


def _has_creator_permission(s):
    return 'creator' in (s.get('read') or []) or 'creator' in (s.get('write') or [])


async def _check_write_perm(s, ctx, coll, condition=None, deny_msg='无写入权限'):
    """Schema 级写权限检查：guest 直接拒绝；非写授权时仅 creator 命中才放行"""
    if 'guest' in (ctx.get('roles') or []):
        raise PermissionError(deny_msg)
    if can_write_schema(s, ctx):
        return
    if s.get('write') and 'creator' in s['write'] and condition:
        existing = await coll.find_one(condition, {'_id': 1, 'createdBy': 1})
        if not existing or evaluate(ctx, s['write'], existing) is False:
            raise PermissionError(deny_msg)
    else:
        raise PermissionError(deny_msg)


async def insert(schema_name: str, data: dict) -> dict[str, Any]:
    """插入一条"""
    ctx = get_context()
    s = _get_schema(schema_name)

    # Schema 级写权限检查
    if not can_write_schema(s, ctx):
        raise PermissionError('无写入权限')

    # 字段级写权限过滤
    filtered = filter_writable_data(s, ctx, data) if ctx else data

    coll = _col(schema_name)
    doc = _remove_undefined({**filtered})

    # 自动生成 ID
    if not doc.get('_id') and s['idPrefix']:
        doc['_id'] = s['idPrefix'] + _to_base36(int(time.time() * 1000)).upper() + ''.join(
            random.choices(string.ascii_lowercase + string.digits, k=4)).upper()

    # 自动设置 createdBy（creator 权限场景）
    if _has_creator_permission(s) and not doc.get('createdBy') and ctx and ctx.get('userId'):
        doc['createdBy'] = ctx['userId']

    # 自动时间戳
    if s['timestamps']:
        now = int(time.time() * 1000)
        if not doc.get('createdAt'):
            doc['createdAt'] = now
        doc['updatedAt'] = now

    await coll.insert_one(doc)
    return apply_defaults_and_computes(doc, s)


async def insert_many(schema_name: str, docs: list[dict]) -> list[dict[str, Any]]:
    """批量插入（带权限检查，自动生成 _id 和时间戳）"""
    if not isinstance(docs, list) or not docs:
        return []

    ctx = get_context()
    s = _get_schema(schema_name)
    coll = _col(schema_name)

    if not can_write_schema(s, ctx):
        raise PermissionError('无写入权限')

    processed_docs = []
    for data in docs:
        filtered = filter_writable_data(s, ctx, data) if ctx else data
        doc = _remove_undefined({**filtered})

        if not doc.get('_id') and s['idPrefix']:
            doc['_id'] = _generate_id(s)

        if _has_creator_permission(s) and not doc.get('createdBy') and ctx and ctx.get('userId'):
            doc['createdBy'] = ctx['userId']

        if s['timestamps']:
            now = int(time.time() * 1000)
            if not doc.get('createdAt'):
                doc['createdAt'] = now
            doc['updatedAt'] = now

        processed_docs.append(doc)

    await coll.insert_many(processed_docs)
    return [apply_defaults_and_computes(doc, s) for doc in processed_docs]


async def update(schema_name: str, condition: dict, data: dict, options: dict | None = None) -> dict[str, Any] | None:
    """
    更新一条（支持原生操作符，不触发默认值）

    data 的 key 以 '$' 开头 → 原生 MongoDB 操作符（$set/$inc/$unset 等）直接透传。
    否则自动包装为 $set 模式。
    """
    options = options or {}
    ctx = get_context()
    s = _get_schema(schema_name)
    coll = _col(schema_name)

    # Schema 级写权限检查
    if ctx:
        await _check_write_perm(s, ctx, coll, condition)

    has_raw_operators = bool(data) and any(k.startswith('$') for k in data)

    if has_raw_operators:
        # 原生操作符模式（透传 $inc/$unset/$addToSet 等）
        if ctx and data.get('$set') is not None:
            data['$set'] = _remove_undefined(filter_writable_data(s, ctx, data['$set']))
        if s['timestamps']:
            set_part = data.get('$set') or {}
            data['$set'] = {**set_part, 'updatedAt': int(time.time() * 1000)}
        result = await coll.find_one_and_update(
            condition, data,
            return_document=ReturnDocument.AFTER,
            **options,
        )
        return apply_defaults_and_computes(result, s) if result else None

    # $set 模式
    set_data = _remove_undefined(filter_writable_data(s, ctx, data) if ctx else {**data})
    set_data.pop('_id', None)

    if not set_data:
        raise ValueError('没有提供要更新的字段')

    if s['timestamps']:
        set_data['updatedAt'] = int(time.time() * 1000)

    result = await coll.find_one_and_update(
        condition,
        {'$set': set_data},
        return_document=ReturnDocument.AFTER,
        **options,
    )
    return apply_defaults_and_computes(result, s) if result else None


async def update_many(schema_name: str, condition: dict, data: dict) -> dict[str, Any]:
    """批量更新（支持原生操作符）"""
    ctx = get_context()
    s = _get_schema(schema_name)
    coll = _col(schema_name)

    # Schema 级写权限检查
    if ctx:
        if 'guest' in (ctx.get('roles') or []):
            raise PermissionError('无批量写入权限')
        if not can_write_schema(s, ctx):
            raise PermissionError('无批量写入权限')

    has_raw_operators = bool(data) and any(k.startswith('$') for k in data)

    if has_raw_operators:
        if ctx and data.get('$set') is not None:
            data['$set'] = _remove_undefined(filter_writable_data(s, ctx, data['$set']))
        if s['timestamps']:
            set_part = data.get('$set') or {}
            data['$set'] = {**set_part, 'updatedAt': int(time.time() * 1000)}
    else:
        set_data = _remove_undefined(filter_writable_data(s, ctx, data) if ctx else {**data})
        set_data.pop('_id', None)
        if s['timestamps']:
            set_data['updatedAt'] = int(time.time() * 1000)
        data = {'$set': set_data}

    result = await coll.update_many(condition, data)
    return {'modifiedCount': result.modified_count}


async def remove(schema_name: str, condition: dict) -> dict[str, Any]:
    """删除 —— 原表数据先归档到对应 `_deleted` 附表（附 deletedAt），再物理删除原表数据"""
    ctx = get_context()
    s = _get_schema(schema_name)
    coll = _col(schema_name)

    if ctx:
        await _check_write_perm(s, ctx, coll, condition, deny_msg='无删除权限')

    # 归档：完整拷贝到删除附表（保留原字段与时间戳，附 deletedAt）
    archived_count = 0
    archive_name = f"{schema_name}Deleted"
    if _has_schema(archive_name):
        archive_coll = _col(archive_name)
        docs = await coll.find(condition).to_list(length=None)
        if docs:
            now = int(time.time() * 1000)
            for d in docs:
                d['deletedAt'] = now
            await archive_coll.insert_many(docs)
            archived_count = len(docs)

    result = await coll.delete_many(condition)
    return {'deletedCount': result.deleted_count, 'archivedCount': archived_count}


async def exists(schema_name: str, condition: dict) -> bool:
    """判断是否存在"""
    coll = _col(schema_name)
    doc = await coll.find_one(condition, {'_id': 1})
    return doc is not None


async def count(schema_name: str, filter: dict | None = None) -> int:
    """统计符合条件的文档数量"""
    coll = _col(schema_name)
    return await coll.count_documents(filter or {})


# ─── Mutation ──────────────────────────────────────────────


def _build_upsert_conditions(schema, data):
    """
    构建 upsert 条件组（$or 数组）

    规则：
      1. data._id 非空 → 加入 { _id: data._id }
      2. unique 索引的 keys 在 data 中均非空 → 整组作为一条条件加入 $or
    """
    conditions = []

    if data.get('_id') and str(data['_id']).strip():
        conditions.append({'_id': data['_id']})

    for idx in schema.get('indexes') or []:
        options = idx.get('options')
        if not options or not options.get('unique'):
            continue
        keys = list(idx['keys'].keys())
        all_present = all(
            data.get(k) is not None and not (isinstance(data.get(k), str) and not data[k].strip())
            for k in keys
        )
        if all_present:
            conditions.append({k: data[k] for k in keys})

    return conditions


async def _upsert_one(schema, data, foreign_field):
    """type: 'one' 子文档强制按 foreignKey upsert"""
    db = _get_db()
    coll = db[schema['collection']]

    set_data = _remove_undefined({**data})
    set_on_insert = {}

    # _id：有则 $setOnInsert（不 $set，避免修改已有文档的 _id）
    if set_data.get('_id') and str(set_data['_id']).strip():
        set_on_insert['_id'] = set_data['_id']
    elif schema['idPrefix']:
        set_on_insert['_id'] = _generate_id(schema)
    set_data.pop('_id', None)

    # 时间戳
    if schema['timestamps']:
        now = int(time.time() * 1000)
        set_data['updatedAt'] = now
        set_on_insert['createdAt'] = data.get('createdAt', now)
    set_data.pop('createdAt', None)

    update_doc = {'$set': set_data}
    if set_on_insert:
        update_doc['$setOnInsert'] = set_on_insert

    result = await coll.find_one_and_update(
        {foreign_field: data[foreign_field]},
        update_doc,
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return result


async def _mutation_one(schema, data):
    """单条 mutation 核心逻辑"""
    ctx = get_context()

    if not can_write_schema(schema, ctx):
        raise PermissionError('无写入权限')

    # ── 1. 按 schema.relations 拆分 fieldData + relationData ──
    field_data = {}
    relation_data = {}
    rel_keys = set(schema['relations'].keys())

    for key, val in data.items():
        if key in rel_keys:
            if ctx:
                rel_def = schema['relations'][key]
                if rel_def.get('read') and not evaluate(ctx, rel_def['read']):
                    continue
            relation_data[key] = val
        else:
            field_data[key] = val

    filtered_field_data = filter_writable_data(schema, ctx, field_data) if ctx else field_data

    db = _get_db()
    coll = db[schema['collection']]

    # ── 2. 构建 upsert 条件 ──
    or_conditions = _build_upsert_conditions(schema, filtered_field_data)

    # ── 3. 写入 parent ──
    if or_conditions:
        # ── Upsert 路径 ──
        parent_doc = await coll.find_one_and_update(
            {'$or': or_conditions},
            _build_upsert_update(schema, filtered_field_data),
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
    else:
        # ── Insert 路径（复用现有 insert 逻辑） ──
        parent_doc = await insert(schema['name'], filtered_field_data)

    # ── 4. 处理 relation 子文档 ──
    await _apply_relations(schema, relation_data, parent_doc)

    # ── 5. 补默认值后返回 ──
    return apply_defaults_and_computes(parent_doc, schema)


def _build_upsert_update(schema, field_data):
    """由 field_data 生成 upsert 的 update_doc（$set + $setOnInsert）"""
    set_data = _remove_undefined({**field_data})
    set_on_insert = {}

    if set_data.get('_id') and str(set_data['_id']).strip():
        set_on_insert['_id'] = set_data['_id']
    elif schema['idPrefix']:
        set_on_insert['_id'] = _generate_id(schema)
    set_data.pop('_id', None)

    if schema['timestamps']:
        now = int(time.time() * 1000)
        set_data['updatedAt'] = now
        set_on_insert['createdAt'] = field_data.get('createdAt', now)
    set_data.pop('createdAt', None)

    # 自动设置 createdBy（upsert 新文档时）
    if _has_creator_permission(schema) and not set_on_insert.get('createdBy'):
        set_on_insert['createdBy'] = set_on_insert.get('_id')

    update_doc = {'$set': set_data}
    if set_on_insert:
        update_doc['$setOnInsert'] = set_on_insert
    return update_doc


async def _apply_relations(schema, relation_data, parent_doc):
    """将 relation 子文档写入（one→外键 upsert / many→递归 mutation）"""
    for rel_name, rel_val in relation_data.items():
        if rel_val is None:
            continue
        rel_def = schema['relations'].get(rel_name)
        if not rel_def:
            continue

        parent_id = parent_doc['_id']
        rel_schema = _get_schema(rel_def['model'])

        if rel_def['type'] == 'one':
            child_data = {**rel_val}
            child_data[rel_def['foreignField']] = parent_id
            await _upsert_one(rel_schema, child_data, rel_def['foreignField'])
        elif rel_def['type'] == 'many':
            arr = rel_val if isinstance(rel_val, list) else [rel_val]
            for child_item in arr:
                if child_item is None:
                    continue
                child_item[rel_def['foreignField']] = parent_id
                await _mutation_one(rel_schema, child_item)


async def mutation(schema_name: str, data: dict | list[dict]) -> Any:
    """
    mutation — 智能持久化

    自动判断 upsert/insert，支持父子文档关联填充。
    """
    s = _get_schema(schema_name)
    is_array = isinstance(data, list)
    items = data if is_array else [data]

    if not items:
        return [] if is_array else None

    results = []
    for item in items:
        results.append(await _mutation_one(s, item))

    return results if is_array else results[0]


async def upsert(schema_name: str, condition: dict, data: dict, options: dict | None = None) -> dict[str, Any] | None:
    """
    upsert — 显式条件 upsert

    与 mutation 不同，upsert 需要调用方显式提供 match 条件，不处理父子关系。
    """
    options = options or {}
    ctx = get_context()
    s = _get_schema(schema_name)
    coll = _col(schema_name)

    if not can_write_schema(s, ctx):
        raise PermissionError('无写入权限')

    filtered_data = filter_writable_data(s, ctx, data) if ctx else data

    return_new = options.get('returnNew', True)
    set_data = _remove_undefined({**filtered_data})
    set_on_insert = {}

    # _id：从 data 移到 $setOnInsert（不 $set，避免修改已有文档的 _id）
    if set_data.get('_id') and str(set_data['_id']).strip():
        set_on_insert['_id'] = set_data['_id']
    elif s['idPrefix'] and not condition.get('_id'):
        set_on_insert['_id'] = _generate_id(s)
    set_data.pop('_id', None)

    # 时间戳
    if s['timestamps']:
        now = int(time.time() * 1000)
        set_data['updatedAt'] = now
        set_on_insert['createdAt'] = filtered_data.get('createdAt', now)
    set_data.pop('createdAt', None)

    # 自动设置 createdBy
    if _has_creator_permission(s) and not set_on_insert.get('createdBy'):
        set_on_insert['createdBy'] = set_on_insert.get('_id') or condition.get('_id')

    update_doc = {'$set': set_data}
    if set_on_insert:
        update_doc['$setOnInsert'] = set_on_insert

    result = await coll.find_one_and_update(
        condition,
        update_doc,
        upsert=True,
        return_document=ReturnDocument.AFTER if return_new else ReturnDocument.BEFORE,
    )

    return apply_defaults_and_computes(result, s) if result else None


async def aggregate(schema_name: str, pipeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """对指定 schema 执行 MongoDB 原生聚合查询"""
    s = _get_schema(schema_name)
    coll = _get_db()[s['collection']]
    return await (await coll.aggregate(pipeline)).to_list(length=None)
