"""向量能力探测（与 nodejs-store/src/capabilities.js 逐条同构）。"""

from __future__ import annotations

from typing import Any

from . import datasource, feedback, schema
from .core import native

DEFAULT_SOURCE = datasource.DEFAULT_SOURCE


def _backend_of(connection: Any) -> str:
    kind = datasource.kind_of(connection)
    return 'mongodb' if kind in (None, 'mongo') else kind


def _atlas_declared(source: str) -> bool:
    for name in schema.list():
        meta = schema.get(name)
        if (meta.get('datasource') or DEFAULT_SOURCE) != source:
            continue
        for ix in meta.get('indexes') or []:
            options = ix.get('options') or {}
            index_name = options.get('indexName')
            if isinstance(index_name, str) and index_name:
                return True
    return False


async def _pgvector_ready(source: str) -> bool:
    try:
        out = await datasource.execute_raw(
            source,
            "SELECT 1 AS present FROM pg_extension WHERE extname = 'vector'",
            None,
            False,
        )
        return bool(out.get('rows'))
    except Exception as e:  # noqa: BLE001 — 探测失败必须留痕，禁静默改判
        feedback.emit({
            'type': 'capability_probe_failed',
            'code': 'capabilityProbeFailed',
            'layer': 'host',
            'backend': 'postgres',
            'source': source,
            'message': f'探测 pgvector 扩展失败：{e}',
            'hint': '检查该数据源的连接权限；探测失败按「未安装」处理（不猜、不放行）',
        })
        return False


async def capabilities(source: str | None = None) -> dict[str, Any]:
    src = source or DEFAULT_SOURCE
    if not datasource.has_connection(src):
        feedback.emit({
            'type': 'capability_unknown_source',
            'code': 'capabilityUnknownSource',
            'layer': 'host',
            'source': src,
            'message': f'能力探测请求的数据源 {src} 未配置',
            'hint': '先经 init(connections) 注入该数据源，或改用已配置的 source',
        })
        return {
            'source': src,
            'vector': {'supported': False, 'native': False,
                       'backend': 'unknown', 'metrics': [], 'maxDim': 0},
        }

    backend = _backend_of(datasource.get_connection(src))
    native_ready = await _pgvector_ready(src) if backend == 'postgres' else False
    atlas_index = _atlas_declared(src) if backend == 'mongodb' else False

    return {
        'source': src,
        'vector': native.vector_capability(backend, native_ready, atlas_index),
    }
