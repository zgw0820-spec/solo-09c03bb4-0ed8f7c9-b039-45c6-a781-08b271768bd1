"""Tests for the tape restore scheduler.

The important test is ``test_matches_bruteforce``: it compares the BFS result
with an independent exhaustive search over mount sequences on many random small
instances, which validates both the minimum count and the state model.

``replay`` is an independent validator: it replays a returned schedule against
the request and asserts every rule (one replica per shard, forward-only reads,
capacity, immediate delivery) without trusting the solver's own bookkeeping.
"""

from __future__ import annotations

import itertools
import random
from typing import Any

import pytest

from app.models import ScheduleRequest
from app.solver import solve


# --------------------------------------------------------------------------- #
# Independent replay validator
# --------------------------------------------------------------------------- #
def replay(payload: dict[str, Any], result) -> None:
    assert result.feasible, result.message

    shard_size: dict[tuple[str, str], float] = {}
    replicas: dict[tuple[str, str], list[tuple[str, float]]] = {}
    file_shards: dict[str, set[str]] = {}
    seen_keys: set[tuple[str, str]] = set()
    for f in payload["files"]:
        file_shards[f["id"]] = set()
        for s in f["shards"]:
            key = (f["id"], s["id"])
            assert key not in seen_keys, f"duplicate shard {key}"
            seen_keys.add(key)
            shard_size[key] = float(s["size"])
            replicas[key] = [(r["tape"], float(r["position"])) for r in s["replicas"]]
            file_shards[f["id"]].add(s["id"])

    capacity = float(payload["staging_capacity"])
    read: set[tuple[str, str]] = set()
    delivered: set[str] = set()
    pending: dict[tuple[str, str], float] = {}

    assert result.min_mounts == len(result.mounts)

    for expected_mount, step in enumerate(result.mounts, start=1):
        assert step.mount == expected_mount
        positions: list[float] = []
        peak_seen = sum(pending.values())
        assert step.staging_before == pytest.approx(sum(pending.values()))
        for action in step.reads:
            assert action.tape == step.tape
            key = (action.file_id, action.shard_id)
            # One replica per shard, never read twice.
            assert key not in read, f"shard {key} read twice"
            assert key in replicas, f"unknown shard {key}"
            read.add(key)
            # The chosen replica must actually exist on this tape at this position.
            assert (action.tape, action.position) in replicas[key]
            # Forward-only.
            positions.append(action.position)
            # Size matches the catalogue.
            assert action.size == pytest.approx(shard_size[key])

            pending[key] = action.size
            # Peak is measured before release, so include the read just made.
            peak_seen = max(peak_seen, sum(pending.values()))
            assert sum(pending.values()) <= capacity + 1e-9, (
                f"mount {step.mount}: staging usage {sum(pending.values())} "
                f"exceeds {capacity}"
            )

            fid = action.file_id
            complete = file_shards[fid] <= {sid for (f, sid) in read if f == fid}
            if complete:
                assert action.delivers_file == fid, (
                    f"file {fid} completed at shard {action.shard_id} but not delivered"
                )
                assert fid not in delivered
                for sid in file_shards[fid]:
                    pending.pop((fid, sid))
                delivered.add(fid)
            else:
                assert action.delivers_file is None
        assert positions == sorted(positions), "reads not in forward order"
        if positions:
            assert step.read_from == positions[0]
            assert step.read_to == positions[-1]
        assert step.staging_peak == pytest.approx(peak_seen), (
            "staging_peak does not match the observed peak"
        )
        assert step.staging_after == pytest.approx(sum(pending.values()))

    assert read == seen_keys, "not every shard was read"
    assert delivered == set(file_shards), "not every file was delivered"
    assert set(result.files_restored) == set(file_shards)


# --------------------------------------------------------------------------- #
# Independent brute-force minimum (tiny instances only)
# --------------------------------------------------------------------------- #
def brute_force_min(payload: dict[str, Any]) -> int | None:
    """Exhaustive search over mount sequences, independent of the solver."""

    ShardKey = tuple[str, str]  # (file id, shard id)

    file_shards: dict[str, set[ShardKey]] = {}
    shard_size: dict[ShardKey, float] = {}
    shard_file: dict[ShardKey, str] = {}
    reps_of: dict[ShardKey, list[dict[str, Any]]] = {}
    on_tape: dict[str, set[ShardKey]] = {}
    for f in payload["files"]:
        file_shards[f["id"]] = set()
        for s in f["shards"]:
            key = (f["id"], s["id"])
            shard_size[key] = float(s["size"])
            shard_file[key] = f["id"]
            reps_of[key] = s["replicas"]
            file_shards[f["id"]].add(key)
            for r in s["replicas"]:
                on_tape.setdefault(r["tape"], set()).add(key)

    capacity = float(payload["staging_capacity"])
    all_shards = frozenset(shard_size)
    tapes = sorted(on_tape)

    best: int | None = None

    def mount_feasible(
        tape: str, read_before: frozenset[ShardKey], subset: frozenset[ShardKey]
    ) -> tuple[bool, frozenset[ShardKey]]:
        # Choose replica positions: each shard may have several on the tape.
        choices = []
        for key in subset:
            positions = sorted(
                {float(r["position"]) for r in reps_of[key] if r["tape"] == tape}
            )
            choices.append(positions)
        for combo in itertools.product(*choices):
            # Ties on position may be read in any order; try every ordering.
            order_pairs = sorted(zip(combo, subset))
            groups: list[list[Any]] = []
            for pos, key in order_pairs:
                if groups and groups[-1][0] == pos:
                    groups[-1][1].append(key)
                else:
                    groups.append([pos, [key]])
            orders = itertools.product(
                *[list(itertools.permutations(g[1])) for g in groups]
            )
            for flat in orders:
                order = [key for perm in flat for key in perm]
                cur_read = read_before
                done = {f for f, sids in file_shards.items() if sids <= cur_read}
                pending = {
                    k: shard_size[k] for k in cur_read if shard_file[k] not in done
                }
                peak = sum(pending.values())
                for key in order:
                    pending[key] = shard_size[key]
                    peak = max(peak, sum(pending.values()))
                    cur_read = cur_read | {key}
                    done = {f for f, sids in file_shards.items() if sids <= cur_read}
                    for f in done:
                        for k in file_shards[f]:
                            pending.pop(k, None)
                if peak <= capacity + 1e-9:
                    return True, cur_read
        return False, read_before

    def dfs(read: frozenset[ShardKey], depth: int) -> None:
        nonlocal best
        if best is not None and depth >= best:
            return
        if read == all_shards:
            best = depth
            return
        for tape in tapes:
            remaining = on_tape[tape] - read
            if not remaining:
                continue
            subset_list = []
            for r in range(1, len(remaining) + 1):
                subset_list.extend(frozenset(c) for c in itertools.combinations(remaining, r))
            # Prefer larger subsets to find good solutions early.
            subset_list.sort(key=len, reverse=True)
            for subset in subset_list:
                ok, new_read = mount_feasible(tape, read, subset)
                if ok:
                    dfs(new_read, depth + 1)

    dfs(frozenset(), 0)
    return best


# --------------------------------------------------------------------------- #
# Fixed cases
# --------------------------------------------------------------------------- #
def test_example_is_two_mounts():
    payload = {
        "staging_capacity": 10,
        "files": [
            {
                "id": "file-A",
                "shards": [
                    {"id": "a1", "size": 2, "replicas": [{"tape": "T1", "position": 10}, {"tape": "T2", "position": 5}]},
                    {"id": "a2", "size": 3, "replicas": [{"tape": "T1", "position": 30}, {"tape": "T3", "position": 7}]},
                ],
            },
            {
                "id": "file-B",
                "shards": [
                    {"id": "b1", "size": 4, "replicas": [{"tape": "T1", "position": 20}, {"tape": "T2", "position": 9}]},
                    {"id": "b2", "size": 1, "replicas": [{"tape": "T2", "position": 20}]},
                ],
            },
        ],
    }
    result = solve(ScheduleRequest.model_validate(payload))
    replay(payload, result)
    assert result.min_mounts == 2
    assert result.explanation.lower_bound_achieved is True


def test_forward_only_requires_second_mount_same_tape():
    # a1 at 10, a2 at 5 on the same tape only: reading a1 then a2 needs a rewind.
    payload = {
        "staging_capacity": 100,
        "files": [
            {
                "id": "F",
                "shards": [
                    {"id": "s1", "size": 1, "replicas": [{"tape": "T1", "position": 10}]},
                    {"id": "s2", "size": 1, "replicas": [{"tape": "T1", "position": 5}]},
                ],
            }
        ],
    }
    result = solve(ScheduleRequest.model_validate(payload))
    replay(payload, result)
    # Read order is ascending, so a single forward pass suffices.
    assert result.min_mounts == 1


def test_capacity_forces_repeated_mount_of_same_tape():
    # Two files, 6 units each, capacity 8, shards interleaved by position:
    # x1(10), y1(20), x2(30), y2(40).  Any single forward pass peaks at 9 before
    # a file completes, so the tape must be mounted twice (a partial pass can
    # never complete both files either, since the missing shard is always
    # behind the head).
    payload = {
        "staging_capacity": 8,
        "files": [
            {
                "id": "F1",
                "shards": [
                    {"id": "x1", "size": 3, "replicas": [{"tape": "T1", "position": 10}]},
                    {"id": "x2", "size": 3, "replicas": [{"tape": "T1", "position": 30}]},
                ],
            },
            {
                "id": "F2",
                "shards": [
                    {"id": "y1", "size": 3, "replicas": [{"tape": "T1", "position": 20}]},
                    {"id": "y2", "size": 3, "replicas": [{"tape": "T1", "position": 40}]},
                ],
            },
        ],
    }
    result = solve(ScheduleRequest.model_validate(payload))
    replay(payload, result)
    assert result.min_mounts == 2
    assert [m.tape for m in result.mounts] == ["T1", "T1"]
    assert result.explanation.lower_bound_achieved is False


def test_missing_replica_is_reported():
    payload = {
        "staging_capacity": 100,
        "files": [
            {
                "id": "F",
                "shards": [
                    {"id": "s1", "size": 1, "replicas": []},
                    {"id": "s2", "size": 1, "replicas": [{"tape": "T1", "position": 1}]},
                ],
            }
        ],
    }
    result = solve(ScheduleRequest.model_validate(payload))
    assert not result.feasible
    assert result.missing_shards[0].shard_id == "s1"
    assert result.capacity_issues == []


def test_capacity_issue_is_reported():
    payload = {
        "staging_capacity": 5,
        "files": [
            {
                "id": "F",
                "shards": [
                    {"id": "s1", "size": 4, "replicas": [{"tape": "T1", "position": 1}]},
                    {"id": "s2", "size": 4, "replicas": [{"tape": "T1", "position": 2}]},
                ],
            }
        ],
    }
    result = solve(ScheduleRequest.model_validate(payload))
    assert not result.feasible
    assert result.capacity_issues[0].file_id == "F"
    assert result.capacity_issues[0].required == 8


def test_both_failure_kinds_reported_together():
    payload = {
        "staging_capacity": 1,
        "files": [
            {"id": "F", "shards": [{"id": "s1", "size": 2, "replicas": []}]},
        ],
    }
    result = solve(ScheduleRequest.model_validate(payload))
    assert not result.feasible
    assert len(result.missing_shards) == 1
    assert len(result.capacity_issues) == 1


def test_limits_rejected():
    too_many_tapes = {
        "staging_capacity": 100,
        "files": [
            {
                "id": "F",
                "shards": [
                    {"id": f"s{i}", "size": 1, "replicas": [{"tape": f"T{i}", "position": 1}]}
                    for i in range(7)
                ],
            }
        ],
    }
    with pytest.raises(Exception):
        ScheduleRequest.model_validate(too_many_tapes)

    too_many_shards = {
        "staging_capacity": 100,
        "files": [
            {
                "id": "F",
                "shards": [
                    {"id": f"s{i}", "size": 1, "replicas": [{"tape": "T1", "position": i}]}
                    for i in range(13)
                ],
            }
        ],
    }
    with pytest.raises(Exception):
        ScheduleRequest.model_validate(too_many_shards)


def test_shard_ids_may_repeat_across_files():
    # Shard "s0" appears in both files; they are distinct data and both must be
    # read exactly once.
    payload = {
        "staging_capacity": 10,
        "files": [
            {
                "id": "F0",
                "shards": [{"id": "s0", "size": 1, "replicas": [{"tape": "T1", "position": 5}]}],
            },
            {
                "id": "F1",
                "shards": [{"id": "s0", "size": 1, "replicas": [{"tape": "T1", "position": 9}]}],
            },
        ],
    }
    result = solve(ScheduleRequest.model_validate(payload))
    replay(payload, result)
    assert result.min_mounts == 1
    assert len(result.mounts[0].reads) == 2


def test_duplicate_shard_ids_rejected():
    payload = {
        "staging_capacity": 100,
        "files": [
            {
                "id": "F",
                "shards": [
                    {"id": "s", "size": 1, "replicas": [{"tape": "T1", "position": 1}]},
                    {"id": "s", "size": 1, "replicas": [{"tape": "T1", "position": 2}]},
                ],
            }
        ],
    }
    with pytest.raises(Exception):
        ScheduleRequest.model_validate(payload)


# --------------------------------------------------------------------------- #
# Randomised cross-check against brute force
# --------------------------------------------------------------------------- #
def random_instance(rng: random.Random, n_shards: int, n_tapes: int) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    shard_files = rng.randint(1, min(3, n_shards))
    sizes = [rng.randint(1, 5) for _ in range(n_shards)]
    assignments = [[] for _ in range(shard_files)]
    for i in range(n_shards):
        assignments[rng.randrange(shard_files)].append(i)
    files = []
    for fi, idxs in enumerate(assignments):
        if not idxs:
            continue
        shards = []
        for i in idxs:
            rep_count = rng.randint(1, min(2, n_tapes))
            tapes = rng.sample(range(n_tapes), rep_count)
            replicas = []
            used: set[int] = set()
            for t in tapes:
                pos = rng.randint(0, 30)
                while (t, pos) in used:
                    pos = rng.randint(0, 30)
                used.add((t, pos))
                replicas.append({"tape": f"T{t}", "position": pos})
            shards.append({"id": f"s{i}", "size": sizes[i], "replicas": replicas})
        files.append({"id": f"F{fi}", "shards": shards})
    if not files:
        return random_instance(rng, n_shards, n_tapes)
    total_max_file = max(sum(s["size"] for s in f["shards"]) for f in files)
    capacity = rng.randint(total_max_file, total_max_file + 6)
    return {"staging_capacity": capacity, "files": files}


@pytest.mark.parametrize("seed", range(120))
def test_matches_bruteforce(seed: int):
    rng = random.Random(seed)
    n_shards = rng.randint(1, 4)
    n_tapes = rng.randint(1, 2)
    payload = random_instance(rng, n_shards, n_tapes)
    result = solve(ScheduleRequest.model_validate(payload))
    expected = brute_force_min(payload)
    assert result.feasible
    replay(payload, result)
    assert result.min_mounts == expected, (
        f"seed={seed} solver={result.min_mounts} brute={expected} payload={payload}"
    )


@pytest.mark.parametrize("seed", range(300, 312))
def test_matches_bruteforce_five_shards(seed: int):
    rng = random.Random(seed)
    payload = random_instance(rng, 5, rng.randint(1, 3))
    result = solve(ScheduleRequest.model_validate(payload))
    assert result.feasible
    replay(payload, result)
    assert result.min_mounts == brute_force_min(payload), f"seed={seed} payload={payload}"
