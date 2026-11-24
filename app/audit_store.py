"""SQLite-backed persistence for inspection rounds and receipt batches.

All mutations run inside ``BEGIN IMMEDIATE`` transactions, so concurrent
requests serialise at the write lock and each transaction re-reads the effective
receipts before validating and committing.  An accepted change is therefore
never lost by a concurrent update.  The optional ``expected_revision`` field is
an extra optimistic guard for clients that hold a stale view.

Data lives in one SQLite file (``AUDIT_DB_PATH``); Docker Compose keeps it on a
named volume, and the filtered catalogue is always recomputed from the effective
receipts, so it still agrees with them after a restart.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, Sequence

from .audit_logic import Analysis, ReceiptRec, analyse, explanation_rationale
from .audit_models import (
    AuditCatalogueFile,
    AuditCatalogueReplica,
    AuditCatalogueResponse,
    AuditCatalogueShard,
    AuditStartRequest,
    AuditState,
    BatchRequest,
    BatchResult,
    Explanation,
    FaultLocation,
    InspectionResponse,
    RoadmapStep,
    RetractResult,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rounds (
    round_id    TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    tape_units  TEXT NOT NULL,
    catalogue   TEXT NOT NULL,
    revision    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS batches (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id       TEXT NOT NULL REFERENCES rounds(round_id) ON DELETE CASCADE,
    receipt_id     TEXT NOT NULL,
    content_hash   TEXT NOT NULL,
    content        TEXT NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('effective', 'retracted')),
    created_at     TEXT NOT NULL,
    retracted_at   TEXT,
    retract_reason TEXT NOT NULL DEFAULT '',
    UNIQUE (round_id, receipt_id)
);
CREATE INDEX IF NOT EXISTS idx_batches_round_status
    ON batches (round_id, status, id);
"""

_init_lock = threading.Lock()
_initialised_paths: set[str] = set()


class RoundNotFound(LookupError):
    pass


class RevisionMismatch(RuntimeError):
    def __init__(self, current: int):
        super().__init__(f"修订号不匹配：当前为 {current}")
        self.current = current


class BatchReferenceError(ValueError):
    """The batch names a drive/replica that does not match the catalogue."""

    def __init__(self, result: BatchResult):
        super().__init__(result.detail)
        self.result = result


def db_path() -> str:
    return os.environ.get("AUDIT_DB_PATH", os.path.join("data", "audit.db"))


def _connect() -> sqlite3.Connection:
    path = db_path()
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.DatabaseError:  # pragma: no cover - read-only filesystems
        pass
    _ensure_schema(conn, path)
    return conn


def _ensure_schema(conn: sqlite3.Connection, path: str) -> None:
    key = os.path.abspath(path)
    if key in _initialised_paths:
        return
    with _init_lock:
        if key in _initialised_paths:
            return
        conn.executescript(_SCHEMA)
        _initialised_paths.add(key)


@contextmanager
def _transaction() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


@contextmanager
def _reader() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


def reset_storage() -> None:
    """Drop all rounds.  Intended for tests; not exposed over HTTP."""
    with _transaction() as conn:
        conn.execute("DELETE FROM batches")
        conn.execute("DELETE FROM rounds")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


# --------------------------------------------------------------------------- #
# Round lifecycle
# --------------------------------------------------------------------------- #
def create_round(request: AuditStartRequest) -> AuditState:
    round_id = uuid.uuid4().hex
    catalogue = request.catalogue.model_dump(mode="json")
    with _transaction() as conn:
        conn.execute(
            "INSERT INTO rounds (round_id, created_at, tape_units, catalogue, revision)"
            " VALUES (?, ?, ?, ?, 0)",
            (
                round_id,
                _now(),
                json.dumps(request.tape_units, ensure_ascii=False),
                json.dumps(catalogue, ensure_ascii=False),
            ),
        )
    return get_state(round_id)


def _load_round(conn: sqlite3.Connection, round_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM rounds WHERE round_id = ?", (round_id,)
    ).fetchone()
    if row is None:
        raise RoundNotFound(round_id)
    return row


def _replica_map(catalogue: dict) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for f in catalogue["files"]:
        for s in f["shards"]:
            for r in s["replicas"]:
                mapping[r["replica_id"]] = r["tape"]
    return mapping


def _effective_receipts(conn: sqlite3.Connection, round_id: str) -> list[ReceiptRec]:
    rows = conn.execute(
        "SELECT receipt_id, content FROM batches"
        " WHERE round_id = ? AND status = 'effective' ORDER BY id",
        (round_id,),
    ).fetchall()
    receipts: list[ReceiptRec] = []
    for row in rows:
        for entry in json.loads(row["content"]):
            receipts.append(ReceiptRec(receipt_id=row["receipt_id"], **entry))
    return receipts


def _counts(conn: sqlite3.Connection, round_id: str) -> tuple[int, int, int]:
    rows = conn.execute(
        "SELECT status, content FROM batches WHERE round_id = ?", (round_id,)
    ).fetchall()
    effective = sum(1 for row in rows if row["status"] == "effective")
    retracted = sum(1 for row in rows if row["status"] == "retracted")
    receipt_count = sum(
        len(json.loads(row["content"]))
        for row in rows
        if row["status"] == "effective"
    )
    return effective, retracted, receipt_count


def _state_from_conn(conn: sqlite3.Connection, round_row: sqlite3.Row) -> AuditState:
    catalogue = json.loads(round_row["catalogue"])
    files = len(catalogue["files"])
    shards = sum(len(f["shards"]) for f in catalogue["files"])
    replicas = sum(len(s["replicas"]) for f in catalogue["files"] for s in f["shards"])
    effective, retracted, receipt_count = _counts(conn, round_row["round_id"])
    return AuditState(
        round_id=round_row["round_id"],
        tape_units=json.loads(round_row["tape_units"]),
        files=files,
        shards=shards,
        replicas=replicas,
        revision=round_row["revision"],
        effective_batches=effective,
        retracted_batches=retracted,
        effective_receipts=receipt_count,
    )


def get_state(round_id: str) -> AuditState:
    with _reader() as conn:
        return _state_from_conn(conn, _load_round(conn, round_id))


# --------------------------------------------------------------------------- #
# Analysis helpers shared by the endpoints
# --------------------------------------------------------------------------- #
def _analyse(conn: sqlite3.Connection, round_row: sqlite3.Row,
             receipts: Sequence[ReceiptRec]) -> Analysis:
    catalogue = json.loads(round_row["catalogue"])
    return analyse(receipts, _replica_map(catalogue), json.loads(round_row["tape_units"]))


def _explanations(analysis: Analysis) -> list[Explanation]:
    return [
        Explanation(
            faults=[FaultLocation(kind=kind, id=ident) for kind, ident in cover],
            rationale=explanation_rationale(cover),
        )
        for cover in analysis.explanations
    ]


def _roadmap(analysis: Analysis) -> list[RoadmapStep]:
    return [
        RoadmapStep(
            action=action,  # type: ignore[arg-type]
            target=FaultLocation(kind=fault[0], id=fault[1]),
            reason=reason,
        )
        for action, fault, reason in analysis.roadmap
    ]


def _filtered_catalogue(
    round_row: sqlite3.Row, excluded: Sequence[str]
) -> AuditCatalogueResponse:
    catalogue = json.loads(round_row["catalogue"])
    dropped = set(excluded)
    files: list[AuditCatalogueFile] = []
    for f in catalogue["files"]:
        shards = [
            AuditCatalogueShard(
                id=s["id"],
                size=s["size"],
                replicas=[
                    AuditCatalogueReplica(
                        replica_id=r["replica_id"],
                        tape=r["tape"],
                        position=r["position"],
                    )
                    for r in s["replicas"]
                    if r["replica_id"] not in dropped
                ],
            )
            for s in f["shards"]
        ]
        files.append(AuditCatalogueFile(id=f["id"], shards=shards))
    return AuditCatalogueResponse(
        round_id=round_row["round_id"],
        revision=round_row["revision"],
        staging_capacity=catalogue["staging_capacity"],
        files=files,
        excluded_replicas=sorted(dropped),
        message=(
            "已滤除在每个故障解释下都判坏的副本；原文件与分片全部保留。"
            if dropped
            else "没有在每个故障解释下都判坏的副本，目录保持原样。"
        ),
    )


# --------------------------------------------------------------------------- #
# Batch submission
# --------------------------------------------------------------------------- #
def _normalise(receipts: Sequence) -> list[dict]:
    entries = [
        {
            "kind": r.kind,
            "tape": r.tape,
            "file_id": r.file_id,
            "shard_id": r.shard_id,
            "replica_id": r.replica_id,
            "position": r.position,
        }
        for r in receipts
    ]
    entries.sort(
        key=lambda e: (
            e["tape"],
            e["file_id"],
            e["shard_id"],
            "" if e["replica_id"] is None else e["replica_id"],
            -1.0 if e["position"] is None else e["position"],
            e["kind"],
        )
    )
    return entries


def _content_hash(entries: Sequence[dict]) -> str:
    payload = json.dumps(
        entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _resolve_entries(entries: Sequence[dict], catalogue: dict,
                     tape_units: Sequence[str]) -> tuple[list[dict], list[str]]:
    """Resolve each receipt to exactly one catalogue replica.

    Returns the entries rewritten with a concrete ``replica_id`` plus a list of
    reference problems.  A receipt may identify its replica by id or by
    ``(tape, position)``; both must land on the same catalogue copy as the
    ``file_id``/``shard_id`` pair.
    """
    known: dict[str, tuple[str, str, str, float]] = {}
    by_position: dict[tuple[str, str, str, float], str] = {}
    for f in catalogue["files"]:
        for s in f["shards"]:
            for r in s["replicas"]:
                known[r["replica_id"]] = (
                    f["id"],
                    s["id"],
                    r["tape"],
                    r["position"],
                )
                by_position[
                    (f["id"], s["id"], r["tape"], r["position"])
                ] = r["replica_id"]

    problems: list[str] = []
    resolved: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for entry in entries:
        replica_id = entry["replica_id"]
        if replica_id is None:
            position = entry["position"]
            replica_id = by_position.get(
                (entry["file_id"], entry["shard_id"], entry["tape"], position)
            )
            if replica_id is None:
                problems.append(
                    f"目录中找不到 {entry['file_id']}/{entry['shard_id']} 在带机 "
                    f"{entry['tape']} 位置 {position:g} 的副本"
                )
                continue
        target = known.get(replica_id)
        if target is None:
            problems.append(f"副本 {replica_id!r} 不在本轮目录中")
            continue
        file_id, shard_id, tape, position = target
        if (entry["file_id"], entry["shard_id"]) != (file_id, shard_id):
            problems.append(
                f"副本 {replica_id!r} 属于 {file_id}/{shard_id}，"
                f"回执写的是 {entry['file_id']}/{entry['shard_id']}"
            )
        if entry["tape"] != tape:
            problems.append(
                f"副本 {replica_id!r} 位于带机 {tape}，回执写的是 {entry['tape']}"
            )
        if entry["position"] is not None and abs(entry["position"] - position) > 1e-9:
            problems.append(
                f"副本 {replica_id!r} 位于位置 {position:g}，"
                f"回执写的是 {entry['position']:g}"
            )
        if entry["tape"] not in tape_units:
            problems.append(f"带机 {entry['tape']!r} 不在本轮巡检范围内")
        dedupe_key = (entry["tape"], file_id, replica_id)
        if dedupe_key in seen:
            problems.append(
                f"同一批次内重复报送副本 {replica_id!r}（可能一条用 "
                "replica_id、另一条用 position 指明）"
            )
            continue
        seen.add(dedupe_key)
        resolved.append(
            {
                "kind": entry["kind"],
                "tape": entry["tape"],
                "file_id": entry["file_id"],
                "shard_id": entry["shard_id"],
                "replica_id": replica_id,
            }
        )
    return resolved, problems


def _make_result(
    conn: sqlite3.Connection,
    round_row: sqlite3.Row,
    *,
    receipt_id: str,
    status: str,
    detail: str,
    effective: bool,
    analysis: Analysis,
    conflicts: Sequence[str] = (),
) -> BatchResult:
    return BatchResult(
        receipt_id=receipt_id,
        status=status,  # type: ignore[arg-type]
        revision=round_row["revision"],
        effective=effective,
        detail=detail,
        conflicts=list(conflicts),
        explanations=_explanations(analysis),
        excluded_replicas=analysis.excluded_replicas,
        state=_state_from_conn(conn, round_row),
    )


def submit_batch(round_id: str, request: BatchRequest) -> BatchResult:
    """Validate and store one receipt batch.

    Returns ``status="accepted"``, ``status="duplicate"`` or
    ``status="rejected"``; a rejected batch makes no database change.  A stale
    ``expected_revision`` raises ``RevisionMismatch``.
    """
    entries = _normalise(request.receipts)

    with _transaction() as conn:
        round_row = _load_round(conn, round_id)

        catalogue = json.loads(round_row["catalogue"])
        tape_units = json.loads(round_row["tape_units"])
        resolved, problems = _resolve_entries(entries, catalogue, tape_units)
        if problems:
            current = _analyse(
                conn, round_row, _effective_receipts(conn, round_id)
            )
            raise BatchReferenceError(
                _make_result(
                    conn,
                    round_row,
                    receipt_id=request.receipt_id,
                    status="rejected",
                    detail=(
                        f"回执号 {request.receipt_id!r} 引用了目录中不存在或不匹配"
                        "的带机/副本，整批拒收（未写入）：" + "；".join(problems)
                    ),
                    effective=False,
                    analysis=current,
                    conflicts=problems,
                )
            )
        entries = resolved
        content_hash = _content_hash(entries)

        existing = conn.execute(
            "SELECT * FROM batches WHERE round_id = ? AND receipt_id = ?",
            (round_id, request.receipt_id),
        ).fetchone()

        current_receipts = _effective_receipts(conn, round_id)
        current_analysis = _analyse(conn, round_row, current_receipts)

        # An identical re-submission is idempotent, even when the client retries
        # with the stale revision it saw before the first submission took effect.
        if (
            existing is not None
            and existing["status"] == "effective"
            and existing["content_hash"] == content_hash
        ):
            return _make_result(
                conn,
                round_row,
                receipt_id=request.receipt_id,
                status="duplicate",
                detail=(
                    f"回执号 {request.receipt_id!r} 内容与已生效批次相同，"
                    "本次不重复计数。"
                ),
                effective=True,
                analysis=current_analysis,
            )

        if (
            request.expected_revision is not None
            and request.expected_revision != round_row["revision"]
        ):
            raise RevisionMismatch(round_row["revision"])

        if existing is not None and existing["status"] == "retracted":
            return _make_result(
                conn,
                round_row,
                receipt_id=request.receipt_id,
                status="rejected",
                detail=(
                    f"回执号 {request.receipt_id!r} 已撤回，不得复用；"
                    "请换用新的回执号报送。"
                ),
                effective=False,
                analysis=current_analysis,
            )

        if existing is not None:
            return _make_result(
                conn,
                round_row,
                receipt_id=request.receipt_id,
                status="rejected",
                detail=(
                    f"回执号 {request.receipt_id!r} 已存在且内容不同，整批拒收；"
                    "如需更正请先撤回该号并使用新的回执号。"
                ),
                effective=True,
                analysis=current_analysis,
            )

        new_receipts = [
            ReceiptRec(receipt_id=request.receipt_id, **entry) for entry in entries
        ]
        analysis = _analyse(conn, round_row, list(current_receipts) + new_receipts)
        if analysis.conflict:
            labels = [r.label() for r in analysis.conflict_receipts]
            return _make_result(
                conn,
                round_row,
                receipt_id=request.receipt_id,
                status="rejected",
                detail=(
                    f"加入回执号 {request.receipt_id!r} 后，有效回执无法由不超过 "
                    f"2 处持续故障解释，整批拒收（未写入）。冲突回执："
                    + "；".join(labels)
                ),
                effective=False,
                analysis=current_analysis,
                conflicts=labels,
            )

        conn.execute(
            "INSERT INTO batches"
            " (round_id, receipt_id, content_hash, content, status, created_at)"
            " VALUES (?, ?, ?, ?, 'effective', ?)",
            (
                round_id,
                request.receipt_id,
                content_hash,
                json.dumps(entries, ensure_ascii=False),
                _now(),
            ),
        )
        conn.execute(
            "UPDATE rounds SET revision = revision + 1 WHERE round_id = ?",
            (round_id,),
        )
        round_row = _load_round(conn, round_id)
        return _make_result(
            conn,
            round_row,
            receipt_id=request.receipt_id,
            status="accepted",
            detail=f"回执号 {request.receipt_id!r} 的 {len(entries)} 条回执已生效。",
            effective=True,
            analysis=analysis,
        )


# --------------------------------------------------------------------------- #
# Retraction
# --------------------------------------------------------------------------- #
def retract_batch(
    round_id: str, receipt_id: str, reason: str, expected_revision: int | None
) -> RetractResult:
    with _transaction() as conn:
        round_row = _load_round(conn, round_id)

        existing = conn.execute(
            "SELECT * FROM batches WHERE round_id = ? AND receipt_id = ?",
            (round_id, receipt_id),
        ).fetchone()

        before_receipts = _effective_receipts(conn, round_id)
        before = _analyse(conn, round_row, before_receipts)

        # Retracting an already-retracted number is idempotent; report it even
        # if the caller's revision is now stale (nothing changes).
        if existing is not None and existing["status"] == "retracted":
            return RetractResult(
                receipt_id=receipt_id,
                status="already_retracted",
                revision=round_row["revision"],
                detail=f"回执号 {receipt_id!r} 已撤回；该号不得复用。",
                explanations=_explanations(before),
                excluded_replicas=before.excluded_replicas,
                state=_state_from_conn(conn, round_row),
            )

        if (
            expected_revision is not None
            and expected_revision != round_row["revision"]
        ):
            raise RevisionMismatch(round_row["revision"])

        if existing is None:
            return RetractResult(
                receipt_id=receipt_id,
                status="not_found",
                revision=round_row["revision"],
                detail=f"回执号 {receipt_id!r} 未报送过，无可撤回。",
                explanations=_explanations(before),
                excluded_replicas=before.excluded_replicas,
                state=_state_from_conn(conn, round_row),
            )

        conn.execute(
            "UPDATE batches SET status = 'retracted', retracted_at = ?,"
            " retract_reason = ? WHERE round_id = ? AND receipt_id = ?",
            (_now(), reason, round_id, receipt_id),
        )
        conn.execute(
            "UPDATE rounds SET revision = revision + 1 WHERE round_id = ?",
            (round_id,),
        )
        round_row = _load_round(conn, round_id)
        after = _analyse(conn, round_row, _effective_receipts(conn, round_id))
        restored = sorted(
            set(before.excluded_replicas) - set(after.excluded_replicas)
        )
        detail = f"已撤回回执号 {receipt_id!r} 的整批回执；该号不得复用。"
        if restored:
            detail += "以下副本不再被判为确定损坏，已恢复为可选：" + "、".join(
                restored
            ) + "。"
        return RetractResult(
            receipt_id=receipt_id,
            status="retracted",
            revision=round_row["revision"],
            detail=detail,
            explanations=_explanations(after),
            excluded_replicas=after.excluded_replicas,
            restored_replicas=restored,
            state=_state_from_conn(conn, round_row),
        )


# --------------------------------------------------------------------------- #
# Read endpoints
# --------------------------------------------------------------------------- #
def inspection(round_id: str) -> InspectionResponse:
    with _reader() as conn:
        round_row = _load_round(conn, round_id)
        analysis = _analyse(
            conn, round_row, _effective_receipts(conn, round_id)
        )
        state = _state_from_conn(conn, round_row)

    return InspectionResponse(
        round_id=round_id,
        state=state,
        explanations=_explanations(analysis),
        conflicts=[r.label() for r in analysis.conflict_receipts],
        requires_decision=analysis.conflict,
        excluded_replicas=analysis.excluded_replicas,
        undecided_replicas=analysis.undecided_replicas,
        certain_faults=[
            FaultLocation(kind=kind, id=ident)
            for kind, ident in analysis.certain_faults
        ],
        possible_faults=[
            FaultLocation(kind=kind, id=ident)
            for kind, ident in analysis.possible_faults
        ],
        verdict=analysis.verdict,
        roadmap=_roadmap(analysis),
    )


def schedule_catalogue(round_id: str) -> AuditCatalogueResponse:
    with _reader() as conn:
        round_row = _load_round(conn, round_id)
        analysis = _analyse(
            conn, round_row, _effective_receipts(conn, round_id)
        )
        if analysis.conflict:
            raise ValueError(
                "当前有效回执无法解释，拒绝生成供 /schedule 使用的目录；"
                "请先复核并撤回误报批次。"
            )
        return _filtered_catalogue(round_row, analysis.excluded_replicas)
