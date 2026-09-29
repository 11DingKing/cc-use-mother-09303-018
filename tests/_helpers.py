"""测试共用夹具与样例数据。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from seal_ledger.service import ReportOffice
from seal_ledger.store import Store

RULESET = {
    "rules": [
        {
            "id": "total_employment",
            "title": "合作项目就业总人数",
            "expr": {"path": "rows.employment"},
        },
        {
            "id": "growth_rate",
            "title": "同比增长率（百分数）",
            "expr": {
                "round": [
                    {
                        "*": [
                            {
                                "-": [
                                    {"path": "rows.employment"},
                                    {"path": "rows.baseline"},
                                ]
                            },
                            {"/": [100, {"path": "rows.baseline"}]},
                        ]
                    },
                    2,
                ]
            },
        },
    ]
}

DATA_V1 = {"rows": {"employment": 1200, "baseline": 1000}, "batch": "首批"}
# 次日补到的就业数据：人数更新为 1320
DATA_V2 = {"rows": {"employment": 1320, "baseline": 1000}, "batch": "补录批次"}


class OfficeCase(unittest.TestCase):
    def setUp(self) -> None:
        self.office = ReportOffice(Store(":memory:"))

    def seed_versions(self):
        data = self.office.register_data(DATA_V1, source_label="首批就业数据")
        rules = self.office.register_ruleset(RULESET, note="2026 版规则")
        return data["digest"], rules["digest"]

    def seal_a_report(self, title: str = "年度合作成果报告"):
        """走完整流程并返回封账稿信息。"""
        data_h, rule_h = self.seed_versions()
        trial = self.office.create_trial(title, data_h, rule_h, "报告编制组")
        self.office.submit_for_signing(trial["id"], "报告编制组")
        self.office.sign(trial["id"], "中方", "中方签署人")
        self.office.sign(trial["id"], "外方", "外方签署人")
        sealed = self.office.seal("报告编制组", trial["id"])
        return sealed, data_h, rule_h
