"""向量检索降级扫描（03 执行文档 §4.7，对齐 ``nodejs-store/src/vector-scan.js``）

把 ``$vectorSearch`` 从聚合管道剥离 → 先取候选行 → 交 core ``local_eval`` 精确打分。
适用于不支持 ``$vectorSearch`` 阶段的后端（MySQL / SQLite / 非 Atlas 的自建 Mongo），
以及 PostgreSQL 带 filter（近似索引退化为后过滤）的场景；
PostgreSQL 无过滤走原生 SQL，local / memory 由 core 直接求值，均不进入本模块。

硬约束：
  - 候选行必须含向量字段（候选管道只保留 ``$match``，不投影 → 全字段天然含 path）；
  - 候选行数 > MAX_VECTOR_SCAN_ROWS → Err code=vectorScanOverflow（禁止静默截断）；
  - 打分与 TopK 一律调 ``core.local_eval``，宿主不得自实现余弦。
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping

MAX_VECTOR_SCAN_ROWS = 100_000

#: 向量降级统一 hint（05 §4.9，node/py 逐字一致）
DEGRADED_HINT = '需要原生 TopK 与更低延迟请改用 postgres(pgvector) 或 MongoDB Atlas（索引声明 options.indexName）'

#: 各后端向量降级文案（05 §4.9，逐字）
DEGRADED_MESSAGES = {
    'mysql': '数据源 mysql 无原生向量索引，本次按应用层精确检索（结果已按 _score 降序）',
    'sqlite': '数据源 sqlite 无原生向量索引（vec0 未启用），本次按应用层精确检索（结果已按 _score 降序）',
    'mongodb': '数据源 mongodb 未声明 Atlas Search 索引，本次按应用层精确检索（结果已按 _score 降序）',
    'postgres': '数据源 postgres 的 pgvector 近似索引为后过滤，本次降级为精确检索以保证 ACL 与 TopK 正确',
    # 嵌入式载体（local / memory）无原生近似索引，查询恒为应用层精确检索（06 §4.6 C10）
    'local': '数据源 local 无原生向量索引，本次按应用层精确检索（结果已按 _score 降序）',
    'memory': '数据源 memory 无原生向量索引，本次按应用层精确检索（结果已按 _score 降序）',
}

_SCORE_META = {'$meta': 'vectorSearchScore'}


def _registry(core=None):
    """取可用的 ``Registry`` 实例（``local_eval`` 是实例方法，非模块级导出）"""
    if core is not None and callable(getattr(core, 'local_eval', None)):
        return core
    from .core import (
        core as _default_core,  # 延迟导入：避免与 datasource / crud 形成导入环
    )

    return _default_core


def is_vector_command(cmd):
    """命令是否为 ``$vectorSearch`` 打头的聚合"""
    if not isinstance(cmd, Mapping) or cmd.get('kind') != 'aggregate':
        return False
    pipeline = cmd.get('pipeline')
    if not isinstance(pipeline, list) or not pipeline:
        return False
    first = pipeline[0]
    return isinstance(first, Mapping) and bool(first.get('$vectorSearch'))


def has_filter(cmd):
    """``$vectorSearch.filter`` 是否有实际条件（缺省 / 空对象视为无过滤）"""
    if not is_vector_command(cmd):
        return False
    spec = cmd['pipeline'][0]['$vectorSearch']
    flt = spec.get('filter') if isinstance(spec, Mapping) else None
    return isinstance(flt, Mapping) and bool(flt)


def can_scan(cmd, backend):
    """该命令能否在本后端走降级扫描。

    backend 为连接 kind（mysql / postgres / sqlite）或 ``'mongo'``；
    postgres 无过滤时由方言翻译为原生 SQL（pgvector TopK），带过滤时近似索引退化为
    后过滤（ACL / TopK 不成立）→ 本后端回退应用层精确扫描（运行期降级，不改静态 native）。
    """
    if not is_vector_command(cmd):
        return False
    if backend == 'postgres':
        return has_filter(cmd)
    return True


def _is_score_stage(stage):
    """``$addFields: { _score: { $meta: 'vectorSearchScore' } }``（core 紧随 ``$vectorSearch`` 产出）"""
    if not isinstance(stage, Mapping):
        return False
    af = stage.get('$addFields')
    if not isinstance(af, Mapping) or len(af) != 1 or '_score' not in af:
        return False
    return af.get('_score') == _SCORE_META


def _strip_score(node):
    """递归剔除 ``_score`` 条件（未打分前该字段不存在）；空映射返回 None"""
    if isinstance(node, list):
        arr = [x for x in (_strip_score(n) for n in node) if x is not None]
        return arr or None
    if isinstance(node, Mapping):
        out = {}
        for k, v in node.items():
            if k == '_score':
                continue
            s = _strip_score(v)
            if s is None:
                continue
            out[k] = s
        return out or None
    return node


def _candidate_pipeline(flt, rest):
    """候选过滤管道：``$vectorSearch.filter`` → ``$match``，加上残余 ``$match``；
    其余阶段（``$limit`` / ``$project`` / ``$sort`` …）一律不进入候选阶段
    （TopK 与投影由第 3 段按原始完整管道重放）。"""
    out = []
    if flt is not None:
        f = _strip_score(flt)
        if f:
            out.append({'$match': f})
    for stage in rest:
        if not isinstance(stage, Mapping):
            continue
        keys = list(stage.keys())
        if len(keys) != 1 or keys[0] != '$match':
            continue
        m = _strip_score(stage['$match'])
        if m:
            out.append({'$match': m})
    return out


async def scan(cmd, exec=None, core=None, max_rows=MAX_VECTOR_SCAN_ROWS):
    """降级扫描：剥离 → 取候选 → core 精确打分。

    :param cmd: aggregate 命令（逻辑名），``{kind:'aggregate', collection, pipeline}``
    :param exec: ``async (candidate_command) -> list[dict]``，以候选命令执行普通聚合
    :param core: 提供 ``local_eval(collections, command)`` 的对象（缺省 core 单例）
    :param max_rows: 候选行数上限
    :return: 最终行（含 ``_score``）
    """
    if not callable(exec):
        raise TypeError('vector_scan.scan 需要 exec(candidate_command)（返回行数组）')
    if not is_vector_command(cmd):
        raise ValueError('vector_scan.scan 只接受首阶段为 $vectorSearch 的 aggregate 命令')
    pipeline = cmd['pipeline']
    spec = pipeline[0]['$vectorSearch']
    if not isinstance(spec, Mapping) or not isinstance(spec.get('path'), str) or spec['path'] == '':
        raise ValueError('vector_scan.scan: $vectorSearch 缺少 path（向量字段名）')

    # 1. 剥离 `$vectorSearch` 与紧随的 `$addFields._score`
    rest = list(pipeline[1:])
    if rest and _is_score_stage(rest[0]):
        rest.pop(0)
    candidate = _candidate_pipeline(spec.get('filter'), rest)

    # 2. 取候选（无 $limit；全字段 → 必然包含向量字段）
    rows = await exec({**cmd, 'pipeline': candidate})
    if not isinstance(rows, list):
        raise TypeError(f'vector_scan.scan: 候选阶段返回非数组（{type(rows).__name__}）')
    if len(rows) > max_rows:
        err = VectorScanOverflow(
            f'vectorScanOverflow：候选行数 {len(rows)} 超过上限 {max_rows}'
            '（禁止静默截断，请先缩小过滤条件）'
        )
        err.candidate_rows = len(rows)
        err.max_rows = max_rows
        raise err

    # 3. 精确打分：原始完整管道（`$vectorSearch` / `_score` / residual `$match` / `$limit`）
    out = _registry(core).local_eval(
        {cmd['collection']: rows},
        {'kind': 'aggregate', 'collection': cmd['collection'], 'pipeline': pipeline},
    )
    return out['result']


class VectorScanOverflow(RuntimeError):
    """候选行数超上限（稳定 ``code`` 供调用方识别；禁止静默截断）"""

    code = 'vectorScanOverflow'
    candidate_rows: int
    max_rows: int


def strip_vector_meta(cmd):
    """真实 Mongo 驱动执行前剥离 core 内部元数据键 ``$vectorSearch.metric``
    （Atlas 阶段只认官方键；local 求值仍需该键，故只作用于物理命令副本）。"""
    if not isinstance(cmd, Mapping) or not isinstance(cmd.get('pipeline'), list):
        return cmd
    for stage in cmd['pipeline']:
        if not isinstance(stage, Mapping):
            continue
        spec = stage.get('$vectorSearch')
        if isinstance(spec, MutableMapping):
            spec.pop('metric', None)
    return cmd


def is_unsupported_stage_error(err):
    """Mongo 驱动是否因「不认识 ``$vectorSearch`` 阶段」而报错。
    只认该形态（code 40324 或 Unrecognized pipeline stage），其余错误原样上抛。
    """
    if err is None:
        return False
    if getattr(err, 'code', None) == 40324:
        return True
    msg = str(err)
    return 'unrecognized pipeline stage' in msg.lower() and '$vectorSearch' in msg
