"""HTTP 接口端到端测试。"""
from __future__ import annotations

import base64
import hashlib
import json
import threading
import unittest
import urllib.error
import urllib.request

from seal_ledger.api import build_server
from tests._helpers import DATA_V1, DATA_V2, RULESET


def _request(method: str, url: str, body: dict | None = None, headers: dict | None = None):
    data = None
    req_headers = headers or {}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req_headers = {"Content-Type": "application/json; charset=utf-8", **req_headers}
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = build_server("127.0.0.1", 0, ":memory:")
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def _json(self, method: str, path: str, body: dict | None = None, headers: dict | None = None) -> tuple[int, dict]:
        status, _, raw = _request(method, self.base + path, body, headers)
        return status, json.loads(raw)

    def test_end_to_end_report_lifecycle(self) -> None:
        # 登记数据与规则版本
        status, data = self._json("POST", "/data-versions",
                                  {"content": DATA_V1, "source_label": "首批"})
        self.assertEqual(status, 200)
        status, rules = self._json("POST", "/rule-versions", {"spec": RULESET})
        self.assertEqual(status, 200)

        # 生成试算稿（幂等键防重复提交）
        payload = {"title": "年度合作成果报告", "data_digest": data["digest"],
                   "rule_digest": rules["digest"], "created_by": "编制组"}
        s1, trial1 = self._json("POST", "/reports", payload, {"Idempotency-Key": "k-1"})
        s2, trial2 = self._json("POST", "/reports", payload, {"Idempotency-Key": "k-1"})
        self.assertEqual((s1, s2), (201, 201))
        self.assertEqual(trial1["id"], trial2["id"])
        rid = trial1["id"]

        # 未会签完成不能封账
        self.assertEqual(self._json("POST", f"/reports/{rid}/submit", {"actor": "编制组"})[0], 200)
        self.assertEqual(self._json("POST", f"/reports/{rid}/signatures",
                                    {"party": "中方", "signer": "中"})[0], 200)
        status, err = self._json("POST", f"/reports/{rid}/seal", {"actor": "编制组"})
        self.assertEqual(status, 409)
        self.assertEqual(err["error"], "conflict")

        # 双方齐签后封账
        self.assertEqual(self._json("POST", f"/reports/{rid}/signatures",
                                    {"party": "外方", "signer": "外"})[0], 200)
        status, sealed = self._json("POST", f"/reports/{rid}/seal", {"actor": "编制组"})
        self.assertEqual(status, 200)
        self.assertEqual(sealed["version_no"], 1)

        # 迟到数据 -> 独立批准重开 -> 更正版
        _, late = self._json("POST", "/data-versions", {"content": DATA_V2, "source_label": "补录"})
        status, approval = self._json(
            "POST", f"/reports/{rid}/reopen-requests",
            {"requester": "编制组", "approver": "审计人员", "reason": "次日补到就业数据"})
        self.assertEqual(status, 201)
        self.assertEqual(self._json(
            "POST", f"/reopen-approvals/{approval['id']}/decision",
            {"approver": "审计人员", "approve": True})[0], 200)
        status, correction = self._json("POST", "/corrections", {
            "approval_id": approval["id"], "title": "年度合作成果报告 v2",
            "data_digest": late["digest"], "created_by": "编制组"})
        self.assertEqual(status, 201)
        cid = correction["id"]
        self.assertEqual(self._json("POST", f"/reports/{cid}/submit", {})[0], 200)
        self.assertEqual(self._json("POST", f"/reports/{cid}/signatures",
                                    {"party": "中方", "signer": "中"})[0], 200)
        self.assertEqual(self._json("POST", f"/reports/{cid}/signatures",
                                    {"party": "外方", "signer": "外"})[0], 200)
        _, sealed_v2 = self._json("POST", f"/reports/{cid}/seal", {})
        self.assertEqual(sealed_v2["version_no"], 2)

        # 分块导出：清单、下载、续传、重复下载 ETag
        _, manifest = self._json("GET", f"/reports/{rid}/manifest")
        client = "foreign-party"
        assembled = b""
        for chunk in manifest["chunks"]:
            status, headers, raw = _request(
                "GET", f"{self.base}/reports/{rid}/chunks/{chunk['index']}?client_key={client}")
            self.assertEqual(status, 200)
            body = json.loads(raw)
            assembled += base64.b64decode(body["data"])
        self.assertEqual(hashlib.sha256(assembled).hexdigest(), manifest["body_digest"])

        # 重复下载命中 304
        status, headers, _ = _request(
            "GET", f"{self.base}/reports/{rid}/chunks/0?client_key={client}",
            headers={"If-None-Match": f'"{manifest["chunks"][0]["digest"]}"'})
        self.assertEqual(status, 304)

        # 旧版已更正但仍可核对；审计事件可查
        _, old = self._json("GET", f"/reports/{rid}")
        self.assertEqual(old["status"], "已更正")
        status, events = self._json("GET", f"/reports/{rid}/events")
        self.assertEqual(status, 200)
        kinds = {e["event"] for e in events}
        self.assertIn("sealed", kinds)
        self.assertIn("reopen_approved", kinds)

    def test_unknown_route_and_validation_errors(self) -> None:
        status, body = self._json("GET", "/nope")
        self.assertEqual(status, 404)
        status, body = self._json("POST", "/data-versions", {"content": [1, 2]})
        self.assertEqual(status, 400)
