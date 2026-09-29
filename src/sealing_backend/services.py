"""封账领域服务：试算、会签、封账、更正、分块导出。

状态机：
  报告版本：试算 → 会签 → 封账 → 导出
  更正单：  试算 → 会签 → 更正

核心不变量：
  * 试算稿创建时即固定数据版本、规则版本与数据本体，并写入输入摘要；
  * 双方签署人各签一次，签署摘要必须等于当前内容摘要；
  * 封账在单个写事务内完成并分配唯一的正式序号，重复/并发封账只会
    得到同一个正式版本，绝不产生第二个；
  * 封账后内容冻结，迟到数据只能进入下一版本或更正单；
  * 重开（更正）必须经独立于签署人和申请人的批准人批准；
  * 导出件按分块摘要核对，生成后不可变，重复导出/下载结果一致。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from . import canon
from .errors import BadRequest, Conflict, NotFound, Unprocessable
from .storage import Storage

PARTIES = ("中方", "外方")

STATE_TRIAL = "试算"
STATE_SIGNING = "会签"
STATE_SEALED = "封账"
STATE_EXPORTED = "导出"
STATE_CORRECTED = "更正"

TARGET_VERSION = "version"
TARGET_CORRECTION = "correction"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _require(value: Any, field: str) -> Any:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise BadRequest(f"缺少必要字段：{field}")
    return value


def _require_payload(payload: Any) -> dict:
    if not isinstance(payload, dict) or not payload:
        raise BadRequest("payload 必须是非空 JSON 对象")
    return payload


class SealingService:
    """封账领域服务，所有写操作都在单个写事务内完成。"""

    def __init__(self, storage: Storage, *, chunk_size: int = 4096, clock: Callable[[], str] = _utcnow):
        if chunk_size <= 0:
            raise ValueError("chunk_size 必须为正整数")
        self.storage = storage
        self.chunk_size = chunk_size
        self.clock = clock

    # ------------------------------------------------------------------
    # 报告
    # ------------------------------------------------------------------
    def create_report(
        self,
        *,
        title: str,
        period: str,
        actor: str,
        report_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        _require(title, "title")
        _require(period, "period")
        _require(actor, "actor")
        request = {"title": title, "period": period, "report_id": report_id, "actor": actor}
        with self.storage.write() as con:
            hit = self._replay(con, "POST:/reports", idempotency_key, request)
            if hit is not None:
                return hit
            rid = report_id or _new_id("rep")
            if con.execute("SELECT 1 FROM reports WHERE report_id = ?", (rid,)).fetchone():
                raise Conflict(f"报告已存在：{rid}")
            con.execute(
                "INSERT INTO reports (report_id, title, period, created_by, created_at) VALUES (?,?,?,?,?)",
                (rid, title, period, actor, self.clock()),
            )
            self._audit(con, actor, "创建报告", "report", rid, {"title": title, "period": period})
            result = self._report_view(con, rid)
            self._store_replay(con, "POST:/reports", idempotency_key, request, result)
            return result

    def get_report(self, report_id: str) -> dict:
        with self.storage.read() as con:
            return self._report_view(con, report_id)

    def list_versions(self, report_id: str) -> list[dict]:
        with self.storage.read() as con:
            self._report_row(con, report_id)
            rows = con.execute(
                "SELECT * FROM versions WHERE report_id = ? ORDER BY version_no", (report_id,)
            ).fetchall()
            return [self._version_summary(con, row) for row in rows]

    # ------------------------------------------------------------------
    # 试算稿
    # ------------------------------------------------------------------
    def create_version(
        self,
        *,
        report_id: str,
        data_version: str,
        rule_version: str,
        payload: dict,
        actor: str,
        idempotency_key: str | None = None,
    ) -> dict:
        """从确定的数据版本与规则版本生成试算稿，并写入输入摘要。"""
        _require(data_version, "data_version")
        _require(rule_version, "rule_version")
        _require(actor, "actor")
        payload = _require_payload(payload)
        request = {
            "report_id": report_id,
            "data_version": data_version,
            "rule_version": rule_version,
            "payload": payload,
            "actor": actor,
        }
        scope = f"POST:/reports/{report_id}/versions"
        with self.storage.write() as con:
            hit = self._replay(con, scope, idempotency_key, request)
            if hit is not None:
                return hit
            self._report_row(con, report_id)
            version_no = con.execute(
                "SELECT COALESCE(MAX(version_no), 0) + 1 AS n FROM versions WHERE report_id = ?",
                (report_id,),
            ).fetchone()["n"]
            version_id = _new_id("ver")
            now = self.clock()
            input_digest = canon.digest_of(
                {
                    "report_id": report_id,
                    "data_version": data_version,
                    "rule_version": rule_version,
                    "payload": payload,
                }
            )
            content = {
                "report_id": report_id,
                "version_id": version_id,
                "version_no": version_no,
                "data_version": data_version,
                "rule_version": rule_version,
                "input_digest": input_digest,
                "payload": payload,
                "created_by": actor,
                "created_at": now,
            }
            content_digest = canon.digest_of(content)
            con.execute(
                """INSERT INTO versions
                   (version_id, report_id, version_no, state, data_version, rule_version,
                    payload_json, input_digest, content_digest, created_by, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    version_id,
                    report_id,
                    version_no,
                    STATE_TRIAL,
                    data_version,
                    rule_version,
                    canon.canonical_bytes(payload).decode("utf-8"),
                    input_digest,
                    content_digest,
                    actor,
                    now,
                ),
            )
            self._audit(
                con,
                actor,
                "生成试算稿",
                "version",
                version_id,
                {"version_no": version_no, "input_digest": input_digest, "content_digest": content_digest},
            )
            result = self._version_view(con, version_id)
            self._store_replay(con, scope, idempotency_key, request, result)
            return result

    def get_version(self, version_id: str) -> dict:
        with self.storage.read() as con:
            return self._version_view(con, version_id)

    # ------------------------------------------------------------------
    # 会签
    # ------------------------------------------------------------------
    def sign_version(
        self,
        *,
        version_id: str,
        party: str,
        signer: str,
        idempotency_key: str | None = None,
    ) -> dict:
        _require(party, "party")
        if party not in PARTIES:
            raise BadRequest(f"签署方必须是 {' 或 '.join(PARTIES)}")
        _require(signer, "signer")
        request = {"version_id": version_id, "party": party, "signer": signer}
        scope = f"POST:/versions/{version_id}/signatures"
        with self.storage.write() as con:
            hit = self._replay(con, scope, idempotency_key, request)
            if hit is not None:
                return hit
            row = self._version_row(con, version_id)
            if row["state"] not in (STATE_TRIAL, STATE_SIGNING):
                raise Conflict(f"版本当前状态为「{row['state']}」，不能会签")
            self._require_latest(con, row)
            if con.execute(
                "SELECT 1 FROM signatures WHERE version_id = ? AND party = ?", (version_id, party)
            ).fetchone():
                raise Conflict(f"{party}已完成会签，不能重复签署")
            now = self.clock()
            con.execute(
                "INSERT INTO signatures (version_id, party, signer, signed_digest, signed_at) VALUES (?,?,?,?,?)",
                (version_id, party, signer, row["content_digest"], now),
            )
            if row["state"] == STATE_TRIAL:
                con.execute("UPDATE versions SET state = ? WHERE version_id = ?", (STATE_SIGNING, version_id))
            self._audit(
                con, actor=signer, action="会签", target_type="version", target_id=version_id,
                detail={"party": party, "signed_digest": row["content_digest"]},
            )
            result = self._version_view(con, version_id)
            self._store_replay(con, scope, idempotency_key, request, result)
            return result

    # ------------------------------------------------------------------
    # 封账
    # ------------------------------------------------------------------
    def seal_version(
        self,
        *,
        version_id: str,
        actor: str,
        idempotency_key: str | None = None,
    ) -> dict:
        """满足双方签署条件后封账。

        重复调用与并发调用都是安全的：整个检查与状态迁移在一个写事务
        内完成，正式序号按报告唯一分配，已封账的版本直接返回既有结果，
        绝不会产生第二个正式版本。
        """
        _require(actor, "actor")
        request = {"version_id": version_id, "actor": actor}
        scope = f"POST:/versions/{version_id}/seal"
        with self.storage.write() as con:
            hit = self._replay(con, scope, idempotency_key, request)
            if hit is not None:
                return hit
            row = self._version_row(con, version_id)
            if row["state"] in (STATE_SEALED, STATE_EXPORTED):
                # 幂等：重复封账返回同一个正式版本。
                result = self._version_view(con, version_id)
                self._store_replay(con, scope, idempotency_key, request, result)
                return result
            if row["state"] != STATE_SIGNING:
                raise Conflict(f"版本当前状态为「{row['state']}」，不能封账")
            self._require_latest(con, row)
            sigs = con.execute(
                "SELECT party, signed_digest FROM signatures WHERE version_id = ?", (version_id,)
            ).fetchall()
            if {s["party"] for s in sigs} != set(PARTIES):
                raise Conflict("双方会签未完成，不能封账")
            for sig in sigs:
                if sig["signed_digest"] != row["content_digest"]:
                    raise Conflict("签署摘要与内容摘要不一致，不能封账")
            seq = con.execute(
                "SELECT COALESCE(MAX(sealed_seq), 0) + 1 AS n FROM versions WHERE report_id = ?",
                (row["report_id"],),
            ).fetchone()["n"]
            now = self.clock()
            updated = con.execute(
                "UPDATE versions SET state = ?, sealed_at = ?, sealed_seq = ? WHERE version_id = ? AND state = ?",
                (STATE_SEALED, now, seq, version_id, STATE_SIGNING),
            )
            if updated.rowcount != 1:
                raise Conflict("封账冲突：版本状态已变化，请重试")
            con.execute(
                "UPDATE reports SET current_official_version_id = ? WHERE report_id = ?",
                (version_id, row["report_id"]),
            )
            self._audit(
                con, actor, "封账", "version", version_id,
                {"sealed_seq": seq, "content_digest": row["content_digest"]},
            )
            result = self._version_view(con, version_id)
            self._store_replay(con, scope, idempotency_key, request, result)
            return result

    # ------------------------------------------------------------------
    # 更正单（重开需独立批准）
    # ------------------------------------------------------------------
    def create_correction(
        self,
        *,
        version_id: str,
        reason: str,
        payload: dict,
        approver: str,
        actor: str,
        idempotency_key: str | None = None,
    ) -> dict:
        """对已封账版本申请更正单，批准人必须独立。"""
        _require(reason, "reason")
        _require(approver, "approver")
        _require(actor, "actor")
        payload = _require_payload(payload)
        request = {
            "version_id": version_id,
            "reason": reason,
            "payload": payload,
            "approver": approver,
            "actor": actor,
        }
        scope = f"POST:/versions/{version_id}/corrections"
        with self.storage.write() as con:
            hit = self._replay(con, scope, idempotency_key, request)
            if hit is not None:
                return hit
            row = self._version_row(con, version_id)
            if row["state"] not in (STATE_SEALED, STATE_EXPORTED):
                raise Conflict("只有已封账的版本才能申请更正单")
            signers = {
                s["signer"]
                for s in con.execute("SELECT signer FROM signatures WHERE version_id = ?", (version_id,))
            }
            if approver in signers or approver == actor:
                raise Unprocessable("重开批准人必须独立于该版本的签署人和申请人")
            if con.execute(
                "SELECT 1 FROM corrections WHERE version_id = ? AND state != ?", (version_id, STATE_CORRECTED)
            ).fetchone():
                raise Conflict("该版本已存在未生效的更正单")
            correction_id = _new_id("cor")
            now = self.clock()
            content_digest = canon.digest_of(
                {
                    "correction_id": correction_id,
                    "version_id": version_id,
                    "corrected_content_digest": row["content_digest"],
                    "reason": reason,
                    "payload": payload,
                    "approver": approver,
                    "created_at": now,
                }
            )
            con.execute(
                """INSERT INTO corrections
                   (correction_id, version_id, state, reason, payload_json, content_digest,
                    requested_by, approver, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    correction_id,
                    version_id,
                    STATE_TRIAL,
                    reason,
                    canon.canonical_bytes(payload).decode("utf-8"),
                    content_digest,
                    actor,
                    approver,
                    now,
                ),
            )
            self._audit(
                con, actor, "申请更正", "correction", correction_id,
                {"version_id": version_id, "approver": approver, "content_digest": content_digest},
            )
            result = self._correction_view(con, correction_id)
            self._store_replay(con, scope, idempotency_key, request, result)
            return result

    def get_correction(self, correction_id: str) -> dict:
        with self.storage.read() as con:
            return self._correction_view(con, correction_id)

    def list_corrections(self, version_id: str) -> list[dict]:
        with self.storage.read() as con:
            self._version_row(con, version_id)
            rows = con.execute(
                "SELECT correction_id FROM corrections WHERE version_id = ? ORDER BY created_at, correction_id",
                (version_id,),
            ).fetchall()
            return [self._correction_view(con, row["correction_id"]) for row in rows]

    def sign_correction(
        self,
        *,
        correction_id: str,
        party: str,
        signer: str,
        idempotency_key: str | None = None,
    ) -> dict:
        _require(party, "party")
        if party not in PARTIES:
            raise BadRequest(f"签署方必须是 {' 或 '.join(PARTIES)}")
        _require(signer, "signer")
        request = {"correction_id": correction_id, "party": party, "signer": signer}
        scope = f"POST:/corrections/{correction_id}/signatures"
        with self.storage.write() as con:
            hit = self._replay(con, scope, idempotency_key, request)
            if hit is not None:
                return hit
            row = self._correction_row(con, correction_id)
            if row["state"] not in (STATE_TRIAL, STATE_SIGNING):
                raise Conflict(f"更正单当前状态为「{row['state']}」，不能会签")
            if con.execute(
                "SELECT 1 FROM correction_signatures WHERE correction_id = ? AND party = ?",
                (correction_id, party),
            ).fetchone():
                raise Conflict(f"{party}已对更正单完成会签，不能重复签署")
            now = self.clock()
            con.execute(
                "INSERT INTO correction_signatures (correction_id, party, signer, signed_digest, signed_at)"
                " VALUES (?,?,?,?,?)",
                (correction_id, party, signer, row["content_digest"], now),
            )
            if row["state"] == STATE_TRIAL:
                con.execute(
                    "UPDATE corrections SET state = ? WHERE correction_id = ?", (STATE_SIGNING, correction_id)
                )
            self._audit(
                con, signer, "更正会签", "correction", correction_id,
                {"party": party, "signed_digest": row["content_digest"]},
            )
            result = self._correction_view(con, correction_id)
            self._store_replay(con, scope, idempotency_key, request, result)
            return result

    def seal_correction(
        self,
        *,
        correction_id: str,
        actor: str,
        idempotency_key: str | None = None,
    ) -> dict:
        """更正单双方会签完成后生效（进入「更正」状态）。"""
        _require(actor, "actor")
        request = {"correction_id": correction_id, "actor": actor}
        scope = f"POST:/corrections/{correction_id}/seal"
        with self.storage.write() as con:
            hit = self._replay(con, scope, idempotency_key, request)
            if hit is not None:
                return hit
            row = self._correction_row(con, correction_id)
            if row["state"] == STATE_CORRECTED:
                result = self._correction_view(con, correction_id)
                self._store_replay(con, scope, idempotency_key, request, result)
                return result
            if row["state"] != STATE_SIGNING:
                raise Conflict(f"更正单当前状态为「{row['state']}」，不能生效")
            sigs = con.execute(
                "SELECT party, signed_digest FROM correction_signatures WHERE correction_id = ?",
                (correction_id,),
            ).fetchall()
            if {s["party"] for s in sigs} != set(PARTIES):
                raise Conflict("更正单双方会签未完成，不能生效")
            for sig in sigs:
                if sig["signed_digest"] != row["content_digest"]:
                    raise Conflict("签署摘要与更正单内容摘要不一致，不能生效")
            now = self.clock()
            updated = con.execute(
                "UPDATE corrections SET state = ?, sealed_at = ? WHERE correction_id = ? AND state = ?",
                (STATE_CORRECTED, now, correction_id, STATE_SIGNING),
            )
            if updated.rowcount != 1:
                raise Conflict("更正生效冲突：更正单状态已变化，请重试")
            self._audit(
                con, actor, "更正生效", "correction", correction_id,
                {"version_id": row["version_id"], "content_digest": row["content_digest"]},
            )
            result = self._correction_view(con, correction_id)
            self._store_replay(con, scope, idempotency_key, request, result)
            return result

    # ------------------------------------------------------------------
    # 分块导出
    # ------------------------------------------------------------------
    def create_export(self, *, target_type: str, target_id: str, actor: str) -> dict:
        """生成（或返回既有）分块导出件。

        导出件在首次生成时随封账内容一起冻结并落库；重复导出、中断
        续传、重复下载都读取同一份字节，结果完全一致。
        """
        _require(actor, "actor")
        if target_type not in (TARGET_VERSION, TARGET_CORRECTION):
            raise BadRequest("target_type 必须是 version 或 correction")
        with self.storage.write() as con:
            document = self._render_document(con, target_type, target_id)
            doc_digest = canon.sha256_hex(document)
            existing = con.execute(
                "SELECT * FROM exports WHERE target_type = ? AND target_id = ?", (target_type, target_id)
            ).fetchone()
            if existing:
                stored_manifest = json.loads(existing["manifest_json"])
                if stored_manifest["document_digest"] != doc_digest:
                    raise Conflict("导出件与已封账内容不一致，已拒绝覆盖")
                return self._export_view(existing)
            chunks = canon.split_chunks(document, self.chunk_size)
            manifest = {
                "target_type": target_type,
                "target_id": target_id,
                "document_digest": doc_digest,
                "document_size": len(document),
                "chunk_size": self.chunk_size,
                "chunk_count": len(chunks),
                "chunks": [
                    {"index": i, "size": len(chunk), "digest": canon.sha256_hex(chunk)}
                    for i, chunk in enumerate(chunks)
                ],
            }
            manifest_digest = canon.digest_of(manifest)
            now = self.clock()
            con.execute(
                """INSERT INTO exports
                   (target_type, target_id, chunk_size, chunk_count, document,
                    manifest_json, manifest_digest, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    target_type,
                    target_id,
                    self.chunk_size,
                    len(chunks),
                    sqlite3.Binary(document),
                    canon.canonical_bytes(manifest).decode("utf-8"),
                    manifest_digest,
                    now,
                ),
            )
            if target_type == TARGET_VERSION:
                con.execute(
                    "UPDATE versions SET state = ? WHERE version_id = ? AND state = ?",
                    (STATE_EXPORTED, target_id, STATE_SEALED),
                )
            self._audit(
                con, actor, "生成导出", target_type, target_id,
                {"manifest_digest": manifest_digest, "chunk_count": len(chunks)},
            )
            row = con.execute(
                "SELECT * FROM exports WHERE target_type = ? AND target_id = ?", (target_type, target_id)
            ).fetchone()
            return self._export_view(row)

    def get_manifest(self, *, target_type: str, target_id: str) -> dict:
        with self.storage.read() as con:
            return self._export_view(self._export_row(con, target_type, target_id))

    def get_chunk(self, *, target_type: str, target_id: str, index: int) -> tuple[bytes, dict]:
        """按序号取分块；客户端凭清单断点续传、逐块校验摘要。"""
        with self.storage.read() as con:
            row = self._export_row(con, target_type, target_id)
            if not 0 <= index < row["chunk_count"]:
                raise NotFound(f"分块不存在：{index}")
            document = bytes(row["document"])
            start = index * row["chunk_size"]
            chunk = document[start : start + row["chunk_size"]]
            meta = {
                "index": index,
                "digest": canon.sha256_hex(chunk),
                "manifest_digest": row["manifest_digest"],
                "chunk_count": row["chunk_count"],
            }
            return chunk, meta

    # ------------------------------------------------------------------
    # 审计
    # ------------------------------------------------------------------
    def list_audit(self, *, target_type: str | None = None, target_id: str | None = None) -> list[dict]:
        sql = "SELECT * FROM audit_log"
        conditions, args = [], []
        if target_type:
            conditions.append("target_type = ?")
            args.append(target_type)
        if target_id:
            conditions.append("target_id = ?")
            args.append(target_id)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY seq"
        with self.storage.read() as con:
            rows = con.execute(sql, args).fetchall()
        return [
            {
                "seq": row["seq"],
                "at": row["at"],
                "actor": row["actor"],
                "action": row["action"],
                "target_type": row["target_type"],
                "target_id": row["target_id"],
                "detail": json.loads(row["detail_json"]),
            }
            for row in rows
        ]

    # ------------------------------------------------------------------
    # 内部：行读取与视图
    # ------------------------------------------------------------------
    def _report_row(self, con: sqlite3.Connection, report_id: str) -> sqlite3.Row:
        row = con.execute("SELECT * FROM reports WHERE report_id = ?", (report_id,)).fetchone()
        if row is None:
            raise NotFound(f"报告不存在：{report_id}")
        return row

    def _version_row(self, con: sqlite3.Connection, version_id: str) -> sqlite3.Row:
        row = con.execute("SELECT * FROM versions WHERE version_id = ?", (version_id,)).fetchone()
        if row is None:
            raise NotFound(f"版本不存在：{version_id}")
        return row

    def _correction_row(self, con: sqlite3.Connection, correction_id: str) -> sqlite3.Row:
        row = con.execute("SELECT * FROM corrections WHERE correction_id = ?", (correction_id,)).fetchone()
        if row is None:
            raise NotFound(f"更正单不存在：{correction_id}")
        return row

    def _export_row(self, con: sqlite3.Connection, target_type: str, target_id: str) -> sqlite3.Row:
        row = con.execute(
            "SELECT * FROM exports WHERE target_type = ? AND target_id = ?", (target_type, target_id)
        ).fetchone()
        if row is None:
            raise NotFound(f"导出件不存在：{target_type}/{target_id}")
        return row

    def _require_latest(self, con: sqlite3.Connection, version_row: sqlite3.Row) -> None:
        latest = con.execute(
            "SELECT MAX(version_no) AS m FROM versions WHERE report_id = ?", (version_row["report_id"],)
        ).fetchone()["m"]
        if version_row["version_no"] != latest:
            raise Conflict("该版本已被更新版本取代，不能继续会签或封账")

    def _report_view(self, con: sqlite3.Connection, report_id: str) -> dict:
        row = self._report_row(con, report_id)
        versions = con.execute(
            "SELECT * FROM versions WHERE report_id = ? ORDER BY version_no", (report_id,)
        ).fetchall()
        return {
            "report_id": row["report_id"],
            "title": row["title"],
            "period": row["period"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "current_official_version_id": row["current_official_version_id"],
            "versions": [self._version_summary(con, item) for item in versions],
        }

    def _version_summary(self, con: sqlite3.Connection, row: sqlite3.Row) -> dict:
        latest = con.execute(
            "SELECT MAX(version_no) AS m FROM versions WHERE report_id = ?", (row["report_id"],)
        ).fetchone()["m"]
        return {
            "version_id": row["version_id"],
            "report_id": row["report_id"],
            "version_no": row["version_no"],
            "state": row["state"],
            "is_latest": row["version_no"] == latest,
            "input_digest": row["input_digest"],
            "content_digest": row["content_digest"],
            "sealed_seq": row["sealed_seq"],
        }

    def _version_view(self, con: sqlite3.Connection, version_id: str) -> dict:
        row = self._version_row(con, version_id)
        sigs = con.execute(
            "SELECT party, signer, signed_digest, signed_at FROM signatures WHERE version_id = ? ORDER BY party",
            (version_id,),
        ).fetchall()
        corrections = con.execute(
            "SELECT correction_id, state, reason, approver, created_at, sealed_at"
            " FROM corrections WHERE version_id = ? ORDER BY created_at, correction_id",
            (version_id,),
        ).fetchall()
        export = con.execute(
            "SELECT manifest_digest, chunk_count, created_at FROM exports"
            " WHERE target_type = ? AND target_id = ?",
            (TARGET_VERSION, version_id),
        ).fetchone()
        view = self._version_summary(con, row)
        view.update(
            {
                "data_version": row["data_version"],
                "rule_version": row["rule_version"],
                "payload": json.loads(row["payload_json"]),
                "created_by": row["created_by"],
                "created_at": row["created_at"],
                "sealed_at": row["sealed_at"],
                "signatures": [dict(sig) for sig in sigs],
                "corrections": [dict(item) for item in corrections],
                "export": dict(export) if export else None,
            }
        )
        return view

    def _correction_view(self, con: sqlite3.Connection, correction_id: str) -> dict:
        row = self._correction_row(con, correction_id)
        sigs = con.execute(
            "SELECT party, signer, signed_digest, signed_at FROM correction_signatures"
            " WHERE correction_id = ? ORDER BY party",
            (correction_id,),
        ).fetchall()
        export = con.execute(
            "SELECT manifest_digest, chunk_count, created_at FROM exports"
            " WHERE target_type = ? AND target_id = ?",
            (TARGET_CORRECTION, correction_id),
        ).fetchone()
        return {
            "correction_id": row["correction_id"],
            "version_id": row["version_id"],
            "state": row["state"],
            "reason": row["reason"],
            "payload": json.loads(row["payload_json"]),
            "content_digest": row["content_digest"],
            "requested_by": row["requested_by"],
            "approver": row["approver"],
            "created_at": row["created_at"],
            "sealed_at": row["sealed_at"],
            "signatures": [dict(sig) for sig in sigs],
            "export": dict(export) if export else None,
        }

    def _export_view(self, row: sqlite3.Row) -> dict:
        return {
            "target_type": row["target_type"],
            "target_id": row["target_id"],
            "chunk_size": row["chunk_size"],
            "chunk_count": row["chunk_count"],
            "manifest": json.loads(row["manifest_json"]),
            "manifest_digest": row["manifest_digest"],
            "created_at": row["created_at"],
        }

    # ------------------------------------------------------------------
    # 内部：导出件渲染（只使用封账时冻结的字段，保证确定性）
    # ------------------------------------------------------------------
    def _render_document(self, con: sqlite3.Connection, target_type: str, target_id: str) -> bytes:
        if target_type == TARGET_VERSION:
            row = self._version_row(con, target_id)
            if row["state"] not in (STATE_SEALED, STATE_EXPORTED):
                raise Conflict(f"版本当前状态为「{row['state']}」，封账后才能导出正式件")
            report = self._report_row(con, row["report_id"])
            sigs = con.execute(
                "SELECT party, signer, signed_digest, signed_at FROM signatures"
                " WHERE version_id = ? ORDER BY party",
                (target_id,),
            ).fetchall()
            document = {
                "document_type": "合作成果报告正式件",
                "schema_version": 1,
                "report": {
                    "report_id": report["report_id"],
                    "title": report["title"],
                    "period": report["period"],
                },
                "version": {
                    "version_id": row["version_id"],
                    "version_no": row["version_no"],
                    "data_version": row["data_version"],
                    "rule_version": row["rule_version"],
                    "input_digest": row["input_digest"],
                    "content_digest": row["content_digest"],
                    "created_by": row["created_by"],
                    "created_at": row["created_at"],
                    "sealed_at": row["sealed_at"],
                    "sealed_seq": row["sealed_seq"],
                },
                "payload": json.loads(row["payload_json"]),
                "signatures": [dict(sig) for sig in sigs],
            }
            return canon.canonical_bytes(document)
        row = self._correction_row(con, target_id)
        if row["state"] != STATE_CORRECTED:
            raise Conflict(f"更正单当前状态为「{row['state']}」，生效后才能导出")
        sigs = con.execute(
            "SELECT party, signer, signed_digest, signed_at FROM correction_signatures"
            " WHERE correction_id = ? ORDER BY party",
            (target_id,),
        ).fetchall()
        document = {
            "document_type": "合作成果报告更正单",
            "schema_version": 1,
            "correction": {
                "correction_id": row["correction_id"],
                "version_id": row["version_id"],
                "reason": row["reason"],
                "content_digest": row["content_digest"],
                "requested_by": row["requested_by"],
                "approver": row["approver"],
                "created_at": row["created_at"],
                "sealed_at": row["sealed_at"],
            },
            "payload": json.loads(row["payload_json"]),
            "signatures": [dict(sig) for sig in sigs],
        }
        return canon.canonical_bytes(document)

    # ------------------------------------------------------------------
    # 内部：幂等键与审计
    # ------------------------------------------------------------------
    def _replay(
        self, con: sqlite3.Connection, scope: str, key: str | None, request: dict
    ) -> dict | None:
        if not key:
            return None
        row = con.execute(
            "SELECT request_json, response_json FROM idempotency_keys WHERE key = ? AND scope = ?",
            (key, scope),
        ).fetchone()
        if row is None:
            return None
        if row["request_json"] != canon.canonical_bytes(request).decode("utf-8"):
            raise Conflict("幂等键已被不同请求使用")
        return json.loads(row["response_json"])

    def _store_replay(
        self, con: sqlite3.Connection, scope: str, key: str | None, request: dict, response: dict
    ) -> None:
        if not key:
            return
        con.execute(
            "INSERT INTO idempotency_keys (key, scope, request_json, response_json, created_at)"
            " VALUES (?,?,?,?,?)",
            (
                key,
                scope,
                canon.canonical_bytes(request).decode("utf-8"),
                json.dumps(response, ensure_ascii=False, sort_keys=True),
                self.clock(),
            ),
        )

    def _audit(
        self,
        con: sqlite3.Connection,
        actor: str,
        action: str,
        target_type: str,
        target_id: str,
        detail: dict,
    ) -> None:
        con.execute(
            "INSERT INTO audit_log (at, actor, action, target_type, target_id, detail_json)"
            " VALUES (?,?,?,?,?,?)",
            (
                self.clock(),
                actor,
                action,
                target_type,
                target_id,
                json.dumps(detail, ensure_ascii=False, sort_keys=True),
            ),
        )
