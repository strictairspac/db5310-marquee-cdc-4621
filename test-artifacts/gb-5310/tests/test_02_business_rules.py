"""断点续放口径的边界规则（BUSINESS.md「断点续放口径」「失败态和生产范围」）。"""

import unittest

from src.services.domain import solve, validate

from support.store import checkpoints, event, make_engine, require_mysql_url


def payload(events, streams=(('NORTH', 0),), snapshot=()):
    return {'checkpoints': checkpoints(*streams),
            'snapshot': [dict(stream=s, sign_id=sid, sequence=seq, content=text) for s, sid, seq, text in snapshot],
            'events': events}


class RuleEdgeCasesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        require_mysql_url()
        cls.engine = make_engine()

    def run_solve(self, data):
        report = solve(data, self.engine)
        rows = {section['title']: section['rows'] for section in report['sections']}
        return report['metrics'], rows

    def test_event_id_reused_across_streams_is_allowed(self):
        data = payload([
            event(1, 'NORTH', 1, 'N-01', '北区文案', event_id='E-SHARED'),
            event(1, 'SOUTH', 1, 'S-01', '南区文案', event_id='E-SHARED'),
        ], streams=(('NORTH', 0), ('SOUTH', 0)))
        metrics, rows = self.run_solve(data)
        self.assertEqual(metrics['已提交事件数'], 2)
        self.assertEqual(metrics['矛盾隔离事件数'], 0)
        signs = {(r['分区流'], r['牌面编号']) for r in rows['牌面发布单']}
        self.assertEqual(signs, {('NORTH', 'N-01'), ('SOUTH', 'S-01')})

    def test_identical_envelope_merges_into_first_receipt(self):
        envelope = event(1, 'NORTH', 1, 'N-01', '桂花米酿｜18元')
        data = payload([envelope, {**envelope, 'batch': 2}, {**envelope, 'batch': 3}])
        metrics, rows = self.run_solve(data)
        self.assertEqual(metrics['已提交事件数'], 1)
        self.assertEqual(metrics['重投忽略数'], 2)
        journal = rows['事件接收与处置']
        self.assertEqual(len(journal), 3)
        self.assertTrue(all('接收行 1' in row['依据'] for row in journal if row['状态'] == '重投忽略'))
        offset = next(row for row in rows['消费位点与首个阻塞'] if row['分区流'] == 'NORTH')
        self.assertEqual(offset['消费位点'], 1)

    def test_two_versions_of_same_sequence_both_quarantined(self):
        data = payload([
            event(1, 'NORTH', 1, 'N-09', '红糖糍粑｜15元'),
            event(1, 'NORTH', 1, 'N-09', '红糖糍粑｜17元'),
        ])
        metrics, rows = self.run_solve(data)
        self.assertEqual(metrics['矛盾隔离事件数'], 2)
        self.assertEqual(metrics['已提交事件数'], 0)
        self.assertEqual(rows['牌面发布单'], [])
        blocker = rows['消费位点与首个阻塞'][0]
        self.assertEqual(blocker['续放状态'], '矛盾待核')
        self.assertEqual(blocker['阻塞起号'], 1)
        self.assertEqual({row['状态'] for row in rows['事件接收与处置']}, {'矛盾隔离'})

    def test_gap_holds_offset_then_catches_up_automatically(self):
        data = payload([event(1, 'NORTH', 2, 'N-02', '先到的高序号'),
                        event(2, 'NORTH', 1, 'N-01', '补齐的低序号')])
        metrics, rows = self.run_solve(data)
        self.assertEqual(metrics['已提交事件数'], 2)
        blocker = rows['消费位点与首个阻塞'][0]
        self.assertEqual(blocker['消费位点'], 2)
        self.assertEqual(blocker['续放状态'], '已追平')
        progress = {(row['到货批号'], row['消费位点']) for row in rows['分批推进记录']}
        self.assertIn((1, 0), progress)
        self.assertIn((2, 2), progress)

    def test_delete_of_missing_sign_still_consumes_sequence(self):
        data = payload([event(1, 'NORTH', 1, 'N-09', '', operation='delete')])
        metrics, rows = self.run_solve(data)
        self.assertEqual(metrics['已提交事件数'], 1)
        self.assertEqual(rows['牌面发布单'], [])
        self.assertEqual(rows['消费位点与首个阻塞'][0]['消费位点'], 1)

    def test_late_conflict_keeps_committed_version(self):
        data = payload([event(1, 'NORTH', 1, 'S-02', '烤梨｜14元'),
                        event(2, 'NORTH', 1, 'S-02', '烤梨｜15元')])
        metrics, rows = self.run_solve(data)
        self.assertEqual(metrics['已提交事件数'], 1)
        self.assertEqual(metrics['矛盾隔离事件数'], 1)
        sign = rows['牌面发布单'][0]
        self.assertEqual(sign['当前内容'], '烤梨｜14元')
        self.assertEqual(rows['消费位点与首个阻塞'][0]['消费位点'], 1)

    def test_history_without_envelope_is_unverified(self):
        data = payload([event(1, 'NORTH', 3, 'N-03', '迟到的历史信封')], streams=(('NORTH', 5),))
        metrics, rows = self.run_solve(data)
        self.assertEqual(metrics['历史未核实数'], 1)
        self.assertEqual(metrics['已提交事件数'], 0)
        self.assertEqual(rows['事件接收与处置'][0]['状态'], '历史未核实')
        self.assertEqual(rows['消费位点与首个阻塞'][0]['消费位点'], 5)

    def test_partial_batch_commits_other_streams(self):
        data = payload([
            event(1, 'NORTH', 2, 'N-02', '北区缺号 1'),
            event(1, 'SOUTH', 1, 'S-01', '南区正常提交'),
        ], streams=(('NORTH', 0), ('SOUTH', 0)))
        metrics, rows = self.run_solve(data)
        self.assertEqual(metrics['已提交事件数'], 1)
        offsets = {row['分区流']: row for row in rows['消费位点与首个阻塞']}
        self.assertEqual(offsets['NORTH']['消费位点'], 0)
        self.assertEqual(offsets['NORTH']['续放状态'], '缺号待补')
        self.assertEqual(offsets['SOUTH']['消费位点'], 1)
        self.assertEqual(offsets['SOUTH']['续放状态'], '已追平')

    def test_same_event_id_with_new_envelope_in_one_stream_conflicts(self):
        data = payload([event(1, 'NORTH', 1, 'N-01', '第一版', event_id='E-X'),
                        event(2, 'NORTH', 2, 'N-02', '复用事件号的另一封信', event_id='E-X')])
        metrics, rows = self.run_solve(data)
        self.assertEqual(metrics['已提交事件数'], 1)
        self.assertEqual(metrics['矛盾隔离事件数'], 1)
        blocker = rows['消费位点与首个阻塞'][0]
        self.assertEqual(blocker['续放状态'], '矛盾待核')
        self.assertEqual(blocker['阻塞起号'], 2)

    def test_empty_delivery_leaves_streams_caught_up(self):
        data = payload([], streams=(('NORTH', 2),),
                       snapshot=[('NORTH', 'N-07', 2, '快照牌面')])
        metrics, rows = self.run_solve(data)
        self.assertEqual(metrics['已提交事件数'], 0)
        self.assertEqual(rows['消费位点与首个阻塞'][0]['续放状态'], '已追平')
        self.assertEqual(rows['牌面发布单'][0]['当前内容'], '快照牌面')


class ValidationMessageTest(unittest.TestCase):
    def _expect_line(self, data, fragment):
        with self.assertRaises(ValueError) as caught:
            validate(data)
        self.assertIn(fragment, str(caught.exception))

    def test_messages_carry_input_line_numbers(self):
        base = {'checkpoints': [{'stream': 'NORTH', 'sequence': 4}], 'snapshot': [], 'events': []}
        self._expect_line({**base, 'checkpoints': [
            {'stream': 'NORTH', 'sequence': 4}, {'stream': 'NORTH', 'sequence': 5}]}, '起始位点第 2 行')
        self._expect_line({**base, 'snapshot': [
            {'stream': 'NORTH', 'sign_id': 'N-1', 'sequence': 9, 'content': '超出位点'}]}, '牌面快照第 1 行')
        self._expect_line({**base, 'events': [
            {**event(1, 'SOUTH', 1, 'S-1', '未登记流')}]}, '到货事件第 1 行')
        self._expect_line({**base, 'events': [
            {**event(1, 'NORTH', 1, 'N-1', '撤牌带正文', operation='delete')}]}, '到货事件第 1 行')
        self._expect_line({**base, 'events': [
            {**event(1, 'NORTH', 1, 'bad sign', '非法标识')}]}, '到货事件第 1 行')

    def test_empty_checkpoints_rejected(self):
        with self.assertRaises(ValueError) as caught:
            validate({'checkpoints': [], 'snapshot': [], 'events': []})
        self.assertIn('起始位点', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
