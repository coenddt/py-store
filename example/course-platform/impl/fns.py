"""fn / asyncFn 计算列实现；键 = schema.json / probes.py 里的 fnRef。

调用契约（对齐 py_store）：
  - fn       : `def fn(item) -> value`  单行回调，入参为当前文档 dict
  - asyncFn  : `async def fn(items, ctx) -> None`  Host 读路径尾处理，原地写结果数组
"""


def course_revenue(item):
    """D-01/D-04/D-09：revenue = price * enrolledCount"""
    return (item.get('price') or 0) * (item.get('enrolledCount') or 0)


def course_lesson_duration(item):
    """D-07：跨层级 depends（lessons{duration}）→ 累计课时时长"""
    lessons = item.get('lessons') or []
    return sum((l.get('duration') or 0) for l in lessons)


async def user_display_name(items, ctx):
    """D-02/D-05：User.displayName asyncFn 尾处理 =>
       若其结果随后被再次查询（lib 复用），名字保持稳定"""
    for it in items:
        it['displayName'] = f"{it.get('name')}#{it.get('_id')}"


def course_revenue_via_secret(item):
    """D-06 探针：depends 依赖不可读字段 `secret`（read=admin）"""
    return (item.get('secret') or '')[:4]


FNS = {
    'course_revenue': course_revenue,
    'course_lesson_duration': course_lesson_duration,
    'user_display_name': user_display_name,
    'course_revenue_via_secret': course_revenue_via_secret,
}