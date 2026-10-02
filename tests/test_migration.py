"""声明式 schema 迁移单测（阶段 4）：diff 白名单 / 破坏性拒绝 / per-dialect 生成 / 双宿主 parity。

parity 锚：nodejs-store/tests/migration.test.js 用同一组输入断言相同输出（逐字节一致）。
运行：$env:LOCAL_CORE='1'; $env:PYTHONPATH='py-store/src'; python -m pytest py-store/tests/test_migration.py -q
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from py_store import ddl

OLD = {
    'name': 'Order', 'collection': 'orders', 'idPrefix': 'o', 'timestamps': False,
    'fields': {'_id': {'type': 'string'}, 'orderNo': {'type': 'string'},
               'amount': {'type': 'int'}},
    'indexes': [{'keys': {'orderNo': 1}, 'unique': True}],
}
NEW = {
    'name': 'Order', 'collection': 'orders', 'idPrefix': 'o', 'timestamps': False,
    'fields': {'_id': {'type': 'string'}, 'orderNo': {'type': 'string'},
               'amount': {'type': 'float'},
               'channel': {'type': 'string', 'default': 'web'},
               'count': {'type': 'int'}},
    'indexes': [{'keys': {'orderNo': 1}, 'unique': True},
                {'keys': {'channel': 1}}],
}


def test_diff_whitelist():
    plan = ddl.diff_defs(OLD, NEW)
    ops = [(c['op'], c.get('name')) for c in plan['changes']]
    assert plan['errors'] == []
    assert ('addColumn', 'channel') in ops
    assert ('addColumn', 'count') in ops
    assert ('widenColumn', 'amount') in ops
    assert any(o == 'addIndex' for o, _ in ops)
    # 恒等（_id / orderNo / 既有 unique 索引）不上报
    assert ('addColumn', '_id') not in ops and ('addColumn', 'orderNo') not in ops


def test_diff_destructive_rejected():
    dropped = {'name': 'Order', 'collection': 'orders', 'idPrefix': 'o',
               'fields': {'_id': {'type': 'string'}, 'orderNo': {'type': 'string'}}}
    plan = ddl.diff_defs(OLD, dropped)
    assert plan['changes'] == []
    assert any('删除字段' in e for e in plan['errors'])

    narrowed = {'name': 'Order', 'collection': 'orders', 'idPrefix': 'o',
                'fields': {'_id': {'type': 'string'}, 'orderNo': {'type': 'string'},
                           'amount': {'type': 'int'}}}
    plan = ddl.diff_defs(NEW, narrowed)  # float → int 收窄
    assert plan['changes'] == []
    assert any('非放宽' in e for e in plan['errors'])

    renamed_coll = dict(NEW, collection='orders_v2')
    plan = ddl.diff_defs(OLD, renamed_coll)
    assert any('collection 改名' in e for e in plan['errors'])

    id_changed = {'name': 'Order', 'collection': 'orders', 'idPrefix': '',
                  'fields': {'_id': {'type': 'int', 'strategy': 'autoincrement'},
                             'orderNo': {'type': 'string'}, 'amount': {'type': 'int'}},
                  'indexes': OLD['indexes']}
    plan = ddl.diff_defs(OLD, id_changed)
    assert plan['changes'] == []
    assert len(plan['errors']) == 2  # 类型 + 策略


def test_generate_add_column_with_archive_follow():
    only_add = {'name': 'Order', 'collection': 'orders', 'idPrefix': 'o',
                'fields': dict(OLD['fields'], channel={'type': 'string', 'default': 'web'}),
                'indexes': OLD['indexes']}
    stmts = ddl.generate_migration('sqlite', OLD, only_add)
    assert stmts == [
        'ALTER TABLE "orders" ADD COLUMN "channel" TEXT DEFAULT \'web\'',
        'ALTER TABLE "orders_deleted" ADD COLUMN "channel" TEXT DEFAULT \'web\'',
    ]


def test_generate_widen_per_dialect():
    only_widen = {'name': 'Order', 'collection': 'orders', 'idPrefix': 'o',
                  'fields': dict(OLD['fields'], amount={'type': 'float'}),
                  'indexes': OLD['indexes']}
    assert ddl.generate_migration('mysql', OLD, only_widen) == [
        'ALTER TABLE `orders` MODIFY COLUMN `amount` DOUBLE']
    assert ddl.generate_migration('postgres', OLD, only_widen) == [
        'ALTER TABLE "orders" ALTER COLUMN "amount" TYPE DOUBLE PRECISION '
        'USING "amount"::DOUBLE PRECISION']
    with pytest.raises(ValueError, match='MIGRATION_UNSUPPORTED'):
        ddl.generate_migration('sqlite', OLD, only_widen)


def test_generate_add_table_with_archive():
    fresh = {'name': 'Mig', 'collection': 'mig_t', 'idPrefix': 'm', 'timestamps': False,
             'fields': {'_id': {'type': 'int', 'strategy': 'autoincrement'},
                        'n': {'type': 'string'}},
             'relations': {}, 'computes': {},
             'indexes': [{'keys': {'n': 1}, 'unique': True}]}
    stmts = ddl.generate_migration('sqlite', None, fresh)
    assert len(stmts) == 4  # 主表 + 主索引 + 归档表 + 归档索引
    assert 'CREATE TABLE "mig_t" (' in stmts[0]
    assert 'INTEGER PRIMARY KEY AUTOINCREMENT' in stmts[0]
    assert 'CREATE UNIQUE INDEX "idx_mig_t_n" ON "mig_t"' in stmts[1]
    assert 'CREATE TABLE "mig_t_deleted" (' in stmts[2]
    # 归档表剔除自增策略（显式拷贝源 _id）
    assert 'AUTOINCREMENT' not in stmts[2]


def test_generate_bad_backend():
    with pytest.raises(ValueError, match='不支持的后端'):
        ddl.generate_migration('mongodb', OLD, NEW)


def test_parity_anchor():
    """双宿主 parity 锚：nodejs-store/tests/migration.test.js 的同输入输出必须逐字节一致。"""
    # sqlite 遇混合变更中的 widen → 整体显式拒绝（不产半截语句）
    with pytest.raises(ValueError, match='MIGRATION_UNSUPPORTED'):
        ddl.generate_migration('sqlite', OLD, NEW)
    # mysql（支持 widen）作为 parity 锚
    assert ddl.generate_migration('mysql', OLD, NEW) == [
        "ALTER TABLE `orders` ADD COLUMN `channel` VARCHAR(255) DEFAULT 'web'",
        "ALTER TABLE `orders_deleted` ADD COLUMN `channel` VARCHAR(255) DEFAULT 'web'",
        'ALTER TABLE `orders` ADD COLUMN `count` INT',
        'ALTER TABLE `orders_deleted` ADD COLUMN `count` INT',
        'ALTER TABLE `orders` MODIFY COLUMN `amount` DOUBLE',
        'CREATE INDEX `idx_orders_channel` ON `orders` (`channel` ASC)',
    ]
