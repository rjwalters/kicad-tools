"""Issue #9647: a thin branch leaving an endpoint pad is not path evidence.

A declared current path's first thin branch segment can lie wholly inside a
large endpoint pad. ``_bfs_path`` used to add every wholly pad-internal
segment at a route hub as path evidence, so the resolver reported the sense
branch's 0.3 mm neck as the resolved path's minimum width -- and
``path_ampacity`` raised a false "too narrow / would fuse" error on exactly
the nets that carry Kelvin/sense taps.

The branch must be excluded at *both* endpoints, while copper that really is
in series with the route (a pad-internal trunk piece, a stub to a via buried
in the pad) stays in the result.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.current_paths import (
    CurrentPathSpec,
    PathEndpoint,
    audit_current_paths,
    resolve_current_path,
)
from kicad_tools.schema.pcb import PCB

from .test_current_paths import _add_pad_footprint, _seg_len


def _branch_spec(*, source=("J1", "1"), sink=("J2", "1")) -> CurrentPathSpec:
    return CurrentPathSpec(
        name="TRUNK",
        net_name="NET1",
        source=PathEndpoint(*source),
        sink=PathEndpoint(*sink),
        continuous_a=15.0,
    )


def _branch_board(
    *,
    branch_at: str,
    divider: bool = True,
    branch_layer: str = "F.Cu",
    pad_size: float = 5.0,
):
    """A 4.0 mm J1->J2 trunk with a 0.3 mm branch leaving one endpoint pad.

    The branch's first segment ``(ex, ey) -> (ex+1, ey+1)`` lies wholly
    inside that pad's ``pad_size`` copper (its far end is sqrt(2) ~ 1.41 mm
    from the pad centre), so it is wholly pad-internal copper: the branch is
    not a dead-end stub, so the #9494 pruning cannot remove it either.
    """
    pcb = PCB.create(width=200, height=120, center=False)
    _add_pad_footprint(pcb, ref="J1", x=20, y=50, net="NET1")
    _add_pad_footprint(pcb, ref="J2", x=120, y=50, net="NET1")
    pcb.get_footprint("J1").pads[0].size = (pad_size, pad_size)
    pcb.get_footprint("J2").pads[0].size = (pad_size, pad_size)
    if divider:
        _add_pad_footprint(pcb, ref="R1", x=130, y=70, net="NET1")
    pcb.add_trace(("J1", "1"), (60, 50), width=4.0, layer="F.Cu", net="NET1")
    pcb.add_trace((60, 50), ("J2", "1"), width=4.0, layer="F.Cu", net="NET1")
    ex, ey = (120, 50) if branch_at == "sink" else (20, 50)
    pcb.add_trace((ex, ey), (ex + 1, ey + 1), width=0.3, layer=branch_layer, net="NET1")
    pcb.add_trace((ex + 1, ey + 1), (ex + 1, 70), width=0.3, layer=branch_layer, net="NET1")
    if divider:
        pcb.add_trace((ex + 1, 70), ("R1", "1"), width=0.3, layer=branch_layer, net="NET1")
    return pcb


def _trunk_ids(pcb):
    return {id(s) for s in pcb.segments if s.width == 4.0}


def _branch_ids(pcb):
    return {id(s) for s in pcb.segments if s.width == 0.3}


@pytest.mark.parametrize("branch_at", ["sink", "source"])
@pytest.mark.parametrize("divider", [True, False], ids=["divider_pad", "dangling"])
def test_in_pad_branch_segment_is_not_path_evidence(branch_at, divider):
    pcb = _branch_board(branch_at=branch_at, divider=divider)
    result = resolve_current_path(pcb, _branch_spec())
    assert result.ok, result.reason
    assert {id(s) for s in result.segments} == _trunk_ids(pcb)
    assert not _branch_ids(pcb) & {id(s) for s in result.segments}
    assert result.length_mm == pytest.approx(sum(_seg_len(s) for s in pcb.segments[:2]))
    assert result.min_width is not None and result.min_width.width_mm == pytest.approx(4.0)


def test_in_pad_branch_on_opposite_layer_is_not_path_evidence():
    # A thru-hole endpoint pad covers *.Cu, so a B.Cu branch's first segment
    # is just as pad-internal as an F.Cu one.
    pcb = _branch_board(branch_at="sink", branch_layer="B.Cu")
    result = resolve_current_path(pcb, _branch_spec())
    assert result.ok, result.reason
    assert {id(s) for s in result.segments} == _trunk_ids(pcb)
    assert result.min_width is not None and result.min_width.width_mm == pytest.approx(4.0)


@pytest.mark.parametrize("branch_at", ["sink", "source"])
def test_in_pad_branch_stays_uncovered_audit_evidence(branch_at):
    pcb = _branch_board(branch_at=branch_at)
    audit = audit_current_paths(pcb, [_branch_spec()])
    assert audit.all_resolved
    assert not audit.fully_covered
    assert {id(s) for s in audit.uncovered["NET1"]} == _branch_ids(pcb)


def test_small_pad_branch_is_still_excluded():
    # The default 1.0 mm pad also swallows a short first branch segment.
    pcb = _branch_board(branch_at="sink", divider=True, pad_size=1.0)
    branch = pcb.segments[2]
    branch.start, branch.end = (120, 50), (120.3, 50.2)
    pcb.segments[3].start = (120.3, 50.2)
    result = resolve_current_path(pcb, _branch_spec())
    assert result.ok, result.reason
    assert {id(s) for s in result.segments} == _trunk_ids(pcb)


def test_pad_internal_trunk_piece_stays_on_route():
    # The trunk's last span runs on inside the J2 pad to the pad centre:
    # wholly pad-internal copper that genuinely is in series with the route
    # must stay evidence (and keep carrying its 1.0 mm neck).
    pcb = _branch_board(branch_at="sink", divider=False, pad_size=5.0)
    pcb._segments = []
    pcb.add_trace(("J1", "1"), (60, 50), width=4.0, layer="F.Cu", net="NET1", dedupe=False)
    pcb.add_trace((60, 50), (118, 50), width=4.0, layer="F.Cu", net="NET1", dedupe=False)
    pcb.add_trace((118, 50), (120, 50), width=1.0, layer="F.Cu", net="NET1", dedupe=False)
    result = resolve_current_path(pcb, _branch_spec())
    assert result.ok, result.reason
    assert {id(s) for s in result.segments} == {
        id(s) for s in pcb.segments if s.width in (4.0, 1.0)
    }
    assert result.min_width is not None and result.min_width.width_mm == pytest.approx(1.0)


def test_stub_to_via_buried_in_pad_stays_on_route():
    # A single via dropped inside the endpoint pad: the stub from the pad
    # centre to the via is wholly pad-internal, but it is the only copper
    # joining the pad to the B.Cu trunk, so it stays evidence.
    pcb = _branch_board(branch_at="sink", divider=False, pad_size=5.0)
    pcb._segments = []
    pcb.add_trace((20, 50), (21, 50), width=1.5, layer="F.Cu", net="NET1", dedupe=False)
    pcb.add_via(21, 50, net="NET1", dedupe=False)
    pcb.add_trace((21, 50), (120, 50), width=3.0, layer="B.Cu", net="NET1")
    result = resolve_current_path(pcb, _branch_spec())
    assert result.ok, result.reason
    stub = next(s for s in pcb.segments if s.width == 1.5)
    trunk = next(s for s in pcb.segments if s.width == 3.0)
    assert {id(s) for s in result.segments} == {id(stub), id(trunk)}
    assert result.min_width is not None and result.min_width.width_mm == pytest.approx(1.5)
