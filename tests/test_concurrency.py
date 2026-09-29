"""并发封账测试：同时封账不得产生两个相同正式版本或破坏版本号。"""
from __future__ import annotations

import threading

from seal_ledger.errors import ConflictError
from tests._helpers import OfficeCase


class ConcurrentSealTest(OfficeCase):
    def _two_signing_reports(self):
        data_h, rule_h = self.seed_versions()
        ids = []
        for title in ("报告甲", "报告乙"):
            trial = self.office.create_trial(title, data_h, rule_h, "编制组")
            self.office.submit_for_signing(trial["id"], "编制组")
            self.office.sign(trial["id"], "中方", "中")
            self.office.sign(trial["id"], "外方", "外")
            ids.append(trial["id"])
        return ids

    def test_concurrent_seal_different_reports_both_succeed(self) -> None:
        ids = self._two_signing_reports()
        results: list[BaseException | dict] = []

        def worker(rid: str) -> None:
            try:
                results.append(self.office.seal("编制组", rid))
            except BaseException as exc:  # noqa: BLE001
                results.append(exc)

        threads = [threading.Thread(target=worker, args=(rid,)) for rid in ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(results), 2)
        self.assertNotIn(Exception, [type(r) for r in results if isinstance(r, BaseException)])
        versions = sorted(r["version_no"] for r in results if not isinstance(r, BaseException))
        self.assertEqual(versions, [1, 2])

    def test_concurrent_double_seal_same_report_only_one_wins(self) -> None:
        data_h, rule_h = self.seed_versions()
        trial = self.office.create_trial("报告", data_h, rule_h, "编制组")
        self.office.submit_for_signing(trial["id"], "编制组")
        self.office.sign(trial["id"], "中方", "中")
        self.office.sign(trial["id"], "外方", "外")

        outcomes: list[str] = []
        lock = threading.Lock()

        def worker() -> None:
            try:
                self.office.seal("编制组", trial["id"])
                with lock:
                    outcomes.append("ok")
            except ConflictError:
                with lock:
                    outcomes.append("conflict")

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(outcomes.count("ok"), 1)
        self.assertEqual(outcomes.count("conflict"), 7)
        sealed = [r for r in self.office.list_reports() if r["version_no"] is not None]
        self.assertEqual(len(sealed), 1)
        self.assertEqual(sealed[0]["version_no"], 1)

    def test_concurrent_corrections_create_only_one_successor(self) -> None:
        sealed, _, _ = self.seal_a_report()

        def make_approval(approver: str) -> str:
            a = self.office.request_reopen(sealed["id"], "编制组", approver, "补录")
            self.office.decide_reopen(a["id"], approver, True)
            return a["id"]

        # 同一报告只允许一个生效申请，其余直接冲突
        approval_id = make_approval("审计人员")
        with self.assertRaises(ConflictError):
            make_approval("审计人员二")

        outcomes: list[str] = []
        lock = threading.Lock()

        def worker() -> None:
            try:
                self.office.create_correction(approval_id, "更正稿", "编制组")
                with lock:
                    outcomes.append("ok")
            except ConflictError:
                with lock:
                    outcomes.append("conflict")

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(outcomes.count("ok"), 1)
        self.assertEqual(outcomes.count("conflict"), 4)
