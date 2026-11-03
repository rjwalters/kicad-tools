"""Router attach-zone waiver must agree with the census (issue #8277).

``AttachZone.exempts`` was evaluated on the closest-gap *midpoint* only.  For a
trace running past a rated 2-pin THT connector the midpoint sits inside the
pad-bbox + margin zone whenever the pad-side point does, so the in-run audit
waived a 0.85 mm gap against a 1.6 mm requirement that ``kct creepage
--waive-attach-zone`` (both nearest points must be in the zone, then the
residual outside it must clear) fails.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_tools.cli.route_cmd import _audit_pairwise_clearance
from kicad_tools.router.layers import Layer
from kicad_tools.router.pairwise_clearance import build_pairwise_clearance_table
from kicad_tools.router.primitives import Route, Segment

OX, OY = 50.0, 40.0
REQUIRED_MM = 1.6  # 150 V, IEC 60664-1, PD2, IIIa


def _board(segment: str = "") -> str:
    return f"""(kicad_pcb (version 20240108) (generator "test") (generator_version "8.0")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "") (net 1 "/LINE") (net 2 "/NEUT")
  (gr_rect (start {OX} {OY}) (end {OX + 40} {OY + 30}) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
  (footprint "Connector:J2" (layer "F.Cu") (uuid "00000000-0000-0000-0000-000000000010") (at {OX + 10} {OY + 10})
    (property "Reference" "J1" (at 0 -3 0) (layer "F.SilkS")) (property "Value" "x" (at 0 3 0) (layer "F.Fab"))
    (pad "1" thru_hole circle (at 0 0) (size 2.4 2.4) (drill 1.2) (layers "*.Cu" "*.Mask") (net 1 "/LINE"))
    (pad "2" thru_hole circle (at 5 0) (size 2.4 2.4) (drill 1.2) (layers "*.Cu" "*.Mask") (net 2 "/NEUT")))
  {segment}
)
"""


def _x(offset: float) -> float:
    return OX + 10 + offset


def _audit(tmp_path: Path, offset: float):
    board = tmp_path / "b.kicad_pcb"
    board.write_text(_board())
    table = build_pairwise_clearance_table({"/LINE": 150.0, "/NEUT": 0.0}, dru=0.2)
    seg = Segment(
        _x(offset), OY + 5, _x(offset), OY + 15, 0.25, Layer.B_CU, net=1, net_name="/LINE"
    )
    router = SimpleNamespace(
        rules=SimpleNamespace(pairwise_clearance=table, trace_clearance=0.2),
        routes=[Route(net=1, net_name="/LINE", segments=[seg])],
        _emitted_preserved_routes=[],
        _pairwise_attach_zone_pcb_path=str(board),
    )
    return _audit_pairwise_clearance(router, SimpleNamespace(_pairwise_required={}))


# Pad centre at +5, radius 1.2 -> edge at 6.2 (zone edge 6.7); trace half-width 0.125.
@pytest.mark.parametrize(("offset", "gap"), [(7.15, 0.825), (7.3, 0.975), (7.5, 1.175)])
def test_trace_leaving_the_zone_beside_a_rated_pad_is_reported(tmp_path, offset, gap):
    violations = _audit(tmp_path, offset)
    assert len(violations) == 1
    assert violations[0].actual_mm == pytest.approx(gap, abs=5e-3)
    assert violations[0].required_mm == pytest.approx(REQUIRED_MM)


def test_trace_clearing_the_requirement_is_clean(tmp_path):
    assert _audit(tmp_path, 8.0) == []


def test_trace_wholly_inside_the_zone_is_still_waived(tmp_path):
    # Segment edge at 6.35 + 0.125... centre 6.5 -> inside pad bbox + margin (6.7)
    # for its full width is impossible (needs <= 6.575); use a short stub.
    board = tmp_path / "b.kicad_pcb"
    board.write_text(_board())
    table = build_pairwise_clearance_table({"/LINE": 150.0, "/NEUT": 0.0}, dru=0.2)
    seg = Segment(_x(6.5), OY + 9, _x(6.5), OY + 11, 0.1, Layer.B_CU, net=1, net_name="/LINE")
    router = SimpleNamespace(
        rules=SimpleNamespace(pairwise_clearance=table, trace_clearance=0.2),
        routes=[Route(net=1, net_name="/LINE", segments=[seg])],
        _emitted_preserved_routes=[],
        _pairwise_attach_zone_pcb_path=str(board),
    )
    assert _audit_pairwise_clearance(router, SimpleNamespace(_pairwise_required={})) == []


def test_router_and_census_agree(tmp_path):
    """Whatever the audit passes, the census (with --waive-attach-zone) passes."""
    vmap = tmp_path / "vm.json"
    vmap.write_text(json.dumps({"/LINE": 150.0, "/NEUT": 0.0}))
    for offset in (7.3, 8.0):
        seg = (
            f"(segment (start {_x(offset)} {OY + 5}) (end {_x(offset)} {OY + 15}) "
            '(width 0.25) (layer "B.Cu") (net 1))'
        )
        pcb = tmp_path / f"c{offset}.kicad_pcb"
        pcb.write_text(_board(seg))
        rc = subprocess.run(
            [
                sys.executable,
                "-m",
                "kicad_tools.cli",
                "creepage",
                str(pcb),
                "--voltage-map",
                str(vmap),
                "--standard",
                "iec60664",
                "--pollution-degree",
                "2",
                "--material-group",
                "IIIa",
                "--waive-same-footprint",
                "--waive-attach-zone",
            ],
            capture_output=True,
            text=True,
        ).returncode
        audit_clean = _audit(tmp_path, offset) == []
        assert (rc == 0) == audit_clean, (
            f"offset {offset}: census rc={rc}, audit clean={audit_clean}"
        )
