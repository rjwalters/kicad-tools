"""Board06's pour repair must bridge to an existing same-net through-hole pad.

Issue #5606 (the Board06 port of Board07's #5564 fix), follow-on to the
barrel case in ``tests/test_board06_pour_repair_barrel_reuse.py`` (#5551).
``plan_via_hop_bridge`` learned to reuse an existing same-net element whose
plated barrel already spans the copper stack -- first a via barrel, now a
**through-hole pad** terminus.  A pad's ``kind`` string (``pad:<name>``)
cannot tell a through-hole pad from an SMD one, so the caller must state
plating explicitly (the ``th_pads`` set + ``_bridge_side()`` helper this
port adds); without it the planner demands a fresh drill beside the pad,
which ``_via_ok`` (no drill on any pad) and the drill-to-drill floor both
reject for every retreat point -- the component was printed
``UNREPAIRED`` even though the pad's barrel needs no new via at all.

The stranded component here is a fill island carrying no pad, so board06's
extra bounded ``pour_escape`` fallback does not run -- the via-hop stage is
the only strategy under test, exactly like the barrel case.

The fixture is constructed directly rather than regenerated from the board,
so it is a durable control independent of any retained artifact.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_DIR = REPO_ROOT / "boards" / "06-diffpair-test"

# Geometry of the constructed case, in board coordinates.  Board06's repair
# clamps candidates to the board outline (99.0 .. 197.5 by 48.0 .. 127.0),
# so the whole fixture sits well inside it.
TH_PAD = (150.900, 85.000)
ISLAND = (130.0, 82.0, 135.0, 88.0)


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
    gp = _load_module("board_06_generate_pcb_th", BOARD_DIR / "generate_pcb.py")
    sys.modules["generate_pcb"] = gp
    gs = _load_module("board_06_generate_schematic_th", BOARD_DIR / "generate_schematic.py")
    sys.modules["generate_schematic"] = gs
    return _load_module("board_06_generate_design_th", BOARD_DIR / "generate_design.py")


# A stranded ``+1V2`` fill island on In1.Cu, and a primary component whose
# only copper is a single same-net THROUGH-HOLE pad (a bulk-cap / connector
# power pad -- the everyday shape on a real board).  A foreign In1.Cu trace
# walls off the direct same-layer bridge, so the repair must hop layers, and
# the pad can host no new drill at all.
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
    (at 150.9 85)
    (property "Reference" "C40" (at 0 0 0) (layer "F.SilkS") (uuid "ref-c40"))
    (pad "1" thru_hole circle (at 0 0) (size 1.6 1.6) (drill 0.8) \
(layers "*.Cu" "*.Mask") (net 1 "+1V2"))
  )
  (segment (start 142.000 80.000) (end 142.000 90.000) (width 0.2) \
(layer "In1.Cu") (net 2) (uuid "seg-wall"))
  (zone (net 1) (net_name "+1V2") (layer "In1.Cu")
    (uuid "zone-island")
    (fill yes)
    (polygon
      (pts
        (xy 130 82)
        (xy 135 82)
        (xy 135 88)
        (xy 130 88)
      )
    )
    (filled_polygon
      (layer "In1.Cu")
      (pts
        (xy 130 82)
        (xy 135 82)
        (xy 135 88)
        (xy 130 88)
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
    """Restore the pre-#5606 behaviour: only a via barrel is reusable.

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


def test_the_reused_pad_keeps_its_own_drill_and_copper(generate_design_mod, board):
    """Preservation: reuse adds copper, it never rewrites the pad."""
    before = [line for line in board.read_text().splitlines() if "thru_hole" in line]
    generate_design_mod._repair_pour_connectivity(board, ["+1V2"])
    after = [line for line in board.read_text().splitlines() if "thru_hole" in line]
    assert before == after
    assert before and "(drill 0.8)" in before[0]
