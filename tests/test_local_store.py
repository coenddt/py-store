"""本地磁盘数据源（local）py 宿主单测

镜像 ``nodejs-store/tests/local-store.test.js``；覆盖执行文档 03 第 1 章 A1~A5
与步骤 2/3/4/5/6/7 的分层要点：
  - A1 ``connect`` → ``init`` → insert/query(one 关系)/count/update/remove(归档) 全通，落盘为 JSON 数组；
  - A3 ``store.transaction`` / ``async with store.session()`` 下 commit 可见 / rollback 不可见，且无 ``mongo_transaction_unsupported``；
  - A5 降级不静默：无 ``open_transaction`` → ``transaction_not_atomic``；声明 ``indexes`` → ``local_indexes_ignored``；
       两次 ``init`` 同目录幂等。

运行：PYTHONPATH=src LOCAL_CORE=1 python -m pytest tests/test_local_store.py -q
"""

import asyncio
import json

from py_store import datasource as _ds
from py_store import feedback, init, store
from py_store import schema as _sc
from py_store.local import LOCAL_KIND, connect
from py_store.local.handle import create_db
from py_store.local.store import read_snapshot, with_dir_lock, write_collections


def _run(coro):
    return asyncio.run(coro)


def _register_test_schemas():
    """conftest 模块夹具重放（清场后恢复本模块 schema）"""
    _sc.register({
        'name': 'ElUser', 'collection': 'el_users', 'idPrefix': 'eu_', 'timestamps': False,
        'fields': {'name': {'type': 'string'}}, 'relations': {},
    })
    _sc.register({
        'name': 'ElPost', 'collection': 'el_posts', 'idPrefix': 'ep_', 'timestamps': False,
        'fields': {'title': {'type': 'string'}, 'authorId': {'type': 'string'}},
        'relations': {
            'author': {'model': 'ElUser', 'type': 'one',
                       'localField': 'authorId', 'foreignField': '_id'},
        },
    })


# ─── 步骤 2：文件 IO ─────────────────────────────────────────

def test_store_missing_dir_empty(tmp_path):
    assert read_snapshot(tmp_path / 'not-created') == {}


def test_store_write_read_roundtrip(tmp_path):
    docs = [{'_id': 'u1', 'name': 'Ada'}, {'_id': 'u2', 'name': 'Bob'}]
    write_collections(tmp_path, ['users'], {'users': docs})
    assert read_snapshot(tmp_path) == {'users': docs}
    # 落盘文件确为可读 JSON 数组
    on_disk = json.loads((tmp_path / 'users.json').read_text(encoding='utf-8'))
    assert on_disk == docs


def test_store_only_rewrites_changed(tmp_path):
    write_collections(tmp_path, ['a', 'b'], {'a': [{'_id': 1}], 'b': [{'_id': 2}]})
    write_collections(tmp_path, ['a'], {'a': [{'_id': 1}, {'_id': 3}], 'b': [{'_id': 999}]})
    snap = read_snapshot(tmp_path)
    assert snap['a'] == [{'_id': 1}, {'_id': 3}]
    assert snap['b'] == [{'_id': 2}]  # 未在 changed 中的集合不应被回写


def test_store_non_array_raises(tmp_path):
    (tmp_path / 'users.json').write_text(json.dumps({'_id': 'u1'}), encoding='utf-8')
    try:
        read_snapshot(tmp_path)
        raise AssertionError('应抛错（非文档数组）')
    except ValueError as e:
        assert '文档数组' in str(e)


def test_store_bad_json_raises(tmp_path):
    (tmp_path / 'users.json').write_text('{ not json', encoding='utf-8')
    try:
        read_snapshot(tmp_path)
        raise AssertionError('应抛错（非法 JSON）')
    except ValueError:
        pass  # json.JSONDecodeError 是 ValueError 子类


def test_store_with_dir_lock_serializes(tmp_path):
    snap = {'users': [], 'posts': []}

    def _write(name):
        def _do():
            snap[name] = [*snap[name], {'_id': f'{name}-1'}]
            write_collections(tmp_path, [name], snap)
        with_dir_lock(tmp_path, _do)

    async def _main():
        await asyncio.gather(
            asyncio.to_thread(_write, 'users'),
            asyncio.to_thread(_write, 'posts'),
            asyncio.to_thread(_write, 'users'),
        )

    _run(_main())
    on_disk = read_snapshot(tmp_path)
    assert len(on_disk['users']) == 2, 'users 两次写应累加（串行、无丢失）'
    assert len(on_disk['posts']) == 1


# ─── 步骤 3：PyMongo Database 兼容手柄 ──────────────────────

def _mem_io(initial=None):
    """内存 io（驱动手柄，不经 store/落盘）"""
    box = {'snap': initial or {}}

    def _load():
        return box['snap']

    def _save(_changed, collections):
        box['snap'] = collections

    return box, _load, _save


def test_handle_method_set():
    box, load, save = _mem_io()
    users = create_db(load, save)['users']

    async def _main():
        # insertOne → result 为写入文档；save 已回写到快照
        ins = await users.insert_one({'_id': 'u1', 'name': 'Ada', 'age': 36})
        assert ins == {'_id': 'u1', 'name': 'Ada', 'age': 36}
        assert box['snap']['users'] == [{'_id': 'u1', 'name': 'Ada', 'age': 36}]

        # insertMany（普通）
        assert await users.insert_many([{'_id': 'u2', 'name': 'Bob', 'age': 20}]) == {'insertedCount': 1}

        # find → 游标（同步返回，await to_list）
        assert await users.find({'name': 'Ada'}).to_list() == [{'_id': 'u1', 'name': 'Ada', 'age': 36}]

        # find + projection
        assert await users.find({}, projection={'name': 1, '_id': 0}).to_list() == \
            [{'name': 'Ada'}, {'name': 'Bob'}]

        # findOne 命中 / 未命中
        assert (await users.find_one({'_id': 'u2'}))['name'] == 'Bob'
        assert await users.find_one({'_id': 'nope'}) is None

        # countDocuments
        assert await users.count_documents({'age': {'$gte': 20}}) == 2

        # updateMany / findOneAndUpdate（结果塑形为 PyMongo 等价对象）
        ur = await users.update_many({'name': 'Ada'}, {'$set': {'seen': True}})
        assert ur.modified_count == 1
        assert await users.find_one_and_update({'_id': 'u2'}, {'$set': {'seen': True}}) == \
            {'_id': 'u2', 'name': 'Bob', 'age': 20, 'seen': True}

        # deleteMany
        dr = await users.delete_many({'_id': 'u2'})
        assert dr.deleted_count == 1
        assert len(box['snap']['users']) == 1

        # listIndexes（local v1 空）/ createIndex（no-op）
        assert await users.list_indexes().to_list() == []
        await users.create_index({'name': 1})

    _run(_main())


def test_handle_explicit_null_three_state():
    # exec_mongo 会把 `field: null` 归一为 `{$eq:null,$exists:true}`（mongo.py §_explicit_null）
    _box, load, save = _mem_io({'docs': [{'_id': 1, 'x': None}, {'_id': 2}]})
    docs = create_db(load, save)['docs']

    async def _main():
        return await docs.find({'x': {'$eq': None, '$exists': True}}).to_list()

    assert _run(_main()) == [{'_id': 1, 'x': None}]


def test_handle_aggregate_lookup():
    _box, load, save = _mem_io({
        'probeUsers': [{'_id': 'pu1', 'name': 'Ada'}],
        'probePosts': [{'_id': 'pp1', 'title': 'P1', 'userId': 'pu1'}],
    })
    posts = create_db(load, save)['probePosts']

    async def _main():
        cur = await posts.aggregate([
            {'$match': {}},
            {'$lookup': {
                'as': 'author', 'from': 'probeUsers',
                'let': {'rel_userId': {'$ifNull': ['$userId', None]}},
                'pipeline': [
                    {'$match': {'$expr': {'$eq': ['$_id', '$$rel_userId']}}},
                    {'$project': {'_id': 1, 'name': 1}},
                ],
            }},
            {'$unwind': {'path': '$author', 'preserveNullAndEmptyArrays': True}},
            {'$project': {'_id': 1, 'author': 1, 'title': 1}},
        ])
        return await cur.to_list()

    assert _run(_main()) == [{'_id': 'pp1', 'author': {'_id': 'pu1', 'name': 'Ada'}, 'title': 'P1'}]


def test_handle_archive_upsert_by_id():
    box, load, save = _mem_io()
    deleted = create_db(load, save)['usersDeleted']

    async def _main():
        # 对应 exec_mongo 的 insertMany(upsertById) → 逐条 replaceOne(..., upsert=True)
        await deleted.replace_one({'_id': 'u1'}, {'_id': 'u1', 'deletedAt': 100}, upsert=True)
        await deleted.replace_one({'_id': 'u1'}, {'_id': 'u1', 'deletedAt': 200}, upsert=True)

    _run(_main())
    assert box['snap']['usersDeleted'] == [{'_id': 'u1', 'deletedAt': 200}], '按 _id 覆盖，幂等不重复'
    # 非 upsert 调用被拒（只有 upsertById 路径才会走到 replaceOne）
    try:
        _run(deleted.replace_one({'_id': 'x'}, {'_id': 'x'}))
        raise AssertionError('非 upsert 调用应抛错')
    except ValueError as e:
        assert '仅支持 upsert 语义' in str(e)


# ─── 步骤 4：门面 ───────────────────────────────────────────

def test_facade_connect_and_direct_io(tmp_path):
    write_collections(tmp_path, ['users'], {'users': [{'_id': 'u1', 'name': 'Ada'}]})

    conn = connect({'dir': str(tmp_path)})
    assert conn['kind'] == LOCAL_KIND
    assert conn['kind'] == 'local'
    assert conn['dir'] == str(tmp_path)
    assert callable(conn['handle'].__getitem__)
    assert callable(conn['open_transaction'])
    assert callable(conn['with_transaction'])

    async def _main():
        await conn['handle']['users'].insert_one({'_id': 'u2', 'name': 'Bob'})

    _run(_main())
    assert len(read_snapshot(tmp_path)['users']) == 2, '直连 ready 后即时落盘'


def test_facade_open_transaction_snapshot(tmp_path):
    write_collections(tmp_path, ['users'], {'users': [{'_id': 'u1'}]})
    conn = connect({'dir': str(tmp_path)})

    async def _main():
        # commit：tx 内写先入内存快照，commit 才落盘
        tx = await conn['open_transaction']()
        await tx['handle']['users'].insert_one({'_id': 'u2'})
        assert len(tx['session']['snapshot']()['users']) == 2, 'tx 内快照含新写'
        assert len(read_snapshot(tmp_path)['users']) == 1, 'commit 前磁盘不可见（快照隔离）'
        await tx['commit']()
        await tx['release']()
        assert len(read_snapshot(tmp_path)['users']) == 2, 'commit 后落盘可见'

        # rollback：丢弃
        tx2 = await conn['open_transaction']()
        await tx2['handle']['users'].insert_one({'_id': 'u3'})
        await tx2['rollback']()
        await tx2['release']()
        assert len(read_snapshot(tmp_path)['users']) == 2, 'rollback 后丢弃'

        # with_transaction 便捷包装
        async def _body(_s, t):
            await t['handle']['users'].insert_one({'_id': 'u4'})
            return 'ok'

        out = await conn['with_transaction'](_body)
        return out

    assert _run(_main()) == 'ok'
    assert len(read_snapshot(tmp_path)['users']) == 3


# ─── 步骤 5：datasource 接线 ────────────────────────────────

def test_datasource_bare_descriptor_and_tx(tmp_path):
    write_collections(tmp_path, ['users'], {'users': [{'_id': 'u1'}]})
    conn = connect({'dir': str(tmp_path)})
    events = []
    feedback.set_sink(lambda e: events.append(e))
    try:
        assert _ds.LOCAL_KIND == 'local'
        assert _ds.is_local(conn) is True
        _ds.set_connections(conn)  # 裸描述符 → {default: conn}
        assert _ds.get_connection('default') is conn

        async def _main():
            async def _body():
                view = _ds.connection_for('default')
                assert view['kind'] == 'local'
                assert callable(view['handle'].__getitem__), '事务视图须携带 handle'
                await view['handle']['users'].insert_one({'_id': 'u2'})
                assert len(view['session']['snapshot']()['users']) == 2, 'tx 内快照含新写'
                assert len(read_snapshot(tmp_path)['users']) == 1, 'commit 前磁盘不可见'

            await _ds.run_in_transaction('default', _body)

        _run(_main())
        assert len(read_snapshot(tmp_path)['users']) == 2, 'commit 后落盘可见'
    finally:
        feedback.set_sink(None)
        _ds.set_connections({})
    assert not any(e.get('code') == 'mongoTransactionUnsupported' for e in events), \
        'local 源不得出现 mongo 事务告警'


def test_datasource_no_with_transaction_warns():
    events = []
    feedback.set_sink(lambda e: events.append(e))
    try:
        _ds.set_connections({'default': {'kind': 'local'}})  # 缺 open_transaction/with_transaction

        async def _body():
            return 'ok'

        ran = _run(_ds.run_in_transaction('default', _body))
        assert ran == 'ok', '降级后按原样执行'
    finally:
        feedback.set_sink(None)
        _ds.set_connections({})
    assert any(e.get('code') == 'transactionNotAtomic' for e in events), \
        '应声明 transaction_not_atomic'


# ─── 步骤 6：命令路由接线（store 门面端到端） ───────────────

def test_exec_store_e2e(tmp_path):
    try:
        _run(init(connect({'dir': str(tmp_path)})))

        async def _main():
            u = await store.insert('ElUser', {'name': 'Ada'})
            p = await store.insert('ElPost', {'title': 'P1', 'authorId': u['_id']})
            assert str(u['_id']).startswith('eu_'), '应生成 idPrefix 前缀 _id'

            # 落盘为物理名（camelCase）文件、内容为文档数组
            assert len(read_snapshot(tmp_path)['elUsers']) == 1
            assert len(read_snapshot(tmp_path)['elPosts']) == 1

            # query（含 one 关系 → 对象）
            rows = await store.query('ElPost{ title author { name } }')
            assert len(rows) == 1
            assert rows[0]['title'] == 'P1'
            assert rows[0]['author']['name'] == 'Ada'

            # count
            assert await store.count('ElPost', {}) == 1

            # update（回读被更新文档）
            out = await store.update('ElPost', {'_id': p['_id']}, {'title': 'P2'})
            assert out['title'] == 'P2'
            assert read_snapshot(tmp_path)['elPosts'][0]['title'] == 'P2'

            # remove → 归档集合 ElPostDeleted 出现（文件 elPostsDeleted.json）
            r = await store.remove('ElPost', {'_id': p['_id']})
            assert r['deletedCount'] == 1
            assert r['archivedCount'] == 1
            assert read_snapshot(tmp_path)['elPosts'] == []
            assert len(read_snapshot(tmp_path)['elPostsDeleted']) == 1

        _run(_main())
    finally:
        _ds.set_connections({})


# ─── 步骤 7：索引声明告警 + 模块导出 ───────────────────────

def test_index_declares_indexes_warns(tmp_path):
    import py_store
    assert callable(py_store.local.connect), '__init__.py 应导出 local 模块'

    _sc.register({
        'name': 'ElIdx', 'collection': 'el_idx', 'idPrefix': 'ei_', 'timestamps': False,
        'fields': {'name': {'type': 'string'}}, 'relations': {},
        'indexes': [{'keys': {'name': 1}}],
    })

    events = []
    feedback.set_sink(lambda e: events.append(e))
    try:
        _run(init(connect({'dir': str(tmp_path)})))
        assert any(e.get('code') == 'localIndexesIgnored' for e in events), \
            'local schema 声明 indexes 应发 localIndexesIgnored'

        async def _main():
            # 不建索引、不抛错：集合仍可正常读写
            await store.insert('ElIdx', {'name': 'x'})

        _run(_main())
        assert len(read_snapshot(tmp_path)['elIdx']) == 1
    finally:
        feedback.set_sink(None)
        _ds.set_connections({})


# ─── A1 / A3 / A5 端到端补齐 ───────────────────────────────

def test_a1_relation_returns_object(tmp_path):
    try:
        _run(init(connect({'dir': str(tmp_path)})))

        async def _main():
            u = await store.insert('ElUser', {'name': 'Rel'})
            await store.insert('ElPost', {'title': 'T', 'authorId': u['_id']})
            rows = await store.query('ElPost{ author { _id name } }')
            assert rows[0]['author'] == {'_id': u['_id'], 'name': 'Rel'}

        _run(_main())
    finally:
        _ds.set_connections({})


def test_a3_transaction_commit_and_rollback(tmp_path):
    events = []
    feedback.set_sink(lambda e: events.append(e))
    try:
        _run(init(connect({'dir': str(tmp_path)})))

        async def _body():
            await store.insert('ElUser', {'name': 'TxAda'})

        _run(store.transaction('default', _body))
        assert len(read_snapshot(tmp_path)['elUsers']) == 1, 'commit 后落盘可见'

        async def _boom():
            await store.insert('ElUser', {'name': 'TxBob'})
            raise RuntimeError('boom')

        try:
            _run(store.transaction('default', _boom))
            raise AssertionError('应上抛 boom')
        except RuntimeError as e:
            assert 'boom' in str(e)
        assert len(read_snapshot(tmp_path)['elUsers']) == 1, 'rollback 后不落盘'
    finally:
        feedback.set_sink(None)
        _ds.set_connections({})
    assert not any(e.get('code') == 'mongoTransactionUnsupported' for e in events), \
        'local 源事务不应出现 mongo 事务告警'


def test_a3_session_commits_visibly(tmp_path):
    try:
        _run(init(connect({'dir': str(tmp_path)})))

        async def _main():
            async with store.session() as s:
                await s.insert('ElUser', {'name': 'S1'})
                await s.insert('ElUser', {'name': 'S2'})

        _run(_main())
        assert len(read_snapshot(tmp_path)['elUsers']) == 2, '会话提交后两条写可见'
    finally:
        _ds.set_connections({})


def test_a5_init_idempotent(tmp_path):
    try:
        _run(init(connect({'dir': str(tmp_path)})))

        async def _ins():
            await store.insert('ElUser', {'name': 'Keep'})

        _run(_ins())
        before = len(read_snapshot(tmp_path)['elUsers'])

        _run(init(connect({'dir': str(tmp_path)})))  # 二次 init：同一目录
        assert len(read_snapshot(tmp_path)['elUsers']) == before, '二次 init 不应清空/重复'

        async def _count():
            return await store.count('ElUser', {})

        assert _run(_count()) == before
    finally:
        _ds.set_connections({})
