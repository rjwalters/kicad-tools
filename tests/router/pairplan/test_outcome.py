"""Structured diff-pair outcomes (issue #6837, slice 2c-ii).

The outcome strings are a contract (the router's ``Diff pairs (lattice):``
block, the diff-pair bench's parser, the retry gates), so the records must
reproduce every one of them exactly.
"""

from __future__ import annotations

import copy
import pickle

import pytest

from kicad_tools.router.pairplan.outcome import (
    EXHAUSTED_SEARCH_REASONS,
    GATEWAY_RETRY_REASONS,
    PairOutcome,
    SegmentOutcome,
    SegmentOutcomes,
    aggregate_label,
    segment_record,
)

#: Every segment-level outcome string the lattice engine and core produce.
_DECLINES = [
    "pair-no-path",
    "pair-no-path-keepout-constrained",
    "pair-no-path-layer-constrained",
    "pair-escape-a",
    "pair-escape-b",
    "pair-no-parity",
    "pair-partner-pad",
    "pair-polarity-twist",
    "pair-pad-attach",
    "pair-leg-blocked",
    "pair-leg-keepout",
    "pair-leg-pairwise-blocked",
    "pair-intra-clearance",
    "pair-offset-degenerate",
    "pair-degenerate-centerline",
    "pair-empty-route",
    "pair-not-planar",
    "pair-no-common-layer",
    "pair-gateway-no-layer",
    "pair-gateway-no-candidates",
    "pair-gateway-via-limit",
    "pair-gateway-via-count",
    "pair-gateway-no-path",
    "pair-gateway-deadline",
    "pair-gateway-anchor",
    "pair-gateway-conflict",
    "pair-gateway-empty-route",
    "pair-gateway-leg-via",
    "pair-gateway-leg-pad-class",
]
SEGMENT_LABELS = (
    ["coupled", "deadline-exceeded", "not-attempted", "hybrid:U4.B1", "hybrid:J1.A2"]
    + _DECLINES
    + [f"uncoupled-fallback:{r}" for r in _DECLINES]
)


@pytest.mark.parametrize("label", SEGMENT_LABELS)
def test_segment_label_round_trips(label: str) -> None:
    rec = SegmentOutcome.from_label(label)
    assert rec.label == label
    assert SegmentOutcome.from_label(rec.label) == rec


def test_segment_statuses() -> None:
    assert SegmentOutcome.from_label("coupled") == SegmentOutcome.coupled()
    assert SegmentOutcome.from_label("pair-no-path") == SegmentOutcome("declined", "pair-no-path")
    assert SegmentOutcome.from_label("uncoupled-fallback:pair-no-path") == SegmentOutcome(
        "uncoupled-fallback", "pair-no-path"
    )
    assert SegmentOutcome.from_label("deadline-exceeded").status == "deadline-exceeded"
    # Issue #6446: a hybrid names its hard terminal and is not "coupled".
    assert SegmentOutcome.from_label("hybrid:U4.B1") == SegmentOutcome.hybrid("U4.B1")
    assert SegmentOutcome.hybrid("U4.B1").status == "hybrid"
    # A gateway name is carried but is not part of the label.
    assert SegmentOutcome.coupled("B.Cu gateway").label == "coupled"


@pytest.mark.parametrize(
    "bad",
    [
        lambda: SegmentOutcome("declined"),
        lambda: SegmentOutcome("uncoupled-fallback"),
        lambda: SegmentOutcome("coupled", "pair-no-path"),
        lambda: SegmentOutcome("declined", "pair-no-path", gateway="g"),
        lambda: SegmentOutcome("hybrid"),
    ],
)
def test_segment_outcome_rejects_inconsistent_fields(bad) -> None:
    with pytest.raises(ValueError):
        bad()


def _pair(*labels: str) -> PairOutcome:
    return PairOutcome(
        "D", tuple((f"J1.{i}->U1.{i}", SegmentOutcome.from_label(o)) for i, o in enumerate(labels))
    )


@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        ((), "not-attempted"),
        (("coupled",), "coupled"),
        (("pair-no-path",), "pair-no-path"),
        (("uncoupled-fallback:pair-no-path",), "uncoupled-fallback:pair-no-path"),
        (("deadline-exceeded",), "deadline-exceeded"),
        (("coupled", "coupled"), "coupled"),
        (("uncoupled-fallback:x", "coupled"), "partially-coupled:1/2"),
        (("coupled", "pair-no-path", "coupled"), "partially-coupled:2/3"),
        (("uncoupled-fallback:x", "uncoupled-fallback:y"), "uncoupled-fallback:x"),
        (("uncoupled-fallback:x", "pair-no-parity"), "pair-no-parity"),
        (("hybrid:U4.B1",), "hybrid:U4.B1"),
        (("hybrid:U4.B1", "coupled"), "partially-coupled:1/2"),
    ],
)
def test_pair_label_matches_the_aggregate(labels: tuple[str, ...], expected: str) -> None:
    from kicad_tools.router.core import _aggregate_pair_outcome

    po = _pair(*labels)
    assert po.label == expected
    assert aggregate_label(list(labels)) == expected
    assert _aggregate_pair_outcome(list(labels)) == expected
    assert po.coupled_segments == (sum(1 for o in labels if o == "coupled"), len(labels))


@pytest.mark.parametrize("pre_chain", ["no-endpoint-couples", "no-net-class"])
def test_pre_chain_pair_label(pre_chain: str) -> None:
    po = PairOutcome("D", pre_chain=pre_chain)
    assert po.label == pre_chain
    assert po.coupled_segments == (0, 0)


def test_segment_outcomes_is_a_dict_of_labels() -> None:
    m = SegmentOutcomes()
    m["a"] = SegmentOutcome.coupled("B.Cu fan")
    m["b"] = SegmentOutcome.fallback("pair-no-path")
    m["c"] = "pair-no-parity"
    assert m == {"a": "coupled", "b": "uncoupled-fallback:pair-no-path", "c": "pair-no-parity"}
    assert isinstance(m["a"], str)
    assert m.record("a") == SegmentOutcome.coupled("B.Cu fan")
    assert m.record("c") == SegmentOutcome.declined("pair-no-parity")
    assert m.record("missing") is None
    assert m.get("b", "").startswith("uncoupled-fallback:")
    # Every mutator keeps labels and records in step.
    m.pop("c")
    assert m.record("c") is None
    m.update({"d": "coupled"})
    assert m.record("d") == SegmentOutcome.coupled()
    m.setdefault("e", "pair-no-path")
    assert m.record("e") == SegmentOutcome.declined("pair-no-path")
    del m["d"]
    assert m.record("d") is None
    m.clear()
    assert m == {} and m.record("a") is None


def test_segment_outcomes_copies_keep_records() -> None:
    m = SegmentOutcomes({("pair", 1, 2): SegmentOutcome.coupled("gw")})
    for clone in (m.copy(), copy.copy(m), copy.deepcopy(m), pickle.loads(pickle.dumps(m))):
        assert clone == m
        assert clone.record(("pair", 1, 2)) == SegmentOutcome.coupled("gw")


def test_segment_record_reads_plain_and_structured_maps() -> None:
    m = SegmentOutcomes({"k": SegmentOutcome.coupled("gw")})
    assert segment_record(m, "k").gateway == "gw"
    assert segment_record({"k": "pair-no-path"}, "k") == SegmentOutcome.declined("pair-no-path")
    assert segment_record({}, "k") == SegmentOutcome("not-attempted")


def test_reason_sets_are_reexported_unchanged() -> None:
    from kicad_tools.router.lattice import pathfinder

    assert pathfinder.EXHAUSTED_SEARCH_REASONS is EXHAUSTED_SEARCH_REASONS
    assert pathfinder.GATEWAY_RETRY_REASONS is GATEWAY_RETRY_REASONS
    assert {
        "no-path",
        "no-path-keepout-constrained",
        "no-path-layer-constrained",
    } == EXHAUSTED_SEARCH_REASONS
    assert {f"pair-{r}" for r in EXHAUSTED_SEARCH_REASONS} | {
        "pair-leg-keepout"
    } == GATEWAY_RETRY_REASONS


# ---------------------------------------------------------------------------
# The engines fill the records (lattice pathfinder + Autorouter summary)
# ---------------------------------------------------------------------------


def test_pathfinder_records_a_gateway_coupled_segment() -> None:
    from tests.router.lattice.test_pair_gateway_6444 import _board, _f_wall

    pf, pc = _board([_f_wall()])
    pf.route_netset([], coupled=[pc], uncoupled_fallback=True, pair_gateways=True)
    assert pf.pair_outcomes[pc.key] == "coupled"
    rec = pf.pair_outcomes.record(pc.key)
    assert rec == SegmentOutcome.coupled(pf.pair_gateways[pc.key])


def test_pathfinder_records_a_fallback_segment() -> None:
    from tests.router.lattice.test_pair_gateway_6444 import _board, _f_wall

    pf, pc = _board([_f_wall()])
    pf.route_netset([], coupled=[pc], uncoupled_fallback=True)
    rec = pf.pair_outcomes.record(pc.key)
    assert rec is not None and rec.status == "uncoupled-fallback"
    assert rec.label == pf.pair_outcomes[pc.key]
    assert rec.reason in GATEWAY_RETRY_REASONS


def test_autorouter_keeps_a_pair_record_per_outcome_string() -> None:
    from tests.router.lattice.test_coupled_pairs import _HS, _autorouter

    router = _autorouter({"D+": _HS, "D-": _HS})
    router.route_net(1)
    assert router._lattice_pair_outcomes == {"D": "coupled"}
    records = router._lattice_pair_outcome_records
    assert set(records) == {"D"}
    po = records["D"]
    assert po.label == "coupled" and po.coupled_segments == (1, 1)
    assert [span for span, _o in po.segments] == ["J1.1->U1.1"]


# ---------------------------------------------------------------------------
# Issue #6948: edge cases
# ---------------------------------------------------------------------------


def test_segment_outcomes_reject_a_none_default_clearly() -> None:
    m = SegmentOutcomes()
    with pytest.raises(TypeError, match="SegmentOutcome or an outcome label"):
        m.setdefault("a")
    assert "a" not in m
    with pytest.raises(TypeError, match="SegmentOutcome or an outcome label"):
        SegmentOutcomes.fromkeys(["a", "b"])
    # A real default still works for both.
    assert m.setdefault("a", "coupled") == "coupled"
    assert SegmentOutcomes.fromkeys(["x"], "pair-no-path") == {"x": "pair-no-path"}


@pytest.mark.parametrize(
    "reason",
    [
        "coupled",
        "deadline-exceeded",
        "not-attempted",
        "uncoupled-fallback:pair-no-path",
        "hybrid:U4.B1",
    ],
)
def test_declined_reason_that_would_not_round_trip_is_rejected(reason: str) -> None:
    with pytest.raises(ValueError, match="would read back as a different status"):
        SegmentOutcome.declined(reason)


@pytest.mark.parametrize("reason", _DECLINES)
def test_real_declined_reasons_still_round_trip(reason: str) -> None:
    rec = SegmentOutcome.declined(reason)
    assert SegmentOutcome.from_label(rec.label) == rec
