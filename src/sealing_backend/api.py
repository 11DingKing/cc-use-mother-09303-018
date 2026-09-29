"""HTTP 接口层：基于标准库的 REST 服务。

约定：
  * 写操作（POST）必须携带 ``X-Actor`` 请求头标识操作人；
  * 写操作可携带 ``Idempotency-Key`` 请求头，重复提交返回首次结果；
  * 分块下载响应携带 ``X-Chunk-Index`` / ``X-Chunk-Digest`` /
    ``X-Manifest-Digest`` 头，客户端据清单断点续传并逐块校验。
"""
from __future__ import annotations

import json
import re
import sqlite3
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, NamedTuple
from urllib.parse import parse_qs, unquote, urlsplit

from .errors import BadRequest, DomainError, NotFound
from .services import TARGET_CORRECTION, TARGET_VERSION, SealingService


class _Ctx(NamedTuple):
    service: SealingService
    actor: str | None
    idempotency_key: str | None
    body: dict
    query: dict


class _Bytes(NamedTuple):
    data: bytes
    headers: dict


# ----------------------------------------------------------------------
# 路由处理函数
# ----------------------------------------------------------------------
def _service_info(ctx: _Ctx) -> dict:
    return {
        "product": "合作成果报告封账后端",
        "states": ["试算", "会签", "封账", "导出", "更正"],
        "invariants": ["输入摘要", "双方会签", "封账版本", "分块导出"],
    }


def _create_report(ctx: _Ctx) -> dict:
    return ctx.service.create_report(
        title=ctx.body.get("title"),
        period=ctx.body.get("period"),
        report_id=ctx.body.get("report_id"),
        actor=ctx.actor,
        idempotency_key=ctx.idempotency_key,
    )


def _get_report(ctx: _Ctx, report_id: str) -> dict:
    return ctx.service.get_report(report_id)


def _list_versions(ctx: _Ctx, report_id: str) -> dict:
    return {"versions": ctx.service.list_versions(report_id)}


def _create_version(ctx: _Ctx, report_id: str) -> dict:
    return ctx.service.create_version(
        report_id=report_id,
        data_version=ctx.body.get("data_version"),
        rule_version=ctx.body.get("rule_version"),
        payload=ctx.body.get("payload"),
        actor=ctx.actor,
        idempotency_key=ctx.idempotency_key,
    )


def _get_version(ctx: _Ctx, version_id: str) -> dict:
    return ctx.service.get_version(version_id)


def _sign_version(ctx: _Ctx, version_id: str) -> dict:
    return ctx.service.sign_version(
        version_id=version_id,
        party=ctx.body.get("party"),
        signer=ctx.body.get("signer"),
        idempotency_key=ctx.idempotency_key,
    )


def _seal_version(ctx: _Ctx, version_id: str) -> dict:
    return ctx.service.seal_version(
        version_id=version_id, actor=ctx.actor, idempotency_key=ctx.idempotency_key
    )


def _list_corrections(ctx: _Ctx, version_id: str) -> dict:
    return {"corrections": ctx.service.list_corrections(version_id)}


def _create_correction(ctx: _Ctx, version_id: str) -> dict:
    return ctx.service.create_correction(
        version_id=version_id,
        reason=ctx.body.get("reason"),
        payload=ctx.body.get("payload"),
        approver=ctx.body.get("approver"),
        actor=ctx.actor,
        idempotency_key=ctx.idempotency_key,
    )


def _get_correction(ctx: _Ctx, correction_id: str) -> dict:
    return ctx.service.get_correction(correction_id)


def _sign_correction(ctx: _Ctx, correction_id: str) -> dict:
    return ctx.service.sign_correction(
        correction_id=correction_id,
        party=ctx.body.get("party"),
        signer=ctx.body.get("signer"),
        idempotency_key=ctx.idempotency_key,
    )


def _seal_correction(ctx: _Ctx, correction_id: str) -> dict:
    return ctx.service.seal_correction(
        correction_id=correction_id, actor=ctx.actor, idempotency_key=ctx.idempotency_key
    )


def _export_version(ctx: _Ctx, version_id: str) -> dict:
    return ctx.service.create_export(target_type=TARGET_VERSION, target_id=version_id, actor=ctx.actor)


def _export_correction(ctx: _Ctx, correction_id: str) -> dict:
    return ctx.service.create_export(target_type=TARGET_CORRECTION, target_id=correction_id, actor=ctx.actor)


def _manifest(ctx: _Ctx, target_type: str, target_id: str) -> dict:
    return ctx.service.get_manifest(target_type=target_type, target_id=target_id)


def _chunk(ctx: _Ctx, target_type: str, target_id: str, index: str) -> _Bytes:
    data, meta = ctx.service.get_chunk(target_type=target_type, target_id=target_id, index=int(index))
    return _Bytes(
        data=data,
        headers={
            "X-Chunk-Index": str(meta["index"]),
            "X-Chunk-Digest": meta["digest"],
            "X-Manifest-Digest": meta["manifest_digest"],
        },
    )


def _audit_log(ctx: _Ctx) -> dict:
    target_type = ctx.query.get("target_type", [None])[0]
    target_id = ctx.query.get("target_id", [None])[0]
    return {"entries": ctx.service.list_audit(target_type=target_type, target_id=target_id)}


ROUTES: list[tuple[str, re.Pattern, Callable[..., Any]]] = [
    ("GET", re.compile(r"/"), _service_info),
    ("POST", re.compile(r"/reports"), _create_report),
    ("GET", re.compile(r"/reports/(?P<report_id>[^/]+)"), _get_report),
    ("GET", re.compile(r"/reports/(?P<report_id>[^/]+)/versions"), _list_versions),
    ("POST", re.compile(r"/reports/(?P<report_id>[^/]+)/versions"), _create_version),
    ("GET", re.compile(r"/versions/(?P<version_id>[^/]+)"), _get_version),
    ("POST", re.compile(r"/versions/(?P<version_id>[^/]+)/signatures"), _sign_version),
    ("POST", re.compile(r"/versions/(?P<version_id>[^/]+)/seal"), _seal_version),
    ("GET", re.compile(r"/versions/(?P<version_id>[^/]+)/corrections"), _list_corrections),
    ("POST", re.compile(r"/versions/(?P<version_id>[^/]+)/corrections"), _create_correction),
    ("GET", re.compile(r"/corrections/(?P<correction_id>[^/]+)"), _get_correction),
    ("POST", re.compile(r"/corrections/(?P<correction_id>[^/]+)/signatures"), _sign_correction),
    ("POST", re.compile(r"/corrections/(?P<correction_id>[^/]+)/seal"), _seal_correction),
    ("POST", re.compile(r"/versions/(?P<version_id>[^/]+)/export"), _export_version),
    ("GET", re.compile(r"/versions/(?P<version_id>[^/]+)/export/manifest"),
     lambda ctx, version_id: _manifest(ctx, TARGET_VERSION, version_id)),
    ("GET", re.compile(r"/versions/(?P<version_id>[^/]+)/export/chunks/(?P<index>\d+)"),
     lambda ctx, version_id, index: _chunk(ctx, TARGET_VERSION, version_id, index)),
    ("POST", re.compile(r"/corrections/(?P<correction_id>[^/]+)/export"), _export_correction),
    ("GET", re.compile(r"/corrections/(?P<correction_id>[^/]+)/export/manifest"),
     lambda ctx, correction_id: _manifest(ctx, TARGET_CORRECTION, correction_id)),
    ("GET", re.compile(r"/corrections/(?P<correction_id>[^/]+)/export/chunks/(?P<index>\d+)"),
     lambda ctx, correction_id, index: _chunk(ctx, TARGET_CORRECTION, correction_id, index)),
    ("GET", re.compile(r"/audit-log"), _audit_log),
]


class _Handler(BaseHTTPRequestHandler):
    server_version = "SealingBackend/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        if not getattr(self.server, "quiet", False):
            super().log_message(fmt, *args)

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    # ------------------------------------------------------------------
    def _handle(self, method: str) -> None:
        try:
            result = self._route(method)
        except DomainError as exc:
            self._send_json(exc.status, {"error": {"code": exc.code, "message": exc.message}})
            return
        except sqlite3.IntegrityError as exc:
            self._send_json(409, {"error": {"code": "conflict", "message": f"数据约束冲突：{exc}"}})
            return
        except Exception:  # noqa: BLE001 - 兜底，避免连接悬挂
            traceback.print_exc()
            self._send_json(500, {"error": {"code": "internal", "message": "服务内部错误"}})
            return
        if isinstance(result, _Bytes):
            self._send_bytes(result)
        else:
            self._send_json(200, result)

    def _route(self, method: str) -> Any:
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        for route_method, pattern, handler in ROUTES:
            if route_method != method:
                continue
            match = pattern.fullmatch(path)
            if not match:
                continue
            body = self._read_body() if method == "POST" else {}
            actor = self.headers.get("X-Actor")
            if actor:
                # 头部仅支持 latin-1，非 ASCII 操作人按 URL 百分号编码传输
                actor = unquote(actor)
            if method == "POST" and not actor:
                raise BadRequest("缺少 X-Actor 请求头")
            ctx = _Ctx(
                service=self.server.service,
                actor=actor,
                idempotency_key=self.headers.get("Idempotency-Key"),
                body=body,
                query=parse_qs(parsed.query),
            )
            return handler(ctx, **match.groupdict())
        raise NotFound(f"接口不存在：{method} {path}")

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise BadRequest("请求体不是合法 JSON") from None
        if not isinstance(value, dict):
            raise BadRequest("请求体必须是 JSON 对象")
        return value

    def _send_json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_bytes(self, result: _Bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        for name, value in result.headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(result.data)))
        self.end_headers()
        self.wfile.write(result.data)


def create_server(
    service: SealingService, *, host: str = "127.0.0.1", port: int = 8000, quiet: bool = False
) -> ThreadingHTTPServer:
    """创建多线程 HTTP 服务；每个请求独立数据库连接，写事务串行。"""
    httpd = ThreadingHTTPServer((host, port), _Handler)
    httpd.service = service
    httpd.quiet = quiet
    return httpd
