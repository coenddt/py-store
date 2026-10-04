# store-fns-py

L2 计算列实现包（Python）——语言级共享层。宿主通过 `core::naming` 归一算法把
schema 声明的逻辑 `fn_ref` 与本包导出的实现名匹配后绑定。

- 导出形状：扁平字典 `FNS = { impl_name: impl(item, ctx) }`。
- 命名约定：`impl_name` 用 snake_case；与逻辑 `fn_ref`（默认 `<schema.name>.<计算列key>`）
  由宿主归一后匹配，故实现名与声明名书写可不同（`Order.total` ↔ `order_total`）。
- 零运行时依赖：本包不 import core 或宿主，唯一耦合是「导出名遵循 Python 语言风格」。
- 独立版本：本包版本独立于 py-store 宿主版本。

规范正文（`ask_knowledge.md` 等）由 08 号分步回填。
