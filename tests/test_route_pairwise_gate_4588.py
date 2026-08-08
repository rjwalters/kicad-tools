"""CLI-level tests for the post-route pairwise HV clearance gate (issue #4588).

Before this gate, ``rules.pairwise_clearance`` was consumed **only** by the
grid engines' route-acceptance predicates (``router/pathfinder.py``,
``router/cpp_backend.py``, and the C++ ``Grid3D`` halo).  ``--route-engine
lattice`` resolves clearance as a pure scalar
(``router/lattice/pathfinder.py``: ``max(net_class.clearance,
rules.trace_clearance)``), so a ``--voltage-map`` run on lattice committed HV
copper at DRU spacing, printed a reassuring ``HV pairwise clearance: N mapped
nets`` line, and exited 0 with ``SUCCESS`` -- a silent false pass of a safety
feature on a mains board.

The gate is a post-route, board-level audit of the copper the engine actually
committed (``router.routes``), so it is engine-agnostic.  These tests pin:

1. lattice + ``--voltage-map`` + planted HV/LV proximity the search cannot
   route around (pads pinned inside the requirement) -> non-zero exit, no
   ``SUCCESS`` banner.  Since issue #4602 the lattice search *avoids* HV<->LV
   proximity at route time, so the tight fixture now fails as an honest
   in-search decline (``pad-escape``) rather than as committed-then-gated
   copper -- either way, never a silent false pass;
2. the identical board **without** ``--voltage-map`` -> gate is a strict no-op
   (exit 0, ``SUCCESS``);
3. grid + ``--voltage-map`` on a board with no HV/LV proximity -> still exit 0
   (the gate is not a false-positive source on the production default);
4. a board whose only HV/LV proximity is inside a **rated domain-bridging
   footprint** (#4506) -> exit 0 at *any* board origin.  ``PCB.load`` reports
   footprint positions board-relative while ``router.routes`` are
   sheet-absolute, so an unshifted attach zone silently misses on every board
   that does not sit at sheet origin -- i.e. on every real board -- turning
   this gate into a hard false fail;
5. (issue #4602) a two-domain board where a compliant detour EXISTS -> the
   lattice search takes it and the run passes the gate with exit 0, instead
   of committing the naive proximate path and hard-failing.

Boards are fully synthetic S-expression strings (same approach as
``test_route_offboard_gate.py``) -- the softstart rev-C board from the original
report is local-only and must never be a CI dependency.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from kicad_tools.cli.route_cmd import main as route_main

# 300 V vs 0 V under IEC 60664-1 / PD2 / material group IIIa needs millimetres
# of creepage; the planted traces sit a few tenths of a mm apart, so the gate
# has an unambiguous finding while the scalar DRU (0.2 mm) is satisfied.
HV_VOLTS = 300.0


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


def _two_domain_board(lv_offset_mm: float) -> str:
    """Synthetic two-domain board: an HV pair and an LV pair running parallel.

    Both nets are simple two-pad point-to-point links spanning the board
    west-to-east.  ``lv_offset_mm`` is the north/south separation between the
    two corridors: a small value forces the routed copper into HV/LV proximity
    that satisfies the scalar DRU but violates the creepage requirement, while
    a large value keeps the corridors far apart (the clean control board).
    """
    hv_y = 105.0
    lv_y = hv_y + lv_offset_mm
    parts = [
        _fp("R1", 10, 103.0, hv_y, _pad("1", 0, 0, 1, "/HV_LINE")),
        _fp("R2", 11, 127.0, hv_y, _pad("1", 0, 0, 1, "/HV_LINE")),
        _fp("R3", 12, 103.0, lv_y, _pad("1", 0, 0, 2, "/LV_SENSE")),
        _fp("R4", 13, 127.0, lv_y, _pad("1", 0, 0, 2, "/LV_SENSE")),
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
  (gr_rect (start 100 100) (end 130 116)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
{"".join(parts)})
"""


def _bridging_board(origin_x: float, origin_y: float) -> str:
    """Board whose only HV/LV proximity lives inside a rated bridging footprint.

    ``R5`` is the canonical #4506 domain-bridging part: pad 1 on ``/HV_LINE``,
    pad 2 on ``/LV_SENSE``, 2 mm apart.  Each net's other terminal sits 9 mm
    away, so the two nets come within the creepage requirement *only* across
    R5's own body -- exactly what the attach-zone exemption exists to waive.
    The board rectangle starts at ``(origin_x, origin_y)``, which is what
    ``PCB._detect_board_origin`` picks up as the board origin.
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
{"".join(parts)})
"""


def _detour_board() -> str:
    """Two-domain board with a compliant detour available (issue #4602).

    The HV link is a short horizontal run in the board centre; the LV link is
    a vertical run whose NAIVE shortest path crosses straight through the HV
    corridor.  Every pad-to-pad gap is 10 mm -- well beyond the 3.2 mm derived
    requirement (300 V / IEC 60664-1 / PD2 / IIIa) -- so, unlike
    ``_two_domain_board(0.6)``, avoidance CAN produce compliant copper: a
    planar detour west/east of the HV corridor and a B.Cu under-crossing both
    exist with margin.

    Pre-#4602 behaviour (verified on ``main`` @ ``08776b3d``): the lattice
    search jogged around the HV pad at scalar-DRU spacing and the #4588
    post-route gate fired with 3 trace-to-trace findings (0.600 / 2.200 /
    3.000 mm against the 3.200 mm requirement), exit 3.  This fixture is the
    pass-oracle showing the search now avoids what the gate would report.
    """
    parts = [
        _fp("R1", 10, 112.0, 112.0, _pad("1", 0, 0, 1, "/HV_LINE")),
        _fp("R2", 11, 118.0, 112.0, _pad("1", 0, 0, 1, "/HV_LINE")),
        _fp("R3", 12, 112.0, 102.0, _pad("1", 0, 0, 2, "/LV_SENSE")),
        _fp("R4", 13, 112.0, 122.0, _pad("1", 0, 0, 2, "/LV_SENSE")),
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
  (gr_rect (start 100 100) (end 130 124)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
{"".join(parts)})
"""


def _preserved_violation_board() -> str:
    """Board whose *preserved* copper already violates the requirement (#4699).

    ``/HV_LINE`` and ``/LV_SENSE`` carry hand-placed ``(segment ...)`` copper
    0.6 mm apart (0.4 mm edge-to-edge, well inside the 3.2 mm requirement and
    well outside the 0.2 mm DRU floor) and have no pads at all, so the router
    never re-routes them -- with ``--preserve-existing`` that copper is
    re-emitted verbatim into the output.  ``/SIG_A`` is an ordinary two-pad
    link in the far corner that gives the run something to route.

    Pre-#4699 the post-route audit scanned ``router.routes`` only, so this
    board routed to a clean SUCCESS while the file it wrote carried a 0.4 mm
    gap at 300 V.
    """
    parts = [
        _fp("R1", 10, 103.0, 120.0, _pad("1", 0, 0, 3, "/SIG_A")),
        _fp("R2", 11, 127.0, 120.0, _pad("1", 0, 0, 3, "/SIG_A")),
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
  (gr_rect (start 100 100) (end 130 124)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
  (segment (start 105 105) (end 125 105) (width 0.2) (layer "F.Cu") (net 1))
  (segment (start 105 105.6) (end 125 105.6) (width 0.2) (layer "F.Cu") (net 2))
{"".join(parts)})
"""


def _strip_uuids(text: str) -> str:
    """Drop the freshly-generated per-element ``uuid`` lines from a board."""
    return re.sub(r'\(uuid "[^"]*"\)', "(uuid)", text)


def _write_voltage_map(tmp_path: Path) -> Path:
    vmap = tmp_path / "vmap.json"
    vmap.write_text(json.dumps({"/HV_LINE": HV_VOLTS, "/LV_SENSE": 0.0}))
    return vmap


def _route_args(pcb: Path, out: Path, engine: str, vmap: Path | None) -> list[str]:
    args = [
        str(pcb),
        "-o",
        str(out),
        "--route-engine",
        engine,
        "--strategy",
        "basic",
        # The gate is deliberately NOT a DRC sub-step: --skip-drc must not
        # disable it (and keeps these tests free of a kicad-cli dependency).
        "--skip-drc",
    ]
    if vmap is not None:
        args += [
            "--voltage-map",
            str(vmap),
            "--creepage-standard",
            "iec60664",
            "--pollution-degree",
            "2",
            "--material-group",
            "IIIa",
        ]
    return args


@pytest.fixture
def tight_board(tmp_path: Path) -> Path:
    """HV and LV corridors 0.6 mm apart -- DRU-legal, creepage-illegal."""
    pcb = tmp_path / "tight.kicad_pcb"
    pcb.write_text(_two_domain_board(0.6))
    return pcb


@pytest.fixture
def wide_board(tmp_path: Path) -> Path:
    """HV and LV corridors 10 mm apart -- clears creepage by a wide margin."""
    pcb = tmp_path / "wide.kicad_pcb"
    pcb.write_text(_two_domain_board(10.0))
    return pcb


class TestLatticeGate:
    """The engine that had zero pairwise awareness must no longer false-pass."""

    def test_lattice_with_voltage_map_declines_hv_proximity_in_search(
        self, tight_board: Path, tmp_path: Path, capsys
    ) -> None:
        """The tight fixture stays an honest FAILURE -- now declined in-search.

        ``_two_domain_board(0.6)`` pins the HV and LV *pads* 0.6 mm apart --
        inside the 3.2 mm derived requirement -- so no routing outcome can
        produce compliant copper here.  Pre-#4602 the search committed the
        proximate copper and the post-route gate fired; with search-time
        avoidance (#4602) the pairwise pad keep-outs block every escape stub
        and the connections decline honestly instead.  Either way: non-zero
        exit, no SUCCESS banner, never a silent false pass.
        """
        vmap = _write_voltage_map(tmp_path)
        out = tmp_path / "tight_routed.kicad_pcb"
        rc = route_main(_route_args(tight_board, out, "lattice", vmap))
        captured = capsys.readouterr().out

        assert rc != 0, f"expected a non-zero exit, got {rc}\n{captured}"
        # The SUCCESS banner must be unreachable in this state.
        assert "SUCCESS:" not in captured
        # The search now declines the geometry instead of committing it: the
        # lattice decline census names the pad-escape refusal (the pads sit
        # inside each other's pairwise keep-out).
        assert "decline[pad-escape" in captured
        # And the copper that WAS committed (if any) still passed the gate --
        # a gate hit here would mean avoidance leaked proximate copper.
        assert "pairwise HV clearance violations" not in captured

    def test_engagement_line_reports_route_time_enforcement_on_lattice(
        self, tight_board: Path, tmp_path: Path, capsys
    ) -> None:
        """The pre-route ``HV pairwise clearance:`` line must be engine-honest.

        The original #4588 defect was *reassurance*: the run printed
        ``(route-time creepage rejection active)`` on an engine whose search
        never read ``rules.pairwise_clearance``.  Since #4602 the lattice
        search DOES consume the table (per-pair predicate widening + HV pad
        keep-outs), so it now truthfully joins the grid branch.
        """
        vmap = _write_voltage_map(tmp_path)
        out = tmp_path / "engagement.kicad_pcb"
        route_main(_route_args(tight_board, out, "lattice", vmap))
        captured = capsys.readouterr().out

        assert "HV pairwise clearance:" in captured
        assert "route-time rejection + post-route audit" in captured
        assert "post-route audit only" not in captured

    def test_engagement_line_keeps_audit_only_wording_on_mesh(self, capsys) -> None:
        """Mesh is still pairwise-blind (#4602 scoped lattice only) and must
        keep the audit-only wording -- claiming route-time enforcement there
        would recreate the reassuring line that made the false pass silent.

        Exercised through ``_apply_pairwise_clearance`` directly (the helper
        that prints the line) rather than a full mesh routing run.
        """
        from types import SimpleNamespace

        from kicad_tools.cli.route_cmd import _apply_pairwise_clearance

        router = SimpleNamespace(
            rules=SimpleNamespace(trace_clearance=0.2, pairwise_clearance=None)
        )
        args = SimpleNamespace(
            _pairwise_required={("HV_LINE", "LV_SENSE"): 3.2},
            _pairwise_voltages={"HV_LINE": HV_VOLTS, "LV_SENSE": 0.0},
            route_engine="mesh",
        )
        _apply_pairwise_clearance(router, args)
        captured = capsys.readouterr().out

        assert "HV pairwise clearance:" in captured
        assert "post-route audit only -- the mesh search is pairwise-blind" in captured
        assert "route-time rejection" not in captured

    def test_engagement_line_still_reports_route_time_rejection_on_grid(
        self, wide_board: Path, tmp_path: Path, capsys
    ) -> None:
        """Grid genuinely rejects during search, so it must still say so."""
        vmap = _write_voltage_map(tmp_path)
        out = tmp_path / "engagement_grid.kicad_pcb"
        route_main(_route_args(wide_board, out, "grid", vmap))
        captured = capsys.readouterr().out

        assert "HV pairwise clearance:" in captured
        assert "route-time rejection + post-route audit" in captured

    def test_lattice_without_voltage_map_is_a_strict_noop(
        self, tight_board: Path, tmp_path: Path, capsys
    ) -> None:
        """Backward compatibility: no ``--voltage-map`` -> no gate, exit unchanged."""
        out = tmp_path / "tight_noflag.kicad_pcb"
        rc = route_main(_route_args(tight_board, out, "lattice", None))
        captured = capsys.readouterr().out

        assert rc == 0, captured
        assert "SUCCESS:" in captured
        assert "pairwise HV clearance violations" not in captured

    def test_copper_identical_when_no_hv_proximity_is_at_stake(
        self, wide_board: Path, tmp_path: Path
    ) -> None:
        """Same copper with and without the flag when nothing needs avoiding.

        Pre-#4602 this invariant held on the TIGHT board too (the gate was a
        pure read-only audit); with search-time avoidance the flag now
        legitimately CHANGES the copper wherever HV<->LV proximity must be
        avoided -- that is the whole point.  The invariant that must survive
        is dormancy: on a board whose corridors already clear the requirement
        by a wide margin, the pairwise-aware search takes the identical routes,
        so the ``--voltage-map`` run's copper is byte-identical to the plain
        run's.  Per-element ``uuid`` values are freshly generated on every
        write and are compared out.
        """
        vmap = _write_voltage_map(tmp_path)
        gated = tmp_path / "gated.kicad_pcb"
        ungated = tmp_path / "ungated.kicad_pcb"
        route_main(_route_args(wide_board, gated, "lattice", vmap))
        route_main(_route_args(wide_board, ungated, "lattice", None))
        assert _strip_uuids(gated.read_text()) == _strip_uuids(ungated.read_text())


class TestLatticeAvoidance:
    """Issue #4602 pass-oracle: the lattice search avoids what the gate reports.

    On ``_detour_board()`` every pad-to-pad gap (10 mm) exceeds the 3.2 mm
    derived requirement, but the LV net's naive shortest path runs straight
    through the HV corridor.  Pre-#4602 (verified on ``main`` @ ``08776b3d``)
    the lattice committed that path and the #4588 gate hard-failed the run
    (exit 3, findings at 0.600 / 2.200 / 3.000 mm vs required 3.200 mm).  With
    search-time avoidance the LV route detours (or under-crosses on B.Cu) and
    the run passes the very same gate.
    """

    def test_detour_board_routes_clean_with_voltage_map(self, tmp_path: Path, capsys) -> None:
        pcb = tmp_path / "detour.kicad_pcb"
        pcb.write_text(_detour_board())
        vmap = _write_voltage_map(tmp_path)
        out = tmp_path / "detour_routed.kicad_pcb"

        rc = route_main(_route_args(pcb, out, "lattice", vmap))
        captured = capsys.readouterr().out

        assert rc == 0, f"expected exit 0, got {rc}\n{captured}"
        assert "SUCCESS:" in captured
        assert "Nets routed:     2/2" in captured
        # Zero findings from the #4588 post-route gate -- the pass oracle.
        assert "pairwise HV clearance violations" not in captured
        assert "ROUTING FAILED" not in captured
        # The run banner reports route-time enforcement (#4602).
        assert "route-time rejection + post-route audit" in captured

    def test_detour_board_without_voltage_map_takes_the_naive_path(
        self, tmp_path: Path, capsys
    ) -> None:
        """Dormancy control: without the flag the board still routes (exit 0)
        and no pairwise machinery engages -- the detour is a consequence of
        the voltage map, not a general routing behaviour change."""
        pcb = tmp_path / "detour_plain.kicad_pcb"
        pcb.write_text(_detour_board())
        out = tmp_path / "detour_plain_routed.kicad_pcb"

        rc = route_main(_route_args(pcb, out, "lattice", None))
        captured = capsys.readouterr().out

        assert rc == 0, captured
        assert "SUCCESS:" in captured
        assert "HV pairwise clearance:" not in captured


class TestNonEscalationPath:
    """``--no-auto-layers`` takes ``main()``'s own post-DRC block, not a wrapper.

    ``--auto-layers`` defaults to True, so the escalation wrappers own the
    default flow; ``main()``'s inline summary/exit-code block is only reached
    with ``--no-auto-layers``.  Both terminal paths must gate.
    """

    def test_inline_path_fails_on_hv_proximity(
        self, tight_board: Path, tmp_path: Path, capsys
    ) -> None:
        """Both terminal paths stay honest failures on the tight fixture.

        Since #4602 the lattice search declines the geometry in-search (the
        pads sit inside each other's pairwise keep-out), so the inline path
        exits non-zero on incomplete routing rather than on the gate banner.
        """
        vmap = _write_voltage_map(tmp_path)
        out = tmp_path / "inline_routed.kicad_pcb"
        rc = route_main(
            _route_args(tight_board, out, "lattice", vmap) + ["--no-auto-layers", "--layers", "2"]
        )
        captured = capsys.readouterr().out

        assert rc != 0, f"expected a non-zero exit, got {rc}\n{captured}"
        assert "decline[pad-escape" in captured
        assert "SUCCESS:" not in captured

    def test_inline_path_without_voltage_map_is_a_strict_noop(
        self, tight_board: Path, tmp_path: Path, capsys
    ) -> None:
        out = tmp_path / "inline_noflag.kicad_pcb"
        rc = route_main(
            _route_args(tight_board, out, "lattice", None) + ["--no-auto-layers", "--layers", "2"]
        )
        captured = capsys.readouterr().out

        assert rc == 0, captured
        assert "SUCCESS:" in captured
        assert "pairwise HV clearance violations" not in captured


class TestGridUnaffected:
    """The production default must not acquire a new false-positive source."""

    def test_grid_with_voltage_map_on_a_clean_board_still_exits_zero(
        self, wide_board: Path, tmp_path: Path, capsys
    ) -> None:
        vmap = _write_voltage_map(tmp_path)
        out = tmp_path / "wide_routed.kicad_pcb"
        rc = route_main(_route_args(wide_board, out, "grid", vmap))
        captured = capsys.readouterr().out

        assert rc == 0, captured
        assert "SUCCESS:" in captured
        assert "pairwise HV clearance violations" not in captured


class TestAttachZoneExemptionCoordinateSpace:
    """The #4506 exemption must survive a non-zero board origin (PR #4603).

    Every pre-existing attach-zone test builds ``AttachZone`` literals in the
    same space as its synthetic ``Route``s, so none of them *can* observe a
    coordinate-space mismatch.  These tests vary only where the board sits on
    the sheet: ``(0, 0)`` used to pass and every real origin used to fail with
    a hard ``ROUTING FAILED``, because ``PCB.load`` hands back board-relative
    footprint positions while ``router.routes`` are sheet-absolute.
    """

    # (0, 0) is the only origin the pre-fix code handled; 100/100 is the
    # Judge's reproduction; 136/77.5 is boards/00-simple-led's real origin.
    ORIGINS = [(0.0, 0.0), (100.0, 100.0), (136.0, 77.5)]

    @pytest.mark.parametrize("origin", ORIGINS)
    def test_rated_bridging_footprint_is_exempt_at_any_board_origin(
        self, origin: tuple[float, float], tmp_path: Path, capsys
    ) -> None:
        ox, oy = origin
        pcb = tmp_path / f"bridge_{ox}_{oy}.kicad_pcb"
        pcb.write_text(_bridging_board(ox, oy))
        vmap = _write_voltage_map(tmp_path)
        out = tmp_path / f"bridge_{ox}_{oy}_routed.kicad_pcb"

        rc = route_main(_route_args(pcb, out, "lattice", vmap))
        captured = capsys.readouterr().out

        assert rc == 0, f"origin {origin} exited {rc}\n{captured}"
        assert "SUCCESS:" in captured
        assert "ROUTING FAILED" not in captured
        assert "pairwise HV clearance violations" not in captured

    @pytest.mark.parametrize("origin", ORIGINS)
    def test_grid_engine_shares_the_corrected_zones(
        self, origin: tuple[float, float], tmp_path: Path, capsys
    ) -> None:
        """``_apply_pairwise_clearance`` feeds the pathfinder the same resolver.

        The mis-spaced zones also blinded the grid engine's *route-time*
        rejection, which walled the bridging footprint off and dropped the
        board to 1/2 nets.  One shared helper repairs both paths.
        """
        ox, oy = origin
        pcb = tmp_path / f"bridge_grid_{ox}_{oy}.kicad_pcb"
        pcb.write_text(_bridging_board(ox, oy))
        vmap = _write_voltage_map(tmp_path)
        out = tmp_path / f"bridge_grid_{ox}_{oy}_routed.kicad_pcb"

        rc = route_main(_route_args(pcb, out, "grid", vmap))
        captured = capsys.readouterr().out

        assert rc == 0, f"origin {origin} exited {rc}\n{captured}"
        assert "Routed: 2/2 nets" in captured

    def test_zones_are_returned_in_sheet_absolute_millimetres(self, tmp_path: Path) -> None:
        """Pin the space directly: zones must be offset by ``pcb.board_origin``."""
        from types import SimpleNamespace

        from kicad_tools.cli.route_cmd import _pairwise_attach_zones

        origin = (100.0, 100.0)
        pcb = tmp_path / "space.kicad_pcb"
        pcb.write_text(_bridging_board(*origin))

        zones = _pairwise_attach_zones(SimpleNamespace(_pairwise_attach_zone_pcb_path=str(pcb)))

        assert len(zones) == 1, f"expected only the 2-net bridging footprint, got {zones}"
        zone = zones[0]
        assert zone.net_names == frozenset({"HV_LINE", "LV_SENSE"})
        # R5 sits 15 mm / 5 mm into a board whose origin is (100, 100), so the
        # zone must straddle the sheet-absolute point (115, 105).
        assert zone.min_x < 115.0 < zone.max_x, zone
        assert zone.min_y < 105.0 < zone.max_y, zone
        assert zone.exempts(115.0, 105.0, "/HV_LINE", "/LV_SENSE")

    def test_unreadable_board_warns_on_stderr_instead_of_failing_silently(
        self, tmp_path: Path, capsys
    ) -> None:
        """An empty tuple must never be mistaken for "no exemptions apply"."""
        from types import SimpleNamespace

        from kicad_tools.cli.route_cmd import _pairwise_attach_zones

        broken = tmp_path / "broken.kicad_pcb"
        broken.write_text("(kicad_pcb (this is not a board")

        zones = _pairwise_attach_zones(SimpleNamespace(_pairwise_attach_zone_pcb_path=str(broken)))

        assert zones == ()
        err = capsys.readouterr().err
        assert "rated attach zones" in err
        assert "broken.kicad_pcb" in err


class TestPreservedCopperIsInScope:
    """Issue #4699: ``--preserve-existing`` copper is on the board, so audit it.

    The #4588 gate scanned ``router.routes``.  Preserved copper is re-emitted
    from a *separate* list, so a trace-to-trace shortfall involving it could
    never be reported -- the run printed SUCCESS over a file it had just
    written unsafe copper into.
    """

    def test_preserved_violation_fails_the_run_end_to_end(self, tmp_path: Path, capsys) -> None:
        pcb = tmp_path / "preserved.kicad_pcb"
        pcb.write_text(_preserved_violation_board())
        vmap = _write_voltage_map(tmp_path)
        out = tmp_path / "preserved_routed.kicad_pcb"

        rc = route_main(
            _route_args(pcb, out, "lattice", vmap) + ["--preserve-existing"],
        )
        captured = capsys.readouterr().out

        assert rc != 0, f"expected a non-zero exit, got {rc}\n{captured}"
        assert "SUCCESS:" not in captured
        assert "pairwise HV clearance violations" in captured
        assert "/HV_LINE vs /LV_SENSE" in captured or "/LV_SENSE vs /HV_LINE" in captured
        # The banner explains the preserved-copper provenance (#4699).
        assert "preserved" in captured

    def test_same_board_without_voltage_map_is_still_a_strict_noop(
        self, tmp_path: Path, capsys
    ) -> None:
        """Dormancy: the audit widening never engages without ``--voltage-map``."""
        pcb = tmp_path / "preserved_noflag.kicad_pcb"
        pcb.write_text(_preserved_violation_board())
        out = tmp_path / "preserved_noflag_routed.kicad_pcb"

        rc = route_main(
            _route_args(pcb, out, "lattice", None) + ["--preserve-existing"],
        )
        captured = capsys.readouterr().out

        assert rc == 0, captured
        assert "SUCCESS:" in captured
        assert "pairwise HV clearance violations" not in captured

    def test_run_without_preserve_existing_does_not_audit_dropped_copper(
        self, tmp_path: Path, capsys
    ) -> None:
        """Without ``--preserve-existing`` that copper is stripped, not written.

        Auditing it anyway would fail a run over geometry the output does not
        contain -- the mirror-image false signal of the bug being fixed.
        """
        pcb = tmp_path / "stripped.kicad_pcb"
        pcb.write_text(_preserved_violation_board())
        vmap = _write_voltage_map(tmp_path)
        out = tmp_path / "stripped_routed.kicad_pcb"

        rc = route_main(_route_args(pcb, out, "lattice", vmap))
        captured = capsys.readouterr().out

        assert rc == 0, captured
        assert "SUCCESS:" in captured
        assert "pairwise HV clearance violations" not in captured
        # The violating copper really is absent from the written board.
        assert "(net 1)" not in out.read_text()


class TestAuditHelper:
    """Unit-level coverage of the gate helper's edge cases (no routing)."""

    def test_no_voltage_map_short_circuits_before_touching_the_router(self) -> None:
        from types import SimpleNamespace

        from kicad_tools.cli.route_cmd import _audit_pairwise_clearance

        args = SimpleNamespace(_pairwise_required=None)

        class _Exploding:
            """Any attribute access would mean the no-op path did real work."""

            def __getattr__(self, name):  # pragma: no cover - must never run
                raise AssertionError(f"gate touched router.{name} without --voltage-map")

        assert _audit_pairwise_clearance(_Exploding(), args) == []

    def test_absent_table_is_a_noop(self) -> None:
        from types import SimpleNamespace

        from kicad_tools.cli.route_cmd import _audit_pairwise_clearance

        args = SimpleNamespace(_pairwise_required={})
        router = SimpleNamespace(rules=SimpleNamespace(pairwise_clearance=None), routes=[])
        assert _audit_pairwise_clearance(router, args) == []

    def test_empty_routes_are_a_noop(self) -> None:
        from types import SimpleNamespace

        from kicad_tools.cli.route_cmd import _audit_pairwise_clearance
        from kicad_tools.router.pairwise_clearance import build_pairwise_clearance_table

        table = build_pairwise_clearance_table({"/HV": HV_VOLTS, "/LV": 0.0}, dru=0.2)
        args = SimpleNamespace(_pairwise_required={})
        router = SimpleNamespace(rules=SimpleNamespace(pairwise_clearance=table), routes=[])
        assert _audit_pairwise_clearance(router, args) == []

    def test_net_absent_from_the_voltage_map_resolves_to_the_dru_floor(self) -> None:
        """An unmapped net must not crash the scan.

        ``PairwiseClearanceTable.required_clearance`` returns the DRU floor for
        any pair absent from ``required_by_pair`` (the documented contract the
        route-acceptance validators already rely on), so an unmapped net yields
        no HV widening and therefore no finding.  The gate inherits that
        semantics verbatim rather than inventing its own.
        """
        from types import SimpleNamespace

        from kicad_tools.cli.route_cmd import _audit_pairwise_clearance
        from kicad_tools.router.layers import Layer
        from kicad_tools.router.pairwise_clearance import build_pairwise_clearance_table
        from kicad_tools.router.primitives import Route, Segment

        table = build_pairwise_clearance_table({"/HV": HV_VOLTS}, dru=0.2)
        routes = [
            Route(
                net=1,
                net_name="/HV",
                segments=[Segment(0, 0, 5, 0, 0.2, Layer.F_CU, net=1, net_name="/HV")],
            ),
            Route(
                net=2,
                net_name="/UNMAPPED",
                segments=[Segment(0, 0.4, 5, 0.4, 0.2, Layer.F_CU, net=2, net_name="/UNMAPPED")],
            ),
        ]
        args = SimpleNamespace(_pairwise_required={})
        router = SimpleNamespace(
            rules=SimpleNamespace(pairwise_clearance=table),
            routes=routes,
            _pairwise_attach_zone_pcb_path=None,
        )
        assert _audit_pairwise_clearance(router, args) == []

    def test_zero_hv_classified_nets_is_a_noop(self) -> None:
        """A voltage map with no net above the HV threshold produces no findings."""
        from types import SimpleNamespace

        from kicad_tools.cli.route_cmd import _audit_pairwise_clearance
        from kicad_tools.router.layers import Layer
        from kicad_tools.router.pairwise_clearance import build_pairwise_clearance_table
        from kicad_tools.router.primitives import Route, Segment

        table = build_pairwise_clearance_table({"/A": 3.3, "/B": 0.0}, dru=0.2)
        routes = [
            Route(
                net=1,
                net_name="/A",
                segments=[Segment(0, 0, 5, 0, 0.2, Layer.F_CU, net=1, net_name="/A")],
            ),
            Route(
                net=2,
                net_name="/B",
                segments=[Segment(0, 0.4, 5, 0.4, 0.2, Layer.F_CU, net=2, net_name="/B")],
            ),
        ]
        args = SimpleNamespace(_pairwise_required={})
        router = SimpleNamespace(
            rules=SimpleNamespace(pairwise_clearance=table),
            routes=routes,
            _pairwise_attach_zone_pcb_path=None,
        )
        assert _audit_pairwise_clearance(router, args) == []

    def test_preserved_copper_is_audited_alongside_fresh_copper(self) -> None:
        """Issue #4699: a fresh-vs-preserved pair must be reportable at all.

        The gate scanned ``router.routes`` only, so under ``--preserve-existing``
        (implied by ``--complete``) a trace-to-trace shortfall against copper
        loaded from the input board was *structurally* unreportable -- four of
        the nine violations in the #4699 report were in that class, and the
        audit dutifully found zero.
        """
        from types import SimpleNamespace

        from kicad_tools.cli.route_cmd import _audit_pairwise_clearance
        from kicad_tools.router.layers import Layer
        from kicad_tools.router.pairwise_clearance import build_pairwise_clearance_table
        from kicad_tools.router.primitives import Route, Segment

        table = build_pairwise_clearance_table({"/HV": HV_VOLTS, "/LV": 0.0}, dru=0.2)
        fresh = Route(
            net=1,
            net_name="/HV",
            segments=[Segment(0, 0, 5, 0, 0.2, Layer.F_CU, net=1, net_name="/HV")],
        )
        preserved = Route(
            net=2,
            net_name="/LV",
            segments=[Segment(0, 0.5, 5, 0.5, 0.2, Layer.F_CU, net=2, net_name="/LV")],
        )
        args = SimpleNamespace(_pairwise_required={("HV", "LV"): 3.2})
        router = SimpleNamespace(
            rules=SimpleNamespace(pairwise_clearance=table),
            routes=[fresh],
            existing_routes=[preserved],
            _pairwise_attach_zone_pcb_path=None,
        )

        violations = _audit_pairwise_clearance(router, args)

        assert len(violations) == 1, violations
        assert {violations[0].net_a, violations[0].net_b} == {"/HV", "/LV"}
        # Edge-to-edge gap is 0.3 mm (0.5 centre - two 0.1 half-widths).
        assert violations[0].actual_mm == pytest.approx(0.3)

    def test_preserved_vs_preserved_violations_are_reported_too(self) -> None:
        """A pre-existing input-board defect still makes the OUTPUT unsafe.

        Documented decision (#4699): the gate's contract is that a SUCCESS
        banner means the *written* board is clean.  A shortfall between two
        preserved routes was inherited rather than created by this run, but the
        board is no safer for it, so it fails the run and the banner says where
        it came from.
        """
        from types import SimpleNamespace

        from kicad_tools.cli.route_cmd import _audit_pairwise_clearance
        from kicad_tools.router.layers import Layer
        from kicad_tools.router.pairwise_clearance import build_pairwise_clearance_table
        from kicad_tools.router.primitives import Route, Segment

        table = build_pairwise_clearance_table({"/HV": HV_VOLTS, "/LV": 0.0}, dru=0.2)
        preserved = [
            Route(
                net=1,
                net_name="/HV",
                segments=[Segment(0, 0, 5, 0, 0.2, Layer.F_CU, net=1, net_name="/HV")],
            ),
            Route(
                net=2,
                net_name="/LV",
                segments=[Segment(0, 0.5, 5, 0.5, 0.2, Layer.F_CU, net=2, net_name="/LV")],
            ),
        ]
        args = SimpleNamespace(_pairwise_required={("HV", "LV"): 3.2})
        router = SimpleNamespace(
            rules=SimpleNamespace(pairwise_clearance=table),
            routes=[],
            _emitted_preserved_routes=preserved,
            _pairwise_attach_zone_pcb_path=None,
        )

        assert len(_audit_pairwise_clearance(router, args)) == 1

    def test_preserved_copper_that_was_rerouted_is_not_double_audited(self) -> None:
        """Only the preserved routes the output actually carries are scanned.

        ``_finalize_routes`` drops a preserved route whose net was re-routed
        (the fresh copper wins).  Auditing the dropped geometry would invent
        violations against copper that is not on the board.
        """
        from types import SimpleNamespace

        from kicad_tools.cli.route_cmd import _audited_trace_copper
        from kicad_tools.router.layers import Layer
        from kicad_tools.router.primitives import Route, Segment

        fresh = Route(
            net=1,
            net_name="/HV",
            segments=[Segment(0, 0, 5, 0, 0.2, Layer.F_CU, net=1, net_name="/HV")],
        )
        stale = Route(
            net=1,
            net_name="/HV",
            segments=[Segment(0, 9, 5, 9, 0.2, Layer.F_CU, net=1, net_name="/HV")],
        )
        router = SimpleNamespace(routes=[fresh], existing_routes=[stale])

        assert _audited_trace_copper(router) == [fresh]

    def test_preserved_routes_are_rekeyed_by_name_not_by_stale_id(self) -> None:
        """Name-vs-id disagreement must not silently mis-key the audit.

        ``find_pairwise_violations`` prefers ``id_to_name`` over a route's own
        ``net_name``; a preserved route carrying a stale id would then be
        audited under a *different* net's voltage -- silently, and in the
        permissive direction.
        """
        from types import SimpleNamespace

        from kicad_tools.cli.route_cmd import _audited_trace_copper
        from kicad_tools.router.layers import Layer
        from kicad_tools.router.primitives import Route, Segment

        preserved = Route(
            net=77,  # stale numbering from a previous board revision
            net_name="/LV",
            segments=[Segment(0, 0.5, 5, 0.5, 0.2, Layer.F_CU, net=77, net_name="/LV")],
        )
        router = SimpleNamespace(routes=[], existing_routes=[preserved])

        (audited,) = _audited_trace_copper(router, id_to_name={1: "/HV", 2: "/LV"})

        assert audited.net == 2
        assert audited.net_name == "/LV"

    def test_formatter_shape_and_more_tail(self) -> None:
        from kicad_tools.cli.route_cmd import _format_pairwise_violations
        from kicad_tools.router.pairwise_clearance import PairwiseViolation

        violations = [
            PairwiseViolation(net_a="/A", net_b="/B", actual_mm=0.4, required_mm=3.2, x=1.0, y=2.0)
            for _ in range(12)
        ]
        text = _format_pairwise_violations(violations, limit=10)
        assert "/A vs /B: 0.400mm (required 3.200mm)" in text
        assert "... and 2 more" in text
        assert len(text.splitlines()) == 11
