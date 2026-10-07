"""Footprint text angles turn with their footprint (Issue #6126).

KiCad stores the angle of a footprint's ``property`` / ``fp_text`` nodes
**board-absolute** -- like a pad angle it already includes the footprint's
rotation and side -- while the text *position* stays footprint-local.  Every
kct path that rotates or flips a footprint must therefore rewrite those
angles too, the way pcbnew does:

* ``SetOrientation`` (absolute writes: ``Footprint.rotation``,
  ``PCB.update_footprint_position``, placement writeback, ``snap-rotation``,
  ``pcb move-footprint``) adds the rotation delta;
* ``FOOTPRINT::Rotate`` (relative rotates: ``pcb modify rotate``, the
  recovery rotate strategy) also re-uprights "keep upright" texts -- one at
  180 degrees or more turns by -180 and swaps its justification;
* ``Flip(LEFT_RIGHT)`` (``pcb modify flip``, the mirror strategy) maps the
  angle ``a -> -a`` and then re-uprights the same way.

``tests/fixtures/fp_zone_transform/`` holds the pcbnew 10.0.1 goldens (see
``generate_golden.py``): AE1 carries keep-upright and ``(unlocked yes)``
user texts with assorted angles and justifications next to its Reference and
Value fields.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_tools.recovery import (
    Action,
    Difficulty,
    ResolutionStrategy,
    StrategyApplicator,
    StrategyType,
)
from kicad_tools.schema.pcb import (
    PCB,
    Footprint,
    footprint_text_angle,
    keep_footprint_texts_upright,
    transform_footprint_text_nodes,
)
from kicad_tools.sexp import parse_string

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "fp_zone_transform"
BASE = FIXTURES / "base.kicad_pcb"
TOL = 2e-6

USER_TEXTS = {"KU_L45", "FREE_R100", "KU_C170", "FREE_0", "KU_R0"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ae1(pcb: PCB) -> Footprint:
    fp = pcb.get_footprint("AE1")
    assert fp is not None
    return fp


def _text_key(node) -> str:
    if node.tag == "property":
        return f"property:{node.get_string(0)}"
    return f"fp_text:{node.get_string(1)}"


def _sheet_texts(pcb: PCB) -> dict[str, dict]:
    """AE1's text nodes as written: local position, angle, layer, justify."""
    node = _ae1(pcb)._sexp_node
    assert node is not None
    out: dict[str, dict] = {}
    for child in node.iter_children():
        if child.tag not in ("property", "fp_text"):
            continue
        at = child.find_child("at")
        effects = child.find_child("effects")
        justify = effects.find_child("justify") if effects is not None else None
        out[_text_key(child)] = {
            "position": (at.get_float(0), at.get_float(1)),
            "angle": (at.get_float(2) or 0.0) % 360.0,
            "layer": child.find_child("layer").get_string(0),
            "justify": sorted(justify.values) if justify is not None else [],
        }
    return out


def _assert_texts_match(actual: dict[str, dict], golden: dict[str, dict]) -> None:
    assert set(actual) == set(golden)
    assert {key.split(":", 1)[1] for key in golden} >= USER_TEXTS
    for key, want in golden.items():
        got = actual[key]
        assert got["position"] == pytest.approx(want["position"], abs=TOL), key
        assert got["angle"] == pytest.approx(want["angle"], abs=1e-6), key
        assert got["layer"] == want["layer"], key
        assert got["justify"] == want["justify"], key


def _assert_model_texts_match(pcb: PCB, golden: PCB) -> None:
    """The parsed ``Footprint.texts`` angles agree with the golden too."""
    got = {(t.text_type, t.text): t.rotation % 360.0 for t in _ae1(pcb).texts}
    want = {(t.text_type, t.text): t.rotation % 360.0 for t in _ae1(golden).texts}
    assert set(got) == set(want)
    for key, angle in want.items():
        assert got[key] == pytest.approx(angle, abs=1e-6), key


def _golden(case: str) -> PCB:
    return PCB.load(str(FIXTURES / f"{case}.kicad_pcb"))


def _move(pcb: PCB, x: float, y: float, rotation: float | None = None) -> None:
    ox, oy = pcb.board_origin
    assert pcb.update_footprint_position("AE1", x - ox, y - oy, rotation)


def _strategy(pcb: PCB, kind: StrategyType, action: Action) -> None:
    result = StrategyApplicator().apply_strategy(
        pcb,
        ResolutionStrategy(type=kind, difficulty=Difficulty.EASY, confidence=1.0, actions=[action]),
    )
    assert result.success, result.message


def _mirror(pcb: PCB) -> None:
    _strategy(pcb, StrategyType.MIRROR_COMPONENT, Action(type="mirror", target="AE1"))


def _rotate_strategy(pcb: PCB, delta: float) -> None:
    _strategy(
        pcb,
        StrategyType.ROTATE_COMPONENT,
        Action(type="rotate", target="AE1", params={"rotation_delta": delta}),
    )


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


def _op_rotate_tool30(pcb: PCB) -> None:
    _rotate_strategy(pcb, 30.0)


def _op_rotate_tool200(pcb: PCB) -> None:
    _rotate_strategy(pcb, 200.0)


CASES: dict[str, Callable[[PCB], None]] = {
    "move": _op_move,
    "rot30": _op_rot30,
    "move_rot90": _op_move_rot90,
    "flip_lr": _op_flip_lr,
    "back_rot90": _op_back_rot90,
    "rotate_tool30": _op_rotate_tool30,
    "rotate_tool200": _op_rotate_tool200,
}


def _pcb_modify(tmp_path: Path, board: Path, *argv: str) -> Path:
    from kicad_tools.cli.pcb_modify import main

    out = tmp_path / f"mod_{len(list(tmp_path.iterdir()))}.kicad_pcb"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("sys.argv", ["pcb-modify", str(board), *argv, "-o", str(out)])
        assert main() in (0, None)
    return out


# ---------------------------------------------------------------------------
# The issue's repro and the golden cases
# ---------------------------------------------------------------------------


def test_issue_repro_properties_turn_with_update_footprint_position():
    pcb = PCB.load(str(BASE))
    assert pcb.update_footprint_position("AE1", 25, 25, 90)
    node = _ae1(pcb)._sexp_node
    angles = {
        prop.get_string(0): prop.find_child("at").get_float(2)
        for prop in node.find_children("property")
    }
    # pcbnew writes every property angle +90 (the Value field started at 90).
    assert angles == {"Reference": 90, "Value": 180, "Datasheet": 90, "Description": 90}


@pytest.mark.parametrize("case", list(CASES))
def test_texts_match_pcbnew_in_memory(case):
    pcb = PCB.load(str(BASE))
    CASES[case](pcb)
    golden = _golden(case)
    _assert_texts_match(_sheet_texts(pcb), _sheet_texts(golden))
    _assert_model_texts_match(pcb, golden)


@pytest.mark.parametrize("case", list(CASES))
def test_texts_match_pcbnew_after_save(case, tmp_path):
    pcb = PCB.load(str(BASE))
    CASES[case](pcb)
    out = tmp_path / f"{case}.kicad_pcb"
    pcb.save(str(out))
    reloaded = PCB.load(str(out))
    golden = _golden(case)
    _assert_texts_match(_sheet_texts(reloaded), _sheet_texts(golden))
    _assert_model_texts_match(reloaded, golden)


def test_unlocked_flag_is_parsed():
    texts = {t.text: t.keep_upright for t in _ae1(PCB.load(str(BASE))).texts}
    assert texts["FREE_0"] is False
    assert texts["FREE_R100"] is False
    assert texts["KU_L45"] is True
    assert texts["AE1"] is True  # fields default to keep-upright


# ---------------------------------------------------------------------------
# Composition and round trips
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

    assert _sheet_texts(a) == _sheet_texts(b)


def test_move_rotate_flip_and_restore_round_trips_byte_identically(tmp_path):
    pcb = PCB.load(str(BASE))
    before = tmp_path / "before.kicad_pcb"
    pcb.save(str(before))

    fp = _ae1(pcb)
    original = (fp.position, fp.rotation, fp.layer)
    fp.position = (31.7, 22.3)
    fp.rotation = 37.3
    fp.layer = "B.Cu"
    fp.rotation = 211.9
    fp.position, fp.rotation, fp.layer = original

    after = tmp_path / "after.kicad_pcb"
    pcb.save(str(after))
    assert after.read_text() == before.read_text()


_LEGACY = """(kicad_pcb (version 20221018) (generator pcbnew)
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (footprint "R:R" (layer "F.Cu") (uuid "u1") (at 10 10)
    (fp_text reference "R1" (at 0 -1.5) (layer "F.SilkS") (effects (font (size 1 1))))
    (fp_text value "10k" (at 0 1.5 270 unlocked) (layer "F.Fab") (effects (font (size 1 1))))
    (fp_text user "LBL" (at 1 0 unlocked) (layer "F.SilkS")
      (effects (font (size 1 1)) (justify left)))
  )
)
"""


def test_legacy_two_token_text_round_trips_byte_identically():
    pcb = PCB(parse_string(_LEGACY))
    before = pcb._sexp.to_string()
    fp = pcb.get_footprint("R1")
    fp.rotation = 37.0
    assert fp._sexp_node.find_children("fp_text")[0].find_child("at").get_float(2) == 37
    fp.rotation = 0.0
    assert pcb._sexp.to_string() == before


def test_legacy_unlocked_token_is_kept_and_respected():
    pcb = PCB(parse_string(_LEGACY))
    fp = pcb.get_footprint("R1")
    assert [t.keep_upright for t in fp.texts] == [True, False, False]
    _strategy(
        pcb,
        StrategyType.ROTATE_COMPONENT,
        Action(type="rotate", target="R1", params={"rotation_delta": 200.0}),
    )
    ref, value, user = fp._sexp_node.find_children("fp_text")
    # Keep-upright reference: 200 -> 20.  The unlocked texts keep the delta.
    assert ref.find_child("at").values == [0, -1.5, 20]
    assert value.find_child("at").values == [0, 1.5, 110, "unlocked"]
    assert user.find_child("at").values == [1, 0, 200, "unlocked"]
    assert user.find_child("effects").find_child("justify").values == ["left"]
    assert [t.rotation for t in fp.texts] == [20, 110, 200]


# ---------------------------------------------------------------------------
# Raw-node helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("old", "new", "angle", "expected"),
    [
        ((0, 0, 0.0, False), (0, 0, 30.0, False), 45.0, 75.0),
        ((0, 0, 300.0, False), (0, 0, 100.0, False), 10.0, 170.0),
        # Left/right flip of a footprint at 30: theta -> 150 on the back, a -> -a.
        ((0, 0, 30.0, False), (0, 0, 150.0, True), 75.0, 285.0),
        ((0, 0, 150.0, True), (0, 0, 30.0, False), 285.0, 75.0),
        ((0, 0, 150.0, True), (0, 0, 90.0, True), 230.0, 170.0),
    ],
)
def test_footprint_text_angle(old, new, angle, expected):
    assert footprint_text_angle(old, new, angle) == pytest.approx(expected)


def test_raw_node_helpers_count_texts():
    fp_node = parse_string(_LEGACY).find_child("footprint")
    assert transform_footprint_text_nodes(fp_node, (10, 10, 0, False), (10, 10, 0, False)) == 0
    assert transform_footprint_text_nodes(fp_node, (10, 10, 0, False), (10, 10, 190, False)) == 3
    # Only the keep-upright reference (now at 190) turns.
    assert keep_footprint_texts_upright(fp_node) == 1
    assert fp_node.find_child("fp_text").find_child("at").values == [0, -1.5, 10]


# ---------------------------------------------------------------------------
# Every rotate/flip path reaches the same result
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "case"),
    [
        (["rotate", "AE1", "30"], "rotate_tool30"),
        (["rotate", "AE1", "200"], "rotate_tool200"),
    ],
)
def test_pcb_modify_rotate(tmp_path, argv, case):
    """A relative rotate is pcbnew's ``FOOTPRINT::Rotate``: keep-upright texts
    re-upright (``KU_C170`` 170 + 30 = 200 -> 20), unlike ``SetOrientation``."""
    out = _pcb_modify(tmp_path, BASE, *argv)
    _assert_texts_match(_sheet_texts(PCB.load(str(out))), _sheet_texts(_golden(case)))


def test_pcb_modify_flip(tmp_path):
    rotated = _pcb_modify(tmp_path, BASE, "rotate", "AE1", "30")
    flipped = _pcb_modify(tmp_path, rotated, "flip", "AE1")
    written = PCB.load(str(flipped))
    golden = _golden("flip_lr")
    _assert_texts_match(_sheet_texts(written), _sheet_texts(golden))
    _assert_model_texts_match(written, golden)


def test_write_footprint_placements(tmp_path):
    from kicad_tools.placement.writeback import write_footprint_placements

    out = tmp_path / "out.kicad_pcb"
    assert write_footprint_placements(BASE, out, [("AE1", 125, 120, 90)]) == 1
    _assert_texts_match(_sheet_texts(PCB.load(str(out))), _sheet_texts(_golden("move_rot90")))


def test_pcb_move_footprint_rotation(tmp_path):
    from kicad_tools.cli.pcb_move_footprint import run_move_footprint

    board = tmp_path / "board.kicad_pcb"
    board.write_text(BASE.read_text())
    rc = run_move_footprint(board, reference="AE1", to=(125, 120), rotation=90.0, absolute=True)
    assert rc == 0
    _assert_texts_match(_sheet_texts(PCB.load(str(board))), _sheet_texts(_golden("move_rot90")))


def _snap(board: Path, grid: float) -> None:
    from kicad_tools.cli.commands.pcb import _run_snap_rotation_command

    args = SimpleNamespace(
        pcb=str(board),
        grid=grid,
        tolerance=None,
        exclude=None,
        only=None,
        dry_run=False,
        format="text",
        output=None,
    )
    assert _run_snap_rotation_command(args, board) == 0


def test_snap_rotation_turns_texts_and_pads_by_the_snap_amount(tmp_path):
    """Snapping pcbnew's 30-degree golden back to 0 gives pcbnew's base board."""
    board = tmp_path / "board.kicad_pcb"
    board.write_text((FIXTURES / "rot30.kicad_pcb").read_text())
    _snap(board, 90.0)
    snapped = PCB.load(str(board))
    base = PCB.load(str(BASE))
    assert _ae1(snapped).rotation == 0
    _assert_texts_match(_sheet_texts(snapped), _sheet_texts(base))
    # The pad angle is board-absolute: 50 at rotation 30 -> 20 at rotation 0.
    assert [p.rotation for p in _ae1(snapped).pads] == pytest.approx(
        [p.rotation for p in _ae1(base).pads]
    )


def test_snap_rotation_small_correction(tmp_path):
    board = tmp_path / "board.kicad_pcb"
    pcb = PCB.load(str(BASE))
    _move(pcb, 120, 109, 89.7)
    pcb.save(str(board))
    _snap(board, 90.0)
    snapped = _ae1(PCB.load(str(board)))
    assert snapped.rotation == 90
    assert snapped.pads[0].rotation == pytest.approx(110.0)
    assert {t.text: t.rotation for t in snapped.texts}["KU_L45"] == pytest.approx(135.0)
