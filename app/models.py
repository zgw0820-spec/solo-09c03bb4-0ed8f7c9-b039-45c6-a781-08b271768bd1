"""Pydantic models for the tape-restore scheduler API.

Semantics
---------
* A *shard* is the unit that must be read exactly once.  It has a size (the
  amount of staging space it needs) and one or more *replicas*.
* A *replica* lives at a position on a tape.  While a tape is mounted the read
  head only moves forward, so the positions read during one mount must be
  non-decreasing.  Shards may be skipped.  Mounting a tape again (including
  rewinding the same tape) starts a new mount.
* Shards read but not yet delivered occupy the staging disk.  As soon as every
  shard of a file has been read the file is verified, delivered, and the space
  of all of its shards is released.  The peak usage therefore has to fit into
  the staging capacity.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_TAPES = 6
MAX_SHARDS = 12
MAX_REPLICAS = 48


class ReplicaIn(BaseModel):
    """One physical copy of a shard at a position on a tape."""

    tape: str = Field(..., min_length=1, description="Tape identifier, e.g. T1")
    position: float = Field(
        ...,
        ge=0,
        description=(
            "Offset of the replica on the tape.  Reads within one mount must "
            "use non-decreasing positions (forward-only head, skipping allowed)."
        ),
    )


class ShardIn(BaseModel):
    """A piece of a file that has to be read exactly once."""

    id: str = Field(..., min_length=1, description="Shard id, unique inside its file")
    size: float = Field(..., gt=0, description="Staging space this shard occupies")
    replicas: list[ReplicaIn] = Field(
        default_factory=list,
        description="Copies of this shard; an empty list marks the shard as missing",
    )


class FileIn(BaseModel):
    """A logical file assembled from shards."""

    id: str = Field(..., min_length=1, description="File id, unique in the catalogue")
    shards: list[ShardIn] = Field(..., min_length=1)


DEFAULT_EXAMPLE = {
    "staging_capacity": 10,
    "files": [
        {
            "id": "file-A",
            "shards": [
                {
                    "id": "a1",
                    "size": 2,
                    "replicas": [{"tape": "T1", "position": 10}, {"tape": "T2", "position": 5}],
                },
                {
                    "id": "a2",
                    "size": 3,
                    "replicas": [{"tape": "T1", "position": 30}, {"tape": "T3", "position": 7}],
                },
            ],
        },
        {
            "id": "file-B",
            "shards": [
                {
                    "id": "b1",
                    "size": 4,
                    "replicas": [{"tape": "T1", "position": 20}, {"tape": "T2", "position": 9}],
                },
                {
                    "id": "b2",
                    "size": 1,
                    "replicas": [{"tape": "T2", "position": 20}],
                },
            ],
        },
    ],
}


class ScheduleRequest(BaseModel):
    """Staging-disk free space plus the catalogue of tapes."""

    model_config = ConfigDict(json_schema_extra={"examples": [DEFAULT_EXAMPLE]})

    staging_capacity: float = Field(
        ...,
        gt=0,
        description="Free space on the staging disk; pending shards must fit at all times",
    )
    files: list[FileIn] = Field(..., min_length=1)
    objective: Literal["min_mounts"] = Field(
        "min_mounts",
        description="Optimisation target (only minimum number of tape mounts)",
    )

    @model_validator(mode="after")
    def _check_limits(self) -> "ScheduleRequest":
        tapes: set[str] = set()
        shard_count = 0
        replica_count = 0
        file_ids: set[str] = set()
        for f in self.files:
            if f.id in file_ids:
                raise ValueError(f"duplicate file id: {f.id!r}")
            file_ids.add(f.id)
            shard_ids: set[str] = set()
            for s in f.shards:
                if s.id in shard_ids:
                    raise ValueError(f"duplicate shard id {s.id!r} in file {f.id!r}")
                shard_ids.add(s.id)
                shard_count += 1
                replica_count += len(s.replicas)
                for r in s.replicas:
                    tapes.add(r.tape)
        if shard_count > MAX_SHARDS:
            raise ValueError(
                f"at most {MAX_SHARDS} shards are supported, got {shard_count}"
            )
        if replica_count > MAX_REPLICAS:
            raise ValueError(
                f"at most {MAX_REPLICAS} replicas are supported, got {replica_count}"
            )
        if len(tapes) > MAX_TAPES:
            raise ValueError(
                f"at most {MAX_TAPES} tapes are supported, got {len(tapes)}"
            )
        return self


class ReadAction(BaseModel):
    """One forward read during a mount."""

    shard_id: str
    file_id: str
    tape: str
    position: float
    size: float
    delivers_file: Optional[str] = Field(
        None,
        description="File that becomes complete (verified + delivered) at this read",
    )


class MountStep(BaseModel):
    """One tape mount: ordered reads, resulting deliveries and staging state."""

    mount: int = Field(..., ge=1, description="1-based mount order")
    tape: str
    read_from: float = Field(..., description="Position of the first read in this mount")
    read_to: float = Field(..., description="Position of the last read in this mount")
    reads: list[ReadAction] = Field(
        ..., description="Reads in tape order (positions are non-decreasing)"
    )
    delivered_files: list[str] = Field(
        default_factory=list, description="Files delivered by the end of this mount"
    )
    staging_before: float = Field(
        ..., description="Staging usage when this mount starts"
    )
    staging_peak: float = Field(
        ..., description="Peak staging usage during this mount"
    )
    staging_after: float = Field(
        ..., description="Staging usage after the deliveries of this mount"
    )


class MissingShard(BaseModel):
    shard_id: str
    file_id: str
    reason: str


class CapacityIssue(BaseModel):
    file_id: str
    required: float
    capacity: float
    message: str


class Explanation(BaseModel):
    """Readable justification for the optimality claim."""

    lower_bound: int = Field(..., description="Set-cover lower bound on mounts")
    constructive_value: int = Field(..., description="Mounts in the returned schedule")
    lower_bound_achieved: bool = Field(
        ..., description="True when the cover lower bound was reached"
    )
    detail: str


class ScheduleResponse(BaseModel):
    feasible: bool
    min_mounts: Optional[int] = None
    mounts: list[MountStep] = Field(default_factory=list)
    files_restored: list[str] = Field(default_factory=list)
    total_bytes_read: Optional[float] = None
    missing_shards: list[MissingShard] = Field(default_factory=list)
    capacity_issues: list[CapacityIssue] = Field(default_factory=list)
    message: str = ""
    explanation: Optional[Explanation] = None
