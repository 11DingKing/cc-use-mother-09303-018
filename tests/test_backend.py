"""封账后端服务层测试：状态机、幂等、并发与分块导出。"""
from __future__ import annotations

import hashlib
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sealing_backend import canon
from sealing_backend.errors import Conflict, NotFound, Unprocessable
from sealing_backend.services import SealingService
from sealing_backend.storage import Storage


class FakeClock:
    def __init__(self) -> None:
        self.n = 0

    def __call__(self) -> str:
        self.n += 1
        return f"2026-09-29T08:{self.n // 60:02d}:{self.n % 60:02d}+00:00"


class ServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.tmp.name) / "sealing.db")
        self.service = SealingService(self.storage, chunk_size=64, clock=FakeClock())
        self.service.create_report(
            report_id="09303-018", title="年度合作成果报告", period="2025年度", actor="编制组-王"
        )
        self.payload = {"就业人数": 1280, "培训人次": 3420, "项目数": 12}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    def _draft(self, payload=None, data_version="employment-2026-09-28", rule_version="rules-v3"):
        return self.service.create_version(
            report_id="09303-018",
            data_version=data_version,
            rule_version=rule_version,
            payload=payload or self.payload,
            actor="编制组-王",
        )

    def _sign_both(self, version_id: str) -> None:
        self.service.sign_version(version_id=version_id, party="中方", signer="张中方")
        self.service.sign_version(version_id=version_id, party="外方", signer="李外方")

    def _sealed(self, payload=None):
        version = self._draft(payload)
        self._sign_both(version["version_id"])
        return self.service.seal_version(version_id=version["version_id"], actor="编制组-王")

    def _effective_correction(self, version_id: str, payload=None):
        correction = self.service.create_correction(
            version_id=version_id,
            reason="次日补到的就业数据",
            payload=payload or {"就业人数": 1312},
            approver="独立审计-陈",
            actor="编制组-王",
        )
        cid = correction["correction_id"]
        self.service.sign_correction(correction_id=cid, party="中方", signer="张中方")
        self.service.sign_correction(correction_id=cid, party="外方", signer="李外方")
        return self.service.seal_correction(correction_id=cid, actor="编制组-王")

    # ------------------------------------------------------------------
    # 试算稿与输入摘要
    # ------------------------------------------------------------------
    def test_trial_draft_carries_input_digest(self) -> None:
        version = self._draft()
        expected = canon.digest_of(
            {
                "report_id": "09303-018",
                "data_version": "employment-2026-09-28",
                "rule_version": "rules-v3",
                "payload": self.payload,
            }
        )
        self.assertEqual(version["state"], "试算")
        self.assertEqual(version["input_digest"], expected)
        self.assertNotEqual(version["content_digest"], version["input_digest"])

    def test_input_digest_changes_with_data_or_rules(self) -> None:
        base = self._draft()
        other_data = self._draft(payload={**self.payload, "就业人数": 1281})
        other_rules = self._draft(rule_version="rules-v4")
        self.assertNotEqual(base["input_digest"], other_data["input_digest"])
        self.assertNotEqual(base["input_digest"], other_rules["input_digest"])

    # ------------------------------------------------------------------
    # 会签
    # ------------------------------------------------------------------
    def test_signing_flow_and_duplicate_rejected(self) -> None:
        version = self._draft()
        after_cn = self.service.sign_version(version_id=version["version_id"], party="中方", signer="张中方")
        self.assertEqual(after_cn["state"], "会签")
        self.assertEqual(after_cn["signatures"][0]["signed_digest"], version["content_digest"])
        with self.assertRaises(Conflict):
            self.service.sign_version(version_id=version["version_id"], party="中方", signer="别人")
        after_fr = self.service.sign_version(version_id=version["version_id"], party="外方", signer="李外方")
        self.assertEqual(len(after_fr["signatures"]), 2)

    def test_seal_requires_both_parties(self) -> None:
        version = self._draft()
        with self.assertRaises(Conflict):
            self.service.seal_version(version_id=version["version_id"], actor="编制组-王")
        self.service.sign_version(version_id=version["version_id"], party="中方", signer="张中方")
        with self.assertRaises(Conflict):
            self.service.seal_version(version_id=version["version_id"], actor="编制组-王")
        self.service.sign_version(version_id=version["version_id"], party="外方", signer="李外方")
        sealed = self.service.seal_version(version_id=version["version_id"], actor="编制组-王")
        self.assertEqual(sealed["state"], "封账")
        self.assertEqual(sealed["sealed_seq"], 1)
        report = self.service.get_report("09303-018")
        self.assertEqual(report["current_official_version_id"], version["version_id"])

    # ------------------------------------------------------------------
    # 封账后冻结与迟到数据
    # ------------------------------------------------------------------
    def test_signed_result_frozen_after_seal(self) -> None:
        sealed = self._sealed()
        with self.assertRaises(Conflict):
            self.service.sign_version(version_id=sealed["version_id"], party="中方", signer="张中方")
        late = self._draft(payload={**self.payload, "就业人数": 1312}, data_version="employment-2026-09-29")
        self.assertEqual(late["version_no"], 2)
        self.assertEqual(late["state"], "试算")
        again = self.service.get_version(sealed["version_id"])
        self.assertEqual(again["state"], "封账")
        self.assertEqual(again["payload"], self.payload)
        self.assertEqual(again["content_digest"], sealed["content_digest"])

    def test_late_data_sealed_as_next_official_version(self) -> None:
        first = self._sealed()
        late = self._draft(payload={**self.payload, "就业人数": 1312}, data_version="employment-2026-09-29")
        self._sign_both(late["version_id"])
        second = self.service.seal_version(version_id=late["version_id"], actor="编制组-王")
        self.assertEqual(second["sealed_seq"], 2)
        report = self.service.get_report("09303-018")
        self.assertEqual(report["current_official_version_id"], late["version_id"])
        # 历史正式件不被改写
        self.assertEqual(self.service.get_version(first["version_id"])["state"], "封账")

    def test_superseded_draft_cannot_be_signed_or_sealed(self) -> None:
        old = self._draft()
        self._draft()  # 更新的试算稿取代旧稿
        with self.assertRaises(Conflict):
            self.service.sign_version(version_id=old["version_id"], party="中方", signer="张中方")
        with self.assertRaises(Conflict):
            self.service.seal_version(version_id=old["version_id"], actor="编制组-王")

    # ------------------------------------------------------------------
    # 并发与幂等
    # ------------------------------------------------------------------
    def test_concurrent_seal_produces_single_official_version(self) -> None:
        version = self._draft()
        self._sign_both(version["version_id"])
        results, errors = [], []

        def worker(i: int) -> None:
            try:
                results.append(
                    self.service.seal_version(
                        version_id=version["version_id"], actor=f"操作员{i}", idempotency_key=f"seal-{i}"
                    )
                )
            except Exception as exc:  # noqa: BLE001 - 收集后统一断言
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        self.assertEqual({item["sealed_seq"] for item in results}, {1})
        seals = [
            entry
            for entry in self.service.list_audit(target_type="version", target_id=version["version_id"])
            if entry["action"] == "封账"
        ]
        self.assertEqual(len(seals), 1)
        report = self.service.get_report("09303-018")
        self.assertEqual(report["current_official_version_id"], version["version_id"])

    def test_concurrent_seal_of_superseded_version_rejected(self) -> None:
        old = self._draft()
        self._sign_both(old["version_id"])
        new = self._draft(payload={**self.payload, "就业人数": 1312})
        self._sign_both(new["version_id"])
        results, errors = [], []

        def worker(version_id: str) -> None:
            try:
                results.append(self.service.seal_version(version_id=version_id, actor="编制组-王"))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(old["version_id"],)),
            threading.Thread(target=worker, args=(new["version_id"],)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["version_id"], new["version_id"])
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], Conflict)
        # 全库只有一个正式版本
        report = self.service.get_report("09303-018")
        self.assertEqual(report["current_official_version_id"], new["version_id"])

    def test_seal_idempotent_replay(self) -> None:
        version = self._draft()
        self._sign_both(version["version_id"])
        first = self.service.seal_version(version_id=version["version_id"], actor="编制组-王", idempotency_key="K-1")
        again = self.service.seal_version(version_id=version["version_id"], actor="编制组-王", idempotency_key="K-1")
        no_key = self.service.seal_version(version_id=version["version_id"], actor="编制组-王")
        self.assertEqual(first["sealed_seq"], again["sealed_seq"])
        self.assertEqual(first["sealed_seq"], no_key["sealed_seq"])
        seals = [
            entry
            for entry in self.service.list_audit(target_type="version", target_id=version["version_id"])
            if entry["action"] == "封账"
        ]
        self.assertEqual(len(seals), 1)

    def test_idempotency_key_conflict_on_different_request(self) -> None:
        self.service.create_report(title="甲", period="2025年度", actor="编制组-王", idempotency_key="R-1")
        with self.assertRaises(Conflict):
            self.service.create_report(title="乙", period="2025年度", actor="编制组-王", idempotency_key="R-1")

    # ------------------------------------------------------------------
    # 更正单与独立批准
    # ------------------------------------------------------------------
    def test_correction_requires_sealed_version(self) -> None:
        draft = self._draft()
        with self.assertRaises(Conflict):
            self.service.create_correction(
                version_id=draft["version_id"], reason="改数", payload={"就业人数": 1},
                approver="独立审计-陈", actor="编制组-王",
            )

    def test_correction_requires_independent_approver(self) -> None:
        sealed = self._sealed()
        with self.assertRaises(Unprocessable):
            self.service.create_correction(
                version_id=sealed["version_id"], reason="改数", payload={"就业人数": 1},
                approver="张中方", actor="编制组-王",
            )
        with self.assertRaises(Unprocessable):
            self.service.create_correction(
                version_id=sealed["version_id"], reason="改数", payload={"就业人数": 1},
                approver="编制组-王", actor="编制组-王",
            )

    def test_correction_lifecycle_keeps_original_untouched(self) -> None:
        sealed = self._sealed()
        correction = self.service.create_correction(
            version_id=sealed["version_id"],
            reason="次日补到的就业数据",
            payload={"就业人数": 1312},
            approver="独立审计-陈",
            actor="编制组-王",
        )
        self.assertEqual(correction["state"], "试算")
        with self.assertRaises(Conflict):
            self.service.create_correction(
                version_id=sealed["version_id"], reason="重复申请", payload={"就业人数": 2},
                approver="独立审计-陈", actor="编制组-王",
            )
        cid = correction["correction_id"]
        with self.assertRaises(Conflict):
            self.service.seal_correction(correction_id=cid, actor="编制组-王")
        self.service.sign_correction(correction_id=cid, party="中方", signer="张中方")
        with self.assertRaises(Conflict):
            self.service.seal_correction(correction_id=cid, actor="编制组-王")
        self.service.sign_correction(correction_id=cid, party="外方", signer="李外方")
        effective = self.service.seal_correction(correction_id=cid, actor="编制组-王")
        self.assertEqual(effective["state"], "更正")
        # 已签结果不变
        original = self.service.get_version(sealed["version_id"])
        self.assertEqual(original["state"], "封账")
        self.assertEqual(original["content_digest"], sealed["content_digest"])
        self.assertEqual(original["payload"], self.payload)
        self.assertEqual(original["corrections"][0]["correction_id"], cid)
        # 生效后允许下一张更正单
        follow_up = self.service.create_correction(
            version_id=sealed["version_id"], reason="再次补正", payload={"培训人次": 3500},
            approver="独立审计-陈", actor="编制组-王",
        )
        self.assertEqual(follow_up["state"], "试算")

    # ------------------------------------------------------------------
    # 分块导出、续传与重复下载
    # ------------------------------------------------------------------
    def _big_payload(self) -> dict:
        return {"就业数据": [{"地区": f"第{i}区", "人数": 100 + i} for i in range(30)]}

    def test_export_requires_sealed_state(self) -> None:
        draft = self._draft()
        with self.assertRaises(Conflict):
            self.service.create_export(target_type="version", target_id=draft["version_id"], actor="编制组-王")

    def test_manifest_chunks_and_resume(self) -> None:
        sealed = self._sealed(payload=self._big_payload())
        export = self.service.create_export(target_type="version", target_id=sealed["version_id"], actor="编制组-王")
        manifest = export["manifest"]
        self.assertGreater(manifest["chunk_count"], 1)
        # 模拟中断续传：先拿到前两块，随后补齐剩余分块
        first = [self.service.get_chunk(target_type="version", target_id=sealed["version_id"], index=i)[0] for i in range(2)]
        rest = [
            self.service.get_chunk(target_type="version", target_id=sealed["version_id"], index=i)[0]
            for i in range(2, manifest["chunk_count"])
        ]
        document = b"".join(first + rest)
        self.assertEqual(hashlib.sha256(document).hexdigest(), manifest["document_digest"])
        for i, chunk_info in enumerate(manifest["chunks"]):
            data, meta = self.service.get_chunk(target_type="version", target_id=sealed["version_id"], index=i)
            self.assertEqual(meta["digest"], chunk_info["digest"])
            self.assertEqual(hashlib.sha256(data).hexdigest(), chunk_info["digest"])
        self.assertEqual(self.service.get_version(sealed["version_id"])["state"], "导出")

    def test_repeated_export_and_download_are_identical(self) -> None:
        sealed = self._sealed(payload=self._big_payload())
        first = self.service.create_export(target_type="version", target_id=sealed["version_id"], actor="编制组-王")
        second = self.service.create_export(target_type="version", target_id=sealed["version_id"], actor="编制组-王")
        self.assertEqual(first["manifest_digest"], second["manifest_digest"])
        chunk_count = first["manifest"]["chunk_count"]
        for i in range(chunk_count):
            a, _ = self.service.get_chunk(target_type="version", target_id=sealed["version_id"], index=i)
            b, _ = self.service.get_chunk(target_type="version", target_id=sealed["version_id"], index=i)
            self.assertEqual(a, b)
        exports = [
            entry
            for entry in self.service.list_audit(target_type="version", target_id=sealed["version_id"])
            if entry["action"] == "生成导出"
        ]
        self.assertEqual(len(exports), 1)

    def test_export_frozen_despite_late_data_and_corrections(self) -> None:
        sealed = self._sealed(payload=self._big_payload())
        vid = sealed["version_id"]
        export = self.service.create_export(target_type="version", target_id=vid, actor="编制组-王")
        snapshot = [
            self.service.get_chunk(target_type="version", target_id=vid, index=i)[0]
            for i in range(export["manifest"]["chunk_count"])
        ]
        # 迟到数据进入下一版本并封账
        late = self._draft(payload=self._big_payload(), data_version="employment-2026-09-29")
        self._sign_both(late["version_id"])
        self.service.seal_version(version_id=late["version_id"], actor="编制组-王")
        # 迟到数据进入更正单并生效
        correction = self._effective_correction(vid)
        self.service.create_export(target_type="correction", target_id=correction["correction_id"], actor="编制组-王")
        # 原导出件逐字节不变
        again = self.service.get_manifest(target_type="version", target_id=vid)
        self.assertEqual(again["manifest_digest"], export["manifest_digest"])
        for i, chunk in enumerate(snapshot):
            now, _ = self.service.get_chunk(target_type="version", target_id=vid, index=i)
            self.assertEqual(now, chunk)

    def test_correction_export_requires_effective(self) -> None:
        sealed = self._sealed()
        correction = self.service.create_correction(
            version_id=sealed["version_id"], reason="补数", payload={"就业人数": 1},
            approver="独立审计-陈", actor="编制组-王",
        )
        with self.assertRaises(Conflict):
            self.service.create_export(
                target_type="correction", target_id=correction["correction_id"], actor="编制组-王"
            )

    def test_chunk_index_out_of_range(self) -> None:
        sealed = self._sealed()
        with self.assertRaises(NotFound):
            self.service.get_chunk(target_type="version", target_id=sealed["version_id"], index=99)

    # ------------------------------------------------------------------
    # 审计与兜底
    # ------------------------------------------------------------------
    def test_audit_trail_records_full_flow(self) -> None:
        sealed = self._sealed()
        self.service.create_export(target_type="version", target_id=sealed["version_id"], actor="编制组-王")
        actions = [entry["action"] for entry in self.service.list_audit()]
        self.assertEqual(
            actions,
            ["创建报告", "生成试算稿", "会签", "会签", "封账", "生成导出"],
        )

    def test_unknown_targets_raise_not_found(self) -> None:
        with self.assertRaises(NotFound):
            self.service.get_version("ver_不存在")
        with self.assertRaises(NotFound):
            self.service.seal_version(version_id="ver_不存在", actor="编制组-王")
        with self.assertRaises(NotFound):
            self.service.get_manifest(target_type="version", target_id="ver_不存在")
        with self.assertRaises(NotFound):
            self.service.get_correction("cor_不存在")


if __name__ == "__main__":
    unittest.main()
