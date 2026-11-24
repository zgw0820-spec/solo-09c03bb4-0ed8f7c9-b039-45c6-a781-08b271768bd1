"""Attribution engine for tape read-verification receipts.

Read model
----------
A read passes exactly when the tape drive and the replica are both healthy; a
fault in either one makes the read fail.  Persistent faults are assumed at at
most two locations in total (drives + replicas).

Given the effective receipts, a *fault set* explains them when

* every ``pass`` receipt names a drive and a replica outside the fault set, and
* every ``fail`` receipt names a drive or a replica inside the fault set.

The second condition is a vertex-cover problem on the graph whose edges are the
``(tape, replica)`` pairs of failed reads.  Because covers have at most two
vertices, all inclusion-minimal explanations are enumerated exactly by testing
single vertices and pairs.  Drives and replicas that passed a read are forced
healthy and can never appear in an explanation.

If no explanation exists the receipt set is *inconsistent*.  A greedy deletion
pass produces an inclusion-minimal subset of receipts witnessing the
inconsistency, so a rejection can name the exact receipts that cannot coexist.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations
from typing import Iterable, Mapping, Sequence

from .audit_models import MAX_FAULTS

Fault = tuple[str, str]  # (kind, id) with kind in {"tape", "replica"}


@dataclass(frozen=True)
class ReceiptRec:
    """A stored receipt, independent of the Pydantic request models."""

    receipt_id: str
    kind: str  # "pass" | "fail"
    tape: str
    file_id: str
    shard_id: str
    replica_id: str

    def label(self) -> str:
        result = "通过" if self.kind == "pass" else "失败"
        return (
            f"回执 {self.receipt_id}：带机 {self.tape} 读 "
            f"{self.file_id}/{self.shard_id}/{self.replica_id} {result}"
        )


@dataclass
class Analysis:
    explanations: list[frozenset[Fault]] = field(default_factory=list)
    conflict: bool = False
    conflict_receipts: list[ReceiptRec] = field(default_factory=list)
    excluded_replicas: list[str] = field(default_factory=list)
    undecided_replicas: list[str] = field(default_factory=list)
    certain_faults: list[Fault] = field(default_factory=list)
    possible_faults: list[Fault] = field(default_factory=list)
    verdict: str = ""
    roadmap: list[tuple[str, Fault, str]] = field(default_factory=list)


def _fault_key(fault: Fault) -> tuple[int, str]:
    return (0 if fault[0] == "tape" else 1, fault[1])


def _fault_name(fault: Fault) -> str:
    return ("带机 " if fault[0] == "tape" else "副本 ") + fault[1]


def _faults_name(faults: Iterable[Fault]) -> str:
    names = [_fault_name(f) for f in sorted(faults, key=_fault_key)]
    return "、".join(names) if names else "无故障"


def _cover(edges: Sequence[tuple[str, str]], fault_set: Iterable[Fault]) -> bool:
    """True when every failed read is covered by a fault in ``fault_set``."""
    fs = set(fault_set)
    return all(("tape", tape) in fs or ("replica", rep) in fs for tape, rep in edges)


def _candidates(
    receipts: Sequence[ReceiptRec],
) -> tuple[list[Fault], list[tuple[str, str]]]:
    """Fault locations that could matter, and the fail-receipt edges."""
    healthy_tapes = {r.tape for r in receipts if r.kind == "pass"}
    healthy_replicas = {r.replica_id for r in receipts if r.kind == "pass"}
    edges = [(r.tape, r.replica_id) for r in receipts if r.kind == "fail"]
    candidates: set[Fault] = set()
    for tape, replica in edges:
        if tape not in healthy_tapes:
            candidates.add(("tape", tape))
        if replica not in healthy_replicas:
            candidates.add(("replica", replica))
    return sorted(candidates, key=_fault_key), edges


def _minimal_covers(
    candidates: Sequence[Fault], edges: Sequence[tuple[str, str]]
) -> list[frozenset[Fault]]:
    """All inclusion-minimal covers of size <= MAX_FAULTS."""
    if not edges:
        return [frozenset()]

    singles = [fault for fault in candidates if _cover(edges, (fault,))]
    covers: list[frozenset[Fault]] = [frozenset({fault}) for fault in singles]
    single_set = set(singles)

    if MAX_FAULTS >= 2:
        for a, b in combinations(candidates, 2):
            if a in single_set or b in single_set:
                continue  # a proper subset already covers; not minimal
            if _cover(edges, (a, b)):
                covers.append(frozenset((a, b)))

    covers.sort(key=lambda f: (len(f), sorted(f, key=_fault_key)))
    return covers


def _explainable(receipts: Sequence[ReceiptRec]) -> bool:
    candidates, edges = _candidates(receipts)
    return bool(_minimal_covers(candidates, edges))


def _minimal_conflict(receipts: Sequence[ReceiptRec]) -> list[ReceiptRec]:
    """One inclusion-minimal unexplainable subset of receipts.

    Explainability depends only on which drives/replicas a pass pins healthy
    and which ``(drive, replica)`` pairs failed, so duplicate pass/fail
    receipts are collapsed first; this keeps the greedy deletion small even
    when many batches repeat the same reads.
    """
    seen: set[tuple[str, str, str]] = set()
    reduced: list[ReceiptRec] = []
    for receipt in receipts:
        key = (receipt.kind, receipt.tape, receipt.replica_id)
        if key in seen:
            continue
        seen.add(key)
        reduced.append(receipt)

    subset = list(reduced)
    for receipt in list(reduced):
        trial = [r for r in subset if r is not receipt]
        if not _explainable(trial):
            subset = trial
    return subset


def _bad_replicas(
    fault_set: Iterable[Fault], replica_tape: Mapping[str, str]
) -> set[str]:
    """Replicas unusable under a given fault set (own fault or faulty drive)."""
    bad = {ident for kind, ident in fault_set if kind == "replica"}
    faulty_tapes = {ident for kind, ident in fault_set if kind == "tape"}
    for replica_id, tape in replica_tape.items():
        if tape in faulty_tapes:
            bad.add(replica_id)
    return bad


def _bad_all(covers: Sequence[frozenset[Fault]], replica_tape: Mapping[str, str]) -> set[str]:
    """Replicas unusable under *every* explanation."""
    if not covers:
        return set()
    result = _bad_replicas(covers[0], replica_tape)
    for cover in covers[1:]:
        result &= _bad_replicas(cover, replica_tape)
    return result


def _bad_any(covers: Sequence[frozenset[Fault]], replica_tape: Mapping[str, str]) -> set[str]:
    result: set[str] = set()
    for cover in covers:
        result |= _bad_replicas(cover, replica_tape)
    return result


def analyse(
    receipts: Sequence[ReceiptRec],
    replica_tape: Mapping[str, str],
    tape_units: Sequence[str],
) -> Analysis:
    """Attribute the effective receipts to at most ``MAX_FAULTS`` locations."""
    result = Analysis()
    candidates, edges = _candidates(receipts)
    covers = _minimal_covers(candidates, edges)
    result.explanations = covers

    if not covers:
        result.conflict = True
        result.conflict_receipts = _minimal_conflict(receipts)
        result.verdict = (
            f"有效回执互相矛盾：没有任何不超过 {MAX_FAULTS} 处持续故障的组合能同时"
            "解释它们。请先复核下列回执并撤回误报批次，再决定停用带机或隔离副本。"
        )
        return result

    certain = set(covers[0])
    possible: set[Fault] = set()
    for cover in covers:
        certain &= cover
        possible |= cover

    excluded = _bad_all(covers, replica_tape)
    undecided = _bad_any(covers, replica_tape) - excluded

    result.excluded_replicas = sorted(excluded)
    result.undecided_replicas = sorted(undecided)
    result.certain_faults = sorted(certain, key=_fault_key)
    result.possible_faults = sorted(possible, key=_fault_key)

    certain_tapes = [ident for kind, ident in result.certain_faults if kind == "tape"]
    certain_replicas = [
        ident for kind, ident in result.certain_faults if kind == "replica"
    ]
    possible_only = [f for f in result.possible_faults if f not in certain]
    possible_tapes = [ident for kind, ident in possible_only if kind == "tape"]

    # Replicas that are unusable under every explanation but were not already
    # named as a certain replica fault (they may go bad only because the drive
    # is faulty in some explanation).
    extra_excluded = sorted(set(excluded) - set(certain_replicas))

    sentences: list[str] = []
    if not result.possible_faults:
        sentences.append("读片全部通过，两台带机与全部副本均正常，无需停用或隔离。")
        result.verdict = "".join(sentences)
    else:
        if certain_tapes:
            sentences.append("建议停用带机 " + "、".join(certain_tapes) + "。")
        if certain_replicas:
            sentences.append("建议隔离副本 " + "、".join(certain_replicas) + "。")
        if extra_excluded:
            sentences.append(
                "无论采用哪种解释都不可用、建议隔离的副本："
                + "、".join(extra_excluded)
                + "。"
            )
        if possible_tapes and not certain_tapes:
            sentences.append(
                "带机 " + "、".join(possible_tapes)
                + " 是否故障取决于解释（示例中它只在部分组合里出故障），"
                "建议换机复读确认后再决定是否停用。"
            )
        if undecided:
            sentences.append(
                "暂保留、建议复读的副本：" + "、".join(sorted(undecided)) + "。"
            )
        if not certain_tapes and not certain_replicas and not extra_excluded:
            sentences.insert(
                0,
                "现有回执存在多种同样成立的解释，尚不能唯一确定应停用带机还是"
                "隔离副本。",
            )
        result.verdict = "".join(sentences)

    for fault in result.certain_faults:
        if fault[0] == "tape":
            result.roadmap.append(
                ("retire", fault, "所有解释都指向该带机，建议停用检修。")
            )
        else:
            result.roadmap.append(
                ("retire", fault, "所有解释都判定该副本损坏，建议隔离。")
            )
    for fault in possible_only:
        result.roadmap.append(
            (
                "verify",
                fault,
                "仅部分解释指向它，建议换机或复读确认后再决定。",
            )
        )

    return result


def explanation_rationale(faults: Iterable[Fault]) -> str:
    faults = sorted(faults, key=_fault_key)
    if not faults:
        return "无需任何故障假设即与全部有效回执一致。"
    return (
        "假设 " + _faults_name(faults) + " 故障：通过的回执均避开故障点，"
        "失败的回执均命中至少一个故障点，故与全部有效回执一致；"
        "去掉其中任一故障都有回执无法解释，因此该组合是极小的。"
    )
