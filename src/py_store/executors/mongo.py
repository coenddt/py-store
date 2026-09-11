"""Mongo 执行器（驱动：PyMongo async）

只做「Command JSON → 原生驱动调用」（铁律 1/3）：全部纯逻辑（GQL 解析、权限、
命令规划、结果后处理）都在 Rust core。对齐 ``nodejs-store/src/crud/exec.js#_execMongo``。
"""

from pymongo import ReturnDocument


async def exec_mongo(db, cmd):
    """Command JSON → PyMongo 驱动调用"""
    coll = db[cmd['collection']]
    kind = cmd['kind']

    if kind == 'find':
        cursor = coll.find(cmd['filter'], cmd.get('projection'))
        return await cursor.to_list(length=None)
    if kind == 'aggregate':
        # PyMongo async 下 ``aggregate`` 是协程（与 ``find`` 直接返回游标不同）
        cursor = await coll.aggregate(cmd['pipeline'])
        return await cursor.to_list(length=None)
    if kind == 'countDocuments':
        return await coll.count_documents(cmd['filter'])
    if kind == 'findOne':
        return await coll.find_one(cmd['filter'], cmd.get('projection'))
    if kind == 'insertOne':
        await coll.insert_one(cmd['doc'])
        return cmd['doc']
    if kind == 'insertMany':
        await coll.insert_many(cmd['docs'])
        return {'insertedCount': len(cmd['docs'])}
    if kind == 'findOneAndUpdate':
        options = dict(cmd.get('options') or {})
        rd = options.pop('returnDocument', 'after')
        options['return_document'] = (
            ReturnDocument.AFTER if rd == 'after' else ReturnDocument.BEFORE)
        return await coll.find_one_and_update(cmd['filter'], cmd['update'], **options)
    if kind == 'updateMany':
        return await coll.update_many(cmd['filter'], cmd['update'])
    if kind == 'deleteMany':
        return await coll.delete_many(cmd['filter'])

    raise RuntimeError(f'未支持的命令: {kind}')
