"""CP@1 / CP@5 aggregation contracts on synthetic attempt records (issue #6332).

PCBWorld's @5 keeps the rollout with the largest potential gain
(``docs/METRICS.md``; ``eval/eval_utils.py`` ``final_potential`` key) and
reads CP from it -- never "any rollout clean". The denominator is always the
full manifest; unknown attempts give bounds, never a silent 0 or pass.
"""

from __future__ import annotations

import pytest

from kicad_tools.benchmark.external.d3 import (
    STATUS_MISSING,
    STATUS_PREP_FAILED,
    STATUS_ROUTE_NO_OUTPUT,
    STATUS_SCORED,
    STATUS_SCORER_UNAVAILABLE,
    aggregate_d3,
    clean_pass_outcome,
    rate,
    select_at5,
)


def rec(board, idx, *, cp=None, gain=None, status=STATUS_SCORED, rout=1.0, drv=0, wl=100.0,
        vias=2, local=None, outcome="completed", wall=1.0, human=None):  # fmt: skip
    return {
        "board_id": board,
        "rollout_idx": idx,
        "seed": 42 + idx,
        "status": status,
        "route_outcome": outcome,
        "pcbworld": (
            {
                "clean_pass": cp,
                "potential_gain": gain,
                "routability": rout,
                "drv_errors_only_count": drv,
                "wirelength_mm": wl,
                "via_count": vias,
            }
            if status == STATUS_SCORED
            else {}
        ),  # fmt: skip
        "local": {"local_clean": local},
        "vias": vias,
        "wirelength_mm": wl,
        "human_wirelength_mm": human,
        "wall_clock_s": wall,
        "timing_valid": wall is not None,
    }


BOARDS = ["b/1", "b/2", "b/3"]


class TestSelection:
    def test_max_potential_gain_wins_even_if_another_rollout_is_clean(self):
        rows = [rec("b/1", 0, cp=True, gain=1.0), rec("b/1", 1, cp=False, gain=5.0)]
        assert select_at5(rows)["rollout_idx"] == 1
        assert clean_pass_outcome(select_at5(rows)) is False

    def test_ties_break_like_pcbworld_then_lowest_index(self):
        rows = [
            rec("b/1", 0, cp=False, gain=2.0, rout=0.9),
            rec("b/1", 1, cp=True, gain=2.0, rout=1.0, wl=120.0),
            rec("b/1", 2, cp=True, gain=2.0, rout=1.0, wl=110.0),
            rec("b/1", 3, cp=True, gain=2.0, rout=1.0, wl=110.0),
        ]
        # routability first, then shorter wirelength, then the lowest index.
        assert select_at5(rows)["rollout_idx"] == 2

    def test_unscored_rollout_never_beats_a_scored_one(self):
        rows = [
            rec("b/1", 0, status=STATUS_ROUTE_NO_OUTPUT),
            rec("b/1", 1, cp=False, gain=-3.0),
        ]
        assert select_at5(rows)["rollout_idx"] == 1

    def test_final_potential_used_when_gain_absent(self):
        a = rec("b/1", 0, cp=False, gain=None)
        a["pcbworld"]["final_potential"] = 9.0
        b = rec("b/1", 1, cp=True, gain=None)
        b["pcbworld"]["final_potential"] = 1.0
        assert select_at5([a, b])["rollout_idx"] == 0


class TestOutcomes:
    def test_route_without_output_is_a_fail_scorer_gap_is_unknown(self):
        assert clean_pass_outcome(rec("b", 0, status=STATUS_ROUTE_NO_OUTPUT)) is False
        assert clean_pass_outcome(rec("b", 0, status=STATUS_SCORER_UNAVAILABLE)) is None
        assert clean_pass_outcome(rec("b", 0, status=STATUS_PREP_FAILED)) is None

    def test_timeout_with_partial_board_stays_a_failure(self):
        assert clean_pass_outcome(rec("b", 0, cp=True, gain=1.0, outcome="timeout")) is False

    def test_rate_bounds_while_unknown(self):
        r = rate([True, False, None, None])
        assert r == {"denominator": 4, "passed": 1, "failed": 1, "unknown": 2,
                     "rate": None, "lower": 0.25, "upper": 0.75}  # fmt: skip
        assert rate([True, False])["rate"] == 0.5


class TestAggregate:
    def test_denominators_and_best_of_five(self):
        records = []
        # b/1: rollout 0 fails, rollout 3 has max gain and is clean -> @1 fail, @5 pass
        for i in range(5):
            records.append(rec("b/1", i, cp=(i == 3), gain=float(i == 3) * 10))
        # b/2: every rollout fails (all-fail row stays in the denominator)
        records += [rec("b/2", i, cp=False, gain=1.0) for i in range(5)]
        # b/3: rollout 0 clean, rollout 1 has higher gain but is not clean -> @5 fail
        records += [rec("b/3", 0, cp=True, gain=1.0), rec("b/3", 1, cp=False, gain=2.0)]
        records += [rec("b/3", i, cp=False, gain=0.0) for i in (2, 3, 4)]
        agg = aggregate_d3(records, board_ids=BOARDS, rollouts=5)
        full = agg["full"]
        assert full["boards"] == 3
        at1, at5 = full["at1"]["pcbworld_clean_pass"], full["at5"]["pcbworld_clean_pass"]
        assert (at1["passed"], at1["denominator"], at1["rate"]) == (1, 3, pytest.approx(1 / 3))
        assert (at5["passed"], at5["denominator"], at5["rate"]) == (1, 3, pytest.approx(1 / 3))
        assert agg["complete"] and agg["attempts_missing"] == 0
        assert full["failure_classes_at1"] == {"scored": 2}

    def test_missing_rollout_is_explicit_and_makes_at5_unknown(self):
        records = [rec("b/1", i, cp=True, gain=1.0) for i in range(4)]  # rollout 4 missing
        records += [rec(b, i, cp=True, gain=1.0) for b in BOARDS[1:] for i in range(5)]
        agg = aggregate_d3(records, board_ids=BOARDS, rollouts=5, seeds=[42, 43, 44, 45, 46])
        assert agg["attempts_missing"] == 1 and agg["attempts_expected"] == 15
        assert agg["status_counts"][STATUS_MISSING] == 1
        assert not agg["complete"]
        at5 = agg["full"]["at5"]["pcbworld_clean_pass"]
        assert at5["unknown"] == 1 and at5["rate"] is None
        assert at5["lower"] == pytest.approx(2 / 3) and at5["upper"] == 1.0
        # @1 is unaffected: rollout 0 of b/1 exists.
        assert agg["full"]["at1"]["pcbworld_clean_pass"]["rate"] == 1.0

    def test_board_without_any_record_counts_in_denominator(self):
        agg = aggregate_d3([rec("b/1", 0, cp=True, gain=1.0)], board_ids=BOARDS, rollouts=1)
        at1 = agg["full"]["at1"]["pcbworld_clean_pass"]
        assert at1["denominator"] == 3 and at1["passed"] == 1 and at1["unknown"] == 2
        assert at1["rate"] is None
        assert agg["full"]["failure_classes_at1"] == {"missing": 2}

    def test_deterministic_baseline_reports_at5_equal_to_at1(self):
        records = [rec("b/1", 0, cp=True, gain=1.0), rec("b/2", 0, cp=False, gain=1.0),
                   rec("b/3", 0, status=STATUS_ROUTE_NO_OUTPUT)]  # fmt: skip
        agg = aggregate_d3(records, board_ids=BOARDS, rollouts=1, deterministic=True)
        full = agg["full"]
        assert full["at5"]["pcbworld_clean_pass"] == full["at1"]["pcbworld_clean_pass"]
        assert "deterministic" in full["at5"]["rule"]

    def test_single_rollout_without_evidence_does_not_claim_at5(self):
        agg = aggregate_d3([rec("b/1", 0, cp=True, gain=1.0)], board_ids=["b/1"], rollouts=1)
        assert "pcbworld_clean_pass" not in agg["full"]["at5"]
        assert agg["full"]["at5"]["rule"].startswith("not measured")

    def test_local_bar_reported_separately(self):
        records = [rec("b/1", 0, cp=True, gain=1.0, local=False),
                   rec("b/2", 0, cp=False, gain=1.0, local=False),
                   rec("b/3", 0, cp=True, gain=1.0, local=True)]  # fmt: skip
        full = aggregate_d3(records, board_ids=BOARDS, rollouts=1)["full"]
        assert full["at1"]["pcbworld_clean_pass"]["passed"] == 2
        assert full["at1"]["local_clean"]["passed"] == 1

    def test_duplicates_are_rejected_and_extraneous_counted(self):
        with pytest.raises(ValueError, match="duplicate"):
            aggregate_d3([rec("b/1", 0, cp=True), rec("b/1", 0, cp=False)],
                         board_ids=BOARDS, rollouts=1)  # fmt: skip
        agg = aggregate_d3([rec("zz", 0, cp=True), rec("b/1", 7, cp=True)],
                           board_ids=BOARDS, rollouts=1)  # fmt: skip
        assert agg["extraneous_records"] == 2

    def test_subset_denominator_and_distributions(self):
        records = [rec("b/1", 0, cp=True, gain=1.0, wl=150.0, human=100.0, wall=2.0),
                   rec("b/2", 0, cp=False, gain=1.0, wl=90.0, human=100.0, wall=None),
                   rec("b/3", 0, cp=True, gain=1.0, wl=80.0, human=None, wall=4.0)]  # fmt: skip
        agg = aggregate_d3(records, board_ids=BOARDS, rollouts=1, subsets={"fair": ["b/1", "b/3"]})
        sub = agg["subsets"]["fair"]
        assert sub["boards"] == 2 and sub["at1"]["pcbworld_clean_pass"]["rate"] == 1.0
        full = agg["full"]
        assert full["at1"]["wirelength_vs_human"]["n"] == 2
        assert full["at1"]["wirelength_vs_human"]["max"] == 1.5
        assert full["wall_clock_s_all_rollouts"]["n"] == 2  # refused timing excluded
        assert full["timing_refused_attempts"] == 1
