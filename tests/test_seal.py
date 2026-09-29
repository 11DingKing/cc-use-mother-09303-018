"""会签与封账测试。"""
from __future__ import annotations

from tests._helpers import OfficeCase
from seal_ledger.errors import ConflictError


class SealTest(OfficeCase):
    def _prepare_signing(self):
        data_h, rule_h = self.seed_versions()
        trial = self.office.create_trial("年度合作成果报告", data_h, rule_h, "编制组")
        self.office.submit_for_signing(trial["id"], "编制组")
        return trial

    def test_full_seal_flow(self) -> None:
        sealed, _, _ = self.seal_a_report()
        self.assertEqual(sealed["status"], "已封账")
        self.assertEqual(sealed["version_no"], 1)
        self.assertIsNotNone(sealed["sealed_at"])
        parties = {s["party"] for s in sealed["signatures"]}
        self.assertEqual(parties, {"中方", "外方"})

    def test_cannot_seal_without_both_parties(self) -> None:
        trial = self._prepare_signing()
        self.office.sign(trial["id"], "中方", "中方签署人")
        with self.assertRaisesRegex(ConflictError, "外方"):
            self.office.seal("编制组", trial["id"])
        # 封账失败后状态仍是会签，可补签后重试
        self.assertEqual(self.office.get_report(trial["id"])["status"], "会签")

    def test_cannot_sign_twice_as_different_signer_for_same_party(self) -> None:
        trial = self._prepare_signing()
        self.office.sign(trial["id"], "中方", "甲")
        result = self.office.sign(trial["id"], "中方", "乙")  # 幂等忽略
        chinese = [s for s in result["signatures"] if s["party"] == "中方"]
        self.assertEqual(len(chinese), 1)
        self.assertEqual(chinese[0]["signer"], "甲")

    def test_sealed_content_is_immutable(self) -> None:
        sealed, data_h, rule_h = self.seal_a_report()
        digest_before = sealed["body_digest"]
        # 封账后再次调用封账直接冲突
        with self.assertRaises(ConflictError):
            self.office.seal("编制组", sealed["id"])
        # 任何读取结果都保持原摘要
        self.assertEqual(self.office.get_report(sealed["id"])["body_digest"], digest_before)

    def test_second_seal_gets_next_sequential_version(self) -> None:
        first, data_h, rule_h = self.seal_a_report("报告一")
        trial2 = self.office.create_trial("报告二", data_h, rule_h, "编制组")
        self.office.submit_for_signing(trial2["id"], "编制组")
        self.office.sign(trial2["id"], "中方", "中")
        self.office.sign(trial2["id"], "外方", "外")
        second = self.office.seal("编制组", trial2["id"])
        self.assertEqual(first["version_no"], 1)
        self.assertEqual(second["version_no"], 2)

    def test_state_machine_guards(self) -> None:
        data_h, rule_h = self.seed_versions()
        trial = self.office.create_trial("报告", data_h, rule_h, "编制组")
        # 试算稿不能直接签署或封账
        with self.assertRaises(ConflictError):
            self.office.sign(trial["id"], "中方", "中")
        with self.assertRaises(ConflictError):
            self.office.seal("编制组", trial["id"])
