"""
计算列引擎（读取时后处理，纯 schema 驱动，禁 DB 访问）

职责：
  - 字段默认值解析与填充（含嵌套 object 递归）
  - fn/asyncFn 计算列执行与权限裁剪
  - asyncFn 依赖（depends）GQL 片段的 AST 注入与结果裁剪
  - process_node：GQL 查询结果的递归后处理（默认值→fn→关系下钻→字段裁剪→权限裁剪）

与 crud 分层：本模块只做「读出后加工」，不做任何 IO；
crud.query 负责 GQL 解析与取数，取数后调本模块完成加工。
"""

import inspect
from typing import Any

from .permission import (
    evaluate,
    get_readable_computes,
    get_readable_fields,
    get_readable_relations,
)
from .pipeline import flatten_object_fields, parse, tokenize
from .schema import get as _get_schema
from .types import get_default

# ─── 默认值 & 计算列（简单 CRUD 用，非递归） ──────────────


def _resolve_default(defn):
    """
    解析字段默认值。
    可变容器必须返回「新实例」，不能返回共享引用（避免跨文档串扰）。
    """
    if callable(defn):
        return defn()
    if isinstance(defn, list):
        return defn[:]
    if isinstance(defn, dict):
        return {**defn}
    return defn


def _fill_nested_defaults(obj, fields_def):
    if not isinstance(obj, dict):
        return
    for key, field in fields_def.items():
        if key not in obj or obj[key] is None:
            field_type = field if isinstance(field, str) else field.get('type')
            defn = field.get('default') if isinstance(field, dict) and field.get('default') is not None else get_default(field_type)
            if defn is not None:
                obj[key] = _resolve_default(defn)
        # 递归：object 内嵌 object
        if isinstance(field, dict) and field.get('type') == 'object' and field.get('fields') \
                and isinstance(obj.get(key), dict):
            _fill_nested_defaults(obj[key], field['fields'])


def apply_defaults_and_computes(doc, schema):
    """对单条记录应用字段默认值 + 简单计算列（仅根文档，不递归）"""
    if not doc:
        return doc
    result = {**doc}

    for key, field in schema['fields'].items():
        if key not in result or result[key] is None:
            field_type = field if isinstance(field, str) else field.get('type')
            defn = field.get('default') if isinstance(field, dict) and field.get('default') is not None else get_default(field_type)
            if defn is not None:
                result[key] = _resolve_default(defn)
        # 递归填充嵌套 object 子字段
        if isinstance(field, dict) and field.get('type') == 'object' and field.get('fields') \
                and isinstance(result.get(key), dict):
            _fill_nested_defaults(result[key], field['fields'])

    for key, comp in schema.get('computes', {}).items():
        if comp.get('fn'):
            result[key] = comp['fn'](result)

    return result


# ─── 默认值缓存 + 递归处理器（GQL 查询用） ────────────────

_defaults_cache: dict = {}


def _ensure_cache(schema):
    if schema['name'] in _defaults_cache:
        return _defaults_cache[schema['name']]

    field_defaults = {}
    for key, field in schema['fields'].items():
        field_type = field if isinstance(field, str) else field.get('type')
        defn = field.get('default') if isinstance(field, dict) and field.get('default') is not None else get_default(field_type)
        if defn is not None:
            field_defaults[key] = defn

    compute_defaults = {}
    fn_list = []
    async_fn_list = []
    for key, comp in (schema.get('computes') or {}).items():
        if comp.get('type'):
            compute_defaults[key] = get_default(comp['type'])
        if comp.get('fn'):
            fn_list.append({'key': key, 'depends': comp.get('depends', []), 'fn': comp['fn']})
        if comp.get('asyncFn'):
            async_fn_list.append({'key': key, 'asyncFn': comp['asyncFn'], 'depends': comp.get('depends', [])})

    cache = {'field_defaults': field_defaults, 'compute_defaults': compute_defaults, 'fn_list': fn_list, 'async_fn_list': async_fn_list}
    _defaults_cache[schema['name']] = cache
    return cache


async def _run_async_fns(items, schema, ctx):
    """执行 asyncFn 计算列（批量），带权限裁剪"""
    if not items:
        return
    cache = _ensure_cache(schema)
    if not cache['async_fn_list']:
        return

    readonly_async_fns = cache['async_fn_list']
    if ctx:
        readonly_async_fns = []
        for entry in cache['async_fn_list']:
            comp = schema.get('computes', {}).get(entry['key'])
            if comp and comp.get('read'):
                if evaluate(ctx, comp['read']):
                    readonly_async_fns.append(entry)
            else:
                readonly_async_fns.append(entry)

    for entry in readonly_async_fns:
        result = entry['asyncFn'](items, ctx)
        if inspect.iscoroutine(result):
            await result


def _collect_rel_deps(schema):
    """收集所有 asyncFn 计算列 depends 中的关系字段需求 { rel_name → set(fields) }"""
    cache = _ensure_cache(schema)
    rel_deps: dict = {}
    for entry in cache['async_fn_list']:
        for dep in entry['depends'] or []:
            trimmed = (dep or '').strip()
            if not trimmed or trimmed == '_id':
                continue
            if '{' in trimmed:
                parsed = parse(tokenize(trimmed))
                rel_name = parsed['model']
                if rel_name not in schema['relations']:
                    continue
                rel_deps.setdefault(rel_name, set())
                for f in parsed['fields']:
                    if f != '_id':
                        rel_deps[rel_name].add(f)
            else:
                if trimmed not in schema['relations']:
                    continue
                rel_deps.setdefault(trimmed, set())
    return rel_deps


def _inject_into_ast(ast, rel_deps):
    """把关系字段需求合并注入到 AST，返回注入信息"""
    if 'relations' not in ast:
        ast['relations'] = {}
    inject_info: dict[str, dict[str, Any]] = {'relations': {}}

    for rel_name, dep_fields in rel_deps.items():
        existing = ast['relations'].get(rel_name)
        if existing:
            existing_fields = set(existing.get('fields') or [])
            added = []
            for f in dep_fields:
                if f not in existing_fields:
                    existing.setdefault('fields', []).append(f)
                    added.append(f)
            if added:
                inject_info['relations'][rel_name] = set(added)
        else:
            ast['relations'][rel_name] = {'fields': list(dep_fields), 'relations': {}, 'params': {}}
            inject_info['relations'][rel_name] = '__all__'

    return inject_info


def _merge_depends_into_ast(ast, schema):
    """收集所有 asyncFn 计算列的 depends GQL 片段，合并注入到查询 AST 中"""
    cache = _ensure_cache(schema)
    if not cache['async_fn_list']:
        return {'relations': {}}

    rel_deps = _collect_rel_deps(schema)
    if not rel_deps:
        return {'relations': {}}

    return _inject_into_ast(ast, rel_deps)


def _strip_dep_injected(items, inject_info, schema):
    """从结果中裁剪依赖注入的字段（不返回客户端）"""
    if not inject_info or not inject_info['relations']:
        return
    for item in items:
        for rel_name, injected in inject_info['relations'].items():
            rel_val = item.get(rel_name)
            if injected == '__all__':
                item.pop(rel_name, None)
            elif isinstance(rel_val, list):
                for sub in rel_val:
                    if isinstance(sub, dict):
                        for f in injected:
                            sub.pop(f, None)
            elif isinstance(rel_val, dict):
                for f in injected:
                    rel_val.pop(f, None)


def _collect_needed(ast_node, cache):
    """GQL 请求字段 + fn 计算列依赖字段（去重）"""
    needed = list(ast_node['fields'])
    seen = set(needed)
    for entry in cache['fn_list']:
        for dep in entry['depends']:
            if dep not in seen:
                seen.add(dep)
                needed.append(dep)
    return needed


def _collect_dot(ast_node):
    """收集点号嵌套字段信息 { root → [subPath, ...] }"""
    dot_fields: dict = {}
    for f in ast_node['fields']:
        if '.' in f:
            root, sub = f.split('.', 1)
            dot_fields.setdefault(root, []).append(sub)
    return dot_fields


def _fill_defaults(doc, cache, needed, dot_fields):
    """补字段默认值（含点号字段的根字段）"""
    for key in needed:
        if key not in doc or doc[key] is None:
            defn = cache['field_defaults'].get(key)
            if defn is not None:
                doc[key] = _resolve_default(defn)
    for root in dot_fields:
        if root not in doc or doc[root] is None:
            defn = cache['field_defaults'].get(root)
            if defn is not None:
                doc[root] = _resolve_default(defn)


def _fill_dot_nested(doc, schema, root, sub_path):
    """填充嵌套 object 的点号精确子字段默认值"""
    field = schema['fields'].get(root)
    if not (isinstance(field, dict) and field.get('type') == 'object' and field.get('fields')
            and isinstance(doc.get(root), dict)):
        return
    sub_field_def = field['fields'].get(sub_path)
    if sub_field_def and (sub_path not in doc[root] or doc[root][sub_path] is None):
        sub_type = sub_field_def if isinstance(sub_field_def, str) else sub_field_def.get('type')
        defn = sub_field_def.get('default') if isinstance(sub_field_def, dict) and sub_field_def.get('default') is not None else get_default(sub_type)
        if defn is not None:
            doc[root][sub_path] = _resolve_default(defn)


def _fill_nested_objects(doc, schema, needed):
    """递归填充嵌套 object 子字段默认值（含点号精确子字段）"""
    for key in needed:
        if '.' in key:
            root, sub_path = key.split('.', 1)
            _fill_dot_nested(doc, schema, root, sub_path)
        else:
            field = schema['fields'].get(key)
            if isinstance(field, dict) and field.get('type') == 'object' and field.get('fields') \
                    and isinstance(doc.get(key), dict):
                _fill_nested_defaults(doc[key], field['fields'])


def _run_computes(doc, cache):
    """跑 fn 计算列，再补计算列默认值"""
    for entry in cache['fn_list']:
        doc[entry['key']] = entry['fn'](doc)
    for entry in cache['fn_list']:
        key = entry['key']
        if key not in doc or doc[key] is None:
            defn = cache['compute_defaults'].get(key)
            if defn is not None:
                doc[key] = _resolve_default(defn)


def _descend_relations(doc, ast_node, schema, ctx):
    """递归下钻嵌套关系文档（跳过不可读关系）"""
    rel_names = list(ast_node['relations'].keys())
    readable_relations = get_readable_relations(schema, ctx) if ctx else None
    for rel_name in rel_names:
        if readable_relations is not None and rel_name not in readable_relations:
            continue
        rel_ast = ast_node['relations'][rel_name]
        rel_def = schema['relations'].get(rel_name)
        if not rel_def:
            continue
        rel_schema = _get_schema(rel_def['model'])
        rel_val = doc.get(rel_name)
        if isinstance(rel_val, list):
            for rel_doc in rel_val:
                process_node(rel_doc, rel_ast, rel_schema, ctx)
        elif isinstance(rel_val, dict):
            process_node(rel_val, rel_ast, rel_schema, ctx)


def _compute_keep(ast_node, dot_fields):
    """仅保留 GQL 字段 + 关系名 + 点号根字段（_id 始终保留）"""
    keep = set(ast_node['fields'])
    for root in dot_fields:
        keep.add(root)
    for rel_name in ast_node['relations']:
        keep.add(rel_name)
    keep.add('_id')
    return keep


def _prune_unreadable_relations(ast_node, readable_relations, keep):
    """从 keep 中移除不可读的关系"""
    if readable_relations is None:
        return
    for rel_name in ast_node['relations']:
        if rel_name not in readable_relations:
            keep.discard(rel_name)


def _apply_readable_prune(doc, ast_node, schema, ctx, keep):
    """从 keep 中移除用户不可读的字段/计算列/关系（读权限，不含 Owner 级）"""
    readable_fields = get_readable_fields(schema, ctx)
    readable_computes = get_readable_computes(schema, ctx)
    readable_relations = get_readable_relations(schema, ctx)

    for key in ast_node['fields']:
        if key == '_id':
            continue
        if (key in schema['fields'] and readable_fields is not None and key not in readable_fields
                or key in schema.get('computes', {}) and readable_computes is not None and key not in readable_computes):
            keep.discard(key)
    _prune_unreadable_relations(ast_node, readable_relations, keep)


def _prune_owner_field_read(doc, ast_node, schema, ctx, keep):
    """字段级 Owner read 校验（含计算列、关系）"""
    for key in ast_node['fields']:
        if key == '_id' or key not in keep:
            continue
        field = schema['fields'].get(key)
        if field and field.get('read') and evaluate(ctx, field['read'], doc) is False:
            keep.discard(key)


def _prune_owner_compute_read(doc, schema, ctx, keep):
    """计算列 Owner read 校验"""
    for key, comp in (schema.get('computes') or {}).items():
        if key not in keep:
            continue
        if comp and comp.get('read') and evaluate(ctx, comp['read'], doc) is False:
            keep.discard(key)


def _prune_owner_relation_read(doc, ast_node, schema, ctx, keep):
    """关系 Owner read 校验"""
    for rel_name in ast_node['relations']:
        if rel_name not in keep:
            continue
        rel = schema['relations'].get(rel_name)
        if rel and rel.get('read') and evaluate(ctx, rel['read'], doc) is False:
            keep.discard(rel_name)


def _apply_owner_read_prune(doc, ast_node, schema, ctx, keep):
    """Owner 级 read 校验：逐字段/计算列/关系传入 doc 做 creator 检查"""
    _prune_owner_field_read(doc, ast_node, schema, ctx, keep)
    _prune_owner_compute_read(doc, schema, ctx, keep)
    _prune_owner_relation_read(doc, ast_node, schema, ctx, keep)


def _apply_permissions(doc, ast_node, schema, ctx, keep):
    """从 keep 中移除当前用户不可读的字段/计算列/关系（含 Owner 级 read 校验）"""
    if not ctx:
        return
    _apply_readable_prune(doc, ast_node, schema, ctx, keep)
    _apply_owner_read_prune(doc, ast_node, schema, ctx, keep)


def _prune_doc(doc, keep):
    """删除不保留的顶层字段"""
    for key in list(doc.keys()):
        if key not in keep:
            del doc[key]


def _prune_dot_subfields(doc, dot_fields):
    """裁剪点号字段父对象中未请求的子字段"""
    for root, subs in dot_fields.items():
        obj = doc.get(root)
        if isinstance(obj, dict):
            sub_keep = set(subs)
            for sub_key in list(obj.keys()):
                if sub_key not in sub_keep:
                    del obj[sub_key]


def process_node(doc, ast_node, schema, ctx):
    """
    递归处理单条文档：补默认值 → 跑 fn 计算列 → 补计算列默认值 → 递归下钻 → 裁剪

    时序严格：
      1. 补字段默认值（计算列依赖的字段必须先有值）
      2. 跑 fn 计算列
      3. 补计算列默认值
      4. 递归处理嵌套关系文档
      5. 裁剪到 GQL 请求字段
      6. 权限裁剪
    """
    if not doc:
        return

    cache = _ensure_cache(schema)

    # 展平 object 子字段花括号语法 → dot-notation
    flatten_object_fields(ast_node, schema)

    # ① 收集 needed 字段（请求字段 + fn 依赖）与点号字段信息
    needed_arr = _collect_needed(ast_node, cache)
    dot_fields = _collect_dot(ast_node)

    # ② 补字段默认值 + 递归填充嵌套 object 子字段
    _fill_defaults(doc, cache, needed_arr, dot_fields)
    _fill_nested_objects(doc, schema, needed_arr)

    # ③ 跑 fn 计算列 + 补计算列默认值
    _run_computes(doc, cache)

    # ④ 递归下钻（跳过不可读的关系）
    _descend_relations(doc, ast_node, schema, ctx)

    # ⑤ 计算 keep（GQL 字段 + 关系名 + 点号根字段 + _id）
    keep = _compute_keep(ast_node, dot_fields)

    # ⑥ 权限裁剪（读权限 + Owner 级 read 校验）
    _apply_permissions(doc, ast_node, schema, ctx, keep)

    # ⑦ 裁剪字段 + 点号父对象中未请求的子字段
    _prune_doc(doc, keep)
    _prune_dot_subfields(doc, dot_fields)
