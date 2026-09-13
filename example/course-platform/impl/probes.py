"""临时探针 schema —— 矩阵需要"额外模型"的条目（E-11/E-12/D-06）。

一律以 ``Probe`` 前缀，避免与主场景冲突；由 harness 在 register 全部 schema 后注册，
数据追加进 seed.json 对应批次。
"""

PROBES = [
    # E-11 关系目标 schema 不可读：ProbeHolder.grade → ProbeGrade（read=admin），student 不可见
    {
        'name': 'ProbeGrade', 'collection': 'probe_grades', 'idPrefix': 'g', 'timestamps': False,
        'read': ['admin'], 'write': ['admin'],
        'fields': {
            '_id': {'type': 'string'}, 'score': {'type': 'int'}, 'createdBy': {'type': 'string'}
        },
        'relations': {},
    },
    {
        'name': 'ProbeHolder', 'collection': 'probe_holders', 'idPrefix': 'h', 'timestamps': False,
        'read': ['student'], 'write': ['student'],
        'fields': {
            '_id': {'type': 'string'}, 'label': {'type': 'string'},
            'gradeId': {'type': 'string'}, 'createdBy': {'type': 'string'}
        },
        'relations': {
            'grade': {'model': 'ProbeGrade', 'type': 'one', 'localField': 'gradeId', 'foreignField': '_id'}
        },
    },
    # E-12 关系目标 owner 隔离：ProbeNote.memos → ProbeMemo（read=creator），ctx=u1 只挂自己的
    {
        'name': 'ProbeMemo', 'collection': 'probe_memos', 'idPrefix': 'm', 'timestamps': False,
        'read': ['creator'], 'write': ['creator'],
        'fields': {
            '_id': {'type': 'string'}, 'noteId': {'type': 'string'},
            'body': {'type': 'string'}, 'createdBy': {'type': 'string'}
        },
        'relations': {},
    },
    {
        'name': 'ProbeNote', 'collection': 'probe_notes', 'idPrefix': 'pn', 'timestamps': False,
        'read': ['student'], 'write': ['student'],
        'fields': {
            '_id': {'type': 'string'}, 'label': {'type': 'string'}, 'createdBy': {'type': 'string'}
        },
        'relations': {
            'memos': {'model': 'ProbeMemo', 'type': 'many', 'localField': '_id', 'foreignField': 'noteId'}
        },
    },
    # D-06 计算列 depends 不可读字段：student 请求 code 不得泄漏 secret
    {
        'name': 'ProbeCompute', 'collection': 'probe_computes', 'idPrefix': 'pc', 'timestamps': False,
        'read': ['student'], 'write': ['student'],
        'fields': {
            '_id': {'type': 'string'}, 'label': {'type': 'string'},
            'secret': {'type': 'string', 'read': ['admin']}, 'createdBy': {'type': 'string'}
        },
        'relations': {},
        'computes': {
            'code': {'type': 'string', 'fn': True, 'fnRef': 'course_revenue_via_secret', 'depends': ['secret']}
        },
    },
]