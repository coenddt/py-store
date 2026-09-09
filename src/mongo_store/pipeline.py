"""
GQL 解析 + Aggregate Pipeline 构建器

GQL 语法（极简，支持5个参数）:
    ModelName($condition:@c0,$sort:@s1,$skip:@sk,$limit:@l1,$pipeline:@p1) {
      field1, field2,
      RelationName($condition:@c2,$sort:@s3) {
        field3,
        NestedRelation { field4 }
      }
    }
值用 @key 引用 params 对象
"""

from typing import Any

from .permission import get_context, get_readable_computes
from .schema import get

# ─── 递归保护 ──────────────────────────────────────────────
MAX_DEPTH = 10
MAX_PAGINATED_DEPTH = 4


# ─── Tokenizer ─────────────────────────────────────────────


def tokenize(gql):
    tokens = []
    i = 0
    n = len(gql)
    while i < n:
        ch = gql[i]
        # 空白
        if ch.isspace():
            i += 1
            continue
        # 标点
        if ch in '(){},:':
            tokens.append({'t': 'p', 'v': ch})
            i += 1
            continue
        # 标识符（模型名/字段名/关系名，支持点号嵌套）
        if ch.isalpha() or ch in '_$':
            v = ''
            while i < n and (gql[i].isalnum() or gql[i] in '_$.'):
                v += gql[i]
                i += 1
            tokens.append({'t': 'id', 'v': v})
            continue
        # 参数引用 @xxx
        if ch == '@':
            v = '@'
            i += 1
            while i < n and (gql[i].isalnum() or gql[i] == '_'):
                v += gql[i]
                i += 1
            tokens.append({'t': 'ref', 'v': v})
            continue
        i += 1  # 跳过未知字符
    return tokens


# ─── Parser ────────────────────────────────────────────────


def parse(tokens):
    pos = 0

    def peek():
        return tokens[pos] if pos < len(tokens) else None

    def consume(t, v=None):
        nonlocal pos
        tk = peek()
        if tk is None:
            raise ValueError(f'期望 {t}({v}) 但已到达末尾')
        if tk['t'] != t or (v is not None and tk['v'] != v):
            raise ValueError(f"期望 {t}({v}) 实际 {tk['t']}({tk['v']}) 位置 {pos}")
        pos += 1
        return tk

    def parse_params():
        nonlocal pos
        p = {}
        tk = peek()
        if tk and tk['t'] == 'p' and tk['v'] == '(':
            consume('p', '(')
            while True:
                tk = peek()
                if tk and tk['v'] == ')':
                    break
                key = consume('id')['v'].lstrip('$')
                consume('p', ':')
                val = peek()
                if val['t'] in ('ref', 'id'):
                    p[key] = val['v']
                pos += 1
                tk = peek()
                if tk and tk['v'] == ',':
                    consume('p', ',')
            consume('p', ')')
        return p

    def parse_body():
        fields: list = []
        relations: dict = {}
        tk = peek()
        if not tk or tk['t'] != 'p' or tk['v'] != '{':
            return {'fields': fields, 'relations': relations}
        consume('p', '{')
        while True:
            tk = peek()
            if not tk or tk['v'] == '}':
                break
            if tk['v'] == ',':
                consume('p', ',')
                continue
            name = consume('id')['v']
            tk = peek()
            has_paren = tk and tk['t'] == 'p' and tk['v'] == '('
            has_brace = tk and tk['t'] == 'p' and tk['v'] == '{'
            if has_paren or has_brace:
                prm = parse_params() if has_paren else {}
                body = parse_body() if has_brace else {'fields': [], 'relations': {}}
                relations[name] = {**body, 'params': prm}
            else:
                fields.append(name)
            tk = peek()
            if tk and tk['v'] == ',':
                consume('p', ',')
        consume('p', '}')
        return {'fields': fields, 'relations': relations}

    model_name = consume('id')['v']
    root_params = parse_params()
    body = parse_body()
    return {'model': model_name, 'params': root_params, **body}


def parse_gql(gql):
    """解析 GQL 字符串 → AST"""
    return parse(tokenize(gql))


def _param(params, ref):
    """从 params 中按 @ref 取值，ref 为空返回 None"""
    if not ref:
        return None
    return params.get(ref[1:])


def _find_stage_idx(stages, key):
    for i, st in enumerate(stages):
        if key in st:
            return i
    return -1


# ─── Pipeline 构建器 ───────────────────────────────────────


def _append_order(stages, sort, skip_val, limit_val):
    """按序追加 $sort/$skip/$limit 到 stages"""
    if sort is not None:
        stages.append({'$sort': sort})
    if skip_val is not None:
        stages.append({'$skip': skip_val})
    if limit_val is not None:
        stages.append({'$limit': limit_val})


def _ns_lookup_stages(rel_ast, rel_schema, params, _depth, _next_paginated):
    """构建所有嵌套关系 lookup 阶段（含 one 关系的 $unwind）"""
    stages: list[dict[str, Any]] = []
    for n_name, n_ast in (rel_ast.get('relations') or {}).items():
        n_def = rel_schema['relations'].get(n_name)
        if not n_def:
            raise ValueError(f'关系 "{n_name}" 未在 schema "{rel_schema["name"]}" 中定义')
        n_schema = get(n_def['model'])
        stages.append(build_lookup(n_name, n_ast, params, n_def, n_schema, rel_schema,
                                   _depth + 1, _next_paginated))
        if n_def['type'] == 'one':
            stages.append({'$unwind': {'path': f'${n_name}', 'preserveNullAndEmptyArrays': True}})
    return stages


def _build_rel_projection(rel_ast, rel_schema):
    """构建嵌套关系的 $project（请求字段 + fn 计算列 depends）"""
    if not rel_ast.get('fields'):
        return None
    proj = {'_id': 1}
    for f in rel_ast['fields']:
        proj[f] = 1
    # 补充 fn 计算列的 depends 字段
    for f in rel_ast['fields']:
        comp = rel_schema.get('computes', {}).get(f)
        if comp and comp.get('fn') and comp.get('depends'):
            for dep in comp['depends']:
                if dep not in proj:
                    proj[dep] = 1
    return proj


def build_lookup(rel_name, rel_ast, params, rel_def, rel_schema, source_schema, _depth=0, _paginated=0):
    local_key = rel_def['localField']
    foreign_key = rel_def['foreignField']
    let_var = f'rel_{local_key}'

    condition = _param(params, rel_ast['params'].get('condition'))
    sort = _param(params, rel_ast['params'].get('sort'))
    skip_val = _param(params, rel_ast['params'].get('skip'))
    limit_val = _param(params, rel_ast['params'].get('limit'))

    # ── 递归保护（分两套深度限制） ──
    has_paginated = skip_val is not None or limit_val is not None
    next_paginated = _paginated + 1 if has_paginated else _paginated
    if _depth >= MAX_DEPTH or (has_paginated and _paginated >= MAX_PAGINATED_DEPTH):
        # 返回空 $lookup（只做外键匹配，不继续嵌套），pipeline 不崩溃
        return build_empty_lookup(rel_name, rel_def, rel_schema, source_schema)

    stages: list[dict[str, Any]] = []

    # $match: 外键关联 + 附加条件
    # 当 localField 是 array 类型时，使用 $in 匹配数组中的任一元素
    source_field = (source_schema or {}).get('fields', {}).get(local_key) or {}
    is_array_field = source_field.get('type') == 'array'
    match_expr = _rel_match_expr(foreign_key, let_var, is_array_field)
    if condition is not None:
        stages.append({'$match': {'$and': [match_expr, condition]}})
    else:
        stages.append({'$match': match_expr})

    # sort / skip / limit（优先执行，避免全量数据流入后续嵌套 $lookup）
    # 当 sort 依赖嵌套关联字段时，嵌套 $lookup 必须优先于 sort
    sorts_by_nested = bool(sort) and any('.' in k for k in sort)
    if not sorts_by_nested:
        _append_order(stages, sort, skip_val, limit_val)

    # 嵌套 relations
    stages += _ns_lookup_stages(rel_ast, rel_schema, params, _depth, next_paginated)

    # sort / skip / limit（兜底：仅在嵌套 $lookup 未提前执行时追加）
    if sorts_by_nested:
        _append_order(stages, sort, skip_val, limit_val)

    # $project: 只返回请求的字段 + 计算列 fn 的 depends
    rel_proj = _build_rel_projection(rel_ast, rel_schema)
    if rel_proj is not None:
        stages.append({'$project': rel_proj})

    return {
        '$lookup': {
            'from': rel_schema['collection'],
            # 防御：localField 为数组字段时，$in 第二参用 $isArray 守卫
            'let': {let_var: _rel_let_expr(local_key, is_array_field)},
            'pipeline': stages,
            'as': rel_name,
        },
    }


def _rel_match_expr(foreign_key, let_var, is_array_field):
    """外键匹配表达式：数组字段用 $in，否则 $eq"""
    if is_array_field:
        return {'$expr': {'$in': [f'${foreign_key}', f'$${let_var}']}}
    return {'$expr': {'$eq': [f'${foreign_key}', f'$${let_var}']}}


def _rel_let_expr(local_key, is_array_field):
    """let 变量守卫：数组字段用 $isArray，否则 $ifNull"""
    if is_array_field:
        return {'$cond': [{'$isArray': f'${local_key}'}, f'${local_key}', []]}
    return {'$ifNull': [f'${local_key}', None]}


def build_empty_lookup(rel_name, rel_def, rel_schema, source_schema):
    """构建空 $lookup（递归保护降级用）"""
    local_key = rel_def['localField']
    foreign_key = rel_def['foreignField']
    let_var = f'rel_{local_key}'
    source_field = (source_schema or {}).get('fields', {}).get(local_key) or {}
    is_array_field = source_field.get('type') == 'array'
    match_expr = (
        {'$expr': {'$in': [f'${foreign_key}', f'$${let_var}']}}
        if is_array_field
        else {'$expr': {'$eq': [f'${foreign_key}', f'$${let_var}']}}
    )
    return {
        '$lookup': {
            'from': rel_schema['collection'],
            'let': {let_var: (
                {'$cond': [{'$isArray': f'${local_key}'}, f'${local_key}', []]}
                if is_array_field
                else {'$ifNull': [f'${local_key}', None]}
            )},
            'pipeline': [{'$match': match_expr}],
            'as': rel_name,
        },
    }


def build_compute_lookup_stages(schema):
    """构建 compute 的独立 $lookup 阶段"""
    stages: list[dict[str, Any]] = []
    for key, comp in (schema.get('computes') or {}).items():
        lookup = comp.get('lookup')
        if lookup and lookup.get('from'):
            stages.append({
                '$lookup': {
                    'from': lookup['from'],
                    'let': lookup.get('let') or {},
                    'pipeline': lookup.get('pipeline') or [],
                    'as': lookup.get('as') or f'_{key}',
                },
            })
    return stages


def build_add_fields(schema, ctx):
    """构建 $addFields 阶段（lookup 类型计算列）"""
    readable_computes = get_readable_computes(schema, ctx) if ctx else None

    add_fields = {}
    for key, comp in (schema.get('computes') or {}).items():
        # 权限裁剪：跳过不可读的计算列
        if readable_computes is not None and key not in readable_computes:
            continue
        lookup = comp.get('lookup')
        if lookup:
            if lookup.get('from'):
                # 独立 $lookup 模式：使用 addFields 表达式提取结果
                if lookup.get('addFields'):
                    add_fields[key] = lookup['addFields']
            else:
                # 简单表达式模式
                add_fields[key] = lookup
    return {'$addFields': add_fields} if add_fields else None


def flatten_object_fields(ast, schema):
    """归一化 AST：将 type=object 的花括号子字段展平为点号字段（原地修改）"""
    if 'relations' not in ast:
        return
    for rel_name in list(ast['relations'].keys()):
        field_def = schema.get('fields', {}).get(rel_name)
        if isinstance(field_def, dict) and field_def.get('type') == 'object' and field_def.get('fields'):
            rel_ast = ast['relations'][rel_name]
            if rel_ast.get('fields'):
                for sub_field in rel_ast['fields']:
                    ast['fields'].append(rel_name + '.' + sub_field)
            del ast['relations'][rel_name]


def _override_or_append(stages, stage_key, value):
    """若 stages 已含该类 stage 则覆盖，否则追加（自定义 pipeline 模式）"""
    idx = _find_stage_idx(stages, stage_key)
    if idx >= 0:
        stages[idx] = {stage_key: value}
    else:
        stages.append({stage_key: value})


def _custom_pipeline_branch(stages, root_pipeline, root_condition, root_sort, root_skip, root_limit):
    """自定义 pipeline 模式：延展用户 pipeline 并用根参数覆盖/追加排序分页"""
    stages.extend(root_pipeline)
    if root_condition is not None:
        _override_or_append(stages, '$match', root_condition)
    if root_sort is not None:
        _override_or_append(stages, '$sort', root_sort)
    if root_skip is not None:
        _override_or_append(stages, '$skip', root_skip)
    if root_limit is not None:
        _override_or_append(stages, '$limit', root_limit)
    return stages


def _root_lookup_stages(ast, schema, params, stages):
    """标准 GQL：逐层展开根 relations 为 $lookup（one 关系附加 $unwind）"""
    for rel_name, rel_ast in (ast.get('relations') or {}).items():
        rel_def = schema['relations'].get(rel_name)
        if not rel_def:
            raise ValueError(f'关系 "{rel_name}" 未在 schema "{schema["name"]}" 中定义')
        rel_schema = get(rel_def['model'])
        stages.append(build_lookup(rel_name, rel_ast, params, rel_def, rel_schema, schema))
        if rel_def['type'] == 'one':
            stages.append({'$unwind': {'path': f'${rel_name}', 'preserveNullAndEmptyArrays': True}})
    return stages


def build_pipeline(ast, params):
    """从 AST 构建 aggregate pipeline"""
    schema = get(ast['model'])
    stages: list[dict[str, Any]] = []

    root_condition = _param(params, ast['params'].get('condition'))
    root_sort = _param(params, ast['params'].get('sort'))
    root_skip = _param(params, ast['params'].get('skip'))
    root_limit = _param(params, ast['params'].get('limit'))
    root_pipeline = _param(params, ast['params'].get('pipeline'))

    if root_pipeline is not None and isinstance(root_pipeline, list):
        return _custom_pipeline_branch(stages, root_pipeline, root_condition, root_sort, root_skip, root_limit)

    # ── 标准 GQL 模式 ──
    # 展平 object 子字段花括号语法 → dot-notation
    flatten_object_fields(ast, schema)

    if root_condition is not None:
        stages.append({'$match': root_condition})

    # $lookup: 逐层展开 relations
    _root_lookup_stages(ast, schema, params, stages)

    # sort / skip / limit（根级别）
    _append_order(stages, root_sort, root_skip, root_limit)

    # $lookup: compute 独立的 $lookup 阶段（在 $addFields 之前）
    stages.extend(build_compute_lookup_stages(schema))

    # $addFields: lookup 计算列（放在最后，确保所有 $lookup 字段已就绪）
    ctx = get_context()
    add_fields = build_add_fields(schema, ctx)
    if add_fields:
        stages.append(add_fields)

    return stages


def _apply_permission_prune(proj, schema, ctx):
    """从投影中移除当前用户不可读的字段"""
    if not ctx:
        return
    from .permission import get_readable_fields
    readable_fields = get_readable_fields(schema, ctx)
    if readable_fields is None:
        return
    for key in list(proj.keys()):
        if key == '_id':
            continue
        if key in schema['fields'] and key not in readable_fields:
            del proj[key]


def _append_compute_deps(proj, schema, compute_keys, dot_parent_fields):
    """按计算列 requires 把依赖字段并入投影"""
    app_computes = [c for c in (schema.get('computes') or {}).values() if c.get('fn') or c.get('asyncFn')]
    if not app_computes:
        return
    all_have_depends = all(c.get('depends') is not None and isinstance(c.get('depends'), list) for c in app_computes)
    if all_have_depends:
        # 最优投影：仅保留 GQL 字段 + 显式声明的依赖
        _merge_compute_depends(proj, app_computes, dot_parent_fields)
    else:
        # 安全兜底：包含所有 schema 定义字段
        _merge_all_schema_fields(proj, schema, dot_parent_fields)


def _merge_compute_depends(proj, app_computes, dot_parent_fields):
    for c in app_computes:
        for dep in c['depends']:
            if dep in dot_parent_fields:
                continue
            if dep not in proj:
                proj[dep] = 1


def _merge_all_schema_fields(proj, schema, dot_parent_fields):
    for key in schema['fields']:
        if key in dot_parent_fields:
            continue
        if key not in proj:
            proj[key] = 1


def _collect_real_fields(ast, schema, compute_keys):
    """收集真实 schema 字段的投影条目（排除计算列/点号重复），返回 (proj, dot_parent_fields, has_real_field)"""
    proj = {}
    dot_parent_fields = set()
    any_field_requested = set()
    has_real_field = False
    for f in ast['fields']:
        if f in compute_keys:
            continue
        if '.' in f:
            root = f.split('.', 1)[0]
            if root in schema['fields']:
                dot_parent_fields.add(root)
                if root not in any_field_requested:
                    proj[f] = 1
                has_real_field = True
        elif f in schema['fields']:
            proj[f] = 1
            any_field_requested.add(f)
            has_real_field = True
    # 如果父字段被整个请求，移除其 dot-notation 子条目
    if has_real_field:
        for root in dot_parent_fields:
            if root in proj:
                for key in list(proj.keys()):
                    if key.startswith(root + '.'):
                        del proj[key]
    return proj, dot_parent_fields, has_real_field


def build_projection(ast, schema, ctx=None):
    """从 GQL 根字段列表 + schema computes 计算投影"""
    if not ast.get('fields'):
        return None

    compute_keys = set((schema.get('computes') or {}).keys())
    proj = {'_id': 1}

    # 仅包含实际 schema 字段（排除计算列与点号重复）
    fields_proj, dot_parent_fields, has_real_field = _collect_real_fields(ast, schema, compute_keys)
    proj.update(fields_proj)

    if not has_real_field:
        return None

    # 收集需要在应用层执行的计算列（fn + asyncFn），其依赖字段不能被投影排除
    _append_compute_deps(proj, schema, compute_keys, dot_parent_fields)

    # 关系名加入投影（否则 $project 阶段会丢弃 $lookup 的结果）
    for rel_name in (ast.get('relations') or {}):
        if rel_name not in proj:
            proj[rel_name] = 1

    # 权限裁剪：从投影中移除当前用户不可读的字段
    _apply_permission_prune(proj, schema, ctx)

    return proj
