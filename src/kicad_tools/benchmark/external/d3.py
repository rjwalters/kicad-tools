"""PCBWorld D3 CP@1 / CP@5 aggregation over compact per-attempt records (issue #6332).

Phase 2 of #5942. ``kct bench external --suite pcbworld --rollouts N`` writes
one compact *attempt record* per (board, rollout) -- including failures --
and this module turns a list of them into the published D3 aggregate. The
aggregation is a pure function of the committed ``results.json``, so a report
can always be re-rendered from the compact data without the boards.

Semantics (PCBWorld ``docs/METRICS.md`` and ``eval/eval_utils.py`` at the
pinned commit, mode ``final_potential``):

* **@1** reads rollout 0 (the declared first seed) of every board.
* **@5** keeps, per board, the rollout with the **largest potential gain**
  and reads every number off that one rollout. Ties break on routability
  (higher), error-level DRV (fewer), wirelength (shorter), vias (fewer),
  then the lowest rollout index -- PCBWorld's own key. It is *not* "any
  rollout clean".
* A **deterministic** baseline is run once, and @5 coincides with @1.
* **Time** ignores the selection and is reported over every rollout.

Denominator rules that keep the number honest:

* The denominator is always the full manifest (every board the split
  names), never "boards that happened to finish".
* A board with no attempt record for a declared rollout gets an explicit
  ``missing`` record -- it is never silently dropped.
* A router that produced no output, or a timed-out attempt, is a **fail**
  (the method did not deliver a clean board).
* A board whose PCBWorld preparation or scoring is unavailable is
  **unknown**, never a fail and never a pass. While any board is unknown the
  point estimate is ``None`` and only the ``[lower, upper]`` bounds are
  reported.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

__all__ = [
    "ATTEMPT_SCHEMA",
    "RESULTS_SCHEMA",
    "STATUS_HARNESS_ERROR",
    "STATUS_MISSING",
    "STATUS_PREP_FAILED",
    "STATUS_ROUTE_NO_OUTPUT",
    "STATUS_SCORED",
    "STATUS_SCORER_UNAVAILABLE",
    "aggregate_d3",
    "attempt_key",
    "clean_pass_outcome",
    "local_clean_outcome",
    "rate",
    "select_at5",
    "selection_key",
]

ATTEMPT_SCHEMA = "kct-pcbworld-attempt/1"
RESULTS_SCHEMA = "kct-pcbworld-d3-results/1"

STATUS_SCORED = "scored"  # PCBWorld returned a verdict for this attempt's routed board
STATUS_ROUTE_NO_OUTPUT = "route_no_output"  # the router wrote no board -> a FAIL
STATUS_SCORER_UNAVAILABLE = "scorer_unavailable"  # routed, but PCBWorld gave no verdict -> unknown
STATUS_PREP_FAILED = "prep_failed"  # PCBWorld's own prep produced no guide board -> unknown
STATUS_HARNESS_ERROR = "harness_error"  # the harness itself failed this attempt -> unknown
STATUS_MISSING = "missing"  # no record at all for a declared rollout -> unknown

_UNKNOWN_STATUSES = frozenset(
    {STATUS_SCORER_UNAVAILABLE, STATUS_PREP_FAILED, STATUS_HARNESS_ERROR, STATUS_MISSING}
)


def attempt_key(record: Mapping[str, Any]) -> tuple[str, int]:
    return (str(record["board_id"]), int(record["rollout_idx"]))


def _f(value: Any, default: float) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(f) else f


def _pw(record: Mapping[str, Any]) -> Mapping[str, Any]:
    return record.get("pcbworld") or {}


def _timed_out(record: Mapping[str, Any]) -> bool:
    return record.get("route_outcome") == "timeout"


def clean_pass_outcome(record: Mapping[str, Any]) -> bool | None:
    """PCBWorld Clean Pass of one attempt: ``True`` / ``False`` / ``None`` (unknown)."""
    status = record.get("status")
    if status == STATUS_ROUTE_NO_OUTPUT:
        return False
    if status != STATUS_SCORED:
        return None
    cp = _pw(record).get("clean_pass")
    if cp is None:
        return None
    # A timed-out attempt with a partial board stays a failure, whatever the
    # scorer made of the partial copper.
    return bool(cp) and not _timed_out(record)


def local_clean_outcome(record: Mapping[str, Any]) -> bool | None:
    """The stricter local bar: 100% connected AND ``kct check`` AND kicad-cli DRC clean."""
    status = record.get("status")
    if status == STATUS_ROUTE_NO_OUTPUT:
        return False
    if status in _UNKNOWN_STATUSES or status is None:
        return None
    if _timed_out(record):
        return False
    local = record.get("local") or {}
    value = local.get("local_clean")
    return None if value is None else bool(value)


def selection_key(record: Mapping[str, Any]) -> tuple[float, ...]:
    """PCBWorld's ``final_potential`` winner key, primary term = potential gain.

    Within one board ``potential_gain = Φ(routed) − Φ(bare)`` differs from
    ``final_potential`` only by a per-board constant, so ranking by either is
    identical; the potential gain is used when present (it is what
    ``docs/METRICS.md`` names), ``final_potential`` otherwise. Unscored
    attempts rank below every scored one (``-inf``), exactly as PCBWorld's
    ``safe_float(..., -inf)`` does.
    """
    pw = _pw(record) if record.get("status") == STATUS_SCORED else {}
    gain = pw.get("potential_gain")
    primary = _f(gain if gain is not None else pw.get("final_potential"), -math.inf)
    return (
        primary,
        _f(pw.get("routability"), -math.inf),
        -_f(pw.get("drv_errors_only_count"), math.inf),
        -_f(pw.get("wirelength_mm", record.get("wirelength_mm")), math.inf),
        -_f(pw.get("via_count", record.get("vias")), math.inf),
        -float(record.get("rollout_idx", 10**9)),
    )


def select_at5(records: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """The @5 winner among one board's attempts (``None`` if there are none)."""
    if not records:
        return None
    return max(records, key=selection_key)


def rate(outcomes: Sequence[bool | None]) -> dict[str, Any]:
    """Rate over the full denominator, with bounds while any outcome is unknown."""
    n = len(outcomes)
    passed = sum(1 for o in outcomes if o is True)
    failed = sum(1 for o in outcomes if o is False)
    unknown = n - passed - failed
    return {
        "denominator": n,
        "passed": passed,
        "failed": failed,
        "unknown": unknown,
        "rate": (passed / n) if n and not unknown else None,
        "lower": (passed / n) if n else None,
        "upper": ((passed + unknown) / n) if n else None,
    }


def _dist(values: Iterable[float | None]) -> dict[str, Any]:
    vals = sorted(float(v) for v in values if v is not None and not math.isnan(float(v)))
    if not vals:
        return {"n": 0}

    def q(p: float) -> float:
        # Nearest-rank on the sorted sample; deterministic and dependency-free.
        idx = min(len(vals) - 1, max(0, math.ceil(p * len(vals)) - 1))
        return round(vals[idx], 4)

    return {
        "n": len(vals),
        "min": round(vals[0], 4),
        "p50": q(0.50),
        "p90": q(0.90),
        "max": round(vals[-1], 4),
        "mean": round(statistics.fmean(vals), 4),
    }


def _missing_record(board_id: str, rollout_idx: int, seed: int | None) -> dict[str, Any]:
    return {
        "schema": ATTEMPT_SCHEMA,
        "board_id": board_id,
        "rollout_idx": rollout_idx,
        "seed": seed,
        "status": STATUS_MISSING,
        "failure_class": "missing",
        "reason": "no attempt record for this declared rollout",
    }


def _wl_ratio(record: Mapping[str, Any]) -> float | None:
    wl, human = record.get("wirelength_mm"), record.get("human_wirelength_mm")
    if wl is None or not human:
        return None
    return float(wl) / float(human)


def _failure_class(record: Mapping[str, Any]) -> str:
    if record.get("failure_class"):
        return str(record["failure_class"])
    return str(record.get("status") or "unknown")


def _summary(selected: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Every per-board number of one selection (@1 or @5), read off its rows."""
    scored = [r for r in selected if r.get("status") == STATUS_SCORED]
    return {
        "pcbworld_clean_pass": rate([clean_pass_outcome(r) for r in selected]),
        "local_clean": rate([local_clean_outcome(r) for r in selected]),
        "routability": _dist(_pw(r).get("routability") for r in scored),
        "drv_errors_only": _dist(_pw(r).get("drv_errors_only_count") for r in scored),
        "vias": _dist(r.get("vias") for r in selected if r.get("status") == STATUS_SCORED),
        "wirelength_mm": _dist(
            r.get("wirelength_mm") for r in selected if r.get("status") == STATUS_SCORED
        ),
        "wirelength_vs_human": _dist(_wl_ratio(r) for r in scored),
    }


def aggregate_d3(
    records: Sequence[Mapping[str, Any]],
    *,
    board_ids: Sequence[str],
    rollouts: int,
    seeds: Sequence[int] | None = None,
    deterministic: bool = False,
    subsets: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    """Aggregate per-attempt records into the D3 CP@1 / CP@5 summary.

    Args:
        records: compact attempt records (any order; duplicates for one
            (board, rollout) are an error -- resume must never double-count).
        board_ids: the full manifest of the split; the denominator.
        rollouts: declared rollouts per board (``1`` for a deterministic run).
        seeds: the declared seed of each rollout index (for ``missing`` rows).
        deterministic: ``kct route`` was shown to be a deterministic baseline;
            @5 is then reported as equal to @1 (PCBWorld's own convention).
        subsets: optional named sub-denominators (e.g. PCBWorld's ``fair95``)
            given as board-id lists; each is aggregated the same way.
    """
    if rollouts < 1:
        raise ValueError("rollouts must be >= 1")
    manifest = list(dict.fromkeys(board_ids))
    known = set(manifest)
    by_key: dict[tuple[str, int], Mapping[str, Any]] = {}
    extraneous = []
    for rec in records:
        key = attempt_key(rec)
        if key[0] not in known or not 0 <= key[1] < rollouts:
            extraneous.append(key)
            continue
        if key in by_key:
            raise ValueError(f"duplicate attempt record for {key}")
        by_key[key] = rec

    per_board: dict[str, list[Mapping[str, Any]]] = {}
    n_missing = 0
    for bid in manifest:
        rows: list[Mapping[str, Any]] = []
        for idx in range(rollouts):
            found = by_key.get((bid, idx))
            if found is None:
                seed = seeds[idx] if seeds is not None and idx < len(seeds) else None
                found = _missing_record(bid, idx, seed)
                n_missing += 1
            rows.append(found)
        per_board[bid] = rows

    def build(ids: Sequence[str]) -> dict[str, Any]:
        at1 = [per_board[b][0] for b in ids]
        at5: list[Mapping[str, Any]] | None
        if deterministic:
            at5 = at1
            at5_rule = "deterministic baseline: @5 coincides with @1 (PCBWorld docs/METRICS.md)"
        elif rollouts == 1:
            at5 = None
            at5_rule = "not measured: one rollout and no deterministic-baseline evidence"
        else:
            at5 = []
            for b in ids:
                rows = per_board[b]
                # A board whose rollouts are not all known cannot have a
                # trustworthy @5 winner: the unknown one might have won.
                if any(r.get("status") in _UNKNOWN_STATUSES for r in rows):
                    at5.append(next(r for r in rows if r.get("status") in _UNKNOWN_STATUSES))
                else:
                    best = select_at5(rows)
                    assert best is not None
                    at5.append(best)
            at5_rule = (
                "max potential gain per board (PCBWorld docs/METRICS.md, final_potential key)"
            )
        all_rows = [r for b in ids for r in per_board[b]]
        failures = Counter(_failure_class(r) for r in at1 if clean_pass_outcome(r) is not True)
        return {
            "boards": len(ids),
            "at1": _summary(at1),
            "at5": {**(_summary(at5) if at5 is not None else {}), "rule": at5_rule},
            "wall_clock_s_all_rollouts": _dist(
                r.get("wall_clock_s") for r in all_rows if r.get("timing_valid")
            ),
            "timing_refused_attempts": sum(
                1
                for r in all_rows
                if r.get("status") != STATUS_MISSING and not r.get("timing_valid")
            ),
            "failure_classes_at1": dict(sorted(failures.items(), key=lambda kv: (-kv[1], kv[0]))),
        }

    status_counts = Counter(r.get("status") for rows in per_board.values() for r in rows)
    result: dict[str, Any] = {
        "schema": "kct-pcbworld-d3-aggregate/1",
        "manifest_boards": len(manifest),
        "declared_rollouts": rollouts,
        "deterministic": bool(deterministic),
        "attempts_expected": len(manifest) * rollouts,
        "attempts_recorded": len(manifest) * rollouts - n_missing,
        "attempts_missing": n_missing,
        "extraneous_records": len(extraneous),
        "status_counts": dict(sorted((str(k), v) for k, v in status_counts.items())),
        "complete": n_missing == 0
        and all(
            r.get("status") not in _UNKNOWN_STATUSES for rows in per_board.values() for r in rows
        ),
        "full": build(manifest),
        "subsets": {},
    }
    for name, ids in (subsets or {}).items():
        sub = [b for b in manifest if b in set(ids)]
        result["subsets"][name] = build(sub)
    return result
