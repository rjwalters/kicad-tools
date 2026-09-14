"""The in-run #4588 pairwise audit must see foreign PAD copper (issue #4507).

#4507's revised Ask item 4 asked for the shared pairwise kernel to be widened
past trace-vs-trace to trace-vs-pad, via-vs-pad and via-vs-trace geometry "so
the #4588 post-route audit **and** the T4 replay can actually see the copper the
census scores".  PRs #4885/#4887 did the kernel half: ``find_pairwise_violations``
grew a ``foreign_pads`` parameter and ``board_pairwise_violations`` populates it
from :func:`~kicad_tools.router.pairwise_clearance.board_pad_geometry`.

Only the *replay* was ever handed pads, though.  The in-run audit -- the gate
that decides whether ``kct route`` prints SUCCESS and exits 0 -- kept calling
``find_pairwise_violations`` with no ``foreign_pads`` at all, so it was
structurally blind to exactly the class of copper the T4 proof measured as the
dominant residual: on the 2026-08-21 softstart rev-C run, **13 of the 17**
board-level census fails were a routed trace or via against a foreign pad, and
0 were trace-vs-trace.  A kernel that can check pads but is never given any is
the same shape of silent false pass #4588 and #4699 each closed in turn.

These tests pin the closing of that gap:

1. a routed HV trace running inside the requirement of an isolated foreign LV
   **pad** (no LV trace anywhere near it) is reported by the in-run audit;
2. it is reported at any board origin -- pad polygons carry the same
   board-relative -> sheet-absolute ``board_origin`` shift the #4506 attach
   zones do, and getting that wrong is silently bidirectional;
3. the #4506 rated-footprint exemption waives a pad finding exactly as it
   waives a trace one (otherwise every domain-bridging package becomes a hard
   false fail);
4. a router with no recorded source board degrades to the pre-#4507 trace/via
   scope instead of raising;
5. end to end: a board whose copper only conflicts with a pad fails the run.

Boards are fully synthetic S-expression strings (the softstart rev-C fixture is
local-only and must never become a CI dependency).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_tools.cli.route_cmd import _audit_pairwise_clearance, _pairwise_pad_geometry
from kicad_tools.router.pairwise_clearance import build_pairwise_clearance_table

# 300 V against 0 V at IEC 60664-1 / PD2 / material group IIIa requires 3.2 mm
# of creepage.  Every planted gap below is far inside that and far outside the
# 0.2 mm scalar DRU floor, so only the HV widening can produce a finding.
HV_VOLTS = 300.0
DRU_MM = 0.2
REQUIRED_MM = 3.2

# Non-zero origins are the interesting ones: a frame-blind pad resolver still
# scores a board at (0, 0) correctly, which is why that case alone proves
# nothing.
ORIGINS = [(0.0, 0.0), (68.5, 55.0), (100.0, 100.0)]


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


def _pad_only_board(origin_x: float, origin_y: float, *, rated: bool = False) -> str:
    """Board whose only HV<->LV proximity is a trace against a foreign PAD.

    ``R1`` carries a single isolated ``/LV_SENSE`` pad at ``(ox+10, oy+10)``;
    the ``/HV_LINE`` trace runs 0.8 mm above its centre, i.e. **0.5 mm**
    edge-to-edge (pad half-extent 0.2 mm + trace half-width 0.1 mm), against a
    3.2 mm requirement.  There is deliberately no ``/LV_SENSE`` *trace*
    anywhere, so a trace-vs-trace-only audit has nothing at all to report and
    any finding must come from pad geometry.

    With ``rated=True`` the same pad sits on a two-net footprint (pad 2 is
    ``/HV_LINE``), which is the #4506 domain-bridging shape ``build_attach_zones``
    recognises -- so the identical geometry must then be waived.
    """
    ox, oy = origin_x, origin_y
    if rated:
        pads = "\n    ".join(
            [
                _pad("1", 0.0, 0.0, 2, "/LV_SENSE"),
                _pad("2", 2.0, 0.0, 1, "/HV_LINE"),
            ]
        )
    else:
        pads = _pad("1", 0.0, 0.0, 2, "/LV_SENSE")
    parts = [
        _fp("R1", 10, ox + 10.0, oy + 10.0, pads),
        _fp("R2", 11, ox + 5.0, oy + 20.0, _pad("1", 0, 0, 3, "/SIG_A")),
        _fp("R3", 12, ox + 25.0, oy + 20.0, _pad("1", 0, 0, 3, "/SIG_A")),
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
  (net 3 "/SIG_A")
  (gr_rect (start {ox} {oy}) (end {ox + 30.0} {oy + 26.0})
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
  (segment (start {ox + 4.0} {oy + 10.8}) (end {ox + 16.0} {oy + 10.8}) \
(width 0.2) (layer "F.Cu") (net 1))
{"".join(parts)})
"""


@pytest.fixture
def table():
    return build_pairwise_clearance_table(
        {"/HV_LINE": HV_VOLTS, "/LV_SENSE": 0.0},
        dru=DRU_MM,
    )


def _hv_trace_route(origin_x: float, origin_y: float):
    """The board's own ``/HV_LINE`` segment, as the router would carry it."""
    from kicad_tools.router.layers import Layer
    from kicad_tools.router.primitives import Route, Segment

    ox, oy = origin_x, origin_y
    return Route(
        net=1,
        net_name="/HV_LINE",
        segments=[
            Segment(
                ox + 4.0,
                oy + 10.8,
                ox + 16.0,
                oy + 10.8,
                0.2,
                Layer.F_CU,
                net=1,
                net_name="/HV_LINE",
            )
        ],
    )


def _router(board: Path | None, table, routes):
    return SimpleNamespace(
        rules=SimpleNamespace(pairwise_clearance=table, trace_clearance=DRU_MM),
        routes=routes,
        _emitted_preserved_routes=[],
        _pairwise_attach_zone_pcb_path=None if board is None else str(board),
    )


def _write(tmp_path: Path, origin: tuple[float, float], *, rated: bool = False) -> Path:
    board = tmp_path / "board.kicad_pcb"
    board.write_text(_pad_only_board(*origin, rated=rated))
    return board


class TestInRunAuditSeesPads:
    """The gap this issue's last increment closes."""

    @pytest.mark.parametrize("origin", ORIGINS)
    def test_trace_against_a_foreign_pad_is_reported(self, tmp_path, table, origin):
        board = _write(tmp_path, origin)
        router = _router(board, table, [_hv_trace_route(*origin)])

        violations = _audit_pairwise_clearance(router, SimpleNamespace(_pairwise_required={}))

        assert len(violations) == 1, f"expected the trace-vs-pad shortfall, got {violations}"
        found = violations[0]
        assert {found.net_a, found.net_b} == {"/HV_LINE", "/LV_SENSE"}
        assert found.actual_mm == pytest.approx(0.5, abs=5e-3)
        assert found.required_mm == pytest.approx(REQUIRED_MM)

    @pytest.mark.parametrize("origin", ORIGINS)
    def test_the_pre_4507_scope_reports_nothing_on_this_board(self, tmp_path, table, origin):
        """Control: without pad geometry there is nothing to find at all.

        This is the exact blindness the widening removes -- the board carries
        no ``/LV_SENSE`` trace and no vias, so a trace/via-only walk is
        vacuously clean over copper the census scores as a hard fail.
        """
        from kicad_tools.router.pairwise_clearance import find_pairwise_violations

        assert (
            find_pairwise_violations(
                [_hv_trace_route(*origin)],
                table,
                dru=DRU_MM,
            )
            == []
        )

    @pytest.mark.parametrize("origin", ORIGINS)
    def test_rated_bridging_footprint_waives_the_pad_finding(self, tmp_path, table, origin):
        """#4506 must apply to pad findings, or every HV board false-fails."""
        board = _write(tmp_path, origin, rated=True)
        router = _router(board, table, [_hv_trace_route(*origin)])

        assert _audit_pairwise_clearance(router, SimpleNamespace(_pairwise_required={})) == []

    def test_unmapped_pair_stays_on_the_scalar_path(self, tmp_path):
        """No HV widening for the pair -> no pad finding (the dormant case)."""
        flat = build_pairwise_clearance_table({"/HV_LINE": 0.0, "/LV_SENSE": 0.0}, dru=DRU_MM)
        board = _write(tmp_path, (68.5, 55.0))
        router = _router(board, flat, [_hv_trace_route(68.5, 55.0)])

        assert _audit_pairwise_clearance(router, SimpleNamespace(_pairwise_required={})) == []


class TestPadGeometryResolver:
    """``_pairwise_pad_geometry`` mirrors ``_pairwise_attach_zones``' contract."""

    def test_memoises_on_the_router(self, tmp_path, table):
        board = _write(tmp_path, (68.5, 55.0))
        router = _router(board, table, [])

        first = _pairwise_pad_geometry(router)
        assert first  # the board has connected pads
        assert router._pairwise_pad_geometry_cache is first
        # Second call must not re-read the file; deleting it proves the cache.
        board.unlink()
        assert _pairwise_pad_geometry(router) is first

    def test_no_source_board_degrades_to_the_trace_only_scope(self, table):
        assert _pairwise_pad_geometry(_router(None, table, [])) == ()

    def test_unreadable_board_warns_on_stderr_instead_of_failing(self, tmp_path, table, capsys):
        broken = tmp_path / "broken.kicad_pcb"
        broken.write_text("(kicad_pcb (this is not a board")
        router = _router(broken, table, [])

        pads = _pairwise_pad_geometry(router)

        assert pads == ()
        assert "could not read pad geometry" in capsys.readouterr().err

    @pytest.mark.parametrize("origin", ORIGINS)
    def test_pads_are_in_the_board_files_own_sheet_absolute_frame(self, tmp_path, table, origin):
        """Same frame as ``board_trace_routes``' segments -- see #4507's replay."""
        ox, oy = origin
        board = _write(tmp_path, origin)

        pads = _pairwise_pad_geometry(_router(board, table, []))

        lv = [p for p in pads if p.net_name == "/LV_SENSE"]
        assert len(lv) == 1
        minx, miny, maxx, maxy = lv[0].polygon.bounds
        assert (minx + maxx) / 2.0 == pytest.approx(ox + 10.0)
        assert (miny + maxy) / 2.0 == pytest.approx(oy + 10.0)


class TestEndToEnd:
    """The gate's verdict, not just the helper's return value."""

    def test_preserved_copper_against_a_foreign_pad_fails_the_run(self, tmp_path, capsys):
        from kicad_tools.cli.route_cmd import main as route_main

        pcb = tmp_path / "pad_only.kicad_pcb"
        pcb.write_text(_pad_only_board(100.0, 100.0))
        vmap = tmp_path / "vmap.json"
        vmap.write_text(json.dumps({"/HV_LINE": HV_VOLTS, "/LV_SENSE": 0.0}))
        out = tmp_path / "out.kicad_pcb"

        rc = route_main(
            [
                str(pcb),
                "-o",
                str(out),
                "--route-engine",
                "lattice",
                "--strategy",
                "basic",
                "--skip-drc",
                "--preserve-existing",
                "--voltage-map",
                str(vmap),
                "--creepage-standard",
                "iec60664",
                "--pollution-degree",
                "2",
                "--material-group",
                "IIIa",
            ]
        )

        captured = capsys.readouterr().out
        assert rc != 0, f"pad-only HV shortfall passed the gate:\n{captured}"
        assert "HV pairwise clearance" in captured or "pairwise" in captured.lower()

    def test_same_board_without_a_voltage_map_is_a_strict_noop(self, tmp_path, capsys):
        from kicad_tools.cli.route_cmd import main as route_main

        pcb = tmp_path / "pad_only.kicad_pcb"
        pcb.write_text(_pad_only_board(100.0, 100.0))
        out = tmp_path / "out.kicad_pcb"

        rc = route_main(
            [
                str(pcb),
                "-o",
                str(out),
                "--route-engine",
                "lattice",
                "--strategy",
                "basic",
                "--skip-drc",
                "--preserve-existing",
            ]
        )

        assert rc == 0, capsys.readouterr().out
