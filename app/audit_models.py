"""Pydantic models for the tape read-verification (巡检) API.

Semantics
---------
A read pass writes *receipts* (回执) in batches identified by a receipt number
(回执号).  Every receipt names the tape drive used and the exact replica that
was read, and reports whether the read passed.

A tape read passes exactly when both the drive and the replica are healthy; a
fault in either one makes it fail.  Persistent faults are assumed to affect at
most two locations in total (drives + replicas).  A batch is accepted only when
the set of effective receipts stays explainable by some combination of at most
two fault locations.  An identical re-submission of an accepted receipt number
is ignored (no double counting); different content under the same number is
rejected; a retracted number is never reused.

The inspection result lists every *inclusion-minimal* combination that explains
all effective receipts (removing any single fault from it breaks the
explanation).  The catalogue handed to ``/schedule`` drops only replicas that
are unusable under *every* such combination, keeping all files and shards.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_FILES = 12
MAX_SHARDS = 24
MAX_REPLICAS = 48
MAX_FAULTS = 2
MAX_BATCH_ENTRIES = 200
MAX_TAPES = 2


# --------------------------------------------------------------------------- #
# Catalogue (the same nesting as /schedule; replica_id is derived if absent)
# --------------------------------------------------------------------------- #
class AuditReplicaIn(BaseModel):
    replica_id: str | None = Field(
        None,
        min_length=1,
        description=(
            "副本编号，目录内唯一；可省略。省略时按 文件/分片@带机:位置 自动生成，"
            "回执仍可用带机+位置指明该副本"
        ),
    )
    tape: str = Field(..., min_length=1, description="副本所在带机编号")
    position: float = Field(..., ge=0, description="副本在带上的位置")


def derived_replica_id(file_id: str, shard_id: str, tape: str, position: float) -> str:
    return f"{file_id}/{shard_id}@{tape}:{position:g}"


class AuditShardIn(BaseModel):
    id: str = Field(..., min_length=1)
    size: float = Field(..., gt=0, description="该分片需要的中转空间")
    replicas: list[AuditReplicaIn] = Field(default_factory=list)


class AuditFileIn(BaseModel):
    id: str = Field(..., min_length=1)
    shards: list[AuditShardIn] = Field(..., min_length=1)


class AuditCatalogueIn(BaseModel):
    """A catalogue in /schedule shape; replica ids are optional."""

    staging_capacity: float = Field(..., gt=0)
    files: list[AuditFileIn] = Field(..., min_length=1)


DEFAULT_ROUND_EXAMPLE = {
    "tape_units": ["DRV-1", "DRV-2"],
    "catalogue": {
        "staging_capacity": 10,
        "files": [
            {
                "id": "file-A",
                "shards": [
                    {
                        "id": "a1",
                        "size": 2,
                        "replicas": [
                            {"replica_id": "a1-d1", "tape": "DRV-1", "position": 10},
                            {"replica_id": "a1-d2", "tape": "DRV-2", "position": 10},
                        ],
                    },
                    {
                        "id": "a2",
                        "size": 3,
                        "replicas": [
                            {"replica_id": "a2-d1", "tape": "DRV-1", "position": 30},
                            {"replica_id": "a2-d2", "tape": "DRV-2", "position": 30},
                        ],
                    },
                ],
            },
            {
                "id": "file-B",
                "shards": [
                    {
                        "id": "b1",
                        "size": 4,
                        "replicas": [
                            {"replica_id": "b1-d1", "tape": "DRV-1", "position": 20},
                            {"replica_id": "b1-d2", "tape": "DRV-2", "position": 20},
                        ],
                    },
                    {
                        "id": "b2",
                        "size": 1,
                        "replicas": [
                            {"replica_id": "b2-d1", "tape": "DRV-1", "position": 40},
                            {"replica_id": "b2-d2", "tape": "DRV-2", "position": 40},
                        ],
                    },
                ],
            },
        ],
    },
}


class AuditStartRequest(BaseModel):
    """Start one inspection round from a catalogue and two tape-drive ids."""

    model_config = ConfigDict(json_schema_extra={"examples": [DEFAULT_ROUND_EXAMPLE]})

    tape_units: list[str] = Field(
        ...,
        min_length=MAX_TAPES,
        max_length=MAX_TAPES,
        description="参与本轮巡检的两台带机编号（顺序不限）",
        json_schema_extra={"examples": [["DRV-1", "DRV-2"]]},
    )
    catalogue: AuditCatalogueIn = Field(
        ...,
        description=(
            "/schedule 格式的目录；可为每个副本附唯一 replica_id（可省略，自动生成）"
        ),
    )

    @model_validator(mode="after")
    def _check_catalogue(self) -> "AuditStartRequest":
        if self.tape_units[0] == self.tape_units[1]:
            raise ValueError("两台带机编号必须不同")

        file_count = 0
        shard_count = 0
        replica_count = 0
        replica_ids: set[str] = set()
        positions: set[tuple[str, str, str, float]] = set()
        for f in self.catalogue.files:
            file_count += 1
            for s in f.shards:
                shard_count += 1
                for r in s.replicas:
                    replica_count += 1
                    if r.tape not in self.tape_units:
                        raise ValueError(
                            f"副本 {r.replica_id or r.position!r} 位于未参与巡检的带机 "
                            f"{r.tape!r}"
                        )
                    if r.replica_id is None:
                        r.replica_id = derived_replica_id(
                            f.id, s.id, r.tape, r.position
                        )
                    if r.replica_id in replica_ids:
                        raise ValueError(f"duplicate replica id: {r.replica_id!r}")
                    replica_ids.add(r.replica_id)
                    key = (f.id, s.id, r.tape, r.position)
                    if key in positions:
                        raise ValueError(
                            "同一分片在同一带机的同一位置出现两次："
                            f"{f.id}/{s.id}@{r.tape}:{r.position:g}"
                        )
                    positions.add(key)
        if file_count > MAX_FILES:
            raise ValueError(f"at most {MAX_FILES} files, got {file_count}")
        if shard_count > MAX_SHARDS:
            raise ValueError(f"at most {MAX_SHARDS} shards, got {shard_count}")
        if replica_count > MAX_REPLICAS:
            raise ValueError(f"at most {MAX_REPLICAS} replicas, got {replica_count}")
        return self


# --------------------------------------------------------------------------- #
# Receipts (batched by receipt number)
# --------------------------------------------------------------------------- #
class PassReceiptIn(BaseModel):
    kind: Literal["pass"]
    tape: str = Field(..., min_length=1, description="读片所用带机编号")
    file_id: str = Field(..., min_length=1)
    shard_id: str = Field(..., min_length=1)
    replica_id: str | None = Field(
        None, min_length=1, description="所读的具体副本编号（或改用 position 指明）"
    )
    position: float | None = Field(
        None, ge=0, description="副本在带上的位置；与 replica_id 二选一"
    )

    @model_validator(mode="after")
    def _identify(self) -> "PassReceiptIn":
        if self.replica_id is None and self.position is None:
            raise ValueError("每条回执必须给出 replica_id 或 position 以指明具体副本")
        return self


class FailReceiptIn(BaseModel):
    kind: Literal["fail"]
    tape: str = Field(..., min_length=1, description="读片所用带机编号")
    file_id: str = Field(..., min_length=1)
    shard_id: str = Field(..., min_length=1)
    replica_id: str | None = Field(
        None, min_length=1, description="所读的具体副本编号（或改用 position 指明）"
    )
    position: float | None = Field(
        None, ge=0, description="副本在带上的位置；与 replica_id 二选一"
    )

    @model_validator(mode="after")
    def _identify(self) -> "FailReceiptIn":
        if self.replica_id is None and self.position is None:
            raise ValueError("每条回执必须给出 replica_id 或 position 以指明具体副本")
        return self


ReceiptIn = Annotated[Union[PassReceiptIn, FailReceiptIn], Field(discriminator="kind")]

DEFAULT_BATCH_EXAMPLE = {
    "receipt_id": "R-2024-001",
    "receipts": [
        {
            "kind": "pass",
            "tape": "DRV-1",
            "file_id": "file-A",
            "shard_id": "a1",
            "replica_id": "a1-d1",
        },
        {
            "kind": "fail",
            "tape": "DRV-2",
            "file_id": "file-A",
            "shard_id": "a1",
            "replica_id": "a1-d2",
        },
    ],
}


class BatchRequest(BaseModel):
    """One submission batch, filed under ``receipt_id``."""

    model_config = ConfigDict(json_schema_extra={"examples": [DEFAULT_BATCH_EXAMPLE]})

    receipt_id: str = Field(..., min_length=1, description="本次报送的回执号")
    receipts: list[ReceiptIn] = Field(
        ...,
        min_length=1,
        max_length=MAX_BATCH_ENTRIES,
        description="本批次全部读片结果；每条指明带机与具体副本",
    )
    expected_revision: int | None = Field(
        None,
        ge=0,
        description="可选：客户端看到的修订号；不一致时以 409 拒绝，避免覆盖已生效的改动",
    )

    @model_validator(mode="after")
    def _no_duplicate_entries(self) -> "BatchRequest":
        seen: set[tuple[str, str, str, str | float]] = set()
        for r in self.receipts:
            target = r.replica_id if r.replica_id is not None else r.position
            key = (r.tape, r.file_id, r.shard_id, target)  # type: ignore[arg-type]
            if key in seen:
                raise ValueError("同一批次内重复报送同一带机/副本: " + "/".join(map(str, key)))
            seen.add(key)
        return self


class RetractRequest(BaseModel):
    """Retract a whole batch by receipt number.  Retracted numbers are never reused."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [{"receipt_id": "R-2024-001", "reason": "误报，撤销该批回执"}]
        }
    )

    receipt_id: str = Field(..., min_length=1)
    reason: str = Field("", description="撤回原因（仅记录）")
    expected_revision: int | None = Field(None, ge=0)


# --------------------------------------------------------------------------- #
# Result models
# --------------------------------------------------------------------------- #
class FaultLocation(BaseModel):
    kind: Literal["tape", "replica"]
    id: str


class Explanation(BaseModel):
    """One inclusion-minimal combination that explains every effective receipt."""

    faults: list[FaultLocation]
    rationale: str


class RoadmapStep(BaseModel):
    action: Literal["retire", "verify"]
    target: FaultLocation
    reason: str


class AuditState(BaseModel):
    round_id: str
    tape_units: list[str]
    files: int
    shards: int
    replicas: int
    revision: int = Field(
        0, description="每接受/撤回一个批次加一；用于读-改-写并发控制"
    )
    effective_batches: int = 0
    retracted_batches: int = 0
    effective_receipts: int = 0


class BatchResult(BaseModel):
    receipt_id: str
    status: Literal["accepted", "duplicate", "rejected"]
    revision: int
    effective: bool = Field(
        True, description="该回执号当前是否计入有效回执（duplicate 为已计入）"
    )
    detail: str = ""
    conflicts: list[str] = Field(
        default_factory=list,
        description="拒绝时给出无法共存的极小冲突回执集（逐条可读）",
    )
    explanations: list[Explanation] = Field(
        default_factory=list,
        description="按当前全部有效回执计算的全部极小故障组合",
    )
    excluded_replicas: list[str] = Field(default_factory=list)
    state: AuditState


class RetractResult(BaseModel):
    receipt_id: str
    status: Literal["retracted", "not_found", "already_retracted", "rejected"]
    revision: int
    detail: str = ""
    explanations: list[Explanation] = Field(default_factory=list)
    excluded_replicas: list[str] = Field(default_factory=list)
    restored_replicas: list[str] = Field(
        default_factory=list,
        description="撤回前被判为确定损坏、撤回后不再确定而恢复可选的副本",
    )
    state: AuditState | None = None


class InspectionResponse(BaseModel):
    round_id: str
    state: AuditState
    explanations: list[Explanation] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    requires_decision: bool = Field(
        False,
        description="有效回执无法由任何 ≤2 处故障的组合解释；需复核并撤回误报",
    )
    excluded_replicas: list[str] = Field(
        default_factory=list,
        description="在每个极小解释组合下都不可用的副本；/schedule 目录会滤掉",
    )
    undecided_replicas: list[str] = Field(
        default_factory=list,
        description="在部分组合下判坏、但并非所有组合都判坏的副本；仍可选，建议复读",
    )
    certain_faults: list[FaultLocation] = Field(default_factory=list)
    possible_faults: list[FaultLocation] = Field(default_factory=list)
    verdict: str = ""
    roadmap: list[RoadmapStep] = Field(default_factory=list)


class AuditCatalogueReplica(BaseModel):
    replica_id: str
    tape: str
    position: float


class AuditCatalogueShard(BaseModel):
    id: str
    size: float
    replicas: list[AuditCatalogueReplica]


class AuditCatalogueFile(BaseModel):
    id: str
    shards: list[AuditCatalogueShard]


class AuditCatalogueResponse(BaseModel):
    round_id: str
    revision: int
    staging_capacity: float
    files: list[AuditCatalogueFile] = Field(
        ..., description="保留原文件与分片；仅过滤确定损坏的副本"
    )
    excluded_replicas: list[str] = Field(default_factory=list)
    message: str = ""
