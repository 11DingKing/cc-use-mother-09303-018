"""重开批准与更正单测试。"""
from __future__ import annotations

from tests._helpers import DATA_V2, OfficeCase
from seal_ledger.errors import ConflictError, ValidationError


class ReopenTest(OfficeCase):
    def test_reopen_requires_independent_approval(self) -> None:
        sealed, _, _ = self.seal_a_report()
        # 申请人不能批准自己的请求
        with self.assertRaisesRegex(ValidationError, "独立批准"):
            self.office.request_reopen(sealed["id"], "编制组", "编制组", "补录数据")
        # 签署人不能当批准人
        with self.assertRaisesRegex(ValidationError, "签署人"):
            self.office.request_reopen(sealed["id"], "编制组", "中方签署人", "补录数据")

        approval = self.office.request_reopen(sealed["id"], "编制组", "审计人员", "次日补到就业数据")
        self.assertEqual(approval["status"], "pending")

        # 非登记批准人不能决定
        with self.assertRaises(ValidationError):
            self.office.decide_reopen(approval["id"], "其他人", True)
        decided = self.office.decide_reopen(approval["id"], "审计人员", True)
        self.assertEqual(decided["status"], "approved")

        # 已处理的申请不能重复决定
        with self.assertRaises(ConflictError):
            self.office.decide_reopen(approval["id"], "审计人员", True)

    def test_rejected_request_blocks_correction(self) -> None:
        sealed, _, _ = self.seal_a_report()
        approval = self.office.request_reopen(sealed["id"], "编制组", "审计人员", "存疑")
        self.office.decide_reopen(approval["id"], "审计人员", False)
        with self.assertRaises(ConflictError):
            self.office.create_correction(approval["id"], "更正稿", "编制组")

    def test_duplicate_open_request_rejected(self) -> None:
        sealed, _, _ = self.seal_a_report()
        self.office.request_reopen(sealed["id"], "编制组", "审计人员", "理由一")
        with self.assertRaises(ConflictError):
            self.office.request_reopen(sealed["id"], "编制组", "审计人员二", "理由二")

    def test_correction_flow_with_late_data(self) -> None:
        sealed, data_h, rule_h = self.seal_a_report()
        original_digest = sealed["body_digest"]

        late = self.office.register_data(DATA_V2, source_label="次日补录")
        approval = self.office.request_reopen(sealed["id"], "编制组", "审计人员", "次日补到就业数据")
        self.office.decide_reopen(approval["id"], "审计人员", True)

        # 创建继任试算稿前，原报告保持已封账
        self.assertEqual(self.office.get_report(sealed["id"])["status"], "已封账")

        correction = self.office.create_correction(
            approval["id"], "年度合作成果报告（更正版）", "编制组", data_digest=late["digest"]
        )
        self.assertEqual(correction["status"], "试算")
        self.assertEqual(correction["parent_report_id"], sealed["id"])
        self.assertNotEqual(correction["body_digest"], original_digest)

        # 继任稿会签封账期间，原报告仍已封账，正式版本号未变
        self.office.submit_for_signing(correction["id"], "编制组")
        self.office.sign(correction["id"], "中方", "中方签署人")
        self.office.sign(correction["id"], "外方", "外方签署人")
        sealed_v2 = self.office.seal("编制组", correction["id"])

        self.assertEqual(sealed_v2["version_no"], 2)
        old = self.office.get_report(sealed["id"])
        self.assertEqual(old["status"], "已更正")
        self.assertEqual(old["superseded_by"], sealed_v2["id"])
        # 旧版本正文摘要逐字节保留，仍可核对
        self.assertEqual(old["body_digest"], original_digest)

        # 只有两个正式版本，版本链唯一
        versions = [r for r in self.office.list_reports() if r["version_no"] is not None]
        self.assertEqual([r["version_no"] for r in versions], [1, 2])

    def test_one_approval_creates_only_one_successor(self) -> None:
        sealed, _, _ = self.seal_a_report()
        approval = self.office.request_reopen(sealed["id"], "编制组", "审计人员", "补录")
        self.office.decide_reopen(approval["id"], "审计人员", True)
        self.office.create_correction(approval["id"], "更正稿", "编制组")
        with self.assertRaises(ConflictError):
            self.office.create_correction(approval["id"], "更正稿二", "编制组")
