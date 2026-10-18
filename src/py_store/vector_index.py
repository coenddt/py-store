"""Atlas Search Index（向量索引）幂等创建通道。

只有 schema 索引项显式声明 ``options.indexName`` 才创建，避免对自建 mongod 盲调
``create_search_index`` / ``$vectorSearch``。连接解析复用 datasource 现有缓存
（``db_of_schema``），禁止在此新建 client。
"""

from . import datasource

_SIMILARITY = {'cosine': 'cosine', 'l2': 'euclidean', 'ip': 'dotProduct'}


def similarity_of(metric):
    """内部 metric → Atlas similarity（唯一映射点）"""
    return _SIMILARITY.get(metric or 'cosine')


def _is_integer(v):
    """等价 JS ``Number.isInteger``（排除 bool；整值 float 视为整数）"""
    if isinstance(v, bool):
        return False
    return isinstance(v, int) or (isinstance(v, float) and v.is_integer())


async def ensure_vector_indexes(store, opts=None) -> dict:
    """按 schema 中声明 options.indexName 的向量索引，幂等创建 Atlas Search Index"""
    created = []
    skipped = []
    for name in store.list():
        defn = store.get(name)
        if not defn:
            continue
        for ix in defn.get('indexes') or []:
            keys = (ix or {}).get('keys')
            if not keys:
                continue
            field = next((k for k, v in keys.items() if v == 'vector'), None)
            options = (ix or {}).get('options') or {}
            if not field or not options.get('indexName'):
                continue
            fdef = (defn.get('fields') or {}).get(field) or {}
            dim = fdef.get('dim')
            similarity = similarity_of(options.get('metric') or fdef.get('metric'))
            if not _is_integer(dim) or not similarity:
                raise ValueError(f'向量索引 {defn.get("name")}.{field} 的 dim/metric 非法')
            db = datasource.db_of_schema(name)
            if db is None:
                raise ValueError(
                    f'向量索引 {defn.get("name")}.{field} 声明了 options.indexName，'
                    '但 schema 不是 Mongo 源，无法创建 Atlas 索引')
            coll = db[defn.get('collection') or defn.get('name')]
            existing = await coll.list_search_indexes().to_list(length=None)
            if any(x.get('name') == options['indexName'] for x in existing):
                skipped.append(options['indexName'])
                continue
            await coll.create_search_index({
                'name': options['indexName'],
                'definition': {
                    'fields': [{
                        'type': 'vector',
                        'path': field,
                        'numDimensions': dim,
                        'similarity': similarity,
                    }],
                },
            })
            created.append(options['indexName'])
    return {'created': created, 'skipped': skipped}
