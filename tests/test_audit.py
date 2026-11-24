"""Tests for the tape read-verification (巡检) API.

The attribution check below is independent of ``app.audit_logic``: it brute-
forces every fault set of size <= 2 over all tapes and replicas, keeps the
inclusion-minimal ones, and compares with the API.  This validates the reported
explanations without trusting the production cover enumeration.
"""

from __future__ import annotations

import itertools
import json
import threading
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def catalogue() -> dict[str, Any]:
    """Two drives, four shards, two replicas per shard (one per drive)."""
    files = []
    for file_id, shard_ids in (("file-A", ["a1", "a2"]), ("file-B", ["b1", "b2"])):
        shards = []
        for sid in shard_ids:
            shards.append(
                {
                    "id": sid,
                    "size": 1,
                    "replicas": [
                        {"replica_id": f"{sid}-d1", "tape": "DRV-1", "position": 10},
                        {"replica_id": f"{sid}-d2", "tape": "DRV-2", "position": 10},
                    ],
                }
            )
        files.append({"id": file_id, "shards": shards})
    return {"staging_capacity": 10, "files": files}


def start_round(cat: dict[str, Any] | None = None) -> str:
    response = client.post(
        "/audit/rounds",
        json={"tape_units": ["DRV-1", "DRV-2"], "catalogue": cat or catalogue()},
    )
    assert response.status_code == 201, response.text
    return response.json()["round_id"]


def receipt(
    kind: str, tape: str, sid: str, replica: str
) -> dict[str, Any]:
    file_id = "file-A" if sid in ("a1", "a2") else "file-B"
    return {
        "kind": kind,
        "tape": tape,
        "file_id": file_id,
        "shard_id": sid,
        "replica_id": replica,
    }


def submit(round_id: str, receipt_id: str, receipts: list[dict], **extra):
    return client.post(
        f"/audit/rounds/{round_id}/batches",
        json={"receipt_id": receipt_id, "receipts": receipts, **extra},
    )


def retract(round_id: str, receipt_id: str, reason: str = "误报"):
    return client.post(
        f"/audit/rounds/{round_id}/retract",
        json={"receipt_id": receipt_id, "reason": reason},
    )


def inspect(round_id: str):
    response = client.get(f"/audit/rounds/{round_id}")
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# Independent attribution reference
# --------------------------------------------------------------------------- #
def brute_explanations(
    cat: dict[str, Any], receipts: list[dict]
) -> list[frozenset[tuple[str, str]]]:
    tapes = sorted(
        {r["tape"] for f in cat["files"] for s in f["shards"] for r in s["replicas"]}
    )
    replicas = sorted(
        {
            r["replica_id"]
            for f in cat["files"]
            for s in f["shards"]
            for r in s["replicas"]
        }
    )
    universe = [("tape", t) for t in tapes] + [("replica", r) for r in replicas]

    def explains(fs: frozenset[tuple[str, str]]) -> bool:
        faulty_tapes = {i for k, i in fs if k == "tape"}
        faulty_reps = {i for k, i in fs if k == "replica"}
        for rec in receipts:
            healthy = (
                rec["tape"] not in faulty_tapes
                and rec["replica_id"] not in faulty_reps
            )
            if healthy != (rec["kind"] == "pass"):
                return False
        return True

    explains_sets = [
        frozenset(combo)
        for size in (0, 1, 2)
        for combo in itertools.combinations(universe, size)
        if explains(frozenset(combo))
    ]
    minimal = [
        fs
        for fs in explains_sets
        if not any(
            other < fs for other in explains_sets
        )
    ]
    return minimal


def normalise_explanations(payload: list[dict]) -> set[frozenset[tuple[str, str]]]:
    return {
        frozenset((f["kind"], f["id"]) for f in explanation["faults"])
        for explanation in payload
    }


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_DB_PATH", str(tmp_path / "audit.db"))
    yield


# --------------------------------------------------------------------------- #
# Round creation / validation
# --------------------------------------------------------------------------- #
def test_start_round_and_example():
    example = client.get("/audit/example").json()
    response = client.post("/audit/rounds", json=example)
    assert response.status_code == 201
    state = response.json()
    assert state["tape_units"] == ["DRV-1", "DRV-2"]
    assert state["files"] == 2
    assert state["shards"] == 4
    assert state["replicas"] == 8
    assert state["revision"] == 0


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c["catalogue"]["files"][0]["shards"][0]["replicas"][0].update(
            {"tape": "DRV-9"}
        ),
        lambda c: c["catalogue"]["files"][0]["shards"][0]["replicas"][1].update(
            {"replica_id": c["catalogue"]["files"][0]["shards"][0]["replicas"][0]["replica_id"]}
        ),
        lambda c: c.update({"tape_units": ["DRV-1", "DRV-1"]}),
        lambda c: c.update({"tape_units": ["DRV-1"]}),
    ],
)
def test_invalid_round_is_422(mutate):
    payload = json.loads(json.dumps(client.get("/audit/example").json()))
    mutate(payload)
    assert client.post("/audit/rounds", json=payload).status_code == 422


def test_unknown_round_is_404():
    assert client.get("/audit/rounds/nope").status_code == 404
    assert submit("nope", "R1", [receipt("pass", "DRV-1", "a1", "a1-d1")]).status_code == 404
    assert retract("nope", "R1").status_code == 404


# --------------------------------------------------------------------------- #
# Attribution
# --------------------------------------------------------------------------- #
def test_clean_round_reports_no_faults():
    rid = start_round()
    response = submit(
        rid,
        "R1",
        [
            receipt("pass", "DRV-1", "a1", "a1-d1"),
            receipt("pass", "DRV-2", "a1", "a1-d2"),
        ],
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "accepted"
    assert body["explanations"][0]["faults"] == []
    assert body["state"]["revision"] == 1
    result = inspect(rid)
    assert result["requires_decision"] is False
    assert result["certain_faults"] == []
    assert result["possible_faults"] == []
    assert result["excluded_replicas"] == []
    assert "均正常" in result["verdict"]


def test_single_replica_fault_is_certain():
    rid = start_round()
    response = submit(
        rid,
        "R1",
        [
            receipt("pass", "DRV-1", "a1", "a1-d1"),
            receipt("fail", "DRV-2", "a1", "a1-d2"),
            receipt("pass", "DRV-2", "a2", "a2-d2"),
        ],
    )
    assert response.status_code == 201, response.text
    result = inspect(rid)
    assert normalise_explanations(result["explanations"]) == {
        frozenset({("replica", "a1-d2")})
    }
    assert result["certain_faults"] == [{"kind": "replica", "id": "a1-d2"}]
    assert result["excluded_replicas"] == ["a1-d2"]
    assert result["roadmap"][0]["action"] == "retire"
    assert result["roadmap"][0]["target"]["id"] == "a1-d2"
    assert "隔离副本 a1-d2" in result["verdict"]


def test_multiple_minimal_explanations_are_all_listed():
    rid = start_round()
    receipts = [
        receipt("fail", "DRV-1", "a1", "a1-d1"),
        receipt("fail", "DRV-2", "a1", "a1-d2"),
    ]
    response = submit(rid, "R1", receipts)
    assert response.status_code == 201, response.text
    result = inspect(rid)
    expected = set(brute_explanations(catalogue(), receipts))
    # No pass receipt pins a drive, so two tape faults and two replica faults
    # are both admissible; all four are inclusion-minimal.
    assert expected == {
        frozenset({("tape", "DRV-1"), ("tape", "DRV-2")}),
        frozenset({("replica", "a1-d1"), ("tape", "DRV-2")}),
        frozenset({("tape", "DRV-1"), ("replica", "a1-d2")}),
        frozenset({("replica", "a1-d1"), ("replica", "a1-d2")}),
    }
    assert normalise_explanations(result["explanations"]) == expected
    # a1-d1 and a1-d2 are bad under every explanation; the other replicas on
    # those drives are bad only when the drive itself is assumed faulty.
    assert result["excluded_replicas"] == ["a1-d1", "a1-d2"]
    assert result["certain_faults"] == []
    assert len(result["possible_faults"]) == 4
    assert "建议隔离的副本：a1-d1、a1-d2" in result["verdict"]
    assert "取决于解释" in result["verdict"]
    assert "尚不能唯一确定" not in result["verdict"]
    assert {step["action"] for step in result["roadmap"]} == {"verify"}
    assert all(
        step["target"]["kind"] == "tape" or step["target"]["id"].startswith("a1-")
        for step in result["roadmap"]
    )


def test_two_fault_combination_with_pinning_passes():
    rid = start_round()
    receipts = [
        # Pin DRV-1 healthy (a pass on it), so the a1-d1 failure must be the replica.
        receipt("pass", "DRV-1", "a2", "a2-d1"),
        receipt("fail", "DRV-1", "a1", "a1-d1"),
        receipt("fail", "DRV-2", "a1", "a1-d2"),
    ]
    response = submit(rid, "R1", receipts)
    assert response.status_code == 201, response.text
    result = inspect(rid)
    expected = set(brute_explanations(catalogue(), receipts))
    assert expected == {
        frozenset({("replica", "a1-d1"), ("tape", "DRV-2")}),
        frozenset({("replica", "a1-d1"), ("replica", "a1-d2")}),
    }
    assert normalise_explanations(result["explanations"]) == expected
    assert result["certain_faults"] == [{"kind": "replica", "id": "a1-d1"}]
    # Every explanation contains a1-d1.  a1-d2 is also bad in every explanation:
    # either the replica is faulty or the whole DRV-2 is, and it lives there.
    assert result["excluded_replicas"] == ["a1-d1", "a1-d2"]
    # DRV-2's other replicas are bad only in the tape-fault explanation.
    assert "a2-d2" in result["undecided_replicas"]
    assert "a1-d1" not in result["undecided_replicas"]
    assert "a1-d1" in [step["target"]["id"] for step in result["roadmap"]]


def test_explanations_are_inclusion_minimal():
    rid = start_round()
    receipts = [
        receipt("fail", "DRV-1", "a1", "a1-d1"),
        receipt("fail", "DRV-2", "a1", "a1-d2"),
    ]
    response = submit(rid, "R1", receipts)
    assert response.status_code == 201, response.text
    result = inspect(rid)
    expected = set(brute_explanations(catalogue(), receipts))
    assert normalise_explanations(result["explanations"]) == expected
    for explanation in result["explanations"]:
        faults = frozenset((f["kind"], f["id"]) for f in explanation["faults"])
        for dropped in faults:
            assert faults - {dropped} not in expected, "explanation was not minimal"


def test_batch_accumulates_and_matches_bruteforce():
    rid = start_round()
    batches = [
        [
            receipt("pass", "DRV-1", "a1", "a1-d1"),
            receipt("fail", "DRV-2", "a1", "a1-d2"),
        ],
        [
            receipt("pass", "DRV-1", "a2", "a2-d1"),
            receipt("fail", "DRV-2", "a2", "a2-d2"),
        ],
    ]
    all_receipts: list[dict] = []
    for i, batch in enumerate(batches, start=1):
        assert submit(rid, f"R{i}", batch).status_code == 201
        all_receipts.extend(batch)
    result = inspect(rid)
    assert normalise_explanations(result["explanations"]) == set(
        brute_explanations(catalogue(), all_receipts)
    )
    assert result["state"]["effective_batches"] == 2
    assert result["state"]["effective_receipts"] == 4
    assert result["state"]["revision"] == 2


# --------------------------------------------------------------------------- #
# Re-submission, conflicts, retraction
# --------------------------------------------------------------------------- #
def test_identical_resubmission_is_not_double_counted():
    rid = start_round()
    batch = [
        receipt("pass", "DRV-1", "a1", "a1-d1"),
        receipt("fail", "DRV-2", "a1", "a1-d2"),
    ]
    assert submit(rid, "R1", batch).status_code == 201
    # Same content, different order -> same digest.
    response = submit(rid, "R1", list(reversed(batch)))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "duplicate"
    assert body["revision"] == 1
    assert body["state"]["effective_batches"] == 1
    assert body["state"]["effective_receipts"] == 2
    assert body["state"]["revision"] == 1


def test_same_number_with_different_content_is_rejected():
    rid = start_round()
    assert (
        submit(rid, "R1", [receipt("pass", "DRV-1", "a1", "a1-d1")]).status_code == 201
    )
    response = submit(rid, "R1", [receipt("pass", "DRV-2", "a2", "a2-d2")])
    assert response.status_code == 409
    body = response.json()
    assert body["status"] == "rejected"
    assert "内容不同" in body["detail"]
    # The effective batch is untouched.
    state = client.get(f"/audit/rounds/{rid}/state").json()
    assert state["effective_batches"] == 1
    assert state["effective_receipts"] == 1
    assert state["revision"] == 1


def test_unexplainable_batch_is_rejected_whole():
    rid = start_round()
    # Pin drive DRV-1 healthy and replica a2-d1 healthy.
    first = submit(
        rid,
        "R1",
        [
            receipt("pass", "DRV-1", "a1", "a1-d1"),
            receipt("pass", "DRV-1", "a2", "a2-d1"),
        ],
    )
    assert first.status_code == 201
    before = client.get(f"/audit/rounds/{rid}/state").json()

    # A failure of a2-d1 on DRV-1 contradicts both pinned facts: no fault set
    # can explain it.  The batch also carries a perfectly fine receipt, which
    # must not be committed either (whole batch rejected).
    bad_batch = [
        receipt("fail", "DRV-1", "a2", "a2-d1"),
        receipt("pass", "DRV-2", "b1", "b1-d2"),
    ]
    response = submit(rid, "R2", bad_batch)
    assert response.status_code == 409
    body = response.json()
    assert body["status"] == "rejected"
    assert body["effective"] is False
    assert "整批拒收" in body["detail"]
    assert body["conflicts"], "rejection must name the conflicting receipts"
    assert any("a2-d1" in c for c in body["conflicts"])

    after = client.get(f"/audit/rounds/{rid}/state").json()
    assert after["effective_batches"] == before["effective_batches"]
    assert after["effective_receipts"] == before["effective_receipts"]
    assert after["revision"] == before["revision"]
    # The whole rejected batch left no trace: b1-d2 is not effective.
    result = inspect(rid)
    assert all("b1-d2" not in e["rationale"] for e in result["explanations"])
    # The rejected number may be reused for corrected content.
    corrected = submit(
        rid,
        "R2",
        [
            receipt("pass", "DRV-1", "a2", "a2-d1"),
            receipt("pass", "DRV-2", "b1", "b1-d2"),
        ],
    )
    assert corrected.status_code == 201


def test_retraction_restores_optional_replicas_and_number_is_retired():
    rid = start_round()
    batch = [
        receipt("pass", "DRV-1", "a1", "a1-d1"),
        receipt("fail", "DRV-2", "a1", "a1-d2"),
    ]
    assert submit(rid, "R1", batch).status_code == 201
    before = inspect(rid)
    assert before["excluded_replicas"] == ["a1-d2"]

    response = retract(rid, "R1")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "retracted"
    assert body["restored_replicas"] == ["a1-d2"]
    assert body["excluded_replicas"] == []
    assert body["state"]["effective_batches"] == 0
    assert body["state"]["retracted_batches"] == 1
    assert body["state"]["revision"] == 2

    after = inspect(rid)
    assert after["explanations"][0]["faults"] == []
    assert after["excluded_replicas"] == []

    # Reusing the retracted number is refused outright, identical or not.
    reused = submit(rid, "R1", batch)
    assert reused.status_code == 409
    assert "不得复用" in reused.json()["detail"]
    # Retraction is idempotent and does not bump the revision.
    again = retract(rid, "R1")
    assert again.json()["status"] == "already_retracted"
    assert again.json()["revision"] == 2
    assert retract(rid, "never-seen").json()["status"] == "not_found"


# --------------------------------------------------------------------------- #
# Catalogue handed to /schedule
# --------------------------------------------------------------------------- #
def test_schedule_catalogue_filters_only_certainly_bad_replicas():
    rid = start_round()
    # Pin DRV-1 healthy so the d2 copy fails and the d1 copy is fine.
    assert (
        submit(
            rid,
            "R1",
            [
                receipt("pass", "DRV-1", "a1", "a1-d1"),
                receipt("fail", "DRV-2", "a1", "a1-d2"),
            ],
        ).status_code
        == 201
    )
    response = client.get(f"/audit/rounds/{rid}/schedule-catalogue")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["excluded_replicas"] == ["a1-d2"]
    # Files and shards are kept in full; only the bad replica is dropped.
    assert [f["id"] for f in body["files"]] == ["file-A", "file-B"]
    assert [
        [s["id"] for s in f["shards"]] for f in body["files"]
    ] == [["a1", "a2"], ["b1", "b2"]]
    a1 = body["files"][0]["shards"][0]
    assert [r["replica_id"] for r in a1["replicas"]] == ["a1-d1"]

    # The filtered catalogue is directly consumable by /schedule, as-is.
    scheduled = client.post("/schedule", json=body)
    assert scheduled.status_code == 200, scheduled.text
    assert scheduled.json()["feasible"] is True


def test_schedule_catalogue_keeps_shard_with_no_replicas_left():
    cat = catalogue()
    # Make shard b2 exist only on DRV-2, and fail every DRV-2 read, with
    # passes that pin DRV-1 healthy: every explanation blames replica b2-d2.
    cat["files"][1]["shards"][1]["replicas"] = [
        {"replica_id": "b2-d2", "tape": "DRV-2", "position": 10}
    ]
    rid = start_round(cat)
    assert (
        submit(
            rid,
            "R1",
            [
                receipt("pass", "DRV-1", "b1", "b1-d1"),
                receipt("fail", "DRV-2", "b2", "b2-d2"),
            ],
        ).status_code
        == 201
    )
    body = client.get(f"/audit/rounds/{rid}/schedule-catalogue").json()
    b2 = body["files"][1]["shards"][1]
    assert b2["id"] == "b2"
    assert b2["replicas"] == []
    assert body["excluded_replicas"] == ["b2-d2"]
    scheduled = client.post("/schedule", json=body).json()
    assert scheduled["feasible"] is False
    assert scheduled["missing_shards"][0]["shard_id"] == "b2"


def test_ambiguity_leaves_never_failed_replicas_optional():
    rid = start_round()
    # Two equally valid explanations for the a1-d2 failure: replica a1-d2, or
    # the whole DRV-2.  a1-d2 itself is therefore bad under both and filtered,
    # but DRV-2's other replicas were never read and stay optional.
    assert (
        submit(
            rid,
            "R1",
            [
                receipt("pass", "DRV-1", "a1", "a1-d1"),
                receipt("fail", "DRV-2", "a1", "a1-d2"),
            ],
        ).status_code
        == 201
    )
    body = client.get(f"/audit/rounds/{rid}/schedule-catalogue").json()
    result = inspect(rid)
    assert result["excluded_replicas"] == ["a1-d2"]
    assert "a2-d2" in result["undecided_replicas"]
    assert "a2-d2" not in result["excluded_replicas"]
    assert body["excluded_replicas"] == ["a1-d2"]
    kept = [
        r["replica_id"]
        for f in body["files"]
        for s in f["shards"]
        for r in s["replicas"]
    ]
    assert "a2-d2" in kept
    assert "a1-d2" not in kept
    assert "a1-d1" in kept


def test_undecided_replica_appears_with_a_second_read():
    rid = start_round()
    # Pinning DRV-1 healthy forces a1-d1 to be a bad replica; the a1-d2 failure
    # is then either the replica or the whole DRV-2, so DRV-2's other replicas
    # (e.g. a2-d2) are bad only under some explanations -> undecided.
    assert (
        submit(
            rid,
            "R1",
            [
                receipt("pass", "DRV-1", "a2", "a2-d1"),
                receipt("fail", "DRV-1", "a1", "a1-d1"),
                receipt("fail", "DRV-2", "a1", "a1-d2"),
            ],
        ).status_code
        == 201
    )
    result = inspect(rid)
    assert result["excluded_replicas"] == ["a1-d1", "a1-d2"]
    assert "a2-d2" in result["undecided_replicas"]
    assert "a1-d1" not in result["undecided_replicas"]
    # Only certainly-bad replicas are filtered from the schedule catalogue.
    body = client.get(f"/audit/rounds/{rid}/schedule-catalogue").json()
    assert body["excluded_replicas"] == ["a1-d1", "a1-d2"]


def test_conflict_is_visible_and_blocks_the_schedule_catalogue():
    from app.audit_logic import ReceiptRec, analyse

    cat = catalogue()
    receipt_map = {
        r["replica_id"]: r["tape"]
        for f in cat["files"]
        for s in f["shards"]
        for r in s["replicas"]
    }
    # One receipt passes a2-d1 on DRV-1 (pinning both the drive and the replica
    # healthy), the other reports the same pair failing: no fault set explains
    # both reads.
    receipts = [
        ReceiptRec("R1", "pass", "DRV-1", "file-A", "a2", "a2-d1"),
        ReceiptRec("R2", "fail", "DRV-1", "file-A", "a2", "a2-d1"),
    ]
    analysis = analyse(receipts, receipt_map, ["DRV-1", "DRV-2"])
    assert analysis.conflict is True
    assert len(analysis.conflict_receipts) == 2
    assert analysis.explanations == []
    assert "矛盾" in analysis.verdict


def test_conflict_blocks_schedule_catalogue_over_http(tmp_path, monkeypatch):
    # The API refuses conflicted batches, so drive the store directly to reach
    # the conflicted state and check the schedule-catalogue guard.
    import json as _json

    from app import audit_store

    monkeypatch.setenv("AUDIT_DB_PATH", str(tmp_path / "conflict.db"))
    rid = start_round()
    entries = [
        {
            "kind": "pass",
            "tape": "DRV-1",
            "file_id": "file-A",
            "shard_id": "a2",
            "replica_id": "a2-d1",
        }
    ]
    with audit_store._transaction() as conn:
        conn.execute(
            "INSERT INTO batches (round_id, receipt_id, content_hash, content,"
            " status, created_at) VALUES (?, ?, ?, ?, 'effective', 'now')",
            (rid, "R1", audit_store._content_hash(entries),
             _json.dumps(entries, ensure_ascii=False)),
        )
        conn.execute(
            "INSERT INTO batches (round_id, receipt_id, content_hash, content,"
            " status, created_at) VALUES (?, ?, ?, ?, 'effective', 'now')",
            (
                rid,
                "R2",
                audit_store._content_hash(
                    [{"kind": "fail", "tape": "DRV-1", "file_id": "file-A",
                      "shard_id": "a2", "replica_id": "a2-d1"}]
                ),
                _json.dumps(
                    [{"kind": "fail", "tape": "DRV-1", "file_id": "file-A",
                      "shard_id": "a2", "replica_id": "a2-d1"}],
                    ensure_ascii=False,
                ),
            ),
        )

    response = client.get(f"/audit/rounds/{rid}/schedule-catalogue")
    assert response.status_code == 409
    assert "撤回" in response.json()["detail"]
    result = inspect(rid)
    assert result["requires_decision"] is True
    assert len(result["conflicts"]) == 2


# --------------------------------------------------------------------------- #
# Concurrency and persistence
# --------------------------------------------------------------------------- #
def test_concurrent_compatible_batches_do_not_lose_updates():
    rid = start_round()
    shards = ["a1", "a2", "b1", "b2"]
    barrier = threading.Barrier(len(shards))
    outcomes: list[str] = []

    def run(i: int, sid: str) -> None:
        barrier.wait()
        response = submit(
            rid,
            f"R{i}",
            [receipt("pass", "DRV-1", sid, f"{sid}-d1")],
        )
        outcomes.append(response.json()["status"])

    threads = [
        threading.Thread(target=run, args=(i, sid))
        for i, sid in enumerate(shards)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(outcomes) == ["accepted"] * 4
    state = client.get(f"/audit/rounds/{rid}/state").json()
    assert state["effective_batches"] == 4
    assert state["effective_receipts"] == 4
    assert state["revision"] == 4
    result = inspect(rid)
    assert result["requires_decision"] is False


def test_concurrent_conflicting_batches_keep_a_consistent_state():
    rid = start_round()
    batch_a = [
        receipt("pass", "DRV-1", "a1", "a1-d1"),
        receipt("fail", "DRV-2", "a1", "a1-d2"),
    ]
    batch_b = [
        receipt("fail", "DRV-1", "a1", "a1-d1"),
        receipt("pass", "DRV-2", "a1", "a1-d2"),
    ]
    barrier = threading.Barrier(2)
    outcomes: dict[str, int] = {}

    def run(name: str, batch: list[dict]) -> None:
        barrier.wait()
        outcomes[name] = submit(rid, name, batch).status_code

    threads = [
        threading.Thread(target=run, args=("RA", batch_a)),
        threading.Thread(target=run, args=("RB", batch_b)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Both are individually fine, but they contradict each other.  Exactly one
    # must have been committed; the other sees it and is rejected.
    assert sorted(outcomes.values()) == [201, 409], outcomes
    state = client.get(f"/audit/rounds/{rid}/state").json()
    assert state["effective_batches"] == 1
    assert state["revision"] == 1
    assert inspect(rid)["requires_decision"] is False


def test_expected_revision_guards_stale_writers():
    rid = start_round()
    assert (
        submit(
            rid, "R1", [receipt("pass", "DRV-1", "a1", "a1-d1")]
        ).status_code
        == 201
    )
    stale = submit(
        rid,
        "R2",
        [receipt("pass", "DRV-1", "a2", "a2-d1")],
        expected_revision=0,
    )
    assert stale.status_code == 409
    assert "修订号" in stale.json()["detail"]
    fresh = submit(
        rid,
        "R2",
        [receipt("pass", "DRV-1", "a2", "a2-d1")],
        expected_revision=1,
    )
    assert fresh.status_code == 201


def test_state_survives_new_connections_and_restart_like_reload(tmp_path, monkeypatch):
    db = tmp_path / "persist.db"
    monkeypatch.setenv("AUDIT_DB_PATH", str(db))
    rid = start_round()
    assert (
        submit(
            rid,
            "R1",
            [
                receipt("pass", "DRV-1", "a1", "a1-d1"),
                receipt("fail", "DRV-2", "a1", "a1-d2"),
            ],
        ).status_code
        == 201
    )
    first = client.get(f"/audit/rounds/{rid}/schedule-catalogue").json()

    # A fresh client opens new SQLite connections: the catalogue must still be
    # derived from the effective receipts on disk.
    with TestClient(app) as fresh_client:
        again = fresh_client.get(f"/audit/rounds/{rid}/schedule-catalogue").json()
        state = fresh_client.get(f"/audit/rounds/{rid}/state").json()
    assert again == first
    assert state["effective_batches"] == 1
    assert state["revision"] == 1

    # Retraction persists too, and the catalogue follows.
    retract(rid, "R1")
    with TestClient(app) as fresh_client:
        final = fresh_client.get(f"/audit/rounds/{rid}/schedule-catalogue").json()
    assert final["excluded_replicas"] == []
    assert all(
        len(s["replicas"]) == 2 for f in final["files"] for s in f["shards"]
    )


# --------------------------------------------------------------------------- #
# Randomised cross-check against brute force
# --------------------------------------------------------------------------- #
def test_random_receipt_sets_match_brute_force():
    import random

    cat = catalogue()
    all_replicas = [
        (r["replica_id"], r["tape"])
        for f in cat["files"]
        for s in f["shards"]
        for r in s["replicas"]
    ]
    rng = random.Random(20241124)
    for trial in range(60):
        chosen = rng.sample(all_replicas, rng.randint(1, 6))
        receipts = []
        for replica_id, tape in chosen:
            sid = replica_id.split("-")[0]
            kind = "pass" if rng.random() < 0.55 else "fail"
            receipts.append(receipt(kind, tape, sid, replica_id))

        from app.audit_logic import ReceiptRec, analyse

        recs = [
            ReceiptRec("R", r["kind"], r["tape"], r["file_id"], r["shard_id"],
                       r["replica_id"])
            for r in receipts
        ]
        rmap = {rid: tape for rid, tape in all_replicas}
        analysis = analyse(recs, rmap, ["DRV-1", "DRV-2"])
        expected = set(brute_explanations(cat, receipts))
        assert set(analysis.explanations) == expected, (trial, receipts)
        if expected:
            assert analysis.conflict is False
            excluded = {
                rid
                for rid, tape in all_replicas
                if all(
                    rid in {i for k, i in fs if k == "replica"}
                    or tape in {i for k, i in fs if k == "tape"}
                    for fs in expected
                )
            }
            assert set(analysis.excluded_replicas) == excluded, (trial, receipts)
        else:
            assert analysis.conflict is True


# --------------------------------------------------------------------------- #
# Catalogue interoperability with the plain /schedule shape
# --------------------------------------------------------------------------- #
def test_round_from_plain_schedule_catalogue_derives_replica_ids():
    schedule_example = client.get("/example").json()
    # /example uses tapes T1..T3; rename them to the two drives under test.
    plain = json.loads(json.dumps(schedule_example))
    for f in plain["files"]:
        for s in f["shards"]:
            for r in s["replicas"]:
                r["tape"] = "DRV-1" if r["tape"] in ("T1", "T3") else "DRV-2"
    response = client.post(
        "/audit/rounds",
        json={"tape_units": ["DRV-1", "DRV-2"], "catalogue": plain},
    )
    assert response.status_code == 201, response.text
    rid = response.json()["round_id"]
    catalogue_response = client.get(f"/audit/rounds/{rid}/schedule-catalogue").json()
    replica_ids = [
        r["replica_id"]
        for f in catalogue_response["files"]
        for s in f["shards"]
        for r in s["replicas"]
    ]
    assert replica_ids, "derived replica ids must be present"
    assert len(replica_ids) == len(set(replica_ids))

    # Receipts can then address a copy by (tape, position) instead of by id.
    first = catalogue_response["files"][0]["shards"][0]
    copy = first["replicas"][0]
    response = submit(
        rid,
        "R1",
        [
            {
                "kind": "pass",
                "tape": copy["tape"],
                "file_id": catalogue_response["files"][0]["id"],
                "shard_id": first["id"],
                "position": copy["position"],
            }
        ],
    )
    assert response.status_code == 201, response.text
    result = inspect(rid)
    assert result["explanations"][0]["faults"] == []


def test_position_receipt_must_match_catalogue():
    rid = start_round()
    response = submit(
        rid,
        "R1",
        [
            {
                "kind": "pass",
                "tape": "DRV-1",
                "file_id": "file-A",
                "shard_id": "a1",
                "position": 999,
            }
        ],
    )
    assert response.status_code == 422
    assert response.json()["status"] == "rejected"
    assert any("找不到" in c for c in response.json()["conflicts"])


def test_receipt_without_replica_or_position_is_422():
    rid = start_round()
    response = submit(
        rid,
        "R1",
        [{"kind": "pass", "tape": "DRV-1", "file_id": "file-A", "shard_id": "a1"}],
    )
    assert response.status_code == 422


def test_receipt_referencing_unknown_replica_is_422():
    rid = start_round()
    response = submit(
        rid,
        "R1",
        [receipt("pass", "DRV-1", "a1", "no-such-copy")],
    )
    assert response.status_code == 422
    assert response.json()["status"] == "rejected"
    assert any("不在本轮目录" in c for c in response.json()["conflicts"])


def test_idempotent_retry_with_stale_revision_is_still_a_duplicate():
    rid = start_round()
    batch = [receipt("pass", "DRV-1", "a1", "a1-d1")]
    assert submit(rid, "R1", batch, expected_revision=0).status_code == 201
    # A client that retries the exact same batch with the revision it read
    # before the first attempt still gets the idempotent answer.
    retry = submit(rid, "R1", batch, expected_revision=0)
    assert retry.status_code == 200
    assert retry.json()["status"] == "duplicate"
    assert retry.json()["state"]["effective_batches"] == 1


def test_mixed_id_and_position_for_same_replica_is_rejected():
    rid = start_round()
    response = submit(
        rid,
        "R1",
        [
            receipt("pass", "DRV-1", "a1", "a1-d1"),
            {
                "kind": "fail",
                "tape": "DRV-1",
                "file_id": "file-A",
                "shard_id": "a1",
                "position": 10,
            },
        ],
    )
    assert response.status_code == 422
    assert response.json()["status"] == "rejected"
    assert any("重复报送" in c for c in response.json()["conflicts"])


def test_retract_retry_with_stale_revision_is_idempotent():
    rid = start_round()
    assert (
        submit(rid, "R1", [receipt("pass", "DRV-1", "a1", "a1-d1")]).status_code
        == 201
    )
    first = client.post(
        f"/audit/rounds/{rid}/retract",
        json={"receipt_id": "R1", "reason": "x", "expected_revision": 1},
    )
    assert first.status_code == 200
    assert first.json()["revision"] == 2
    retry = client.post(
        f"/audit/rounds/{rid}/retract",
        json={"receipt_id": "R1", "reason": "x", "expected_revision": 1},
    )
    assert retry.status_code == 200
    assert retry.json()["status"] == "already_retracted"
    assert retry.json()["revision"] == 2


def test_certain_tape_fault_excludes_all_its_replicas():
    rid = start_round()
    # DRV-1 passes on a1, then every DRV-2 read fails while DRV-1 keeps passing.
    receipts = [receipt("pass", "DRV-1", sid, f"{sid}-d1") for sid in ("a1", "a2")]
    receipts += [
        receipt("fail", "DRV-2", sid, f"{sid}-d2") for sid in ("a1", "a2", "b1")
    ]
    assert submit(rid, "R1", receipts).status_code == 201
    result = inspect(rid)
    # Every failed read is on DRV-2; no replica was ever proven bad on a healthy
    # drive, so the only minimal explanation is the drive itself.
    assert normalise_explanations(result["explanations"]) == {
        frozenset({("tape", "DRV-2")})
    }
    assert result["certain_faults"] == [{"kind": "tape", "id": "DRV-2"}]
    assert "停用带机 DRV-2" in result["verdict"]
    body = client.get(f"/audit/rounds/{rid}/schedule-catalogue").json()
    # Every DRV-2 replica is unusable under the single explanation.
    assert body["excluded_replicas"] == ["a1-d2", "a2-d2", "b1-d2", "b2-d2"]
    kept = [
        (r["replica_id"], r["tape"])
        for f in body["files"]
        for s in f["shards"]
        for r in s["replicas"]
    ]
    assert kept == [
        ("a1-d1", "DRV-1"),
        ("a2-d1", "DRV-1"),
        ("b1-d1", "DRV-1"),
        ("b2-d1", "DRV-1"),
    ]
    # Files and shards are all retained.
    assert [
        [s["id"] for s in f["shards"]] for f in body["files"]
    ] == [["a1", "a2"], ["b1", "b2"]]
