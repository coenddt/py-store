"""A3 残留（D8）：__feedback 落库 flush 与失败不静默 —— mock store，不连库。"""
import asyncio

from py_store import feedback


class _MockStore:
    def __init__(self):
        self.rows = []

    async def insert(self, name, row):
        self.rows.append((name, row))
        return row


class _BoomStore:
    async def insert(self, name, row):
        raise RuntimeError('boom')


def test_flush_persists_inflight():
    async def run():
        s = _MockStore()
        dispose = feedback.enable_feedback_table(s)
        feedback.emit({'type': 't', 'code': 'c', 'layer': 'host', 'message': 'm', 'hint': 'h'})
        await feedback.flush()
        assert len(s.rows) == 1 and s.rows[0][0] == '__feedback' and s.rows[0][1]['code'] == 'c'
        dispose()
    asyncio.run(run())


def test_flush_failure_not_silent():
    async def run():
        before = feedback.fail_count()
        dispose = feedback.enable_feedback_table(_BoomStore())
        feedback.emit({'type': 't', 'code': 'c', 'layer': 'host'})
        await feedback.flush()
        assert feedback.fail_count() == before + 1
        dispose()
    asyncio.run(run())
