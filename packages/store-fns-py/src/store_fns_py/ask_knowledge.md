# store-fns-py — 计算列实现（L2 语言级共享层）

## 导出契约

唯一导出为扁平字典 `FNS`：`{ impl_name: impl(item, ctx) }`。

```python
def order_total(item, ctx=None):
    return int(item.get('qty', 0)) * int(item.get('price', 0))


FNS = {"order_total": order_total}
```

## 命名与匹配

- `impl_name` 用 snake_case。
- schema 侧逻辑 `fn_ref` 默认机械生成 `<schema.name>.<计算列key>`；显式 `fnRef` 可覆盖共享名。
- 宿主用 `core::naming` 归一算法把两侧名称归成 token 序列再比对，故
  `Order.total`、`orderTotal`、`order_total` 归一后等价。
- 归一后实现名重复 ⇒ 宿主显式抛 `ERR_FN_CONFLICT`（禁静默覆盖）。
- 声明了回调计算列却无任何匹配实现 ⇒ 宿主启动期抛 `ERR_FN_MISSING`。

## 依赖

零运行时依赖：不 import core 或宿主。规范正文由 08 号分步回填。
