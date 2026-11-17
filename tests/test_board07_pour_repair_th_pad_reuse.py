"""Board07's pour repair must bridge to an existing same-net through-hole pad.

Issue #5507, follow-on to the barrel case in
``tests/test_board07_pour_repair_barrel_reuse.py``.  ``plan_via_hop_bridge``
learned to reuse an existing via *barrel* rather than demand a second drill
beside it.  A same-net **through-hole pad** is the identical unsatisfiable
shape one element-kind over, for the identical two reasons:

* ``_via_ok`` bans a drill anywhere on a pad -- ``vgeom.intersects(geom)``
  returns False for same-net pads too -- and independently holds every new
  drill off that pad's own hole by the fab drill-to-drill floor.  So no
  retreat point on a pad can ever be drilled;
* a retreat point *off* the pad fails the "must land on the component" guard.

Every retreat on every layer is therefore rejected, and a component whose
nearest primary copper is a through-hole power pad was printed ``UNREPAIRED``
-- even though that pad's plated barrel already spans the copper stack and
needs no new via at all.  Board07's own ``GND`` carries five such pads.

An SMD pad stays non-reusable and is covered by the planner controls in
``tests/test_pour_bridge_via_reuse.py``: it reaches only its own layer, so
crossing a blockage on another layer genuinely needs a new drill elsewhere on
the component.

The fixture is constructed directly rather than regenerated from the board, so
it is a durable control independent of any retained artifact.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_DIR = REPO_ROOT / "boards" / "07-matchgroup-test"

# Geometry of the constructed case, in board coordinates.
TH_PAD = (150.900, 155.000)
ISLAND = (130.0, 152.0, 135.0, 158.0)


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
    """Load ``boards/07-matchgroup-test/generate_design.py`` as a module."""
    gp = _load_module("board_07_generate_pcb_th", BOARD_DIR / "generate_pcb.py")
    sys.modules["generate_pcb"] = gp
    gs = _load_module("board_07_generate_schematic_th", BOARD_DIR / "generate_schematic.py")
    sys.modules["generate_schematic"] = gs
    return _load_module("board_07_generate_design_th", BOARD_DIR / "generate_design.py")


# A stranded ``+1V2`` fill island on In1.Cu, and a primary component whose only
# copper is a single same-net THROUGH-HOLE pad (a bulk-cap / connector power
# pad -- the everyday shape on a real board).  A foreign In1.Cu trace walls off
# the direct same-layer bridge, so the repair must hop layers, and the pad can
# host no new drill at all.
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
  (footprint "Capacitor_THT:CP_Radial"
    (layer "F.Cu")
    (uuid "fp-c40")
    (at 150.9 155)
    (property "Reference" "C40" (at 0 0 0) (layer "F.SilkS") (uuid "ref-c40"))
    (pad "1" thru_hole circle (at 0 0) (size 1.6 1.6) (drill 0.8) \
(layers "*.Cu" "*.Mask") (net 1 "+1V2"))
  )
  (segment (start 142.000 150.000) (end 142.000 160.000) (width 0.2) \
(layer "In1.Cu") (net 2) (uuid "seg-wall"))
  (zone (net 1) (net_name "+1V2") (layer "In1.Cu")
    (uuid "zone-island")
    (fill yes)
    (polygon
      (pts
        (xy 130 152)
        (xy 135 152)
        (xy 135 158)
        (xy 130 158)
      )
    )
    (filled_polygon
      (layer "In1.Cu")
      (pts
        (xy 130 152)
        (xy 135 152)
        (xy 135 158)
        (xy 130 158)
      )
    )
  )
)
"""


@pytest.fixture
def board(tmp_path: Path) -> Path:
    pcb = tmp_path / "th_pad_reuse.kicad_pcb"
    pcb.write_text(_FIXTURE)
    return pcb


def _legacy_bridge_side(monkeypatch) -> None:
    """Restore the pre-fix behaviour: only a via barrel is reusable.

    The recipe imports ``BridgeSide`` from the shared module inside
    ``_repair_pour_connectivity``, so dropping the ``plated_through`` flag
    here reproduces exactly the stage as it behaved before this change --
    every other physical predicate, budget and candidate order unchanged.
    """
    import kicad_tools.zones.pour_bridge as pour_bridge

    real = pour_bridge.BridgeSide

    def without_pad_plating(geometry, layers, kind, plated_through=None):
        return real(geometry, layers, kind)

    monkeypatch.setattr(pour_bridge, "BridgeSide", without_pad_plating)


def test_a_through_hole_pad_destination_is_unrepairable_without_reuse(
    generate_design_mod, board, capsys, monkeypatch
):
    """Red control: demanding a second drill cannot clear this pair."""
    before = board.read_text()
    _legacy_bridge_side(monkeypatch)

    assert generate_design_mod._repair_pour_connectivity(board, ["+1V2"]) == (0, 0)
    # The stage gave up on the fill island and emitted no copper at all.
    assert "UNREPAIRED: +1V2: cannot reconnect component (fill island)" in capsys.readouterr().out
    assert board.read_text() == before


def test_island_reaches_the_through_hole_pad_with_one_new_drill(generate_design_mod, board, capsys):
    before = board.read_text()
    vias, bridges = generate_design_mod._repair_pour_connectivity(board, ["+1V2"])
    assert (vias, bridges) == (1, 1)
    assert "UNREPAIRED" not in capsys.readouterr().out

    after = board.read_text()
    added = [line for line in after.splitlines() if line not in before.splitlines()]
    new_vias = [line for line in added if "(via " in line]
    new_segments = [line for line in added if "(segment " in line]
    assert len(new_vias) == 1 and len(new_segments) == 1

    # The single new drill is on the stranded island, never beside the pad.
    x, y = (
        float(new_vias[0].split("(at ")[1].split()[0]),
        float(new_vias[0].split("(at ")[1].split()[1].rstrip(")")),
    )
    assert ISLAND[0] <= x <= ISLAND[2] and ISLAND[1] <= y <= ISLAND[3]

    # The bridge terminates on the existing pad, which is reused in place.
    assert f"(end {TH_PAD[0]:.3f} {TH_PAD[1]:.3f})" in new_segments[0]

    # Nothing pre-existing was moved or deleted.
    for line in before.splitlines():
        assert line in after.splitlines()


def test_the_repaired_board_is_connected_and_needs_no_second_round(
    generate_design_mod, board, capsys
):
    """The components really merged: a second pass finds nothing to do."""
    generate_design_mod._repair_pour_connectivity(board, ["+1V2"])
    capsys.readouterr()
    text = board.read_text()
    assert text.rstrip().endswith(")") and text.count("(kicad_pcb") == 1

    again = generate_design_mod._repair_pour_connectivity(board, ["+1V2"])
    assert again == (0, 0)
    assert "UNREPAIRED" not in capsys.readouterr().out
    assert board.read_text() == text

    # ``_audit_pour_nets`` only partitions PADS, and this constructed case has
    # a single pad, so its verdict is vacuous here -- the real evidence that
    # the two components merged is the clean second pass above, which runs the
    # stage's own union-find over every copper element.
    audit = generate_design_mod._audit_pour_nets(board, ["+1V2"])["+1V2"]
    assert audit["connected"]


def test_the_reused_pad_keeps_its_own_drill_and_copper(generate_design_mod, board):
    """Preservation: reuse adds copper, it never rewrites the pad."""
    before = [line for line in board.read_text().splitlines() if "thru_hole" in line]
    generate_design_mod._repair_pour_connectivity(board, ["+1V2"])
    after = [line for line in board.read_text().splitlines() if "thru_hole" in line]
    assert before == after
    assert before and "(drill 0.8)" in before[0]
