"""重复执行：不新增收件、报告与校验码保持一致，并可导出。"""

import hashlib
import unittest

from src.services.domain import SPEC, solve
from src.utils.encoding import canonical

from support.store import make_engine, require_mysql_url


class ReplayIdempotencyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        require_mysql_url()
        cls.engine = make_engine()

    def test_repeated_runs_produce_identical_report_and_digest(self):
        first = solve(SPEC['sample'], self.engine)
        second = solve(SPEC['sample'], self.engine)
        third = solve(SPEC['sample'], self.engine)
        self.assertEqual(first, second)
        self.assertEqual(second, third)

        digest = hashlib.sha256(canonical(first)).hexdigest()
        self.assertEqual(hashlib.sha256(canonical(second)).hexdigest(), digest)
        self.assertRegex(digest, r'^[0-9a-f]{64}$')

        # 报告内含首次收件行、跨分区冲突和每批对账结果，支撑复核页展示与导出。
        titles = [section['title'] for section in first['sections']]
        self.assertEqual(titles, ['牌面发布单', '消费位点与首个阻塞', '分批推进记录', '事件接收与处置'])
        journal = next(section['rows'] for section in first['sections']
                       if section['title'] == '事件接收与处置')
        first_receipts = [row for row in journal if row['接收行'] == 1]
        self.assertEqual(len(first_receipts), 1)
        self.assertTrue(any('接收行' in row['依据'] for row in journal if row['状态'] == '矛盾隔离'))
        self.assertTrue(first['sections'][2]['rows'])

    def test_temporary_tables_do_not_leak_between_runs(self):
        solve(SPEC['sample'], self.engine)
        solve(SPEC['sample'], self.engine)
        with self.engine.connect() as conn:
            # 两次回放均已结束，会话级临时表随各自连接关闭而消失。
            for table in ('marquee_cdc_offsets', 'marquee_cdc_projection', 'marquee_cdc_journal'):
                with self.assertRaises(Exception):
                    conn.exec_driver_sql(f'SELECT 1 FROM {table}')


if __name__ == '__main__':
    unittest.main()
