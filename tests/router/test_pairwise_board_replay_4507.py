"""Board-FILE replay of the pairwise HV gate, and the frame it must be read in.

Issue #4507 (router Phase 2 of epic #4431).  T4 -- the softstart rev-C manual
proof -- has to attribute each ``kct creepage`` fail to *routed copper* versus
placement/pour geometry, which the census cannot do: it is net-pair keyed, it
measures pads and pour fills as well as traces, and it has no concept of the
#4506 rated-footprint attach-zone exemption.  The only tool that can answer
"what does the ROUTER's gate say about this board?" is a replay of
``segment_pair_violation`` over the finished file.

Every prior T4 scoring wrote that replay as a throwaway script, and the
2026-08-15 run got the **coordinate frame** wrong in a way that is silently
bidirectional: ``PCB.load`` reports footprint positions *board-relative*
(``schema/pcb.py::_detect_board_origin``) while ``(segment ...)`` coordinates
in the same file are *sheet-absolute*, so attach zones built straight off
``PCB.load(...).footprints`` sit ``board_origin`` away from the copper they are
meant to waive.  On a board that does not sit at sheet origin -- i.e. every real
board -- that turns a waived rated-footprint pair into a reported "router leak"
(and, coincidentally, waives whatever unrelated copper the misplaced rectangle
happens to cover).

These tests pin the supported entry points and, specifically, that they behave
identically at any board origin:

* :func:`board_attach_zones` -- zones in the board file's own frame;
* :func:`board_trace_routes` -- one route per net, with distinct net ids;
* :func:`board_pairwise_violations` -- the replay itself;
* the CLI's in-run resolver and the replay agreeing on the frame.

Boards are fully synthetic S-expression strings (same approach as
``test_route_pairwise_gate_4588.py``) -- the softstart rev-C board is local-only
and must never become a CI dependency.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.router.pairwise_clearance import (
    board_attach_zones,
    board_pairwise_violations,
    board_trace_routes,
    build_attach_zones,
    build_pairwise_clearance_table,
    violation_pair_keys,
)

# 300 V against 0 V under IEC 60664-1 / PD2 / material group IIIa requires
# 3.2 mm of creepage; every planted gap below is well inside that and well
# outside the 0.2 mm scalar DRU floor, so the gate has an unambiguous finding
# that the ordinary scalar clearance check would not make.
HV_VOLTS = 300.0
DRU_MM = 0.2
REQUIRED_MM = 3.2

# Both planted shortfalls are on the same net pair; only their location differs.
HV_LV_PAIR = ("HV_LINE", "LV_SENSE")


def _pad(number: str, x: float, y: float, net: int, net_name: str) -> str:
    return (
        f'(pad "{number}" smd rect (at {x} {y}) (size 0.4 0.4) '
        f'(layers "F.Cu" "F.Paste" "F.Mask") (net {net} "{net_name}"))'
    )


def _fp(ref: str, uid: int, x: float, y: float, pads: str) -> str:
    return f"""  (footprint "Resistor_SMD:R_0603_1608Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-0000000000{uid:02d}")
    (at {x} {y})
    (property "Reference" "{ref}" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    {pads}
  )
"""


def _replay_board(origin_x: float, origin_y: float) -> str:
    """Routed board carrying two HV<->LV shortfalls, one of them rated.

    ``R5`` is the canonical #4506 domain-bridging part (pad 1 ``/HV_LINE``,
    pad 2 ``/LV_SENSE``, 2 mm apart).  The traces attaching to its two pads come
    within 1.8 mm of each other **inside R5's own attach zone** -- the #4506
    exemption exists to waive exactly that.  A second pair of traces runs
    0.8 mm apart in the board's north-west corner, far from any footprint, and
    nothing waives it.

    The board rectangle starts at ``(origin_x, origin_y)``, which is what
    ``PCB._detect_board_origin`` picks up as the board origin -- so a replay
    that forgets the board-relative -> sheet-absolute shift mis-scores this
    board at every origin except (0, 0).
    """
    ox, oy = origin_x, origin_y
    bridge_pads = "\n    ".join(
        [
            _pad("1", -1.0, 0, 1, "/HV_LINE"),
            _pad("2", 1.0, 0, 2, "/LV_SENSE"),
        ]
    )
    parts = [
        _fp("R1", 10, ox + 5.0, oy + 5.0, _pad("1", 0, 0, 1, "/HV_LINE")),
        _fp("R5", 11, ox + 15.0, oy + 5.0, bridge_pads),
        _fp("R3", 12, ox + 25.0, oy + 5.0, _pad("1", 0, 0, 2, "/LV_SENSE")),
    ]
    return f"""(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general
    (thickness 1.6)
  )
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup
    (pad_to_mask_clearance 0)
  )
  (net 0 "")
  (net 1 "/HV_LINE")
  (net 2 "/LV_SENSE")
  (gr_rect (start {ox} {oy}) (end {ox + 30.0} {oy + 16.0})
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
  (segment (start {ox + 10.0} {oy + 5.0}) (end {ox + 14.0} {oy + 5.0}) \
(width 0.2) (layer "F.Cu") (net 1))
  (segment (start {ox + 16.0} {oy + 5.0}) (end {ox + 20.0} {oy + 5.0}) \
(width 0.2) (layer "F.Cu") (net 2))
  (segment (start {ox + 3.0} {oy + 12.0}) (end {ox + 10.0} {oy + 12.0}) \
(width 0.2) (layer "F.Cu") (net 1))
  (segment (start {ox + 3.0} {oy + 13.0}) (end {ox + 10.0} {oy + 13.0}) \
(width 0.2) (layer "F.Cu") (net 2))
{"".join(parts)})
"""


@pytest.fixture
def table():
    return build_pairwise_clearance_table(
        {"/HV_LINE": HV_VOLTS, "/LV_SENSE": 0.0},
        dru=DRU_MM,
    )


def _write(tmp_path: Path, origin_x: float, origin_y: float) -> Path:
    board = tmp_path / "routed.kicad_pcb"
    board.write_text(_replay_board(origin_x, origin_y))
    return board


# Sheet origin, and two offsets a real board actually uses.  The (0, 0) case is
# the one a frame-blind replay still gets right, which is precisely why it is
# not sufficient on its own.
ORIGINS = [(0.0, 0.0), (68.5, 55.0), (100.0, 100.0)]


class TestBoardAttachZones:
    """The #4506 zones must land on the copper they are meant to waive."""

    @pytest.mark.parametrize("origin", ORIGINS)
    def test_zones_are_in_the_board_files_own_frame(self, tmp_path, origin):
        ox, oy = origin
        board = _write(tmp_path, ox, oy)

        zones = board_attach_zones(board)

        assert len(zones) == 1, f"expected only the 2-net bridging footprint, got {zones}"
        zone = zones[0]
        # R5's pads sit at (ox+14, oy+5) and (ox+16, oy+5) in the FILE, so the
        # zone must contain their midpoint -- the closest-gap point the waiver
        # is probed at.
        assert zone.min_x < ox + 15.0 < zone.max_x
        assert zone.min_y < oy + 5.0 < zone.max_y

    @pytest.mark.parametrize("origin", ORIGINS)
    def test_unshifted_zones_are_the_frame_bug_this_prevents(self, tmp_path, origin):
        """The naive ``build_attach_zones(PCB.load(...).footprints)`` form."""
        from kicad_tools.schema.pcb import PCB

        ox, oy = origin
        board = _write(tmp_path, ox, oy)

        naive = build_attach_zones(PCB.load(str(board)).footprints)
        shifted = board_attach_zones(board)

        assert len(naive) == len(shifted) == 1
        # Identical only at sheet origin; off by exactly board_origin otherwise.
        assert shifted[0].min_x == pytest.approx(naive[0].min_x + ox)
        assert shifted[0].min_y == pytest.approx(naive[0].min_y + oy)

    @pytest.mark.parametrize("origin", ORIGINS)
    def test_agrees_with_the_cli_in_run_resolver(self, tmp_path, origin):
        """One frame implementation, shared by the replay and the #4588 gate."""
        from types import SimpleNamespace

        from kicad_tools.cli.route_cmd import _pairwise_attach_zones

        board = _write(tmp_path, *origin)
        router = SimpleNamespace(_pairwise_attach_zone_pcb_path=str(board))

        assert _pairwise_attach_zones(router) == board_attach_zones(board)


class TestBoardTraceRoutes:
    @pytest.mark.parametrize("origin", ORIGINS)
    def test_one_route_per_net_with_distinct_ids(self, tmp_path, origin):
        board = _write(tmp_path, *origin)

        routes = board_trace_routes(board)

        assert sorted(r.net_name for r in routes) == ["/HV_LINE", "/LV_SENSE"]
        # Distinct ids matter: ``find_pairwise_violations`` skips equal-id route
        # pairs, and a name-based board resolves unknown names to id 0.
        assert len({r.net for r in routes}) == 2
        assert all(len(r.segments) == 2 for r in routes)


class TestBoardPairwiseViolations:
    @pytest.mark.parametrize("origin", ORIGINS)
    def test_reports_the_unrated_shortfall_at_any_origin(self, tmp_path, table, origin):
        board = _write(tmp_path, *origin)

        violations = board_pairwise_violations(board, table)

        assert len(violations) == 1, f"expected only the unrated shortfall, got {violations}"
        found = violations[0]
        assert violation_pair_keys(violations) == {HV_LV_PAIR}
        assert found.actual_mm == pytest.approx(0.8)
        assert found.required_mm == pytest.approx(REQUIRED_MM)

    @pytest.mark.parametrize("origin", ORIGINS)
    def test_rated_shortfall_is_waived_only_by_a_correctly_framed_zone(
        self, tmp_path, table, origin
    ):
        """The regression the frame bug produces: a phantom second "leak"."""
        board = _write(tmp_path, *origin)

        waived = board_pairwise_violations(board, table)
        unwaived = board_pairwise_violations(board, table, attach_zones=())

        assert len(waived) == 1
        # Disabling the exemption (the census' view) surfaces R5's own copper.
        assert len(unwaived) == 2
        assert {round(v.actual_mm, 3) for v in unwaived} == {0.8, 1.8}

    def test_dru_floor_defaults_to_the_tables(self, tmp_path, table):
        board = _write(tmp_path, 68.5, 55.0)

        assert board_pairwise_violations(board, table, dru=table.dru) == (
            board_pairwise_violations(board, table)
        )

    def test_pairs_below_the_requirement_floor_are_not_reported(self, tmp_path):
        """No HV widening (all nets at one potential) -> the gate is a no-op."""
        board = _write(tmp_path, 68.5, 55.0)
        flat = build_pairwise_clearance_table({"/HV_LINE": 0.0, "/LV_SENSE": 0.0}, dru=DRU_MM)

        assert board_pairwise_violations(board, flat) == []
