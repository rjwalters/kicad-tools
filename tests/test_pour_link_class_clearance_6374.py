"""Pour-link completion keeps net-class clearance, not ``--clearance`` (Issue #6374).

Board 04 regression: ``PourLinkCloser`` routed a GND link 0.150 mm from
OSC_IN at ``--clearance 0.15`` although both nets sit in 0.2 mm classes.
New pour copper on net P must keep ``c(P, N) = max(--clearance,
class(P).clearance, class(N).clearance)`` from copper on net N -- the grid
router's ``io._get_pair_clearance`` rule -- on both the link path
(``route_link``) and the weld path (``run_stitch``).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

shapely = pytest.importorskip("shapely")
from shapely.geometry import LineString  # noqa: E402

from kicad_tools.router.oracle_completion import (  # noqa: E402
    ClosureAttempt,
    OracleEndpoint,
    OracleLink,
    PourLinkCloser,
    run_oracle_completion,
)
from kicad_tools.router.rules import NetClassRouting  # noqa: E402
from kicad_tools.schema.pcb import PCB  # noqa: E402

# Straight GND corridor: R1.1 (10, 10) -> R2.1 (14, 10), 0.2 mm track, so the
# corridor's copper edges sit at y = 9.9 and y = 10.1.
EDGE_TOP, EDGE_BOTTOM = 10.1, 9.9


def _fp(ref: str, x: float, y: float, net_num: int, net: str, w: float, h: float) -> str:
    return (
        f'  (footprint "T:{ref}" (layer "F.Cu")\n'
        f"    (at {x:g} {y:g})\n"
        f'    (property "Reference" "{ref}" (at 0 0) (layer "F.SilkS"))\n'
        f'    (pad "1" smd rect (at 0 0) (size {w:g} {h:g}) (layers "F.Cu") '
        f'(net {net_num} "{net}"))\n'
        f"  )\n"
    )


def _box(ref: str, net_num: int, net: str, x0: float, y0: float, x1: float, y1: float) -> str:
    return _fp(ref, (x0 + x1) / 2, (y0 + y1) / 2, net_num, net, x1 - x0, y1 - y0)


def _board(tmp_path: Path, obstacles: str) -> Path:
    """One-copper-layer board: two GND pads to join, plus ``obstacles``."""
    text = (
        '(kicad_pcb (version 20240108) (generator "pcbnew")\n'
        '  (layers (0 "F.Cu" signal) (44 "Edge.Cuts" user))\n'
        '  (net 0 "")\n  (net 1 "GND")\n  (net 2 "OSC_IN")\n  (net 3 "NC3")\n'
        + _fp("R1", 10, 10, 1, "GND", 0.8, 0.8)
        + _fp("R2", 14, 10, 1, "GND", 0.8, 0.8)
        + obstacles
        + '  (gr_rect (start 0 0) (end 30 30) (layer "Edge.Cuts") (width 0.1))\n)\n'
    )
    path = tmp_path / "b.kicad_pcb"
    path.write_text(text)
    return path


def _link() -> OracleLink:
    def end(ref: str, x: float) -> OracleEndpoint:
        return OracleEndpoint(
            description=f"Pad 1 [GND] of {ref} on F.Cu",
            kind="pad",
            net="GND",
            x=x,
            y=10.0,
            layer="F.Cu",
            ref=ref,
            pad="1",
        )

    return OracleLink(net="GND", a=end("R1", 10.0), b=end("R2", 14.0))


def _closer(clearance: float, classes: dict[str, float] | None) -> PourLinkCloser:
    ncm = (
        None
        if classes is None
        else {net: NetClassRouting(name=f"C{c}", clearance=c) for net, c in classes.items()}
    )
    return PourLinkCloser(
        via_size=0.6, via_drill=0.3, clearance=clearance, trace_width=0.2, net_class_map=ncm
    )


def _gap(path: Path, net_name: str) -> float:
    """Smallest copper-edge gap between the board's GND tracks and ``net_name`` pads."""
    pcb = PCB.load(str(path))
    from kicad_tools.router.link_router import _build_model

    model = _build_model(pcb)
    ids = {n.name: num for num, n in pcb.nets.items()}
    targets = [it.layers["F.Cu"] for it in model.items if it.net == ids[net_name]]
    gaps = [
        LineString([s.start, s.end]).buffer(s.width / 2).distance(t)
        for s in pcb.segments
        if s.net_number == ids["GND"]
        for t in targets
    ]
    assert gaps, "no GND copper was written"
    return min(gaps)


# OSC_IN pad whose edge is 0.17 mm off the straight corridor: legal at 0.15,
# illegal at a 0.2 mm class clearance.
OSC_NEAR = _box("Y1", 2, "OSC_IN", 11.5, EDGE_TOP + 0.17, 12.5, 11.0)
# Walls that leave ONLY a 0.17 mm-per-side corridor (no way around in the window).
OSC_WALL = _box("Y1", 2, "OSC_IN", 10.6, EDGE_TOP + 0.17, 13.4, 16.0)
NC3_WALL = _box("Y2", 3, "NC3", 10.6, 4.0, 13.4, EDGE_BOTTOM - 0.17)


class TestClosedLinkKeepsClassClearance:
    def test_without_a_class_map_the_link_may_pass_at_the_floor(self, tmp_path):
        board = _board(tmp_path, OSC_NEAR)
        attempt = ClosureAttempt()
        _closer(0.15, None)._route_links(board, [_link()], attempt)
        assert attempt.applied == 1
        assert 0.15 - 1e-6 <= _gap(board, "OSC_IN") < 0.2  # today's behaviour

    def test_pour_class_clearance_is_kept(self, tmp_path):
        board = _board(tmp_path, OSC_NEAR)
        attempt = ClosureAttempt()
        _closer(0.15, {"GND": 0.2})._route_links(board, [_link()], attempt)
        assert attempt.applied == 1
        assert _gap(board, "OSC_IN") >= 0.2 - 1e-6

    def test_only_a_floor_corridor_leaves_the_link_open(self, tmp_path):
        board = _board(tmp_path, OSC_WALL + NC3_WALL)
        before = board.read_bytes()
        attempt = ClosureAttempt()
        _closer(0.15, {"GND": 0.2})._route_links(board, [_link()], attempt)
        assert attempt.applied == 0
        assert any("no clearance-legal route" in n for n in attempt.notes)
        assert not attempt.relaxed
        assert board.read_bytes() == before

    def test_the_same_corridor_closes_at_the_floor_without_classes(self, tmp_path):
        board = _board(tmp_path, OSC_WALL + NC3_WALL)
        attempt = ClosureAttempt()
        _closer(0.15, None)._route_links(board, [_link()], attempt)
        assert attempt.applied == 1


class TestNeighbourTerm:
    def test_neighbour_class_kept_and_classless_net_gets_the_pour_class(self, tmp_path):
        # GND in a 0.15 class, OSC_IN in a 0.2 class, NC3 classless; floor 0.1.
        # The straight corridor passes OSC_IN at 0.21 and NC3 at 0.17.
        osc = _box("Y1", 2, "OSC_IN", 11.5, EDGE_TOP + 0.21, 12.5, 11.0)
        board = _board(tmp_path, osc + NC3_WALL)
        attempt = ClosureAttempt()
        _closer(0.1, {"GND": 0.15, "OSC_IN": 0.2})._route_links(board, [_link()], attempt)
        assert attempt.applied == 1
        assert _gap(board, "OSC_IN") >= 0.2 - 1e-6
        assert 0.15 - 1e-6 <= _gap(board, "NC3") < 0.2

    def test_neighbour_class_above_the_pour_class_blocks_a_tight_pass(self, tmp_path):
        # Same corridor, OSC_IN now 0.17 off: legal for GND's 0.15 class, not
        # for OSC_IN's 0.2 class -- and NC3 walls the other side.
        board = _board(tmp_path, OSC_WALL + NC3_WALL)
        attempt = ClosureAttempt()
        _closer(0.1, {"GND": 0.15, "OSC_IN": 0.2})._route_links(board, [_link()], attempt)
        assert attempt.applied == 0
        assert any("no clearance-legal route" in n for n in attempt.notes)

    def test_pair_clearance_rule(self):
        closer = _closer(0.15, {"GND": 0.2, "OSC_IN": 0.25, "SIG": 0.1})
        assert closer.pair_clearance("GND", "OSC_IN") == 0.25
        assert closer.pair_clearance("GND", "SIG") == 0.2
        assert closer.pair_clearance("GND", "UNCLASSED") == 0.2
        assert closer.pair_clearance("VCC", "SIG") == 0.15  # floor wins
        assert _closer(0.15, None).pair_clearance("GND", "OSC_IN") == 0.15


class TestWeldPath:
    @pytest.fixture
    def stitch_calls(self, monkeypatch):
        from kicad_tools.cli import stitch_cmd

        calls: list[dict] = []

        def fake_run_stitch(pcb_path, nets, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                vias_added=[], already_connected=0, pads_skipped=[], traces_added=[]
            )

        monkeypatch.setattr(stitch_cmd, "run_stitch", fake_run_stitch)
        return calls

    def test_weld_uses_the_pour_class_clearance(self, tmp_path, stitch_calls):
        board = _board(tmp_path, "")
        _closer(0.15, {"GND": 0.2})._weld_pads(board, {"GND": {"R1.1"}}, ClosureAttempt())
        assert stitch_calls and all(c["clearance"] >= 0.2 for c in stitch_calls)

    def test_weld_keeps_a_stricter_neighbour_class(self, tmp_path, stitch_calls):
        board = _board(tmp_path, OSC_NEAR)
        _closer(0.15, {"GND": 0.2, "OSC_IN": 0.25})._weld_pads(
            board, {"GND": {"R1.1"}}, ClosureAttempt()
        )
        assert stitch_calls[0]["clearance"] == pytest.approx(0.25)

    def test_a_distant_stricter_class_does_not_tighten_the_weld(self, tmp_path, stitch_calls):
        far = _box("Y1", 2, "OSC_IN", 25.0, 25.0, 26.0, 26.0)
        board = _board(tmp_path, far)
        _closer(0.15, {"GND": 0.2, "OSC_IN": 0.5})._weld_pads(
            board, {"GND": {"R1.1"}}, ClosureAttempt()
        )
        assert stitch_calls[0]["clearance"] == pytest.approx(0.2)

    def test_without_a_class_map_the_weld_keeps_the_scalar(self, tmp_path, stitch_calls):
        board = _board(tmp_path, OSC_NEAR)
        _closer(0.15, None)._weld_pads(board, {"GND": {"R1.1"}}, ClosureAttempt())
        assert stitch_calls[0]["clearance"] == 0.15


class TestNoSilentRelaxation:
    def test_relaxation_is_recorded_in_notes_and_relaxed(self):
        attempt = ClosureAttempt()
        PourLinkCloser.record_relaxation(attempt, "GND R1.1", 0.127, 0.2, "declared-rule retry")
        assert attempt.relaxed == attempt.notes[-1:]
        assert "closed at 0.127 mm < class clearance 0.2 mm" in attempt.relaxed[0]

    def test_kept_round_relaxations_reach_the_result(self, tmp_path):
        from kicad_tools.drc.geometric import GeometricDRCResult
        from kicad_tools.drc.violation import DRCViolation, Location, Severity, ViolationType

        board = tmp_path / "b.kicad_pcb"
        board.write_text("1")

        def oracle(_path):
            n = int(board.read_text())
            return GeometricDRCResult(
                ran=True,
                error_count=0,
                unconnected_items=[
                    DRCViolation(
                        type=ViolationType.UNCONNECTED_ITEMS,
                        type_str="unconnected_items",
                        severity=Severity.ERROR,
                        message="Missing connection between items",
                        locations=[Location(1.0, 1.0), Location(2.0, 1.0)],
                        items=["Pad 1 [GND] of C1 on F.Cu", "Zone [GND] on In1.Cu"],
                    )
                ]
                * n,
            )

        def closer(path, links, banned):
            path.write_text("0")
            attempt = ClosureAttempt(applied=1)
            PourLinkCloser.record_relaxation(attempt, "GND C1.1", 0.127, 0.2, "retry")
            return attempt

        res = run_oracle_completion(board, oracle=oracle, closer=closer, nets={"GND"})
        assert res.converged
        assert len(res.relaxed_links) == 1 and "0.127" in res.relaxed_links[0]

    def test_class_aware_closer_records_no_relaxation(self, tmp_path):
        board = _board(tmp_path, OSC_NEAR)
        attempt = ClosureAttempt()
        _closer(0.15, {"GND": 0.2})._route_links(board, [_link()], attempt)
        assert attempt.applied == 1 and attempt.relaxed == []


class TestRouteStageWiring:
    """``kct route`` hands the router's effective net-class map to the closer."""

    def test_effective_map_merges_loaded_over_router(self):
        from kicad_tools.cli import route_cmd

        router = SimpleNamespace(
            net_class_map={"GND": NetClassRouting("Power", clearance=0.2), "SIG": "x"}
        )
        loaded = {"GND": NetClassRouting("HV", clearance=0.5)}
        args = SimpleNamespace(_loaded_net_class_map=loaded)
        merged = route_cmd._pour_closer_net_class_map(router, args)
        assert merged["GND"].clearance == 0.5 and merged["SIG"] == "x"
        assert route_cmd._pour_closer_net_class_map(None, SimpleNamespace()) is None

    def test_stage_passes_map_to_closer_and_prints_relaxations(self, monkeypatch, tmp_path, capsys):
        import kicad_tools.cli.runner as runner_mod
        import kicad_tools.cli.stitch_cmd as stitch_mod
        import kicad_tools.core.sexp_file as sexp_mod
        import kicad_tools.router.oracle_completion as oracle_mod
        from kicad_tools.cli import route_cmd
        from kicad_tools.router.oracle_completion import OracleCompletionResult

        seen: dict = {}

        def fake_run(_path, *, closer, **_k):
            seen["closer"] = closer
            return OracleCompletionResult(
                ran=True,
                stop_reason="converged",
                initial_links=1,
                final_links=0,
                relaxed_links=["GND C1.1: closed at 0.127 mm < class clearance 0.2 mm (x)"],
            )

        monkeypatch.setattr(runner_mod, "find_kicad_cli", lambda: Path("/usr/bin/kicad-cli"))
        monkeypatch.setattr(sexp_mod, "load_pcb", lambda _p: object())
        monkeypatch.setattr(stitch_mod, "find_all_plane_nets", lambda _doc: ["GND"])
        monkeypatch.setattr(route_cmd, "_make_pour_oracle", lambda _args: lambda _p: None)
        monkeypatch.setattr(oracle_mod, "run_oracle_completion", fake_run)
        router = SimpleNamespace(net_class_map={"GND": NetClassRouting("Power", clearance=0.2)})
        args = SimpleNamespace(
            dry_run=False,
            oracle_rounds=None,
            allow_stranded_pour_pads=False,
            timeout=None,
            clearance=0.15,
        )
        route_cmd._run_pour_oracle_stage(tmp_path / "b.kicad_pcb", args=args, router=router)
        closer = seen["closer"]
        assert closer.clearance == 0.15
        assert closer.class_clearance("GND") == 0.2
        assert "RELAXED: GND C1.1: closed at 0.127 mm" in capsys.readouterr().out
