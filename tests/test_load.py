"""目录语义装载（共享分层与多落点择优 06）：A2 目录即落点 / A1 同名主重复 /
A3 主从识别 / A11 PG 落点携带 schema / 连接配置与错误上浮。

覆盖 core 纯规划（``plan_load``）与宿主运行期入口（``load_defs``，含真实 IO +
``register_batch``）。须以 ``LOCAL_CORE=1`` 从相邻 rust-store 调试产物加载
（与其余宿主用例同口径）。对标 nodejs-store/tests/load.test.js（双端同名同义用例）。

运行：$env:LOCAL_CORE='1'; $env:PYTHONPATH='<repo>\\rust-store\\core-py\\dist';
     python -m pytest tests/test_load.py -q
"""

import json
import os
import shutil
import tempfile

import pytest

from py_store import load, schema

CFG = {
    'sources': {
        'mongoMain': {'kind': 'mongodb', 'databases': ['sales_db']},
        'pgMain': {'kind': 'pg', 'databases': ['analytics_db']},
    },
    'defs': ['schema'],
}


def f(rel, defn):
    return {'rel': rel, 'defn': defn}


def loc_of(items, name):
    """取某主定义（非 replica）的落点"""
    hit = next((it for it in items
                if it.get('defn') and it['defn'].get('name') == name
                and not it['defn'].get('replica')), None)
    return hit and hit['location']


def test_a2_directory_is_location_mongo_l1_pg_l2_l3_flattened():
    """A2 目录即落点：L1=database；PG L2=schema；L3+ 打平"""
    files = [
        f('sales_db/Order.json', {'name': 'Order', 'fields': {'_id': {'type': 'string'}}}),
        f('sales_db/inventory/Item.json', {'name': 'Item', 'fields': {'_id': {'type': 'string'}}}),
        f('analytics_db/app/Customer.json', {'name': 'Customer', 'fields': {'_id': {'type': 'string'}}}),
        f('analytics_db/app/report/Monthly.json', {'name': 'Monthly', 'fields': {'_id': {'type': 'string'}}}),
    ]
    items = load.plan_load(CFG, files)

    assert loc_of(items, 'Order') == {'source': 'mongoMain', 'database': 'sales_db', 'schema': None}
    # Mongo 不读 L2：inventory 目录被打平，归属仍是 sales_db
    assert loc_of(items, 'Item') == {'source': 'mongoMain', 'database': 'sales_db', 'schema': None}
    # PG 读 L2：app = schema
    assert loc_of(items, 'Customer') == {'source': 'pgMain', 'database': 'analytics_db', 'schema': 'app'}
    # PG L3 自由目录打平：schema 仍是 app
    assert loc_of(items, 'Monthly') == {'source': 'pgMain', 'database': 'analytics_db', 'schema': 'app'}


def test_a1_duplicate_primary_raises():
    """A1 同名主 ≥2 ⇒ ERR:LOAD 主定义重复"""
    files = [
        f('sales_db/Order.json', {'name': 'Order', 'fields': {'_id': {'type': 'string'}}}),
        f('sales_db/inventory/Order.json', {'name': 'Order', 'fields': {'_id': {'type': 'string'}}}),
    ]
    with pytest.raises(RuntimeError, match='ERR:LOAD 主定义重复'):
        load.plan_load(CFG, files)


def test_a3_primary_then_replica_order():
    """A3 主从识别：主 + replica ⇒ 主在前、其后从"""
    files = [
        f('analytics_db/Order.json', {'name': 'Order', 'replica': True}),
        f('sales_db/Order.json', {'name': 'Order', 'collection': 'order',
                                  'fields': {'_id': {'type': 'string'}}}),
    ]
    items = load.plan_load(CFG, files)
    assert len(items) == 2
    assert items[0]['defn'].get('replica') is None          # 主在前
    assert items[0]['location']['database'] == 'sales_db'
    assert items[1]['defn'].get('replica') is True           # 从在后
    assert items[1]['location']['database'] == 'analytics_db'

    schema.register_batch(items, None)
    assert schema.has('Order') is True


def test_a3_all_replica_missing_primary_raises():
    """A3 同名主 0 份（全 replica）⇒ ERR:LOAD 主定义缺失"""
    files = [f('sales_db/Order.json', {'name': 'Order', 'replica': True})]
    with pytest.raises(RuntimeError, match='ERR:LOAD 主定义缺失'):
        load.plan_load(CFG, files)


def test_undeclared_database_directory_raises():
    """库目录未声明 ⇒ ERR:LOAD 库目录未声明"""
    files = [f('other_db/Order.json', {'name': 'Order'})]
    with pytest.raises(RuntimeError, match='ERR:LOAD 库目录未声明'):
        load.plan_load(CFG, files)


def test_invalid_source_kind_raises():
    """连接 kind 非法 ⇒ ERR:LOAD kind 非法"""
    bad = {'sources': {'x': {'kind': 'oracle', 'databases': ['d']}}, 'defs': ['schema']}
    with pytest.raises(RuntimeError, match='ERR:LOAD 连接 x 的 kind 非法'):
        load.plan_load(bad, [])


def test_load_defs_real_dir_io_and_register_batch():
    """load_defs：真实目录 IO + register_batch（落点注入 core）"""
    root = tempfile.mkdtemp(prefix='store-load-')
    try:
        os.makedirs(os.path.join(root, 'schema', 'sales_db'))
        with open(os.path.join(root, 'schema', 'sales_db', 'Order.json'),
                  'w', encoding='utf-8') as fp:
            json.dump({'name': 'Order', 'collection': 'order',
                       'fields': {'_id': {'type': 'string'}}}, fp)
        with open(os.path.join(root, 'store.config.json'), 'w', encoding='utf-8') as fp:
            json.dump({'sources': {'mongoMain': {'kind': 'mongodb',
                                                 'databases': ['sales_db']}},
                       'defs': ['schema']}, fp)

        items = load.load_defs(os.path.join(root, 'store.config.json'))
        assert len(items) == 1
        assert items[0]['location'] == {'source': 'mongoMain', 'database': 'sales_db', 'schema': None}
        assert schema.has('Order') is True
    finally:
        shutil.rmtree(root, ignore_errors=True)
