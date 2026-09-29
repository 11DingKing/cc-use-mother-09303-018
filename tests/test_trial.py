"""试算稿与输入摘要的确定性测试。"""
from __future__ import annotations

from tests._helpers import DATA_V1, DATA_V2, OfficeCase


class TrialTest(OfficeCase):
    def test_trial_carries_input_summary_and_body(self) -> None:
        data_h, rule_h = self.seed_versions()
        trial = self.office.create_trial("年度合作成果报告", data_h, rule_h, "报告编制组")
        self.assertEqual(trial["status"], "试算")
        self.assertIsNone(trial["version_no"])
        summary = trial["input_summary"]
        self.assertEqual(summary["data"]["digest"], data_h)
        self.assertEqual(summary["rules"]["digest"], rule_h)
        # 指标由规则版本确定性算出：(1200-1000)/1000*100 = 20.0
        metrics = {m["id"]: m["value"] for m in summary["metrics"]}
        self.assertEqual(metrics["total_employment"], 1200)
        self.assertEqual(metrics["growth_rate"], 20.0)
        self.assertEqual(summary["body_digest"], trial["body_digest"])

    def test_same_versions_produce_same_body_digest(self) -> None:
        data_h, rule_h = self.seed_versions()
        a = self.office.create_trial("甲", data_h, rule_h, "编制组")
        b = self.office.create_trial("乙", data_h, rule_h, "编制组")
        self.assertEqual(a["body_digest"], b["body_digest"])
        self.assertEqual(a["input_summary"], b["input_summary"])

    def test_late_data_is_a_new_version_and_cannot_change_existing_trial(self) -> None:
        data_h, rule_h = self.seed_versions()
        trial = self.office.create_trial("年度合作成果报告", data_h, rule_h, "编制组")
        sealed_digest_before = trial["body_digest"]

        # 次日补到的数据登记为新版本
        late = self.office.register_data(DATA_V2, source_label="次日补录")
        self.assertNotEqual(late["digest"], data_h)

        # 原试算稿引用的数据摘要与正文均未变化（在线数字不会自动改）
        again = self.office.get_report(trial["id"])
        self.assertEqual(again["data_digest"], data_h)
        self.assertEqual(again["body_digest"], sealed_digest_before)

        # 用新数据生成的试算稿得到不同正文
        trial_v2 = self.office.create_trial("年度合作成果报告（含补录）", late["digest"], rule_h, "编制组")
        self.assertNotEqual(trial_v2["body_digest"], sealed_digest_before)
        metrics = {m["id"]: m["value"] for m in trial_v2["input_summary"]["metrics"]}
        self.assertEqual(metrics["total_employment"], 1320)

    def test_content_addressing_is_idempotent(self) -> None:
        first = self.office.register_data(DATA_V1)
        second = self.office.register_data(DATA_V1)
        self.assertEqual(first["digest"], second["digest"])
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])

    def test_idempotency_key_prevents_duplicate_trials(self) -> None:
        data_h, rule_h = self.seed_versions()
        a = self.office.create_trial("报告", data_h, rule_h, "编制组", idempotency_key="k-1")
        b = self.office.create_trial("报告", data_h, rule_h, "编制组", idempotency_key="k-1")
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(len(self.office.list_reports()), 1)

    def test_unknown_versions_rejected(self) -> None:
        with self.assertRaises(Exception):
            self.office.create_trial("报告", "deadbeef", "deadbeef", "编制组")
