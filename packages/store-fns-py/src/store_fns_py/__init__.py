"""L2 计算列实现（Python）。导出形状：扁平字典 ``{ impl_name: impl(item, ctx) }``。

impl_name 用 snake_case（§6.1）；与 schema 逻辑 fnRef（默认 ``<name>.<key>``）
由宿主经 core::naming 归一后匹配。
"""


def order_total(item, ctx=None):
    return int(item.get('qty', 0)) * int(item.get('price', 0))


# A6：schema `fnRef="Order.total"` ⇒ 此处 `order_total` 归一后同为 [order, total]
FNS = {"order_total": order_total}
