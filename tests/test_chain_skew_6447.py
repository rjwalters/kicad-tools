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


# ---------------------------------------------------------------------------
# Issue #9767: the defect-copper correction on the chain producer
# ---------------------------------------------------------------------------


def _defect_chain_board(stub: bool) -> str:
    """A real board with two pairs joined by two series parts (crossed).

    Every member net runs 20 mm on F.Cu between its driver and series-part
    pad; the series parts cross the polarity (``A_D+`` joins ``B_D-``), so
    the clean chain is perfectly balanced end to end.  With ``stub`` a
    6 mm dead-end branch (above the 4 mm epsilon) hangs off ``A_D+``.
    """
    stub_seg = '  (segment (start 15 5) (end 15 11) (width 0.2) (layer "F.Cu") (net 1))\n'
    return f"""(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "A_D+")
  (net 2 "A_D-")
  (net 3 "B_D+")
  (net 4 "B_D-")
  (gr_rect (start 0 0) (end 60 60) (stroke (width 0.1)) (layer "Edge.Cuts"))
  (footprint "Test:Drv" (layer "F.Cu") (at 0 0)
    (property "Reference" "J1")
    (pad "1" smd rect (at 5 5) (size 1.0 1.0) (layers "F.Cu" "F.Mask") (net 1 "A_D+"))
    (pad "2" smd rect (at 5 15) (size 1.0 1.0) (layers "F.Cu" "F.Mask") (net 2 "A_D-"))
    (pad "3" smd rect (at 50 15) (size 1.0 1.0) (layers "F.Cu" "F.Mask") (net 3 "B_D+"))
    (pad "4" smd rect (at 50 5) (size 1.0 1.0) (layers "F.Cu" "F.Mask") (net 4 "B_D-"))
  )
  (footprint "Test:R1" (layer "F.Cu") (at 0 0)
    (property "Reference" "R1")
    (pad "1" smd rect (at 25 5) (size 1.0 1.0) (layers "F.Cu" "F.Mask") (net 1 "A_D+"))
    (pad "2" smd rect (at 30 5) (size 1.0 1.0) (layers "F.Cu" "F.Mask") (net 4 "B_D-"))
  )
  (footprint "Test:R2" (layer "F.Cu") (at 0 0)
    (property "Reference" "R2")
    (pad "1" smd rect (at 25 15) (size 1.0 1.0) (layers "F.Cu" "F.Mask") (net 2 "A_D-"))
    (pad "2" smd rect (at 30 15) (size 1.0 1.0) (layers "F.Cu" "F.Mask") (net 3 "B_D+"))
  )
  (segment (start 5 5) (end 25 5) (width 0.2) (layer "F.Cu") (net 1))
{stub_seg if stub else ""}  (segment (start 5 15) (end 25 15) (width 0.2) (layer "F.Cu") (net 2))
  (segment (start 30 15) (end 50 15) (width 0.2) (layer "F.Cu") (net 3))
  (segment (start 30 5) (end 50 5) (width 0.2) (layer "F.Cu") (net 4))
)
"""


class TestChainDefectCopper9767:
    """Chain figures exclude member defect copper, polarity-aligned (#9767)."""

    @staticmethod
    def _pcb(tmp_path, stub: bool):
        from kicad_tools.schema.pcb import PCB

        path = tmp_path / f"defect-chain-{stub}.kicad_pcb"
        path.write_text(_defect_chain_board(stub))
        return PCB.load(str(path))

    def test_stub_on_one_member_is_shed_from_the_chain(self, tmp_path):
        from kicad_tools.router.diffpair_length import DiffPairLengthTracker

        clean, stubbed = self._pcb(tmp_path, False), self._pcb(tmp_path, True)
        ncm = _classes()
        (clean_chain,) = derive_chain_skew(clean, ncm)
        (stubbed_chain,) = derive_chain_skew(stubbed, ncm)

        assert clean_chain.skew_mm == pytest.approx(0.0, abs=0.05)
        # Raw KiCad length of the stubbed member keeps the 6 mm branch.
        assert DiffPairLengthTracker.measure_net_from_pcb(stubbed, 1) == pytest.approx(
            26.0, abs=0.05
        )
        # The chain's polarity-aligned sums exclude it.
        assert stubbed_chain.skew_mm == pytest.approx(0.0, abs=0.05)
        assert stubbed_chain.side_a_mm + stubbed_chain.side_b_mm == pytest.approx(80.0, abs=0.1)

    def test_raw_kicad_length_unchanged_for_every_member(self, tmp_path):
        from kicad_tools.router.kicad_length import measure_pcb_net

        stubbed = self._pcb(tmp_path, True)
        clean = self._pcb(tmp_path, False)
        for net_id in (1, 2, 3, 4):
            assert measure_pcb_net(stubbed, net_id) == pytest.approx(
                measure_pcb_net(clean, net_id) + (6.0 if net_id == 1 else 0.0), abs=0.05
            )
