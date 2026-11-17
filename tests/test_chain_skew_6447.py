"""Chain (XNet) pair-skew measurement across series elements (Issue #6447)."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from kicad_tools.router.rules import NetClassRouting
from kicad_tools.validate.diffpair_skew import derive_chain_skew
from kicad_tools.validate.rules.diffpair_length_skew import DiffPairLengthSkewRule

from .test_validate_diffpair_skew import _StubNet, _StubPCB, _StubSegment


@dataclass
class _Pad:
    net_name: str
    type: str = "smd"
    net_number: int = 999  # matches no net: keeps pad geometry out of lengths


@dataclass
class _Footprint:
    pads: list[_Pad] = field(default_factory=list)


@dataclass
class _ChainPCB(_StubPCB):
    footprints: list[_Footprint] = field(default_factory=list)


def _board(lengths: dict[str, float], links: list[tuple[str, str]]) -> _ChainPCB:
    nets = {0: _StubNet(0, "")}
    segs = []
    for i, (name, length) in enumerate(lengths.items(), start=1):
        nets[i] = _StubNet(i, name)
        segs.append(
            _StubSegment(start=(0.0, float(i)), end=(length, float(i)), net_number=i, net_name=name)
        )
    fps = [_Footprint([_Pad(a), _Pad(b)]) for a, b in links]
    return _ChainPCB(_nets=nets, _segments=segs, footprints=fps)


def _classes(chain_limit: float | None = None) -> dict[str, NetClassRouting]:
    nc = NetClassRouting(name="HS", coupled_routing=True, chain_skew_tolerance_mm=chain_limit)
    return dict.fromkeys(("A_D+", "A_D-", "B_D+", "B_D-"), nc)


STRAIGHT = [("A_D+", "B_D+"), ("A_D-", "B_D-")]
SWAPPED = [("A_D+", "B_D-"), ("A_D-", "B_D+")]


def test_segment_skews_cancel_across_a_straight_chain():
    pcb = _board({"A_D+": 10, "A_D-": 12, "B_D+": 7, "B_D-": 5}, STRAIGHT)
    (chain,) = derive_chain_skew(pcb, _classes())
    assert chain.side_a_mm == pytest.approx(17.0)
    assert chain.side_b_mm == pytest.approx(17.0)
    assert chain.skew_mm == pytest.approx(0.0)


def test_segment_skews_add_when_they_lean_the_same_way():
    pcb = _board({"A_D+": 10, "A_D-": 12, "B_D+": 5, "B_D-": 7}, STRAIGHT)
    (chain,) = derive_chain_skew(pcb, _classes())
    assert chain.skew_mm == pytest.approx(4.0)


def test_polarity_swap_across_the_part_is_followed():
    pcb = _board({"A_D+": 10, "A_D-": 12, "B_D+": 5, "B_D-": 7}, SWAPPED)
    (chain,) = derive_chain_skew(pcb, _classes())
    assert chain.skew_mm == pytest.approx(0.0)


def test_one_series_part_is_not_a_chain():
    pcb = _board({"A_D+": 10, "A_D-": 12, "B_D+": 5, "B_D-": 7}, STRAIGHT[:1])
    assert derive_chain_skew(pcb, _classes()) == []


def test_no_class_map_is_a_no_op():
    pcb = _board({"A_D+": 10, "A_D-": 12, "B_D+": 5, "B_D-": 7}, STRAIGHT)
    assert derive_chain_skew(pcb, None) == []


def test_errors_only_where_a_chain_limit_is_declared():
    pcb = _board({"A_D+": 10, "A_D-": 12, "B_D+": 5, "B_D-": 7}, STRAIGHT)

    no_limit = DiffPairLengthSkewRule(chains=derive_chain_skew(pcb, _classes()))
    assert no_limit.check(pcb, None).violations == []

    informational = DiffPairLengthSkewRule(
        chains=derive_chain_skew(pcb, _classes()), emit_info=True
    )
    (info,) = informational.check(pcb, None).violations
    assert info.severity == "info" and info.actual_value == pytest.approx(4.0)

    tight = DiffPairLengthSkewRule(chains=derive_chain_skew(pcb, _classes(1.0)))
    (err,) = tight.check(pcb, None).violations
    assert err.severity == "error" and err.required_value == pytest.approx(1.0)

    loose = DiffPairLengthSkewRule(chains=derive_chain_skew(pcb, _classes(5.0)))
    assert loose.check(pcb, None).violations == []
