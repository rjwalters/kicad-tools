"""Board06's pour repair must bridge to an existing same-net via barrel.

Issue #5551 (the Board06 port of the Board07 fix in #5507).
``boards/06-diffpair-test/generate_design.py``'s
``_repair_pour_connectivity`` ends its bridge ladder with a via-hop stage
that dropped a via inside *each* copper component and crossed the blockage
on another layer.  When the primary-side copper reachable from the stranded
component is an existing via barrel (plus a pad it cannot drill into), that
stage could never place its second via -- a drill inside the barrel violates
the drill-to-drill floor, and a drill outside it is not on the component --
so the component was printed ``UNREPAIRED`` and its pad stayed open.

The fixture below is the minimal constructed form of that topology (built
directly rather than by regenerating the board, so it is a durable control
independent of the committed routed artifact).  It is red on the pre-port
stage -- ``(0, 0)`` with an ``UNREPAIRED`` line -- and green once sub-stage D
delegates to :func:`kicad_tools.zones.pour_bridge.plan_via_hop_bridge`, with
exactly one new drill: the one on the stranded island's side.

The stranded component here carries no pad, so board06's extra bounded
``pour_escape`` fallback (which board07 lacks) does not run -- the via-hop
stage is the only strategy under test.
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
BARREL = (150.900, 85.000)
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
    gp = _load_module("board_06_generate_pcb_bridge", BOARD_DIR / "generate_pcb.py")
    sys.modules["generate_pcb"] = gp
    gs = _load_module("board_06_generate_schematic_bridge", BOARD_DIR / "generate_schematic.py")
    sys.modules["generate_schematic"] = gs
    return _load_module("board_06_generate_design_bridge", BOARD_DIR / "generate_design.py")


# A stranded ``+1V2`` fill island on In1.Cu, and a primary component whose
# only copper is a BGA pad, its 0.2 mm F.Cu tie and the stitching barrel at
# its end.  A foreign In1.Cu trace walls off the direct same-layer bridge, so
# the repair must hop layers.  Nothing in the primary component can host a new
# drill: the pad is under the via-in-pad ban and every point of the tie is
# inside the barrel's drill-to-drill exclusion.
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
  (footprint "Package_BGA:BGA"
    (layer "F.Cu")
    (uuid "fp-u4")
    (at 150 85)
    (property "Reference" "U4" (at 0 0 0) (layer "F.SilkS") (uuid "ref-u4"))
    (pad "C2" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "+1V2"))
  )
  (segment (start 150.000 85.000) (end 150.900 85.000) (width 0.2) \
(layer "F.Cu") (net 1) (uuid "seg-tie"))
  (segment (start 142.000 80.000) (end 142.000 90.000) (width 0.2) \
(layer "In1.Cu") (net 2) (uuid "seg-wall"))
  (via (at 150.900 85.000) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") \
(net 1) (uuid "via-stitch"))
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
    pcb = tmp_path / "barrel_reuse.kicad_pcb"
    pcb.write_text(_FIXTURE)
    return pcb


def test_island_reaches_the_existing_barrel_with_one_new_drill(generate_design_mod, board, capsys):
    before = board.read_text()
    vias, bridges = generate_design_mod._repair_pour_connectivity(board, ["+1V2"])
    assert (vias, bridges) == (1, 1)
    assert "UNREPAIRED" not in capsys.readouterr().out

    after = board.read_text()
    added = [line for line in after.splitlines() if line not in before.splitlines()]
    new_vias = [line for line in added if "(via " in line]
    new_segments = [line for line in added if "(segment " in line]
    assert len(new_vias) == 1 and len(new_segments) == 1

    # The single new drill is on the stranded island, not beside the barrel.
    x, y = (
        float(new_vias[0].split("(at ")[1].split()[0]),
        float(new_vias[0].split("(at ")[1].split()[1].rstrip(")")),
    )
    assert ISLAND[0] <= x <= ISLAND[2] and ISLAND[1] <= y <= ISLAND[3]

    # The bridge terminates on the existing barrel, which is reused in place.
    assert f"(end {BARREL[0]:.3f} {BARREL[1]:.3f})" in new_segments[0]

    # Nothing pre-existing was moved or deleted.
    for line in before.splitlines():
        assert line in after.splitlines()


def test_the_repaired_board_needs_no_second_repair_round(generate_design_mod, board, capsys):
    """The components really merged: a second pass finds nothing to do."""
    generate_design_mod._repair_pour_connectivity(board, ["+1V2"])
    capsys.readouterr()
    text = board.read_text()
    assert text.rstrip().endswith(")") and text.count("(kicad_pcb") == 1

    again = generate_design_mod._repair_pour_connectivity(board, ["+1V2"])
    assert again == (0, 0)
    assert "UNREPAIRED" not in capsys.readouterr().out
    assert board.read_text() == text
    assert generate_design_mod._audit_pour_nets(board, ["+1V2"])["+1V2"]["connected"]
