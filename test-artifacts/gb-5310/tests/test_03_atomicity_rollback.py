"""批次原子性：位点 SQL 执行后、提交前注入故障，同批回滚，重试后再提交。"""

import unittest

from sqlalchemy import text

from src.services import marquee_store as store
from src.services.domain import SPEC, _apply_batch, solve

from support.store import events_by_batch, make_engine, require_mysql_url


class InjectedRollback:
    def __init__(self, batch, stream):
        self.batch = batch
        self.stream = stream
        self.fired = False

    def __call__(self, context):
        if context['batch'] == self.batch and context['stream'] == self.stream:
            self.fired = True
            raise RuntimeError('注入故障：位点已更新但事务尚未提交')


class BatchAtomicityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        require_mysql_url()
        cls.engine = make_engine()
        cls.batches = events_by_batch(SPEC['sample']['events'])

    def test_failed_batch_rolls_back_together_and_retry_commits(self):
        failure = InjectedRollback(batch=2, stream='NORTH')
        with store.isolated_replay(self.engine) as conn:
            store.initialize(conn, SPEC['sample']['checkpoints'], SPEC['sample']['snapshot'])
            _apply_batch(conn, 1, self.batches[1])

            with self.assertRaises(RuntimeError):
                _apply_batch(conn, 2, self.batches[2], failpoint=failure)
            self.assertTrue(failure.fired)
            # 故障发生在事务提交前：显式回滚，恢复连接后直接核对同一 MySQL 会话。
            conn.rollback()

            # 直接查询同一 MySQL 会话：第二批的投影、位点和处置记录全部回滚。
            offset = conn.execute(text(
                'SELECT seq FROM marquee_cdc_offsets WHERE stream = :stream'),
                {'stream': 'NORTH'}).scalar_one()
            self.assertEqual(offset, 12)
            west = conn.execute(text(
                'SELECT seq FROM marquee_cdc_offsets WHERE stream = :stream'),
                {'stream': 'WEST'}).scalar_one()
            self.assertEqual(west, 1)

            north07 = conn.execute(text(
                'SELECT content FROM marquee_cdc_projection WHERE stream=:s AND sign_id=:i'),
                {'s': 'NORTH', 'i': 'N-07'}).scalar_one()
            self.assertEqual(north07, '桂花米酿｜22元')
            n08 = conn.execute(text(
                'SELECT COUNT(*) FROM marquee_cdc_projection WHERE stream=:s AND sign_id=:i'),
                {'s': 'NORTH', 'i': 'N-08'}).scalar_one()
            self.assertEqual(n08, 1)
            w03 = conn.execute(text(
                'SELECT COUNT(*) FROM marquee_cdc_projection WHERE stream=:s AND sign_id=:i'),
                {'s': 'WEST', 'i': 'W-03'}).scalar_one()
            self.assertEqual(w03, 1)

            # 完全一致的重投（接收行 3）不进日志表，首批只留下 1、2、4 三行。
            receipts = conn.execute(text(
                'SELECT receipt, state FROM marquee_cdc_journal ORDER BY receipt')).all()
            self.assertEqual([row[0] for row in receipts], [1, 2, 4])
            states = dict(receipts)
            self.assertEqual(states[1], 'pending')
            self.assertEqual(states[2], 'applied')
            self.assertEqual(states[4], 'applied')

            # 重试第二批（不注入故障），再放第三批：回滚的记录重新登记并一起提交。
            conn.rollback()
            _apply_batch(conn, 2, self.batches[2])
            _apply_batch(conn, 3, self.batches[3])

            offsets = dict(conn.execute(text(
                'SELECT stream, seq FROM marquee_cdc_offsets')).all())
            self.assertEqual(offsets, {'NORTH': 14, 'SOUTH': 5, 'WEST': 2})
            journal_count = conn.execute(text(
                'SELECT COUNT(*) FROM marquee_cdc_journal')).scalar_one()
            self.assertEqual(journal_count, 10)
            applied = conn.execute(text(
                "SELECT COUNT(*) FROM marquee_cdc_journal WHERE state='applied'")).scalar_one()
            self.assertEqual(applied, 5)

    def test_replay_after_retry_matches_clean_run(self):
        # 故障与重试只发生在临时会话内；干净重放仍得到相同报告，记录不丢失。
        report = solve(SPEC['sample'], self.engine)
        self.assertEqual(report['metrics'], SPEC['expect'])


if __name__ == '__main__':
    unittest.main()
