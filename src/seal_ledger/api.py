"""HTTP 接口（标准库实现，零第三方依赖）。

路由：
- POST /data-versions            登记数据版本
- POST /rule-versions            登记规则版本
- POST /reports                  生成试算稿（支持 Idempotency-Key 头）
- GET  /reports                  列表
- GET  /reports/{id}             详情（含输入摘要、会签）
- POST /reports/{id}/submit      提交会签
- POST /reports/{id}/signatures  一方签署
- POST /reports/{id}/seal        封账
- POST /reports/{id}/reopen-requests  申请重开（登记独立批准人）
- POST /reopen-approvals/{id}/decision  批准/驳回
- POST /corrections              凭批准创建继任试算稿
- GET  /reports/{id}/manifest    封账清单与分块摘要
- GET  /reports/{id}/chunks/{n}?client_key=...  下载分块（ETag 去重）
- GET  /reports/{id}/download-status?client_key=...  续传进度
- GET  /reports/{id}/events      审计事件
"""
from __future__ import annotations

import base64
import json
import re
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from .errors import ConflictError, NotFoundError, SealLedgerError, ValidationError
from .service import ReportOffice
from .store import Store


class _Router:
    def __init__(self) -> None:
        self._routes: list[tuple[str, re.Pattern[str], Callable[..., Any]]] = []

    def add(self, method: str, pattern: str, handler: Callable[..., Any]) -> None:
        self._routes.append((method, re.compile("^" + pattern + "$"), handler))

    def match(self, method: str, path: str):
        for m, regex, handler in self._routes:
            if m == method:
                found = regex.match(path)
                if found:
                    return handler, found.groupdict()
        return None, None


class SealLedgerHandler(BaseHTTPRequestHandler):
    server_version = "SealLedger/1.0"
    router = _Router()

    # ---- 路由表（类装载时构建一次）------------------------------------
    @classmethod
    def _bind_routes(cls) -> None:
        r = cls.router
        r.add("POST", r"/data-versions", lambda h: h.register_data())
        r.add("POST", r"/rule-versions", lambda h: h.register_ruleset())
        r.add("POST", r"/reports", lambda h: h.create_report())
        r.add("GET", r"/reports", lambda h: h.list_reports())
        r.add("GET", r"/reports/(?P<rid>[^/]+)", lambda h, rid: h.get_report(rid))
        r.add("POST", r"/reports/(?P<rid>[^/]+)/submit", lambda h, rid: h.submit(rid))
        r.add("POST", r"/reports/(?P<rid>[^/]+)/signatures", lambda h, rid: h.sign(rid))
        r.add("POST", r"/reports/(?P<rid>[^/]+)/seal", lambda h, rid: h.seal(rid))
        r.add("POST", r"/reports/(?P<rid>[^/]+)/reopen-requests",
              lambda h, rid: h.request_reopen(rid))
        r.add("POST", r"/reopen-approvals/(?P<aid>[^/]+)/decision",
              lambda h, aid: h.decide_reopen(aid))
        r.add("POST", r"/corrections", lambda h: h.create_correction())
        r.add("GET", r"/reports/(?P<rid>[^/]+)/manifest", lambda h, rid: h.manifest(rid))
        r.add("GET", r"/reports/(?P<rid>[^/]+)/chunks/(?P<idx>\d+)",
              lambda h, rid, idx: h.chunk(rid, int(idx)))
        r.add("GET", r"/reports/(?P<rid>[^/]+)/download-status",
              lambda h, rid: h.download_status(rid))
        r.add("GET", r"/reports/(?P<rid>[^/]+)/events", lambda h, rid: h.events(rid))

    # ---- 基础处理 ------------------------------------------------------

    @property
    def office(self) -> ReportOffice:
        return self.server.office  # type: ignore[attr-defined]

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValidationError(f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(value, dict):
            raise ValidationError("请求体必须是 JSON 对象")
        return value

    def _query(self) -> dict[str, str]:
        return {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}

    def _send_json(self, status: int, value: Any, extra_headers: dict[str, str] | None = None) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _handle_error(self, exc: Exception) -> None:
        if isinstance(exc, ValidationError):
            status = HTTPStatus.BAD_REQUEST
        elif isinstance(exc, NotFoundError):
            status = HTTPStatus.NOT_FOUND
        elif isinstance(exc, ConflictError):
            status = HTTPStatus.CONFLICT
        elif isinstance(exc, SealLedgerError):
            status = HTTPStatus.INTERNAL_SERVER_ERROR
        else:
            raise exc
        self._send_json(status, {"error": exc.code if isinstance(exc, SealLedgerError) else "error",
                                 "message": str(exc)})

    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def _dispatch(self) -> None:
        try:
            path = urlsplit(self.path).path.rstrip("/") or "/"
            handler, kwargs = self.router.match(self.command, path)
            if handler is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found", "message": f"无此路由：{path}"})
                return
            handler(self, **(kwargs or {}))
        except Exception as exc:  # noqa: BLE001 - 统一错误出口
            self._handle_error(exc)

    def log_message(self, fmt: str, *args: Any) -> None:
        # 访问日志走 stderr，避免污染标准输出
        import sys
        print("%s - %s" % (self.address_string(), fmt % args), file=sys.stderr)

    # ---- 端点 ----------------------------------------------------------

    def register_data(self) -> None:
        body = self._read_json()
        result = self.office.register_data(body.get("content"), body.get("source_label"))
        self._send_json(HTTPStatus.OK, result)

    def register_ruleset(self) -> None:
        body = self._read_json()
        result = self.office.register_ruleset(body["spec"], body.get("note"))
        self._send_json(HTTPStatus.OK, result)

    def create_report(self) -> None:
        body = self._read_json()
        result = self.office.create_trial(
            title=body["title"],
            data_digest=body["data_digest"],
            rule_digest=body["rule_digest"],
            created_by=body.get("created_by", "报告编制组"),
            idempotency_key=self.headers.get("Idempotency-Key"),
        )
        self._send_json(HTTPStatus.CREATED, result)

    def list_reports(self) -> None:
        self._send_json(HTTPStatus.OK, self.office.list_reports())

    def get_report(self, rid: str) -> None:
        self._send_json(HTTPStatus.OK, self.office.get_report(rid))

    def submit(self, rid: str) -> None:
        body = self._read_json()
        self._send_json(HTTPStatus.OK,
                        self.office.submit_for_signing(rid, body.get("actor", "报告编制组")))

    def sign(self, rid: str) -> None:
        body = self._read_json()
        result = self.office.sign(rid, body["party"], body["signer"])
        self._send_json(HTTPStatus.OK, result)

    def seal(self, rid: str) -> None:
        body = self._read_json()
        self._send_json(HTTPStatus.OK, self.office.seal(body.get("actor", "报告编制组"), rid))

    def request_reopen(self, rid: str) -> None:
        body = self._read_json()
        result = self.office.request_reopen(
            rid, body["requester"], body["approver"], body["reason"])
        self._send_json(HTTPStatus.CREATED, result)

    def decide_reopen(self, aid: str) -> None:
        body = self._read_json()
        result = self.office.decide_reopen(aid, body["approver"], bool(body["approve"]))
        self._send_json(HTTPStatus.OK, result)

    def create_correction(self) -> None:
        body = self._read_json()
        result = self.office.create_correction(
            approval_id=body["approval_id"],
            title=body["title"],
            created_by=body.get("created_by", "报告编制组"),
            data_digest=body.get("data_digest"),
            rule_digest=body.get("rule_digest"),
        )
        self._send_json(HTTPStatus.CREATED, result)

    def manifest(self, rid: str) -> None:
        self._send_json(HTTPStatus.OK, self.office.get_manifest(rid))

    def chunk(self, rid: str, idx: int) -> None:
        client_key = self._query().get("client_key", "")
        result = self.office.download_chunk(rid, idx, client_key)
        etag = f'"{result["digest"]}"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(HTTPStatus.NOT_MODIFIED)
            self.send_header("ETag", etag)
            self.end_headers()
            return
        payload = {**result, "data": base64.b64encode(result["data"]).decode("ascii")}
        self._send_json(
            HTTPStatus.OK,
            payload,
            {
                "ETag": etag,
                "X-Chunk-Digest": result["digest"],
                "X-First-Download": "1" if result["first_download"] else "0",
                "Cache-Control": "immutable",
            },
        )

    def download_status(self, rid: str) -> None:
        client_key = self._query().get("client_key", "")
        self._send_json(HTTPStatus.OK, self.office.download_status(rid, client_key))

    def events(self, rid: str) -> None:
        self._send_json(HTTPStatus.OK, self.office.list_events(rid))


SealLedgerHandler._bind_routes()


def build_server(host: str, port: int, db_path: str) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), SealLedgerHandler)
    server.office = ReportOffice(Store(db_path))  # type: ignore[attr-defined]
    return server


def serve(host: str = "127.0.0.1", port: int = 8080, db_path: str = "seal_ledger.sqlite3") -> None:
    server = build_server(host, port, db_path)
    print(f"封账后端监听 http://{host}:{port}（数据库 {db_path}）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
