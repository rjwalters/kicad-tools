"""``kct route-auto`` never writes copper that shorts or crowds another net (#6107).

Follow-up to #6001, whose output gate refused copper *overlapping* another
net's straight tracks and vias only.  Three variants of
``tests/fixtures/route_auto_6001/crossing.kicad_pcb`` -- ``/B`` routes south to
north at x = 115 mm through the place ``/A`` used to cross it -- each wrote copper
that ``kicad-cli pcb drc`` flagged, while route-auto exited 0:

* ``pad_on_path`` -- a 3.0 x 1.0 mm ``/A`` SMD pad (``R5.1``) on ``/B``'s path
  (``shorting_items``: 1 under ``auto``, 14 under ``hierarchical``);
* ``track_near_path`` -- an ``/A`` track at x = 115.25 mm, 0.05 mm from a
  straight ``/B`` (``clearance``: 88 under ``hierarchical``);
* ``arc_on_path`` -- an ``/A`` arc between ``/A`` pads ``R6``/``R7`` whose bulge
  crosses x = 115 mm but whose chord does not (``shorting_items``: 14 under
  ``hierarchical``).

Two layers are pinned here.  Prevention: the hierarchical strategy routes on
the real board (other nets' pads, tracks, vias and arcs on its grid) and goes
around each obstacle.  Backstop: the output gate refuses copper that shorts
another net's pad, track, arc or via -- measured on real pad shapes and the
arc's true circle -- or that comes closer than the board's clearance, and
names the conflicting net and item.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from kicad_tools.cli import main as kct_main
from kicad_tools.mcp.tools.routing import route_net_auto
from kicad_tools.router.clearance_kernel import CLEARANCE_EPSILON_MM, KSegment, copper_gap
from kicad_tools.router.cpp_backend import is_cpp_available
from kicad_tools.router.foreign_copper import (
    KICAD_DEFAULT_CLEARANCE_MM,
    board_copper,
    board_required_clearance,
)
from kicad_tools.router.layers import Layer
from kicad_tools.router.orchestrator import RoutingOrchestrator
from kicad_tools.router.primitives import Segment, Via
from kicad_tools.router.rules import DesignRules
from kicad_tools.router.strategies import RoutingResult, RoutingStrategy
from kicad_tools.schema.pcb import PCB

FIXTURES = Path(__file__).parent / "fixtures" / "route_auto_6107"
PAD = FIXTURES / "pad_on_path.kicad_pcb"
NEAR = FIXTURES / "track_near_path.kicad_pcb"
ARC = FIXTURES / "arc_on_path.kicad_pcb"
VARIANTS = [
    pytest.param(PAD, id="pad"),
    pytest.param(NEAR, id="track-0.05mm"),
    pytest.param(ARC, id="arc"),
]

# The fixtures' board origin: Edge.Cuts starts at (100, 100), so the schema
# PCB (and the orchestrator) see /B's pads at board-relative (15, 2) / (15, 14).
B_PATH = ((15.0, 2.0), (15.0, 14.0))


def _copy(tmp_path: Path, board: Path) -> Path:
    dst = tmp_path / board.name
    shutil.copy(board, dst)
    return dst


def _orchestrator(path: Path) -> RoutingOrchestrator:
    from kicad_tools.router.io import detect_layer_stack, parse_pcb_design_rules

    text = path.read_text()
    pcb = PCB.load(str(path))
    return RoutingOrchestrator(
        pcb=pcb,  # type: ignore[arg-type]
        rules=parse_pcb_design_rules(text).to_design_rules(),
        layer_stack=detect_layer_stack(text),
    )


def _straight_b(net: str = "/B", net_id: int = 2) -> RoutingResult:
    """The copper a corridor strategy draws: /B straight up x = 115."""
    (x1, y1), (x2, y2) = B_PATH
    return RoutingResult(
        success=True,
        net=net,
        strategy_used=RoutingStrategy.GLOBAL_WITH_REPAIR,
        segments=[Segment(x1, y1, x2, y2, 0.2, Layer.F_CU, net_id, net)],
    )


def _min_gap_to_other_nets(out: Path, net_name: str) -> float:
    """Smallest copper gap between ``net_name``'s tracks and any other net's copper.

    An independent re-measurement of the written board with the clearance
    kernel, not a call into the gate under test.
    """
    items = board_copper(PCB.load(str(out)))
    mine = [i for i in items if i.net_name == net_name and i.kind == "track"]
    others = [i for i in items if i.net_name != net_name and i.net > 0]
    assert mine, f"no {net_name} copper written"
    best = float("inf")
    for a in mine:
        for b in others:
            if b.layers is not None and a.layers is not None and not (a.layers & b.layers):
                continue
            for sa in a.shapes:
                for sb in b.shapes:
                    best = min(best, copper_gap(sa, sb))
    return best


# ---------------------------------------------------------------------------
# Prevention: the hierarchical strategy routes around other nets' copper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("board", VARIANTS)
def test_hierarchical_routes_around_other_nets_copper(tmp_path, board) -> None:
    src = _copy(tmp_path, board)
    out = tmp_path / "out.kicad_pcb"
    result = route_net_auto(str(src), "/B", output_path=str(out), strategy="hierarchical")

    assert result["success"] is True, result.get("error_message")
    assert out.exists()
    gap = _min_gap_to_other_nets(out, "/B")
    assert gap >= KICAD_DEFAULT_CLEARANCE_MM - CLEARANCE_EPSILON_MM, gap


@pytest.mark.parametrize("board", VARIANTS)
def test_auto_never_writes_a_conflict(tmp_path, capsys, board) -> None:
    src = _copy(tmp_path, board)
    out = tmp_path / "out.kicad_pcb"
    rc = kct_main(["route-auto", str(src), "--net", "/B", "-o", str(out), "--format", "json"])
    doc = json.loads(capsys.readouterr().out)
    [entry] = doc["nets"]
    if entry["success"]:
        assert rc == 0
        gap = _min_gap_to_other_nets(out, "/B")
        assert gap >= KICAD_DEFAULT_CLEARANCE_MM - CLEARANCE_EPSILON_MM, gap
    else:  # refused: the message names the other net, and nothing is written
        assert rc == 1
        assert "'/A'" in entry["error"]
        assert not out.exists()


def test_hierarchical_grid_holds_other_nets_pads_and_arcs(tmp_path) -> None:
    """The board-loaded grid blocks /A's pad and /A's arc -- on both backends' grids."""
    orchestrator = _orchestrator(_copy(tmp_path, ARC))
    board_nets = {n.name for n in orchestrator.pcb.nets.values() if n.name}
    path = orchestrator._board_path()
    assert path is not None
    router, net_id = orchestrator._load_board_router(path, "/B", board_nets)
    assert net_id == 2
    assert set(router.nets) == {2}  # only /B is routed; /A's pads are obstacles

    # The arc's apex, (115.6, 108) in the sheet frame the loader uses.
    gx, gy = router.grid.world_to_grid(115.6, 108.0)
    assert router.grid._blocked[0, gy, gx]
    cpp = getattr(router, "_cpp_grid", None)
    if cpp is not None:  # #6103: the C++ grid must see the same copper
        assert cpp._impl.at(gx, gy, 0).blocked


@pytest.mark.skipif(not is_cpp_available(), reason="C++ router backend not built")
def test_hierarchical_board_route_uses_the_cpp_grid(tmp_path) -> None:
    """The C++ grid sees the obstacles, so the C++ search -- not a fallback -- routes."""
    orchestrator = _orchestrator(_copy(tmp_path, PAD))
    pcb = orchestrator.pcb
    from kicad_tools.mcp.tools.routing import _build_pads_for_net

    pads = _build_pads_for_net(pcb, 2, "/B")
    result = orchestrator._route_hierarchical("/B", None, pads)
    assert result.success, result.error_message
    assert orchestrator._hierarchical_board_router is not None
    assert orchestrator._hierarchical is None  # the legacy own-pads router never ran


# ---------------------------------------------------------------------------
# Backstop: the output gate
# ---------------------------------------------------------------------------


def test_gate_refuses_copper_on_another_nets_pad(tmp_path) -> None:
    orchestrator = _orchestrator(_copy(tmp_path, PAD))
    result = _straight_b()
    orchestrator._enforce_no_foreign_shorts(result, RoutingStrategy.GLOBAL_WITH_REPAIR)

    assert result.success is False
    assert result.segments == [] and result.vias == []
    assert "shorts net(s) '/A'" in result.error_message
    assert "pad R5.1 at (115.000, 108.000)" in result.error_message
    # A corridor strategy is steered to the strategy that routes around it.
    assert [a.strategy for a in result.alternative_strategies] == [
        RoutingStrategy.HIERARCHICAL_DIFF_PAIR
    ]


def test_gate_measures_the_arc_not_its_chord(tmp_path) -> None:
    orchestrator = _orchestrator(_copy(tmp_path, ARC))
    result = _straight_b()
    orchestrator._enforce_no_foreign_shorts(result, RoutingStrategy.GLOBAL_WITH_REPAIR)

    assert result.success is False
    assert "shorts net(s) '/A'" in result.error_message
    assert "arc (113.000, 105.000) via (115.600, 108.000)" in result.error_message

    # The chord (x = 113) is 2 mm from /B, the bulge (x = 115.6) crosses it:
    # a chord-only model would have passed this copper.
    [arc] = [i for i in orchestrator._board_copper() if i.kind == "arc"]
    assert max(max(s.x1, s.x2) for s in arc.shapes) == pytest.approx(15.6, abs=1e-3)


def test_gate_enforces_clearance_not_just_overlap(tmp_path) -> None:
    orchestrator = _orchestrator(_copy(tmp_path, NEAR))
    result = _straight_b()
    orchestrator._enforce_no_foreign_shorts(result, RoutingStrategy.GLOBAL_WITH_REPAIR)

    assert result.success is False
    assert "shorts" not in result.error_message  # no overlap ...
    assert "comes within 0.050 mm of net(s) '/A'" in result.error_message  # ... too close
    assert "track (115.250, 104.000)-(115.250, 112.000) on F.Cu" in result.error_message
    assert "below the board's 0.200 mm clearance" in result.error_message


def test_gate_accepts_copper_at_exactly_the_clearance(tmp_path) -> None:
    orchestrator = _orchestrator(_copy(tmp_path, NEAR))
    # /A's edge is at x = 15.15; a 0.2 mm /B track at x = 14.85 has its edge
    # at 14.95 -- exactly 0.2 mm away.
    result = RoutingResult(
        success=True,
        net="/B",
        strategy_used=RoutingStrategy.GLOBAL_WITH_REPAIR,
        segments=[Segment(14.85, 2.0, 14.85, 14.0, 0.2, Layer.F_CU, 2, "/B")],
    )
    orchestrator._enforce_no_foreign_shorts(result, RoutingStrategy.GLOBAL_WITH_REPAIR)
    assert result.success is True, result.error_message


def test_gate_checks_vias_against_pads(tmp_path) -> None:
    orchestrator = _orchestrator(_copy(tmp_path, PAD))
    result = RoutingResult(
        success=True,
        net="/B",
        strategy_used=RoutingStrategy.GLOBAL_WITH_REPAIR,
        vias=[Via(16.4, 8.0, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), 2, "/B")],
    )
    orchestrator._enforce_no_foreign_shorts(result, RoutingStrategy.GLOBAL_WITH_REPAIR)
    assert result.success is False
    assert "pad R5.1" in result.error_message
    assert "(0 segment(s), 1 via(s))" in result.error_message


def test_gate_spares_own_net_and_unassigned_copper(tmp_path) -> None:
    # /A crossing its own pad is not a conflict.
    orchestrator = _orchestrator(_copy(tmp_path, PAD))
    own = RoutingResult(
        success=True,
        net="/A",
        strategy_used=RoutingStrategy.GLOBAL_WITH_REPAIR,
        segments=[Segment(12.0, 8.0, 18.0, 8.0, 0.2, Layer.F_CU, 1, "/A")],
    )
    orchestrator._enforce_no_foreign_shorts(own, RoutingStrategy.GLOBAL_WITH_REPAIR)
    assert own.success is True, own.error_message


def test_gate_refuses_copper_on_a_no_net_pad(tmp_path) -> None:
    """KiCad reports copper across a net-0 pad as a short (#6107 review)."""
    text = PAD.read_text().replace(
        '(size 3.0 1.0) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "/A")',
        '(size 3.0 1.0) (layers "F.Cu" "F.Paste" "F.Mask")',
    )
    unassigned = tmp_path / "unassigned.kicad_pcb"
    unassigned.write_text(text)
    orchestrator = _orchestrator(unassigned)
    result = _straight_b()
    orchestrator._enforce_no_foreign_shorts(result, RoutingStrategy.GLOBAL_WITH_REPAIR)
    assert result.success is False
    assert result.segments == [] and result.vias == []
    assert "pad R5.1" in result.error_message


def test_gate_ignores_a_bare_npth_hole(tmp_path) -> None:
    """An NPTH pad with no annular copper is a hole, not copper."""
    text = PAD.read_text().replace(
        '(pad "1" smd rect (at 0 0) (size 3.0 1.0) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "/A"))',
        '(pad "" np_thru_hole circle (at 0 0) (size 3.0 3.0) (drill 3.0) (layers "*.Cu" "*.Mask"))',
    )
    assert text != PAD.read_text(), "fixture pad line changed; update the replacement"
    board = tmp_path / "npth.kicad_pcb"
    board.write_text(text)
    orchestrator = _orchestrator(board)
    result = _straight_b()
    orchestrator._enforce_no_foreign_shorts(result, RoutingStrategy.GLOBAL_WITH_REPAIR)
    assert result.success is True, result.error_message


def test_gate_respects_pad_layers(tmp_path) -> None:
    """An F.Cu SMD pad does not conflict with B.Cu copper above it."""
    orchestrator = _orchestrator(_copy(tmp_path, PAD))
    result = RoutingResult(
        success=True,
        net="/B",
        strategy_used=RoutingStrategy.GLOBAL_WITH_REPAIR,
        segments=[Segment(15.0, 2.0, 15.0, 14.0, 0.2, Layer.B_CU, 2, "/B")],
    )
    orchestrator._enforce_no_foreign_shorts(result, RoutingStrategy.GLOBAL_WITH_REPAIR)
    assert result.success is True, result.error_message


def test_corridor_strategy_warns_once_about_other_nets_copper(tmp_path, capsys) -> None:
    orchestrator = _orchestrator(_copy(tmp_path, PAD))
    message = orchestrator._warn_corridor_copper(RoutingStrategy.GLOBAL_WITH_REPAIR)
    assert message is not None and "cannot route around other nets'" in message
    assert "issue #6107" in capsys.readouterr().err
    assert orchestrator._warn_corridor_copper(RoutingStrategy.GLOBAL_WITH_REPAIR) is None
    assert orchestrator._warn_corridor_copper(RoutingStrategy.HIERARCHICAL_DIFF_PAIR) is None


# ---------------------------------------------------------------------------
# Which clearance the gate enforces
# ---------------------------------------------------------------------------


def test_required_clearance_defaults_to_kicads(tmp_path) -> None:
    src = _copy(tmp_path, NEAR)
    assert board_required_clearance(src, 0.1) == KICAD_DEFAULT_CLEARANCE_MM
    assert board_required_clearance(None, 0.1) == 0.1


def test_required_clearance_reads_the_project_netclass(tmp_path) -> None:
    src = _copy(tmp_path, NEAR)
    src.with_suffix(".kicad_pro").write_text(
        json.dumps({"net_settings": {"classes": [{"name": "Default", "clearance": 0.15}]}})
    )
    assert board_required_clearance(src, 0.1) == pytest.approx(0.15)

    # At 0.15 mm, copper 0.18 mm from /A passes; at KiCad's 0.2 it would not.
    orchestrator = _orchestrator(src)
    result = RoutingResult(
        success=True,
        net="/B",
        strategy_used=RoutingStrategy.GLOBAL_WITH_REPAIR,
        segments=[Segment(14.87, 2.0, 14.87, 14.0, 0.2, Layer.F_CU, 2, "/B")],
    )
    orchestrator._enforce_no_foreign_shorts(result, RoutingStrategy.GLOBAL_WITH_REPAIR)
    assert result.success is True, result.error_message


def test_board_copper_reads_real_pad_shapes(tmp_path) -> None:
    pcb = PCB.load(str(_copy(tmp_path, PAD)))
    [pad] = [i for i in board_copper(pcb) if i.label.startswith("pad R5.1")]
    assert pad.net_name == "/A"
    assert pad.layers == frozenset({"F.Cu"})
    # 3.0 x 1.0 mm, centred on (15, 8) board-relative.
    assert pad.bbox == pytest.approx((13.5, 7.5, 16.5, 8.5))
    # 0.05 mm clear of its long edge is not overlap.
    assert copper_gap(KSegment(13.0, 8.65, 17.0, 8.65, 0.2), pad.shapes[0]) == pytest.approx(0.05)


def test_rules_unchanged_when_board_routes(tmp_path) -> None:
    """The board-loaded route bumps clearance on a copy, never the caller's rules."""
    orchestrator = _orchestrator(_copy(tmp_path, PAD))
    orchestrator.rules = DesignRules(trace_clearance=0.1, via_clearance=0.1)
    before = (orchestrator.rules.trace_clearance, orchestrator.rules.via_clearance)
    board_nets = {n.name for n in orchestrator.pcb.nets.values() if n.name}
    path = orchestrator._board_path()
    assert path is not None
    router, _ = orchestrator._load_board_router(path, "/B", board_nets)
    assert router.rules.trace_clearance == pytest.approx(KICAD_DEFAULT_CLEARANCE_MM)
    assert (orchestrator.rules.trace_clearance, orchestrator.rules.via_clearance) == before


def test_gate_ignores_a_bare_oval_npth_slot(tmp_path) -> None:
    """An oval NPTH slot (``drill oval``) the size of its pad is a hole too."""
    text = PAD.read_text().replace(
        '(pad "1" smd rect (at 0 0) (size 3.0 1.0) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "/A"))',
        '(pad "" np_thru_hole oval (at 0 0) (size 3.0 1.0) (drill oval 3.0 1.0) (layers "*.Cu" "*.Mask"))',
    )
    assert text != PAD.read_text(), "fixture pad line changed; update the replacement"
    board = tmp_path / "npth_slot.kicad_pcb"
    board.write_text(text)
    orchestrator = _orchestrator(board)
    result = _straight_b()
    orchestrator._enforce_no_foreign_shorts(result, RoutingStrategy.GLOBAL_WITH_REPAIR)
    assert result.success is True, result.error_message


def test_gate_refuses_copper_on_an_npth_pad_with_annular_copper(tmp_path) -> None:
    """An NPTH pad larger than its drill carries copper and is still checked."""
    text = PAD.read_text().replace(
        '(pad "1" smd rect (at 0 0) (size 3.0 1.0) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "/A"))',
        '(pad "" np_thru_hole circle (at 0 0) (size 3.0 3.0) (drill 1.0) (layers "*.Cu" "*.Mask"))',
    )
    assert text != PAD.read_text(), "fixture pad line changed; update the replacement"
    board = tmp_path / "npth_ring.kicad_pcb"
    board.write_text(text)
    orchestrator = _orchestrator(board)
    result = _straight_b()
    orchestrator._enforce_no_foreign_shorts(result, RoutingStrategy.GLOBAL_WITH_REPAIR)
    assert result.success is False
    assert result.segments == [] and result.vias == []
