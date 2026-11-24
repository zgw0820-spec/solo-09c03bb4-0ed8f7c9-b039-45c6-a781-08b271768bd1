"""Exact scheduler for restoring files from tapes through a staging disk.

Model
-----
While a tape is mounted the head only moves forward.  Shards may be skipped, so
a mount can read any subset of the replicas on that tape, in ascending position
order.  A shard is read exactly once (re-reading only wastes staging space).

Staging is freed when a file is complete: as soon as the last missing shard of a
file has been read, that file is verified and delivered, and the space of all of
its shards is released.  Verification needs the data on the staging disk, so the
usage peak of a read is measured *before* the completed file is released.

Because every shard is read exactly once, the only history that influences the
future is the set of already-read shards; the set of delivered files and the
staging usage are a pure function of that set.  The scheduler therefore runs a
breadth-first search over read-sets.  Every edge is one mount, so the first
layer in which the full set is reached is the minimum number of mounts.

Feasibility
-----------
Two static conditions make a request unschedulable:

* a shard without any replica, and
* a file whose total size exceeds the staging capacity (all of its shards are
  pending at the instant of verification).

They are also sufficient: restore files one at a time, mounting any tape that
holds a replica of the next needed shard.  The pending data never exceeds one
file's total size.

Ordering inside a mount
-----------------------
For a fixed set of shards read in a mount, staging usage depends only on the
set, never on read order or on which replica of a duplicate was used.  Let
``usage(S)`` be the post-delivery usage after reading exactly ``S`` (a global
function of the set).  The capacity rule for appending shard ``i`` is then
simply ``usage(S) + size(i) <= capacity``.

Among the replicas of a shard on one tape, choosing the smallest position that
is not before the current cursor is always optimal: it keeps the widest choice
of later reads.  This gives an exact dynamic program

    best[S] = smallest cursor position reachable after reading the set S

over the subsets of ``allowed[tape]``.  A state is kept only if the capacity
rule holds for the shard just appended, so the DP enumerates exactly the
read-sets that can be realised in one mount.  Its total size is bounded by
``3**shards_per_tape`` per tape, which is small for the supported limits
(6 tapes, 12 shards, 48 replicas).
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from typing import Optional

from .models import (
    CapacityIssue,
    Explanation,
    MissingShard,
    MountStep,
    ReadAction,
    ScheduleRequest,
    ScheduleResponse,
)

EPS = 1e-9


@dataclass(frozen=True)
class _Shard:
    idx: int
    shard_id: str
    file_idx: int
    size: float


def _bits(mask: int) -> list[int]:
    out: list[int] = []
    while mask:
        low = mask & -mask
        out.append(low.bit_length() - 1)
        mask ^= low
    return out


def _set_cover_lower_bound(allowed_masks: list[int], full_mask: int) -> int:
    """Smallest number of tape-mounts whose combined shard coverage is complete."""
    tape_count = len(allowed_masks)
    if tape_count == 0:
        return 0
    best = tape_count + 1
    for subset in range(1, 1 << tape_count):
        union = 0
        count = 0
        for ti in range(tape_count):
            if subset >> ti & 1:
                union |= allowed_masks[ti]
                count += 1
        if union == full_mask and count < best:
            best = count
    return best


@dataclass
class _Step:
    prev_mask: int
    shard_idx: int
    position: float


def _mount_dp(
    start_mask: int,
    tape_idx: int,
    allowed_mask: int,
    capacity: float,
    tol: float,
    sizes: list[float],
    usage: list[float],
    positions: list[dict[int, list[float]]],
) -> dict[int, _Step]:
    """All read-sets reachable from ``start_mask`` in one mount on this tape.

    Returns ``{mask: step}`` where following ``step.prev_mask`` back to
    ``start_mask`` yields the read order.  ``start_mask`` itself is excluded.
    """
    remaining = allowed_mask & ~start_mask
    neg_inf = float("-inf")

    best_last: dict[int, float] = {start_mask: neg_inf}
    step_of: dict[int, _Step] = {}
    queue = [start_mask]

    while queue:
        mask = queue.pop()
        last = best_last[mask]
        base_usage = usage[mask]
        for i in _bits(remaining & ~mask):
            if base_usage + sizes[i] > capacity + tol:
                continue
            cand = positions[i][tape_idx]
            k = bisect_left(cand, last)
            if k == len(cand):
                continue
            pos = cand[k]
            new_mask = mask | (1 << i)
            previous = best_last.get(new_mask)
            if previous is not None and previous <= pos:
                continue
            best_last[new_mask] = pos
            step_of[new_mask] = _Step(mask, i, pos)
            queue.append(new_mask)

    return step_of


def _reconstruct_reads(start_mask: int, target: int, step_of: dict[int, _Step]):
    chain: list[_Step] = []
    node = target
    while node != start_mask:
        st = step_of[node]
        chain.append(st)
        node = st.prev_mask
    chain.reverse()
    return chain


def solve(req: ScheduleRequest) -> ScheduleResponse:
    capacity = float(req.staging_capacity)
    tol = EPS * max(1.0, abs(capacity))

    shards: list[_Shard] = []
    file_masks: list[int] = []
    file_totals: list[float] = []
    file_ids: list[str] = []
    replica_positions: list[tuple[str, float]] = []

    for fi, file in enumerate(req.files):
        fmask = 0
        total = 0.0
        for shard in file.shards:
            idx = len(shards)
            fmask |= 1 << idx
            total += float(shard.size)
            shards.append(
                _Shard(
                    idx=idx,
                    shard_id=shard.id,
                    file_idx=fi,
                    size=float(shard.size),
                )
            )
            replica_positions.append(
                tuple((r.tape, float(r.position)) for r in shard.replicas)
            )
        file_masks.append(fmask)
        file_totals.append(total)
        file_ids.append(file.id)

    n = len(shards)
    state_count = 1 << n
    full_mask = state_count - 1

    # --- static infeasibility -------------------------------------------------
    missing: list[MissingShard] = []
    for sh in shards:
        if not replica_positions[sh.idx]:
            missing.append(
                MissingShard(
                    shard_id=sh.shard_id,
                    file_id=file_ids[sh.file_idx],
                    reason="分片在磁带目录中没有任何副本",
                )
            )

    capacity_issues: list[CapacityIssue] = []
    for fi, total in enumerate(file_totals):
        if total > capacity + tol:
            capacity_issues.append(
                CapacityIssue(
                    file_id=file_ids[fi],
                    required=total,
                    capacity=capacity,
                    message=(
                        f"文件 {file_ids[fi]} 的所有分片在交付前必须同时暂存，"
                        f"需要 {total:g}，超过中转盘容量 {capacity:g}"
                    ),
                )
            )

    if missing or capacity_issues:
        parts = []
        if missing:
            parts.append(f"{len(missing)} 个分片缺失副本")
        if capacity_issues:
            parts.append(f"{len(capacity_issues)} 个文件超出中转盘容量")
        return ScheduleResponse(
            feasible=False,
            missing_shards=missing,
            capacity_issues=capacity_issues,
            message="无法排程：" + "；".join(parts),
        )

    # --- tapes and positions --------------------------------------------------
    tape_names = sorted({tape for reps in replica_positions for tape, _ in reps})
    tape_index = {name: i for i, name in enumerate(tape_names)}
    allowed_masks = [0] * len(tape_names)
    positions: list[dict[int, list[float]]] = [dict() for _ in range(n)]
    for idx, reps in enumerate(replica_positions):
        for tape, pos in reps:
            ti = tape_index[tape]
            allowed_masks[ti] |= 1 << idx
            positions[idx].setdefault(ti, [])
            if pos not in positions[idx][ti]:
                positions[idx][ti].append(pos)
    for per_tape in positions:
        for values in per_tape.values():
            values.sort()

    # --- derived staging tables ----------------------------------------------
    sizes = [sh.size for sh in shards]

    size_sum = [0.0] * state_count
    for mask in range(1, state_count):
        low = mask & -mask
        size_sum[mask] = size_sum[mask ^ low] + sizes[low.bit_length() - 1]

    delivered_sum = [0.0] * state_count
    for mask in range(state_count):
        freed = 0.0
        for fi, fmask in enumerate(file_masks):
            if fmask & mask == fmask:
                freed += file_totals[fi]
        delivered_sum[mask] = freed

    usage = [max(0.0, size_sum[m] - delivered_sum[m]) for m in range(state_count)]

    # --- BFS over read-sets (one layer per mount) -----------------------------
    visited = {0}
    parent: dict[int, Optional[int]] = {0: None}
    edge: dict[int, tuple[int, int]] = {}  # new_mask -> (tape_idx, prev_mask)
    frontier = [0]
    found: Optional[int] = None
    mounts_used = 0

    while frontier and found is None:
        mounts_used += 1
        next_frontier: list[int] = []
        for mask in frontier:
            for ti, allowed in enumerate(allowed_masks):
                if not allowed & ~mask:
                    continue
                step_of = _mount_dp(
                    mask, ti, allowed, capacity, tol, sizes, usage, positions
                )
                for new_mask in step_of:
                    if new_mask in visited:
                        continue
                    visited.add(new_mask)
                    parent[new_mask] = mask
                    edge[new_mask] = (ti, mask)
                    next_frontier.append(new_mask)
                    if new_mask == full_mask:
                        found = new_mask
                        break
                if found is not None:
                    break
            if found is not None:
                break
        frontier = next_frontier

    if found is None:  # pragma: no cover - excluded by the static checks
        return ScheduleResponse(
            feasible=False,
            message="无法排程：未能在给定约束下找到可行装带顺序",
        )

    # --- reconstruct ----------------------------------------------------------
    path: list[int] = []
    node: Optional[int] = found
    while node is not None:
        path.append(node)
        node = parent[node]
    path.reverse()

    steps: list[MountStep] = []
    delivered_order: list[str] = []
    for mount_no, new_mask in enumerate(path[1:], start=1):
        ti, prev_mask = edge[new_mask]
        step_of = _mount_dp(
            prev_mask, ti, allowed_masks[ti], capacity, tol, sizes, usage, positions
        )
        chain = _reconstruct_reads(prev_mask, new_mask, step_of)

        tape = tape_names[ti]
        reads: list[ReadAction] = []
        mount_deliveries: list[str] = []
        peak = usage[prev_mask]
        for step in chain:
            # Pre-delivery peak for this read: pending before it plus the shard.
            peak = max(peak, usage[step.prev_mask] + sizes[step.shard_idx])
            prefix = step.prev_mask | (1 << step.shard_idx)
            sh = shards[step.shard_idx]
            delivered_id: Optional[str] = None
            completed = [
                fi
                for fi, fmask in enumerate(file_masks)
                if fmask & prefix == fmask and fmask & step.prev_mask != fmask
            ]
            for fi in completed:
                delivered_id = file_ids[fi]
                if delivered_id not in mount_deliveries:
                    mount_deliveries.append(delivered_id)
                    delivered_order.append(delivered_id)
            reads.append(
                ReadAction(
                    shard_id=sh.shard_id,
                    file_id=file_ids[sh.file_idx],
                    tape=tape,
                    position=step.position,
                    size=sh.size,
                    delivers_file=delivered_id,
                )
            )

        steps.append(
            MountStep(
                mount=mount_no,
                tape=tape,
                read_from=reads[0].position,
                read_to=reads[-1].position,
                reads=reads,
                delivered_files=mount_deliveries,
                staging_before=usage[prev_mask],
                staging_peak=peak,
                staging_after=usage[new_mask],
            )
        )

    lower_bound = _set_cover_lower_bound(allowed_masks, full_mask)
    if lower_bound == mounts_used:
        detail = (
            f"按“每盘带一次能覆盖的分片”计算的下界为 {lower_bound} 次装带，"
            f"本排程达到该下界，故为最少装带次数。"
        )
    else:
        detail = (
            f"按分片覆盖计算的下界为 {lower_bound} 次装带；中转盘容量限制要求"
            f"部分磁带分多次装带。对读集状态做广度优先搜索得到的最短序列为 "
            f"{mounts_used} 次装带，穷尽更少次数的组合均不可行，故为最少值。"
        )

    return ScheduleResponse(
        feasible=True,
        min_mounts=mounts_used,
        mounts=steps,
        files_restored=delivered_order,
        total_bytes_read=size_sum[full_mask],
        message=f"最少需要 {mounts_used} 次装带",
        explanation=Explanation(
            lower_bound=lower_bound,
            constructive_value=mounts_used,
            lower_bound_achieved=lower_bound == mounts_used,
            detail=detail,
        ),
    )


def solve_dict(payload: dict) -> ScheduleResponse:
    """Convenience wrapper used by the HTTP layer and the tests."""
    return solve(ScheduleRequest.model_validate(payload))
