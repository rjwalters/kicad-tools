"""Footprint-embedded zones move with their footprint (Issue #6119).

KiCad writes the zones inside a ``(footprint ...)`` block -- an RF module's
antenna keepout, a footprint copper pour -- in **board coordinates** with the
footprint's rotation and flip already applied.  Moving, rotating or flipping
the footprint through kct must therefore rewrite those polygons (outline and
fill), in the S-expression ``PCB.save`` writes *and* in the parsed
``PCB.footprint_rule_areas`` the router and ``.kicad_dru`` emitter read.

``tests/fixtures/fp_zone_transform/`` holds goldens produced by pcbnew 10.0.1
itself (see ``generate_golden.py`` there): ``base.kicad_pcb`` and the same
board after each KiCad operation.  KiCad is not needed at test time, except
for the native-DRC check, which skips without ``kicad-cli``.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from kicad_tools.cli.runner import find_kicad_cli
from kicad_tools.recovery import (
    Action,
    Difficulty,
    ResolutionStrategy,
    StrategyApplicator,
    StrategyType,
)
from kicad_tools.schema.pcb import PCB, Footprint

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "fp_zone_transform"
BASE = FIXTURES / "base.kicad_pcb"

# pcbnew stores integer nanometres and writes up to 6 decimals.
TOL = 2e-6
# Zone fills are re-derived by pcbnew rather than transformed, so allow
# a little more on filled_polygon vertices.
FILL_TOL = 5e-6


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ae1(pcb: PCB) -> Footprint:
    fp = pcb.get_footprint("AE1")
    assert fp is not None
    return fp


def _sheet_zones(pcb: PCB) -> dict[str, dict]:
    """Zone geometry read straight from AE1's ``(zone ...)`` nodes (sheet coords)."""
    node = _ae1(pcb)._sexp_node
    assert node is not None
    out: dict[str, dict] = {}
    for zone in node.find_children("zone"):
        name = zone.find_child("name").get_string(0)

        def pts(parent) -> list[tuple[float, float]]:
            return [
                (xy.get_float(0), xy.get_float(1))
                for xy in parent.find_child("pts").find_children("xy")
            ]

        layer = zone.find_child("layer")
        out[name] = {
            "layer": layer.get_string(0) if layer is not None else None,
            "outline": pts(zone.find_child("polygon")),
            "fills": [pts(fill) for fill in zone.find_children("filled_polygon")],
            "fill_layers": [
                fill.find_child("layer").get_string(0)
                for fill in zone.find_children("filled_polygon")
            ],
        }
    return out


def _model_zones(pcb: PCB) -> dict[str, dict]:
    """Zone geometry from the parsed Zone objects, shifted to sheet coords."""
    ox, oy = pcb.board_origin
    out: dict[str, dict] = {}
    for zone in _ae1(pcb)._zones:
        out[zone.name] = {
            "layer": zone.layer,
            "outline": [(x + ox, y + oy) for x, y in zone.polygon],
            "fills": [[(x + ox, y + oy) for x, y in poly] for poly in zone.filled_polygons],
            "fill_layers": list(zone.filled_polygon_layers),
        }
    return out


def _assert_points(actual, expected, tol: float) -> None:
    assert len(actual) == len(expected)
    for (ax, ay), (ex, ey) in zip(actual, expected, strict=True):
        assert ax == pytest.approx(ex, abs=tol)
        assert ay == pytest.approx(ey, abs=tol)


def _assert_zones_match(actual: dict[str, dict], golden: dict[str, dict]) -> None:
    assert set(actual) == set(golden) == {"ANT_KEEPOUT", "FP_POUR"}
    for name, want in golden.items():
        got = actual[name]
        assert got["layer"] == want["layer"], name
        _assert_points(got["outline"], want["outline"], TOL)
        assert got["fill_layers"] == want["fill_layers"], name
        assert len(got["fills"]) == len(want["fills"]), name
        for got_fill, want_fill in zip(got["fills"], want["fills"], strict=True):
            _assert_points(got_fill, want_fill, FILL_TOL)


def _move(pcb: PCB, x: float, y: float, rotation: float | None = None) -> None:
    """``update_footprint_position`` with sheet-absolute target coordinates."""
    ox, oy = pcb.board_origin
    assert pcb.update_footprint_position("AE1", x - ox, y - oy, rotation)


def _mirror(pcb: PCB) -> None:
    result = StrategyApplicator().apply_strategy(
        pcb,
        ResolutionStrategy(
            type=StrategyType.MIRROR_COMPONENT,
            difficulty=Difficulty.EASY,
            confidence=1.0,
            actions=[Action(type="mirror", target="AE1")],
        ),
    )
    assert result.success, result.message


def _op_move(pcb: PCB) -> None:
    _move(pcb, 125, 120)


def _op_rot30(pcb: PCB) -> None:
    _move(pcb, 120, 109, 30)


def _op_move_rot90(pcb: PCB) -> None:
    _move(pcb, 125, 120, 90)


def _op_flip_lr(pcb: PCB) -> None:
    _move(pcb, 120, 109, 30)
    _mirror(pcb)


def _op_back_rot90(pcb: PCB) -> None:
    _op_flip_lr(pcb)
    _move(pcb, 120, 109, 90)


CASES: dict[str, Callable[[PCB], None]] = {
    "move": _op_move,
    "rot30": _op_rot30,
    "move_rot90": _op_move_rot90,
    "flip_lr": _op_flip_lr,
    "back_rot90": _op_back_rot90,
}


# ---------------------------------------------------------------------------
# Golden: kct's transform equals pcbnew's
# ---------------------------------------------------------------------------


def test_golden_board_has_origin_offset_and_both_zone_kinds():
    pcb = PCB.load(str(BASE))
    assert pcb.board_origin == (100.0, 95.0)
    assert [z.name for z in pcb.footprint_rule_areas] == ["ANT_KEEPOUT"]
    pour = next(z for z in _ae1(pcb)._zones if z.name == "FP_POUR")
    assert pour.filled_polygons, "golden pour must carry a fill"


@pytest.mark.parametrize("case", list(CASES))
def test_zones_match_pcbnew_in_memory(case):
    pcb = PCB.load(str(BASE))
    CASES[case](pcb)
    golden = PCB.load(str(FIXTURES / f"{case}.kicad_pcb"))
    _assert_zones_match(_model_zones(pcb), _sheet_zones(golden))
    _assert_zones_match(_sheet_zones(pcb), _sheet_zones(golden))


@pytest.mark.parametrize("case", list(CASES))
def test_zones_match_pcbnew_after_save(case, tmp_path):
    pcb = PCB.load(str(BASE))
    CASES[case](pcb)
    out = tmp_path / f"{case}.kicad_pcb"
    pcb.save(str(out))
    reloaded = PCB.load(str(out))
    golden = PCB.load(str(FIXTURES / f"{case}.kicad_pcb"))
    _assert_zones_match(_sheet_zones(reloaded), _sheet_zones(golden))
    _assert_zones_match(_model_zones(reloaded), _sheet_zones(golden))


@pytest.mark.parametrize("case", list(CASES))
def test_footprint_and_pads_match_pcbnew(case):
    """The footprint anchor and its board-absolute pad angles agree too."""
    pcb = PCB.load(str(BASE))
    CASES[case](pcb)
    golden = PCB.load(str(FIXTURES / f"{case}.kicad_pcb"))
    got, want = _ae1(pcb), _ae1(golden)
    assert got.layer == want.layer
    assert got.position == pytest.approx(want.position, abs=TOL)
    assert got.rotation % 360 == pytest.approx(want.rotation % 360, abs=1e-9)
    for got_pad, want_pad in zip(got.pads, want.pads, strict=True):
        assert got_pad.rotation % 360 == pytest.approx(want_pad.rotation % 360, abs=1e-9)
        assert got_pad.position == pytest.approx(want_pad.position, abs=TOL)


def test_footprint_rule_areas_follow_the_move():
    pcb = PCB.load(str(BASE))
    _op_move_rot90(pcb)
    [area] = pcb.footprint_rule_areas
    ox, oy = pcb.board_origin
    # Issue repro: x 117..123 / y 105..111 at (120, 109) -> rotated 90 at (125, 120).
    xs = [x + ox for x, _ in area.polygon]
    ys = [y + oy for _, y in area.polygon]
    assert (min(xs), max(xs), min(ys), max(ys)) == pytest.approx((121, 127, 117, 123))


def test_written_coordinates_keep_six_decimals(tmp_path):
    """The generic serializer's 6 significant digits would write 124.598."""
    pcb = PCB.load(str(BASE))
    _op_flip_lr(pcb)
    out = tmp_path / "flip.kicad_pcb"
    pcb.save(str(out))
    assert "(xy 124.598076 107.035898)" in out.read_text()


# ---------------------------------------------------------------------------
# Composition: the transform depends only on the end states
# ---------------------------------------------------------------------------


def test_single_field_updates_compose_in_any_order():
    a = PCB.load(str(BASE))
    fa = _ae1(a)
    fa.layer = "B.Cu"
    fa.rotation = 150.0
    fa.position = (fa.position[0] + 3.0, fa.position[1] - 2.0)

    b = PCB.load(str(BASE))
    fb = _ae1(b)
    fb.position = (fb.position[0] + 3.0, fb.position[1] - 2.0)
    fb.rotation = 150.0
    fb.layer = "B.Cu"

    _assert_zones_match(_model_zones(a), _model_zones(b))
    _assert_zones_match(_sheet_zones(a), _sheet_zones(b))


def test_move_and_restore_round_trips_byte_identically(tmp_path):
    """Temporary moves (collision probes, feedback rollback) leave no residue."""
    pcb = PCB.load(str(BASE))
    before = tmp_path / "before.kicad_pcb"
    pcb.save(str(before))

    fp = _ae1(pcb)
    original = (fp.position, fp.rotation, fp.layer)
    fp.position = (31.7, 22.3)
    fp.rotation = 37.0
    fp.layer = "B.Cu"
    fp.position, fp.rotation, fp.layer = original

    after = tmp_path / "after.kicad_pcb"
    pcb.save(str(after))
    assert after.read_text() == before.read_text()


def test_layer_change_without_side_change_leaves_zones_alone():
    pcb = PCB.load(str(BASE))
    before = _sheet_zones(pcb)
    _ae1(pcb).layer = "F.Cu"
    assert _sheet_zones(pcb) == before


def test_multi_layer_keepout_layers_survive_a_flip():
    """``F&B.Cu`` / ``*.Cu`` are side-neutral; ``F.Cu``/``B.Cu`` lists swap."""
    pcb = PCB.load(str(BASE))
    keepout = next(z for z in _ae1(pcb)._zones if z.name == "ANT_KEEPOUT")
    node = next(
        z
        for z in _ae1(pcb)._sexp_node.find_children("zone")
        if z.find_child("name").get_string(0) == "ANT_KEEPOUT"
    )
    from kicad_tools.sexp import SExp

    layer_node = node.find_child("layer")
    node.children[node.children.index(layer_node)] = SExp.list(
        "layers", SExp.quoted_atom("F.Cu"), SExp.quoted_atom("In1.Cu"), SExp.quoted_atom("*.Cu")
    )
    keepout.layers = ["F.Cu", "In1.Cu", "*.Cu"]
    _mirror(pcb)
    assert keepout.layers == ["B.Cu", "In1.Cu", "*.Cu"]
    assert node.find_child("layers").values == ["B.Cu", "In1.Cu", "*.Cu"]


# ---------------------------------------------------------------------------
# Other move paths reach the same transform
# ---------------------------------------------------------------------------


def _golden_sheet(case: str) -> dict[str, dict]:
    return _sheet_zones(PCB.load(str(FIXTURES / f"{case}.kicad_pcb")))


def test_write_footprint_placements(tmp_path):
    from kicad_tools.placement.writeback import write_footprint_placements

    out = tmp_path / "out.kicad_pcb"
    assert write_footprint_placements(BASE, out, [("AE1", 125, 120, 90), ("NOPE", 0, 0, None)]) == 1
    _assert_zones_match(_sheet_zones(PCB.load(str(out))), _golden_sheet("move_rot90"))


def test_optimize_placement_writer_moves_the_footprint_not_its_children(tmp_path):
    """The old line-based writer rewrote AE1's property and pad ``(at ...)``
    on KiCad 8+ files and left the footprint and its zones in place."""
    import numpy as np

    from kicad_tools.cli.optimize_placement_cmd import _read_board_data, _write_placements_to_pcb
    from kicad_tools.placement.vector import PlacementVector

    components, _nets, _outline, _rules, origin = _read_board_data(str(BASE))
    assert [c.reference for c in components] == ["AE1"]
    # Board-relative (25, 25) == sheet (125, 120); rotation slot 1 == 90 deg.
    vector = PlacementVector(data=np.array([25.0, 25.0, 1.0, 0.0]))
    out = tmp_path / "opt.kicad_pcb"
    _write_placements_to_pcb(str(BASE), str(out), vector, components, origin)

    written = PCB.load(str(out))
    fp = _ae1(written)
    assert (fp.position[0] + 100, fp.position[1] + 95, fp.rotation) == pytest.approx((125, 120, 90))
    assert fp.pads[0].position == pytest.approx((-2.0, 1.0))
    _assert_zones_match(_sheet_zones(written), _golden_sheet("move_rot90"))


def test_mcp_optimize_placement_writer(tmp_path):
    import numpy as np

    from kicad_tools.mcp.tools.optimize_placement import _read_board_data, _write_placements_to_pcb
    from kicad_tools.placement.vector import PlacementVector

    components, _nets, _outline, _rules, origin = _read_board_data(str(BASE))
    vector = PlacementVector(data=np.array([25.0, 25.0, 1.0, 0.0]))
    out = tmp_path / "mcp.kicad_pcb"
    _write_placements_to_pcb(str(BASE), str(out), vector, components, origin)
    _assert_zones_match(_sheet_zones(PCB.load(str(out))), _golden_sheet("move_rot90"))


@pytest.mark.parametrize(
    ("argv", "case"),
    [
        (["move", "AE1", "125", "120"], "move"),
        (["rotate", "AE1", "30"], "rot30"),
    ],
)
def test_pcb_modify_move_and_rotate(tmp_path, argv, case):
    from kicad_tools.cli.pcb_modify import main

    out = tmp_path / "mod.kicad_pcb"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("sys.argv", ["pcb-modify", str(BASE), *argv, "-o", str(out)])
        assert main() in (0, None)
    written = PCB.load(str(out))
    golden = PCB.load(str(FIXTURES / f"{case}.kicad_pcb"))
    _assert_zones_match(_sheet_zones(written), _sheet_zones(golden))
    assert _ae1(written).pads[0].rotation == pytest.approx(_ae1(golden).pads[0].rotation)


def test_pcb_modify_flip(tmp_path):
    from kicad_tools.cli.pcb_modify import main

    rotated = tmp_path / "rot.kicad_pcb"
    out = tmp_path / "flip.kicad_pcb"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("sys.argv", ["pcb-modify", str(BASE), "rotate", "AE1", "30", "-o", str(rotated)])
        assert main() in (0, None)
        mp.setattr("sys.argv", ["pcb-modify", str(rotated), "flip", "AE1", "-o", str(out)])
        assert main() in (0, None)
    _assert_zones_match(_sheet_zones(PCB.load(str(out))), _golden_sheet("flip_lr"))


def test_placement_fixer_text_patch_translates_zones():
    from kicad_tools.placement.conflict import PlacementFix, Point
    from kicad_tools.placement.fixer import PlacementFixer

    # The fixer locates footprints by a non-empty library id; pcbnew wrote
    # the golden footprint with an empty one.
    content = BASE.read_text().replace('(footprint ""', '(footprint "RF:Antenna"')
    fixer = PlacementFixer()
    fix = PlacementFix(conflict=None, component="AE1", move_vector=Point(5.0, 11.0))  # type: ignore[arg-type]
    patched = fixer._apply_fix_to_content(content, fix)

    from kicad_tools.sexp import parse_string

    pcb = PCB(parse_string(patched))
    _assert_zones_match(_sheet_zones(pcb), _golden_sheet("move"))


def test_drc_repair_footprint_nudge_translates_zones(tmp_path):
    from kicad_tools.drc.repair_clearance import ClearanceRepairer

    board = tmp_path / "nudge.kicad_pcb"
    board.write_text(BASE.read_text())
    repairer = ClearanceRepairer(board)
    [fp_node] = [c for c in repairer.doc.iter_children() if c.tag == "footprint"]
    repairer._nudge_footprint(fp_node, 5.0, 11.0)
    repairer.save()
    _assert_zones_match(_sheet_zones(PCB.load(str(board))), _golden_sheet("move"))


# ---------------------------------------------------------------------------
# Router and DRC enforce the keepout at its new location
# ---------------------------------------------------------------------------

# AE1 at (107, 108) owns a track/via keepout x 105..109; the move puts it at
# x 120..124.  /OLD runs vertically through the old area, /NEW through the new.
_OLD_X, _NEW_X = 107.0, 122.0
_KEEPOUT_UUID = "eeeeeeee-0000-0000-0000-000000006119"


def _keepout_board(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)

    def track(x: float, net: int, uid: str) -> str:
        return (
            f'  (segment (start {x} 102) (end {x} 114) (width 0.25) (layer "B.Cu") (net {net})\n'
            f'    (uuid "dddddddd-0000-0000-0000-00000000000{uid}"))\n'
        )

    path.write_text(
        f"""(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (35 "F.Paste" user) (37 "F.SilkS" user)
    (39 "F.Mask" user) (44 "Edge.Cuts" user) (47 "F.CrtYd" user) (49 "F.Fab" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "/OLD")
  (net 2 "/NEW")
  (gr_rect (start 100 100) (end 130 116)
    (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
  (footprint "RF:Antenna" (layer "F.Cu") (uuid "00000000-0000-0000-0000-000000000021")
    (at 107 108)
    (property "Reference" "AE1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "ant" (at 0 1.5 0) (layer "F.Fab"))
    (zone (net 0) (net_name "") (name "antkeep") (layers "F.Cu" "B.Cu")
      (uuid "{_KEEPOUT_UUID}") (hatch edge 0.5)
      (keepout (tracks not_allowed) (vias not_allowed) (pads allowed)
        (copperpour not_allowed) (footprints allowed))
      (polygon (pts (xy 105 101) (xy 109 101) (xy 109 115) (xy 105 115))))
  )
{track(_OLD_X, 1, "1")}{track(_NEW_X, 2, "2")})
""",
        encoding="utf-8",
    )
    return path


def _moved_keepout_board(tmp_path: Path) -> Path:
    board = _keepout_board(tmp_path / "ant.kicad_pcb")
    pcb = PCB.load(str(board))
    ox, oy = pcb.board_origin
    assert pcb.update_footprint_position("AE1", 122 - ox, 108 - oy)
    pcb.save(str(board))
    return board


def test_router_blocks_the_new_keepout_location_not_the_old(tmp_path):
    from kicad_tools.router.core import Autorouter
    from kicad_tools.router.io import detect_layer_stack
    from kicad_tools.router.orchestrator import RoutingOrchestrator
    from kicad_tools.router.rules import DesignRules

    board = _moved_keepout_board(tmp_path)

    # kct route: sheet-absolute rule areas read from the saved board.
    router = Autorouter(width=40.0, height=30.0, rules=DesignRules())
    router._pairwise_attach_zone_pcb_path = str(board)
    [area] = router._keepout_rule_area_polygons()
    assert area.bbox == pytest.approx((120.0, 101.0, 124.0, 115.0))

    # kct route-auto: the in-memory mask flags a track through the new spot
    # and passes one through the old spot.
    pcb = PCB.load(str(board))
    orchestrator = RoutingOrchestrator(
        pcb=pcb,  # type: ignore[arg-type]
        rules=DesignRules(),
        layer_stack=detect_layer_stack(board.read_text()),
    )
    mask = orchestrator._keepout_mask()
    ox, oy = pcb.board_origin
    b_cu = 1

    def blocked(x: float) -> bool:
        return mask.segment_blocked((x - ox, 102 - oy), (x - ox, 114 - oy), b_cu, 0, 0.125)

    assert blocked(_NEW_X)
    assert not blocked(_OLD_X)


def test_router_mask_follows_an_in_memory_move(tmp_path):
    """No save/reload: ``footprint_rule_areas`` itself is refreshed."""
    from kicad_tools.router.io import detect_layer_stack
    from kicad_tools.router.orchestrator import RoutingOrchestrator
    from kicad_tools.router.rules import DesignRules

    board = _keepout_board(tmp_path / "ant.kicad_pcb")
    pcb = PCB.load(str(board))
    ox, oy = pcb.board_origin
    assert pcb.update_footprint_position("AE1", 122 - ox, 108 - oy)
    mask = RoutingOrchestrator(
        pcb=pcb,  # type: ignore[arg-type]
        rules=DesignRules(),
        layer_stack=detect_layer_stack(board.read_text()),
    )._keepout_mask()
    assert mask.segment_blocked((_NEW_X - ox, 102 - oy), (_NEW_X - ox, 114 - oy), 1, 0, 0.125)
    assert not mask.segment_blocked((_OLD_X - ox, 102 - oy), (_OLD_X - ox, 114 - oy), 1, 0, 0.125)


def _native_keepout_hits(board: Path) -> list[str]:
    cli = find_kicad_cli()
    if cli is None:
        pytest.skip("Native KiCad CLI is not installed")
    from kicad_tools.manufacturers import get_profile, write_drc_constraints

    rules = get_profile("jlcpcb").get_design_rules(layers=2, copper_oz=1)
    write_drc_constraints(board, rules, manufacturer_id="jlcpcb", layers=2)
    report = board.with_suffix(".drc.json")
    subprocess.run(
        [
            str(cli),
            "pcb",
            "drc",
            "--severity-all",
            "--format",
            "json",
            "-o",
            str(report),
            str(board),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    data = json.loads(report.read_text(encoding="utf-8"))
    return sorted(
        item["description"]
        for v in data["violations"]
        if v["type"] == "items_not_allowed" and "kct keepout" in v["description"]
        for item in v["items"]
        if item["description"].startswith("Track")
    )


def test_native_drc_flags_the_new_keepout_location_not_the_old(tmp_path):
    before = _native_keepout_hits(_keepout_board(tmp_path / "before" / "ant.kicad_pcb"))
    assert len(before) == 1 and "[/OLD]" in before[0]

    after = _native_keepout_hits(_moved_keepout_board(tmp_path))
    assert len(after) == 1 and "[/NEW]" in after[0], after
