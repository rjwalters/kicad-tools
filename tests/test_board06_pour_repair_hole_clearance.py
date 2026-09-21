"""Board06's pour repair must not run a bridge through a foreign DRILL HOLE.

Issue #5631, the dedicated regression control for the two checks PR #5629
(closing #5608) added to ``_path_ok`` in
``boards/06-diffpair-test/generate_design.py``: a candidate bridge / stub
is rejected when it comes closer than ``HOLE_CLEAR`` (0.25 mm) to a foreign
**via drill** or a foreign **through-hole pad drill**.  KiCad enforces that
floor as ``hole_clearance``; kct's clearance rules do not model
track-vs-hole, so the repair's own path acceptance is the only thing
standing between this stage and a DRC error on the regenerated board.

Those checks shipped with the PR's own ``TDD:`` line marked *partial*: the
41 sibling pour-repair tests all run the new code, but none of them places
a foreign drill near a candidate path, so none could fail if the branch
were silently wrong (wrong buffer radius, wrong ``pnet != net``
comparison, wrong ``is_th`` / ``drill_r`` gating, or the branch deleted
outright).  Every test here was confirmed RED against a copy of the recipe
with the two ``HOLE_CLEAR`` blocks removed and GREEN against the current
one.

Isolating the branch means threading a narrow geometric window, because
the pre-existing *copper* guard (``CLEAR`` = 0.15 mm, against the via
barrel / the pad's circumscribed copper circle) covers most of the same
ground.  The blocker therefore sits in the band where the copper guard is
satisfied and only the hole floor is violated -- ``test_*_geometry_isolates_*``
below pins that band with plain shapely so a future reader can see the
rejection has exactly one possible source.  For the through-hole pad that
band only exists at a sub-IPC annulus (the index models pad copper by its
*circumscribed* circle, which outgrows ``drill / 2 + 0.10 mm`` for any
realistically-annular pad), so ``TH_PAD_SIZE``/``TH_PAD_DRILL`` below are
deliberately degenerate: they are a control on the code, not a claim about
board geometry that ships.

The fixture is constructed directly rather than regenerated from the
board, so -- like its siblings ``test_board06_pour_repair_barrel_reuse.py``
(#5551) and ``test_board06_pour_repair_th_pad_reuse.py`` (#5606) -- it is a
durable control independent of any retained artifact.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_DIR = REPO_ROOT / "boards" / "06-diffpair-test"

# --- the recipe's own literals, mirrored for the isolation assertions ------
CLEAR = 0.15  # copper-to-copper floor in ``_path_ok``
HOLE_CLEAR = 0.25  # #5608 drill-hole floor in ``_path_ok``
BRIDGE_W = 0.2  # width of a sub-stage B / D bridge

# --- geometry of the constructed case, in board coordinates ---------------
# Board06's repair clamps candidates to the board outline (99.0 .. 197.5 by
# 48.0 .. 127.0), so the whole fixture sits well inside it.  Two same-net
# ``+1V2`` fills on In1.Cu face each other across a 3 mm gap, each tapering
# to a single vertex so the nearest-point pair -- and therefore the bridge
# the repair proposes -- is unambiguous.
PRIMARY_TIP = (141.0, 85.0)
ISLAND_TIP = (144.0, 85.0)
#: The bridge sub-stage B emits, nearest points plus its 0.35 mm overshoot.
BRIDGE = ((144.350, 85.000), (140.650, 85.000))

#: Foreign blockers sit on the bridge's centre line's perpendicular, midway
#: across the gap.
BLOCKER_X = 142.5

# A foreign via with a 0.45 mm annular ring over a 0.40 mm drill.  Its
# barrel guard reaches 0.225 + CLEAR + BRIDGE_W/2 = 0.475 mm from centre;
# its hole guard reaches 0.200 + HOLE_CLEAR + BRIDGE_W/2 = 0.550 mm.
VIA_SIZE = 0.45
VIA_DRILL = 0.40
VIA_BLOCKING_Y = 85.51  # 0.510 mm off the bridge: inside the hole floor only
VIA_CLEARING_Y = 85.60  # 0.600 mm off the bridge: outside both floors
VIA_SMALL_DRILL = 0.20  # same barrel, hole guard shrinks to 0.450 mm

# A foreign through-hole pad.  Copper guard: 0.19*sqrt(2)/2 + CLEAR +
# BRIDGE_W/2 = 0.384 mm; hole guard: 0.075 + HOLE_CLEAR + BRIDGE_W/2 =
# 0.425 mm.  See the module docstring on why the annulus is degenerate.
TH_PAD_SIZE = 0.19
TH_PAD_DRILL = 0.15
PAD_BLOCKING_Y = 85.405  # 0.405 mm off the bridge: inside the hole floor only
PAD_CLEARING_Y = 85.450  # 0.450 mm off the bridge: outside both floors


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - import guard
        raise ImportError(f"Cannot load module {name!r} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def generate_design_mod():
    """Load ``boards/06-diffpair-test/generate_design.py`` as a module."""
    gp = _load_module("board_06_generate_pcb_hole", BOARD_DIR / "generate_pcb.py")
    sys.modules["generate_pcb"] = gp
    gs = _load_module("board_06_generate_schematic_hole", BOARD_DIR / "generate_schematic.py")
    sys.modules["generate_schematic"] = gs
    return _load_module("board_06_generate_design_hole", BOARD_DIR / "generate_design.py")


# Two disjoint ``+1V2`` fills on In1.Cu, in separate zones so the repair
# sees two components.  Neither carries a pad or a via, which pins the
# strategy under test: sub-stage A (offset via + stub) and board06's bounded
# ``pour_escape`` fallback both need a pad, and sub-stage C (ray casting)
# needs a via, so only the straight-line bridges of sub-stages B and D can
# run -- and every one of those chords is collinear with ``BRIDGE`` (D backs
# its termini off along the same nearest-point axis), so a blocker on that
# line blocks the whole ladder rather than just its first rung.
_FIXTURE = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (2 "In2.Cu" signal)
    (31 "B.Cu" signal)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "+1V2")
  (net 2 "SIG")
{blocker}
  (zone (net 1) (net_name "+1V2") (layer "In1.Cu")
    (uuid "zone-primary")
    (fill yes)
    (polygon
      (pts (xy 130 80) (xy 140 80) (xy 141 85) (xy 140 90) (xy 130 90))
    )
    (filled_polygon
      (layer "In1.Cu")
      (pts (xy 130 80) (xy 140 80) (xy 141 85) (xy 140 90) (xy 130 90))
    )
  )
  (zone (net 1) (net_name "+1V2") (layer "In1.Cu")
    (uuid "zone-island")
    (fill yes)
    (polygon
      (pts (xy 144 85) (xy 145 83) (xy 150 83) (xy 150 87) (xy 145 87))
    )
    (filled_polygon
      (layer "In1.Cu")
      (pts (xy 144 85) (xy 145 83) (xy 150 83) (xy 150 87) (xy 145 87))
    )
  )
)
"""


def _via(y: float, *, drill: float = VIA_DRILL, net: int = 2) -> str:
    """A ``(via ...)`` blocker at ``(BLOCKER_X, y)``.  ``net`` 2 is foreign."""
    return (
        f"  (via (at {BLOCKER_X:.3f} {y:.3f}) (size {VIA_SIZE}) (drill {drill}) "
        f'(layers "F.Cu" "B.Cu") (net {net}) (uuid "via-blocker"))'
    )


def _pad_footprint(y: float, *, pad: str) -> str:
    """A one-pad foreign footprint centred at ``(BLOCKER_X, y)``."""
    return (
        '  (footprint "Test:Blocker"\n'
        '    (layer "F.Cu")\n'
        '    (uuid "fp-tp1")\n'
        f"    (at {BLOCKER_X:.3f} {y:.3f})\n"
        '    (property "Reference" "TP1" (at 0 0 0) (layer "F.SilkS") (uuid "ref-tp1"))\n'
        f"    {pad}\n"
        "  )"
    )


_TH_PAD = (
    f'(pad "1" thru_hole circle (at 0 0) (size {TH_PAD_SIZE} {TH_PAD_SIZE}) '
    f'(drill {TH_PAD_DRILL}) (layers "*.Cu" "*.Mask") (net 2 "SIG"))'
)
_SMD_PAD = (
    f'(pad "1" smd circle (at 0 0) (size {TH_PAD_SIZE} {TH_PAD_SIZE}) '
    '(layers "F.Cu") (net 2 "SIG"))'
)


@pytest.fixture
def board(tmp_path: Path):
    """Factory: write the fixture with ``blocker`` between the two fills."""

    def _make(blocker: str = "") -> Path:
        pcb = tmp_path / "hole_clearance.kicad_pcb"
        pcb.write_text(_FIXTURE.format(blocker=blocker))
        return pcb

    return _make


def _repair(mod, pcb: Path, capsys) -> tuple[tuple[int, int], str, list[str]]:
    """Run the repair; return ``((vias, bridges), stdout, added lines)``."""
    before = pcb.read_text().splitlines()
    counts = mod._repair_pour_connectivity(pcb, ["+1V2"])
    out = capsys.readouterr().out
    added = [line for line in pcb.read_text().splitlines() if line not in before]
    return counts, out, added


def _assert_bridged(mod, pcb: Path, counts, out: str, added: list[str]) -> None:
    """The two fills were joined by exactly the expected straight bridge."""
    assert counts == (0, 1)
    assert "UNREPAIRED" not in out
    segments = [line for line in added if "(segment " in line]
    assert len(segments) == 1 and not [line for line in added if "(via " in line]
    (x0, y0), (x1, y1) = BRIDGE
    assert f"(start {x0:.3f} {y0:.3f})" in segments[0]
    assert f"(end {x1:.3f} {y1:.3f})" in segments[0]
    assert '(layer "In1.Cu")' in segments[0]
    assert mod._audit_pour_nets(pcb, ["+1V2"])["+1V2"]["connected"]


def _assert_unrepaired(pcb: Path, counts, out: str, added: list[str], before: str) -> None:
    """No copper was emitted and the stage reported the island honestly."""
    assert counts == (0, 0)
    assert "UNREPAIRED: +1V2: cannot reconnect component (fill island)" in out
    assert added == []
    assert pcb.read_text() == before


# --- control: the fixture itself is repairable ----------------------------


def test_the_open_island_pair_bridges_straight_across_the_gap(generate_design_mod, board, capsys):
    """Baseline: with nothing in the gap the repair emits one bridge.

    Every rejection below is measured against this, so the fixture's own
    geometry can never be the reason a later case fails.
    """
    pcb = board()
    _assert_bridged(generate_design_mod, pcb, *_repair(generate_design_mod, pcb, capsys))


# --- foreign VIA drill (generate_design.py, the via-hole branch) ----------


def test_a_foreign_via_hole_inside_the_floor_blocks_every_bridge(
    generate_design_mod, board, capsys
):
    """RED before #5629: the bridge grazed the drill and was emitted anyway."""
    pcb = board(_via(VIA_BLOCKING_Y))
    before = pcb.read_text()
    _assert_unrepaired(pcb, *_repair(generate_design_mod, pcb, capsys), before)


def test_the_same_via_moved_just_outside_the_floor_does_not_block(
    generate_design_mod, board, capsys
):
    """The other half of the toggle: 0.09 mm further out and it bridges."""
    pcb = board(_via(VIA_CLEARING_Y))
    _assert_bridged(generate_design_mod, pcb, *_repair(generate_design_mod, pcb, capsys))


def test_only_the_drill_diameter_decides_at_the_blocking_position(
    generate_design_mod, board, capsys
):
    """Sharpest control: identical via *position and barrel*, smaller drill.

    Nothing the pre-#5608 predicate looked at changes between this case and
    the blocking one -- same centre, same ``(size ...)``, same net, same
    layers -- so only the new ``drill``-derived guard can account for the
    different verdict.
    """
    pcb = board(_via(VIA_BLOCKING_Y, drill=VIA_SMALL_DRILL))
    _assert_bridged(generate_design_mod, pcb, *_repair(generate_design_mod, pcb, capsys))


def test_a_same_net_via_hole_at_the_blocking_position_never_blocks(
    generate_design_mod, board, capsys
):
    """The guard is scoped to FOREIGN copper (``pnet != net``).

    A bridge may legally run up to its own net's drill; rejecting here
    would strand islands the repair is supposed to reconnect.
    """
    pcb = board(_via(VIA_BLOCKING_Y, net=1))
    _assert_bridged(generate_design_mod, pcb, *_repair(generate_design_mod, pcb, capsys))


# --- foreign THROUGH-HOLE PAD drill (the pad-hole branch) -----------------


def test_a_foreign_through_hole_pad_hole_inside_the_floor_blocks_every_bridge(
    generate_design_mod, board, capsys
):
    """RED before #5629, for the pad-drill branch of the same fix."""
    pcb = board(_pad_footprint(PAD_BLOCKING_Y, pad=_TH_PAD))
    before = pcb.read_text()
    _assert_unrepaired(pcb, *_repair(generate_design_mod, pcb, capsys), before)


def test_the_same_pad_moved_just_outside_the_floor_does_not_block(
    generate_design_mod, board, capsys
):
    """The pad-side toggle: 0.045 mm further out and it bridges."""
    pcb = board(_pad_footprint(PAD_CLEARING_Y, pad=_TH_PAD))
    _assert_bridged(generate_design_mod, pcb, *_repair(generate_design_mod, pcb, capsys))


def test_an_smd_pad_with_identical_copper_at_that_position_never_blocks(
    generate_design_mod, board, capsys
):
    """``is_th`` gating: an SMD pad has no hole, so it has no hole floor.

    Same centre and same copper footprint as the blocking through-hole pad,
    on F.Cu only -- the bridge runs on In1.Cu, which the copper guard
    correctly ignores.  A guard that fired on every pad, or one that read a
    drill an SMD pad does not have, would strand this island.
    """
    pcb = board(_pad_footprint(PAD_BLOCKING_Y, pad=_SMD_PAD))
    _assert_bridged(generate_design_mod, pcb, *_repair(generate_design_mod, pcb, capsys))


# --- the rejections above can have no other source ------------------------


def test_via_geometry_isolates_the_hole_floor_from_the_copper_floor():
    """The blocking via clears ``CLEAR`` on copper and only violates the hole.

    Pinning this in the test file (rather than only in a comment) means a
    future edit to ``VIA_*`` that accidentally lets the pre-existing barrel
    guard do the rejecting fails here, loudly, instead of quietly turning
    the two cases above into a no-op.
    """
    from shapely.geometry import LineString, Point

    path = LineString(BRIDGE).buffer(BRIDGE_W / 2.0)
    centre = Point(BLOCKER_X, VIA_BLOCKING_Y)
    assert path.distance(centre.buffer(VIA_SIZE / 2.0)) >= CLEAR
    assert path.distance(centre.buffer(VIA_DRILL / 2.0)) < HOLE_CLEAR

    clear_centre = Point(BLOCKER_X, VIA_CLEARING_Y)
    assert clear_centre.buffer(VIA_SIZE / 2.0).distance(path) >= CLEAR
    assert clear_centre.buffer(VIA_DRILL / 2.0).distance(path) >= HOLE_CLEAR
    # The small-drill control keeps the identical barrel, so the copper
    # guard's verdict is unchanged by construction.
    assert path.distance(centre.buffer(VIA_SMALL_DRILL / 2.0)) >= HOLE_CLEAR


def test_pad_geometry_isolates_the_hole_floor_from_the_copper_floor():
    """Same isolation for the pad branch, against the index's copper model.

    ``_repair_pour_connectivity`` models pad copper by the *circumscribed*
    circle (half the diagonal), so that -- not the pad's nominal size -- is
    what the hole floor has to out-reach here.
    """
    import math

    from shapely.geometry import LineString, Point

    path = LineString(BRIDGE).buffer(BRIDGE_W / 2.0)
    half_diag = math.hypot(TH_PAD_SIZE, TH_PAD_SIZE) / 2.0
    centre = Point(BLOCKER_X, PAD_BLOCKING_Y)
    assert path.distance(centre.buffer(half_diag)) >= CLEAR
    assert path.distance(centre.buffer(TH_PAD_DRILL / 2.0)) < HOLE_CLEAR

    clear_centre = Point(BLOCKER_X, PAD_CLEARING_Y)
    assert clear_centre.buffer(half_diag).distance(path) >= CLEAR
    assert clear_centre.buffer(TH_PAD_DRILL / 2.0).distance(path) >= HOLE_CLEAR
