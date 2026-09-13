"""驱动原始值归一（SQL 执行器共用）

绑定层（`rust-store-py`）只认 JSON 可表达的类型（``None``/``bool``/``int``/``float``/
``str``/``list``/``dict``），而 SQL 驱动对**精确数值列**返回 ``decimal.Decimal``：

- MySQL：``DECIMAL`` 列、以及 ``SUM``/``AVG`` 等聚合结果（即使底层是 INT）；
- PostgreSQL：``NUMERIC`` 列、``AVG(int)`` 等。

未归一 → 绑定层抛 ``TypeError``（用户可见的崩溃）。此处按 Mongo 语义归一为数值
（§9.7「数值归 double」）：整值 → ``int``（对齐 Mongo ``$sum`` 对 int 字段返回整数），
非整值 → ``float``（对齐 ``$avg`` 返回 double）。
"""

from decimal import Decimal


def normalize_value(v):
    """标量值归一（当前仅 ``Decimal`` 需要归一；其余原样透传）"""
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    return v


def normalize_rows(rows):
    """行集归一：逐行逐列应用 [`normalize_value`]"""
    if not rows:
        return rows
    return [{k: normalize_value(v) for k, v in row.items()} for row in rows]
