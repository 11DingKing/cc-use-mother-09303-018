"""分块导出、断点续传与重复下载测试。"""
from __future__ import annotations

import hashlib

from seal_ledger.service import CHUNK_SIZE
from tests._helpers import DATA_V1, RULESET, OfficeCase
from seal_ledger.errors import ConflictError


class ExportTest(OfficeCase):
    def _seal_big(self):
        # 用长附注指标构造跨越多个分块的正文
        text_data = {"rows": {"employment": 1200, "baseline": 1000,
                              "pad": "合" * (CHUNK_SIZE * 3 // 2)}}
        data_h = self.office.register_data(text_data, "大正文")["digest"]
        spec = {
            "rules": [
                {"id": "total_employment", "title": "就业", "expr": {"path": "rows.employment"}},
                {"id": "pad", "title": "附注", "expr": {"path": "rows.pad"}},
            ]
        }
        rule_h = self.office.register_ruleset(spec)["digest"]
        trial = self.office.create_trial("大报告", data_h, rule_h, "编制组")
        self.office.submit_for_signing(trial["id"], "编制组")
        self.office.sign(trial["id"], "中方", "中")
        self.office.sign(trial["id"], "外方", "外")
        return self.office.seal("编制组", trial["id"])

    def test_manifest_chunks_cover_body(self) -> None:
        sealed = self._seal_big()
        manifest = self.office.get_manifest(sealed["id"])
        self.assertGreaterEqual(len(manifest["chunks"]), 2)
        self.assertEqual(sum(c["byte_length"] for c in manifest["chunks"]),
                         manifest["body_byte_length"])
        # 清单摘要自洽
        self.assertEqual(manifest["body_digest"], sealed["body_digest"])

    def test_resume_and_repeated_download_are_idempotent(self) -> None:
        sealed = self._seal_big()
        manifest = self.office.get_manifest(sealed["id"])
        client = "client-A"

        # 模拟中断：只下了第 0 块
        first = self.office.download_chunk(sealed["id"], 0, client)
        self.assertTrue(first["first_download"])
        status = self.office.download_status(sealed["id"], client)
        self.assertEqual(status["downloaded"], [0])

        # 重复下载同一块：幂等留痕，字节与摘要完全一致
        repeated = self.office.download_chunk(sealed["id"], 0, client)
        self.assertFalse(repeated["first_download"])
        self.assertEqual(repeated["data"], first["data"])
        self.assertEqual(repeated["digest"], first["digest"])

        # 续传补齐剩余分块
        for c in manifest["chunks"][1:]:
            self.office.download_chunk(sealed["id"], c["index"], client)
        status = self.office.download_status(sealed["id"], client)
        self.assertEqual(status["downloaded"], [c["index"] for c in manifest["chunks"]])

        # 分块拼回应与正文整体摘要一致（外方下载件 == 本地存档）
        assembled = b"".join(
            self.office.download_chunk(sealed["id"], c["index"], client)["data"]
            for c in manifest["chunks"]
        )
        self.assertEqual(hashlib.sha256(assembled).hexdigest(), manifest["body_digest"])
        # 每块摘要与清单一致
        for c in manifest["chunks"]:
            got = self.office.download_chunk(sealed["id"], c["index"], client)
            self.assertEqual(got["digest"], c["digest"])

    def test_downloads_never_change_report(self) -> None:
        sealed = self._seal_big()
        before = self.office.get_report(sealed["id"])
        for c in self.office.get_manifest(sealed["id"])["chunks"]:
            for client in ("a", "b", "c"):
                self.office.download_chunk(sealed["id"], c["index"], client)
        after = self.office.get_report(sealed["id"])
        self.assertEqual(after["body_digest"], before["body_digest"])
        self.assertEqual(after["status"], "已封账")

    def test_unsealed_report_cannot_export(self) -> None:
        data_h = self.office.register_data(DATA_V1)["digest"]
        rule_h = self.office.register_ruleset(RULESET)["digest"]
        trial = self.office.create_trial("草稿", data_h, rule_h, "编制组")
        with self.assertRaises(ConflictError):
            self.office.get_manifest(trial["id"])
        with self.assertRaises(ConflictError):
            self.office.download_chunk(trial["id"], 0, "c")

    def test_superseded_report_still_exportable(self) -> None:
        sealed, _, _ = self.seal_a_report()
        approval = self.office.request_reopen(sealed["id"], "编制组", "审计人员", "补录")
        self.office.decide_reopen(approval["id"], "审计人员", True)
        correction = self.office.create_correction(approval["id"], "更正版", "编制组")
        self.office.submit_for_signing(correction["id"], "编制组")
        self.office.sign(correction["id"], "中方", "中")
        self.office.sign(correction["id"], "外方", "外")
        self.office.seal("编制组", correction["id"])
        # 旧版本已更正但仍可下载核对
        chunk = self.office.download_chunk(sealed["id"], 0, "外方存档")
        self.assertEqual(chunk["digest"],
                         self.office.get_manifest(sealed["id"])["chunks"][0]["digest"])
