"""封账后端 HTTP 接口的端到端测试。"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sealing_backend.api import create_server
from sealing_backend.services import SealingService
from sealing_backend.storage import Storage


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        service = SealingService(Storage(Path(self.tmp.name) / "api.db"), chunk_size=64)
        self.server = create_server(service, host="127.0.0.1", port=0, quiet=True)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    def _req(self, method: str, path: str, body: dict | None = None,
             actor: str | None = None, key: str | None = None):
        url = f"http://127.0.0.1:{self.port}{urllib.parse.quote(path, safe='/?=&')}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if actor:
            request.add_header("X-Actor", urllib.parse.quote(actor))
        if key:
            request.add_header("Idempotency-Key", key)
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def _json(self, method: str, path: str, **kwargs):
        status, headers, raw = self._req(method, path, **kwargs)
        return status, json.loads(raw.decode("utf-8"))

    def _sealed_version(self) -> str:
        status, report = self._json(
            "POST", "/reports",
            body={"report_id": "09303-018", "title": "年度合作成果报告", "period": "2025年度"},
            actor="编制组-王",
        )
        self.assertEqual(status, 200)
        payload = {"就业数据": [{"地区": f"第{i}区", "人数": 100 + i} for i in range(30)]}
        status, version = self._json(
            "POST", "/reports/09303-018/versions",
            body={"data_version": "employment-2026-09-28", "rule_version": "rules-v3", "payload": payload},
            actor="编制组-王",
        )
        self.assertEqual(status, 200)
        vid = version["version_id"]
        for party, signer in (("中方", "张中方"), ("外方", "李外方")):
            status, _ = self._json(
                "POST", f"/versions/{vid}/signatures", body={"party": party, "signer": signer}, actor=signer
            )
            self.assertEqual(status, 200)
        status, sealed = self._json("POST", f"/versions/{vid}/seal", body={}, actor="编制组-王")
        self.assertEqual(status, 200)
        self.assertEqual(sealed["state"], "封账")
        return vid

    # ------------------------------------------------------------------
    # 测试
    # ------------------------------------------------------------------
    def test_happy_path_and_consistent_download(self) -> None:
        vid = self._sealed_version()
        status, export = self._json("POST", f"/versions/{vid}/export", body={}, actor="编制组-王")
        self.assertEqual(status, 200)
        manifest = export["manifest"]
        self.assertGreater(manifest["chunk_count"], 1)

        status, fetched = self._json("GET", f"/versions/{vid}/export/manifest")
        self.assertEqual(status, 200)
        self.assertEqual(fetched["manifest_digest"], export["manifest_digest"])

        # 外方下载件：逐块下载、校验分块摘要、拼装后核对总摘要
        chunks = []
        for i in range(manifest["chunk_count"]):
            status, headers, data = self._req("GET", f"/versions/{vid}/export/chunks/{i}")
            self.assertEqual(status, 200)
            self.assertEqual(headers["X-Chunk-Digest"], manifest["chunks"][i]["digest"])
            self.assertEqual(headers["X-Manifest-Digest"], export["manifest_digest"])
            self.assertEqual(hashlib.sha256(data).hexdigest(), manifest["chunks"][i]["digest"])
            chunks.append(data)
        document = b"".join(chunks)
        self.assertEqual(hashlib.sha256(document).hexdigest(), manifest["document_digest"])

        # 重复下载（含中断后续传）结果不变
        for i in range(manifest["chunk_count"]):
            _, _, again = self._req("GET", f"/versions/{vid}/export/chunks/{i}")
            self.assertEqual(again, chunks[i])

    def test_concurrent_seal_over_http_single_official(self) -> None:
        status, report = self._json(
            "POST", "/reports",
            body={"report_id": "R-并发", "title": "并发封账", "period": "2025年度"}, actor="编制组-王",
        )
        self.assertEqual(status, 200)
        status, version = self._json(
            "POST", "/reports/R-并发/versions",
            body={"data_version": "d1", "rule_version": "r1", "payload": {"指标": 1}}, actor="编制组-王",
        )
        vid = version["version_id"]
        for party, signer in (("中方", "张中方"), ("外方", "李外方")):
            self._json("POST", f"/versions/{vid}/signatures", body={"party": party, "signer": signer}, actor=signer)

        outcomes = []

        def worker(i: int) -> None:
            status, body = self._json("POST", f"/versions/{vid}/seal", body={}, actor=f"操作员{i}", key=f"seal-{i}")
            outcomes.append((status, body))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertTrue(all(status == 200 for status, _ in outcomes))
        self.assertEqual({body["sealed_seq"] for _, body in outcomes}, {1})
        _, report = self._json("GET", "/reports/R-并发")
        self.assertEqual(report["current_official_version_id"], vid)
        _, audit = self._json("GET", f"/audit-log?target_type=version&target_id={vid}")
        seals = [entry for entry in audit["entries"] if entry["action"] == "封账"]
        self.assertEqual(len(seals), 1)

    def test_correction_flow_over_http(self) -> None:
        vid = self._sealed_version()
        _, before = self._json("GET", f"/versions/{vid}")
        # 批准人不独立 → 422
        status, error = self._json(
            "POST", f"/versions/{vid}/corrections",
            body={"reason": "补数", "payload": {"就业人数": 1}, "approver": "张中方"}, actor="编制组-王",
        )
        self.assertEqual(status, 422)
        self.assertEqual(error["error"]["code"], "unprocessable")
        # 独立批准 → 更正单生效
        status, correction = self._json(
            "POST", f"/versions/{vid}/corrections",
            body={"reason": "次日补到的就业数据", "payload": {"就业人数": 1312}, "approver": "独立审计-陈"},
            actor="编制组-王",
        )
        self.assertEqual(status, 200)
        cid = correction["correction_id"]
        for party, signer in (("中方", "张中方"), ("外方", "李外方")):
            self._json("POST", f"/corrections/{cid}/signatures", body={"party": party, "signer": signer}, actor=signer)
        status, effective = self._json("POST", f"/corrections/{cid}/seal", body={}, actor="编制组-王")
        self.assertEqual(status, 200)
        self.assertEqual(effective["state"], "更正")
        # 原版本内容摘要不变，正式版本指针不变
        _, after = self._json("GET", f"/versions/{vid}")
        _, sealed_report = self._json("GET", "/reports/09303-018")
        self.assertEqual(after["content_digest"], before["content_digest"])
        self.assertEqual(sealed_report["current_official_version_id"], vid)

    def test_error_responses(self) -> None:
        status, error = self._json("GET", "/versions/ver_不存在")
        self.assertEqual(status, 404)
        self.assertEqual(error["error"]["code"], "not_found")

        status, error = self._json("POST", "/reports", body={"title": "缺演员"})
        self.assertEqual(status, 400)

        status, _ = self._json("POST", "/reports",
                               body={"report_id": "R-错", "title": "t", "period": "p"}, actor="编制组-王")
        self.assertEqual(status, 200)
        status, version = self._json(
            "POST", "/reports/R-错/versions",
            body={"data_version": "d", "rule_version": "r", "payload": {"x": 1}}, actor="编制组-王",
        )
        status, error = self._json("POST", f"/versions/{version['version_id']}/seal", body={}, actor="编制组-王")
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "conflict")

        status, error = self._json(
            "POST", f"/versions/{version['version_id']}/signatures",
            body={"party": "第三方", "signer": "某"}, actor="某",
        )
        self.assertEqual(status, 400)

    def test_idempotency_key_over_http(self) -> None:
        vid = self._sealed_version()
        _, first = self._json("POST", f"/versions/{vid}/seal", body={}, actor="编制组-王", key="K-9")
        _, second = self._json("POST", f"/versions/{vid}/seal", body={}, actor="编制组-王", key="K-9")
        self.assertEqual(first["sealed_seq"], second["sealed_seq"])
        _, audit = self._json("GET", f"/audit-log?target_type=version&target_id={vid}")
        seals = [entry for entry in audit["entries"] if entry["action"] == "封账"]
        self.assertEqual(len(seals), 1)


if __name__ == "__main__":
    unittest.main()
