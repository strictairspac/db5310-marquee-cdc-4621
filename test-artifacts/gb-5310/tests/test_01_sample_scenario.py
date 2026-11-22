"""固定样例：灯窗市集开市前的完整操作剧本（BUSINESS.md 07:25–07:58）。"""

import unittest

from src.services import marquee_store as store
from src.services.domain import SPEC, solve

from support.store import make_engine, require_mysql_url


class SampleScenarioTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        require_mysql_url()
        cls.engine = make_engine()
        cls.report = solve(SPEC['sample'], cls.engine)
        cls.sections = {section['title']: section['rows'] for section in cls.report['sections']}

    def test_metrics_match_playbook(self):
        self.assertEqual(self.report['metrics'], SPEC['expect'])

    def test_final_offsets_and_blockers(self):
        rows = {row['分区流']: row for row in self.sections['消费位点与首个阻塞']}
        self.assertEqual(rows['NORTH']['消费位点'], 14)
        self.assertEqual(rows['NORTH']['续放状态'], '矛盾待核')
        self.assertEqual((rows['NORTH']['阻塞起号'], rows['NORTH']['阻塞止号']), (15, 15))
        self.assertEqual(rows['SOUTH']['消费位点'], 5)
        self.assertEqual(rows['SOUTH']['续放状态'], '缺号待补')
        self.assertEqual((rows['SOUTH']['阻塞起号'], rows['SOUTH']['阻塞止号']), (6, 6))
        self.assertEqual(rows['WEST']['消费位点'], 2)
        self.assertEqual(rows['WEST']['续放状态'], '已追平')

    def test_projection_holds_only_two_active_signs(self):
        projection = {(row['分区流'], row['牌面编号']): row for row in self.sections['牌面发布单']}
        self.assertEqual(set(projection), {('NORTH', 'N-07'), ('SOUTH', 'S-02')})
        self.assertEqual(projection[('NORTH', 'N-07')]['当前内容'], '桂花米酿｜18元')
        self.assertEqual(projection[('NORTH', 'N-07')]['来源序号'], 14)
        self.assertEqual(projection[('SOUTH', 'S-02')]['当前内容'], '烤梨｜14元')
        # N-08 已按序号 13 撤牌，W-03 已按序号 2 撤牌。
        self.assertNotIn(('NORTH', 'N-08'), projection)
        self.assertNotIn(('WEST', 'W-03'), projection)

    def test_journal_dispositions(self):
        journal = {row['接收行']: row for row in self.sections['事件接收与处置']}
        applied = {receipt for receipt, row in journal.items() if row['状态'] == '已提交'}
        duplicates = {receipt for receipt, row in journal.items() if row['状态'] == '重投忽略'}
        conflicts = {receipt for receipt, row in journal.items() if row['状态'] == '矛盾隔离'}
        pending = {receipt for receipt, row in journal.items() if row['状态'] == '暂缓'}
        self.assertEqual(applied, {1, 2, 4, 7, 10})
        self.assertEqual(duplicates, {3, 12})
        self.assertEqual(conflicts, {6, 8, 11})
        self.assertEqual(pending, {5, 9})
        # 重投始终引用首次接收行，且不再推进位点。
        self.assertIn('接收行 1', journal[3]['依据'])
        self.assertIn('接收行 1', journal[12]['依据'])
        # 矛盾两版互相引用，均保留可追溯证据。
        self.assertIn('接收行 8', journal[6]['依据'])
        self.assertIn('接收行 6', journal[8]['依据'])
        # 已提交版本保留，晚到矛盾单独隔离，牌面维持 14 元。
        self.assertIn('接收行 2', journal[11]['依据'])

    def test_batch_progress_is_per_stream_reconciliation(self):
        progress = self.sections['分批推进记录']
        first = {(row['分区流']): row for row in progress if row['到货批号'] == 1}
        # 第一批北区缺 13：暂留 12；其他分区照常提交。
        self.assertEqual(first['NORTH']['提交事件数'], 0)
        self.assertEqual(first['NORTH']['消费位点'], 12)
        self.assertEqual(first['SOUTH']['提交事件数'], 1)
        self.assertEqual(first['WEST']['提交事件数'], 1)
        second = {row['分区流']: row for row in progress if row['到货批号'] == 2}
        # 第二批补齐 13 后自动跨过缺口，连放 13、14，再停在矛盾序号 15。
        self.assertEqual(second['NORTH']['原位点'], 12)
        self.assertEqual(second['NORTH']['提交事件数'], 2)
        self.assertEqual(second['NORTH']['消费位点'], 14)
        # 南区缺 6：收到 7 也不推进，西区 2 正常撤牌。
        self.assertEqual(second['SOUTH']['提交事件数'], 0)
        self.assertEqual(second['WEST']['提交事件数'], 1)

    def test_replay_uses_session_temporary_tables_only(self):
        # 回放结束后临时表随会话销毁：另起连接看不到任何工作台数据。
        with self.engine.connect() as conn:
            for table in _TEMP_TABLES:
                with self.assertRaises(Exception):
                    conn.exec_driver_sql(f'SELECT COUNT(*) FROM {table}').scalar_one()


_TEMP_TABLES = tuple(store._TABLES)


if __name__ == '__main__':
    unittest.main()
