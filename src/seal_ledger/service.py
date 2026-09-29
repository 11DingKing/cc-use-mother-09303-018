"""封账核心服务。

关键保证：
- 试算稿在创建时即由"数据版本摘要 + 规则版本摘要"确定性地算出正文，
  此后正文字节不再变化；迟到数据因摘要不同而无法影响任何在签/已封账报告。
- 封账在一个 IMMEDIATE 事务内完成状态复查、双方会签核验、正式版本号分配、
  正文重算复核与上一版本标记，并发封账由数据库锁与唯一索引串行裁决，
  至多一个封账成功，正式版本号全局唯一、连续递增。
- 已封账内容只能经"申请—独立批准—继任试算稿"更正：原报告在继任稿封账前
  始终保持已封账；继任稿封账后原报告转为"已更正"但正文与分块仍可核对下载。
- 导出分块由正文唯一切分，重复下载幂等留痕、断点续传只补缺块，下载行为
  永不触碰报告内容。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from .canonical import canonical_json, digest, digest_bytes
from .errors import ConflictError, NotFoundError, ValidationError
from .models import REQUIRED_PARTIES, ReportStatus
from .rules import evaluate, render_body, ruleset_fingerprint, validate_ruleset
from .store import Store, utc_now

# 导出分块大小：64 KiB。固定常量，保证同一份正文的分块方案永远一致。
CHUNK_SIZE = 64 * 1024


class ReportOffice:
    """报告办公室对外服务门面。"""

    def __init__(self, store: Store) -> None:
        self.store = store

    # ---------------------------------------------------------------- 数据/规则版本

    def register_data(self, content: Any, source_label: str | None = None) -> dict[str, Any]:
        """登记一个数据版本；相同内容幂等返回同一摘要。

        次日补到的就业数据只要内容不同就是新的数据版本，已被试算稿/封账稿
        引用的旧版本行永不改变，因此在线数字不会自动改写已签结果。
        """
        if not isinstance(content, dict):
            raise ValidationError("数据内容必须是 JSON 对象")
        raw = canonical_json(content)
        h = digest_bytes(raw)
        created = self.store.register_data(h, raw.decode("utf-8"), len(raw), source_label)
        return {"digest": h, "byte_length": len(raw), "source_label": source_label, "created": created}

    def register_ruleset(self, spec: dict[str, Any], note: str | None = None) -> dict[str, Any]:
        """登记一个规则版本；相同规则幂等返回同一摘要。"""
        validate_ruleset(spec)
        raw = canonical_json(spec)
        h = digest_bytes(raw)
        created = self.store.register_ruleset(h, raw.decode("utf-8"), len(raw), note)
        return {**ruleset_fingerprint(spec), "created": created, "note": note}

    def _load_data_payload(self, data_digest: str) -> tuple[dict[str, Any], Any]:
        row = self.store.get_data(data_digest)
        if row is None:
            raise NotFoundError(f"数据版本不存在：{data_digest}")
        return row, json.loads(row["content"])

    def _load_ruleset(self, rule_digest: str) -> tuple[dict[str, Any], dict[str, Any]]:
        row = self.store.get_ruleset(rule_digest)
        if row is None:
            raise NotFoundError(f"规则版本不存在：{rule_digest}")
        return row, json.loads(row["content"])

    # ---------------------------------------------------------------- 试算稿

    def create_trial(
        self,
        title: str,
        data_digest: str,
        rule_digest: str,
        created_by: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """从确定的数据版本与规则版本生成带输入摘要的试算稿。"""
        if not title or not title.strip():
            raise ValidationError("标题不能为空")
        _, payload = self._load_data_payload(data_digest)
        _, spec = self._load_ruleset(rule_digest)

        with self.store.lock:
            if idempotency_key:
                existing = self.store.query_one(
                    "SELECT report_id FROM idempotency_keys WHERE key=?", (idempotency_key,)
                )
                if existing is not None:
                    return self.get_report(existing["report_id"])

            metrics, body, body_h, input_summary = self._compute(spec, payload, data_digest, rule_digest)
            report_id = uuid.uuid4().hex
            now = utc_now()
            self.store.insert_report(
                {
                    "id": report_id,
                    "version_no": None,
                    "title": title.strip(),
                    "status": ReportStatus.TRIAL.value,
                    "data_digest": data_digest,
                    "rule_digest": rule_digest,
                    "input_summary": canonical_json(input_summary).decode("utf-8"),
                    "body": body,
                    "body_digest": body_h,
                    "parent_report_id": None,
                    "created_at": now,
                    "updated_at": now,
                }
            )
            if idempotency_key:
                self.store.execute(
                    "INSERT INTO idempotency_keys(key, report_id, created_at) VALUES (?,?,?)",
                    (idempotency_key, report_id, now),
                )
            self.store.append_event("trial_created", report_id, created_by, {"input_summary": input_summary})
            self.store.commit()
            return self.get_report(report_id)

    def _compute(
        self, spec: dict[str, Any], payload: Any, data_digest: str, rule_digest: str
    ) -> tuple[list[dict[str, Any]], bytes, str, dict[str, Any]]:
        """纯函数：规则版本 × 数据版本 -> (指标, 正文, 正文摘要, 输入摘要)。"""
        metrics = evaluate(spec, payload)
        body = render_body(spec, metrics)
        body_h = digest_bytes(body)
        input_summary = {
            "data": {"digest": data_digest},
            "rules": {"digest": rule_digest, "rule_count": len(spec["rules"])},
            "metrics": metrics,
            "body_digest": body_h,
        }
        return metrics, body, body_h, input_summary

    # ---------------------------------------------------------------- 会签

    def submit_for_signing(self, report_id: str, actor: str) -> dict[str, Any]:
        with self.store.lock:
            report = self._require_report(report_id)
            self._require_status(report, ReportStatus.TRIAL)
            self.store.set_status(report_id, ReportStatus.SIGNING.value)
            self.store.append_event("submitted_for_signing", report_id, actor, {})
            self.store.commit()
            return self.get_report(report_id)

    def sign(self, report_id: str, party: str, signer: str) -> dict[str, Any]:
        """中外双方之一签署；重复签署同一方幂等。"""
        if party not in REQUIRED_PARTIES:
            raise ValidationError(f"签署方必须是：{'、'.join(sorted(REQUIRED_PARTIES))}")
        if not signer or not signer.strip():
            raise ValidationError("签署人不能为空")
        with self.store.lock:
            report = self._require_report(report_id)
            self._require_status(report, ReportStatus.SIGNING)
            first = self.store.add_signature(report_id, party, signer.strip())
            self.store.append_event(
                "signed", report_id, signer.strip(), {"party": party, "first": first}
            )
            self.store.commit()
            return self.get_report(report_id)

    # ---------------------------------------------------------------- 封账

    def seal(self, actor: str, report_id: str) -> dict[str, Any]:
        """满足双方会签后封账并固定内容。并发调用只有一个能成功。"""
        store = self.store
        with store.lock:
            try:
                store.begin_immediate()
                report = store.get_report(report_id)
                if report is None:
                    raise NotFoundError(f"报告不存在：{report_id}")
                if report["status"] != ReportStatus.SIGNING.value:
                    raise ConflictError(f"只有会签中的报告可以封账，当前状态：{report['status']}")

                parties = {row["party"] for row in store.get_signatures(report_id)}
                missing = REQUIRED_PARTIES - parties
                if missing:
                    raise ConflictError("双方会签未完成，缺少：" + "、".join(sorted(missing)))

                # 正文重算复核：封账内容必须与试算时确定的正文逐字节一致。
                _, payload = self._load_data_payload(report["data_digest"])
                _, spec = self._load_ruleset(report["rule_digest"])
                _, body, body_h, input_summary = self._compute(
                    spec, payload, report["data_digest"], report["rule_digest"]
                )
                if body_h != report["body_digest"] or body != bytes(report["body"]):
                    raise ConflictError("正文与输入版本重算结果不一致，拒绝封账")
                if canonical_json(input_summary) != report["input_summary"].encode("utf-8"):
                    raise ConflictError("输入摘要与输入版本重算结果不一致，拒绝封账")

                parent_id = report["parent_report_id"]
                root = self._lineage_root_locked(report)
                successor_of = None
                if root["id"] != report_id:
                    # 继任稿封账：上一正式版本必须仍处于已封账且未被更正。
                    if root["status"] != ReportStatus.SEALED.value or root["superseded_by"] is not None:
                        raise ConflictError("该版本已有继任的正式版本，不能再次封账")
                    successor_of = root

                row = store.query_one("SELECT COALESCE(MAX(version_no), 0) AS m FROM reports")
                version_no = row["m"] + 1

                manifest = self._build_manifest(
                    version_no, input_summary, body,
                    None if successor_of is None else {
                        "report_id": successor_of["id"],
                        "version_no": successor_of["version_no"],
                        "body_digest": successor_of["body_digest"],
                    },
                )
                store.seal_report(report_id, version_no, manifest)
                if successor_of is not None:
                    store.mark_superseded(successor_of["id"], report_id)
                store.append_event(
                    "sealed", report_id, actor,
                    {"version_no": version_no, "manifest_digest": digest(manifest)},
                )
                store.commit_transaction()
            except ConflictError:
                store.rollback_transaction()
                raise
            except sqlite3.IntegrityError as exc:
                store.rollback_transaction()
                raise ConflictError(f"封账冲突，正式版本已存在：{exc}") from exc
            except Exception:
                store.rollback_transaction()
                raise
            return self.get_report(report_id)

    def _lineage_root_locked(self, report: sqlite3.Row) -> sqlite3.Row:
        cur = report
        while cur["parent_report_id"] is not None:
            parent = self.store.get_report(cur["parent_report_id"])
            if parent is None:
                raise ConflictError("继任链断裂，缺少父报告")
            cur = parent
        return cur

    def _build_manifest(
        self, version_no: int, input_summary: dict[str, Any], body: bytes, parent: dict[str, Any] | None
    ) -> dict[str, Any]:
        chunks = [
            {
                "index": n,
                "offset": offset,
                "digest": digest_bytes(body[offset : offset + CHUNK_SIZE]),
                "byte_length": len(body[offset : offset + CHUNK_SIZE]),
            }
            for n, offset in enumerate(range(0, len(body), CHUNK_SIZE))
        ]
        manifest = {
            "version_no": version_no,
            "input_summary": input_summary,
            "body_digest": digest_bytes(body),
            "body_byte_length": len(body),
            "chunk_size": CHUNK_SIZE,
            "chunks": chunks,
            "parent": parent,
        }
        manifest["manifest_digest"] = digest({k: v for k, v in manifest.items()})
        return manifest

    # ---------------------------------------------------------------- 重开 / 更正

    def request_reopen(
        self, report_id: str, requester: str, approver: str, reason: str
    ) -> dict[str, Any]:
        """申请重开已封账报告；批准人必须独立于申请人和双方签署人。"""
        if not reason or not reason.strip():
            raise ValidationError("必须填写重开理由")
        if requester == approver:
            raise ValidationError("批准人不能与申请人为同一人，重开须经独立批准")
        with self.store.lock:
            report = self._require_report(report_id)
            self._require_status(report, ReportStatus.SEALED)
            signers = {row["signer"] for row in self.store.get_signatures(report_id)}
            if approver in signers:
                raise ValidationError("批准人不能是该报告的签署人，重开须经独立批准")
            approval_id = uuid.uuid4().hex
            try:
                self.store.insert_approval(
                    {
                        "id": approval_id,
                        "report_id": report_id,
                        "requester": requester,
                        "approver": approver,
                        "reason": reason.strip(),
                        "status": "pending",
                        "sealed_body_digest": report["body_digest"],
                        "requested_at": utc_now(),
                    }
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"该报告已有待批/已批的重开申请：{exc}") from exc
            self.store.append_event(
                "reopen_requested", report_id, requester,
                {"approval_id": approval_id, "approver": approver, "reason": reason.strip()},
            )
            self.store.commit()
            return self.get_approval(approval_id)

    def decide_reopen(self, approval_id: str, approver: str, approve: bool) -> dict[str, Any]:
        """独立批准人批准或驳回重开申请。"""
        with self.store.lock:
            self.store.begin_immediate()
            try:
                approval = self.store.get_approval(approval_id)
                if approval is None:
                    raise NotFoundError(f"重开申请不存在：{approval_id}")
                if approval["status"] != "pending":
                    raise ConflictError(f"申请已处理：{approval['status']}")
                if approval["approver"] != approver:
                    raise ValidationError("只有登记的独立批准人可以作出决定")
                new_status = "approved" if approve else "rejected"
                self.store.execute(
                    "UPDATE reopen_approvals SET status=?, approved_at=? WHERE id=?",
                    (new_status, utc_now(), approval_id),
                )
                self.store.append_event(
                    "reopen_approved" if approve else "reopen_rejected",
                    approval["report_id"], approver, {"approval_id": approval_id},
                )
                self.store.commit_transaction()
            except Exception:
                self.store.rollback_transaction()
                raise
            return self.get_approval(approval_id)

    def create_correction(
        self,
        approval_id: str,
        title: str,
        created_by: str,
        data_digest: str | None = None,
        rule_digest: str | None = None,
    ) -> dict[str, Any]:
        """凭已批准的申请创建继任试算稿（迟到数据在此进入新版本）。

        原封账报告保持已封账不变；同一份已封账报告至多产生一个继任稿，
        申请随继任稿创建一并核销。
        """
        store = self.store
        with store.lock:
            try:
                store.begin_immediate()
                approval = store.get_approval(approval_id)
                if approval is None:
                    raise NotFoundError(f"重开申请不存在：{approval_id}")
                if approval["status"] != "approved":
                    raise ConflictError(f"只有已批准的申请可以创建更正稿，当前：{approval['status']}")
                parent = store.get_report(approval["report_id"])
                if parent is None or parent["status"] != ReportStatus.SEALED.value:
                    raise ConflictError("原报告不是已封账状态，无法创建更正稿")
                if parent["body_digest"] != approval["sealed_body_digest"]:
                    raise ConflictError("原报告正文在申请后发生变化，拒绝创建更正稿")

                new_data = data_digest or parent["data_digest"]
                new_rule = rule_digest or parent["rule_digest"]
                _, payload = self._load_data_payload(new_data)
                _, spec = self._load_ruleset(new_rule)
                metrics, body, body_h, input_summary = self._compute(spec, payload, new_data, new_rule)

                report_id = uuid.uuid4().hex
                now = utc_now()
                store.insert_report(
                    {
                        "id": report_id,
                        "version_no": None,
                        "title": title.strip(),
                        "status": ReportStatus.TRIAL.value,
                        "data_digest": new_data,
                        "rule_digest": new_rule,
                        "input_summary": canonical_json(input_summary).decode("utf-8"),
                        "body": body,
                        "body_digest": body_h,
                        "parent_report_id": parent["id"],
                        "created_at": now,
                        "updated_at": now,
                    }
                )
                store.consume_approval(approval_id, report_id)
                store.append_event(
                    "correction_created", report_id, created_by,
                    {"approval_id": approval_id, "parent_report_id": parent["id"],
                     "input_summary": input_summary},
                )
                store.commit_transaction()
            except ConflictError:
                store.rollback_transaction()
                raise
            except sqlite3.IntegrityError as exc:
                store.rollback_transaction()
                raise ConflictError(f"该封账版本已存在继任版本：{exc}") from exc
            except Exception:
                store.rollback_transaction()
                raise
            return self.get_report(report_id)

    # ---------------------------------------------------------------- 分块导出

    def get_manifest(self, report_id: str) -> dict[str, Any]:
        report = self._require_report(report_id)
        if report["version_no"] is None or not report["manifest"]:
            raise ConflictError("只有正式版本可以导出")
        return json.loads(report["manifest"])

    def list_chunks(self, report_id: str) -> list[dict[str, Any]]:
        return self.get_manifest(report_id)["chunks"]

    def download_chunk(self, report_id: str, chunk_index: int, client_key: str) -> dict[str, Any]:
        """下载一个分块；同 client_key 重复下载幂等留痕，可随时断点续传。"""
        if not client_key:
            raise ValidationError("缺少 client_key")
        report = self._require_report(report_id)
        if report["version_no"] is None:
            raise ConflictError("只有正式版本可以导出")
        body = bytes(report["body"])
        start = chunk_index * CHUNK_SIZE
        if start < 0 or start >= len(body):
            raise NotFoundError(f"分块不存在：{chunk_index}")
        data = body[start : start + CHUNK_SIZE]
        first = self.store.record_download(report_id, client_key, chunk_index, len(data))
        return {
            "report_id": report_id,
            "chunk_index": chunk_index,
            "data": data,
            "digest": digest_bytes(data),
            "byte_length": len(data),
            "first_download": first,
        }

    def download_status(self, report_id: str, client_key: str) -> dict[str, Any]:
        """返回某客户端已下载分块，供中断后续传比对。"""
        self._require_report(report_id)
        rows = self.store.query_all(
            "SELECT chunk_index FROM downloads WHERE report_id=? AND client_key=? ORDER BY chunk_index",
            (report_id, client_key),
        )
        return {"report_id": report_id, "client_key": client_key,
                "downloaded": [row["chunk_index"] for row in rows]}

    # ---------------------------------------------------------------- 查询

    def get_report(self, report_id: str) -> dict[str, Any]:
        report = self._require_report(report_id)
        return self._report_dto(report)

    def list_reports(self) -> list[dict[str, Any]]:
        return [self._report_dto(row) for row in self.store.list_reports()]

    def get_approval(self, approval_id: str) -> dict[str, Any]:
        row = self.store.get_approval(approval_id)
        if row is None:
            raise NotFoundError(f"重开申请不存在：{approval_id}")
        return {k: row[k] for k in row.keys()}

    def list_events(self, report_id: str | None = None) -> list[dict[str, Any]]:
        return [
            {"id": row["id"], "ts": row["ts"], "report_id": row["report_id"],
             "event": row["event"], "actor": row["actor"],
             "detail": json.loads(row["detail"])}
            for row in self.store.list_events(report_id)
        ]

    def _report_dto(self, row: sqlite3.Row) -> dict[str, Any]:
        signatures = [
            {"party": r["party"], "signer": r["signer"], "signed_at": r["signed_at"]}
            for r in self.store.get_signatures(row["id"])
        ]
        return {
            "id": row["id"],
            "version_no": row["version_no"],
            "title": row["title"],
            "status": row["status"],
            "data_digest": row["data_digest"],
            "rule_digest": row["rule_digest"],
            "input_summary": json.loads(row["input_summary"]),
            "body_digest": row["body_digest"],
            "body_byte_length": len(bytes(row["body"])),
            "parent_report_id": row["parent_report_id"],
            "superseded_by": row["superseded_by"],
            "signatures": signatures,
            "sealed_at": row["sealed_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _require_report(self, report_id: str) -> sqlite3.Row:
        report = self.store.get_report(report_id)
        if report is None:
            raise NotFoundError(f"报告不存在：{report_id}")
        return report

    @staticmethod
    def _require_status(report: sqlite3.Row, expected: ReportStatus) -> None:
        if report["status"] != expected.value:
            raise ConflictError(f"该操作要求状态为{expected.value}，当前：{report['status']}")
