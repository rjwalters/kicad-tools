"""The recipe repair pass must keep its offset vias out of the same-net slit band.

Issue #8975, extending #8957's ``same_net_pad_gap_acceptable`` predicate
(``src/kicad_tools/router/via_clearance.py``) into both recipes' step-7
``_repair_pour_connectivity`` ``_via_ok``.  A repair via seated a hair
outside its own same-net SMD pad leaves two wedge-shaped notches of air
beside the connecting stub -- in fab view a ``physical_copper_gap`` error
regardless of nets (#8938): the parent measured 93 such findings on a fresh
board 07 regen (81 at the stamped 0.0891 mm gap) and 17 on board 06, all on
the d0.25/s0.45 repair vias.

The fixture is constructed directly (the durable-control pattern of
``tests/test_board06_pour_repair_netclass_clearance.py``): a stranded
target pad whose width is tuned so the FIRST seat the pre-band ladder
accepts -- the east 0.55 mm rung -- sits inside the slit band (gap
0.0891 mm, the parent's stamped finding geometry: a 0.47 mm pad escapes
the half-diagonal via-in-pad obstacle circle at 0.55 mm but its true copper
is only 0.0891 mm from the annulus).  The band test must relocate the via
to the next rung (east 0.65, gap 0.19 mm), not drop the pad: the audit
must still report the net connected after the repair.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Target pad: 0.47 x 0.4 rect at the origin footprint.  half_diag =
#: hypot(0.47, 0.4)/2 ~= 0.3084, so the via-in-pad obstacle circle reaches
#: 0.225 + 0.3084 = 0.5334 mm and the 0.55 rung escapes it; the TRUE copper
#: half-width is 0.235, so the 0.55 seat's fab gap is
#: 0.55 - 0.235 - 0.225 = 0.0891 mm -- the parent's stamped finding, inside
#: the (0, 0.15) band.  The 0.65 rung's gap is 0.19 mm, beyond the edge.
TARGET_SIZE = (0.47, 0.4)
TARGET_AT = (0.0, 0.0)
#: Primary pad (smaller, same net) 10 mm east; listed FIRST in the footprint
#: so its component sorts as the repair's primary.
PRIMARY_AT = (10.0, 0.0)

BOARD06_DIR = REPO_ROOT / "boards" / "06-diffpair-test"
BOARD07_DIR = REPO_ROOT / "boards" / "07-matchgroup-test"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - import guard
        raise ImportError(f"Cannot load module {name!r} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def board06_mod():
    gp = _load_module("board_06_generate_pcb_band", BOARD06_DIR / "generate_pcb.py")
    sys.modules["generate_pcb"] = gp
    gs = _load_module("board_06_generate_schematic_band", BOARD06_DIR / "generate_schematic.py")
    sys.modules["generate_schematic"] = gs
    return _load_module("board_06_generate_design_band", BOARD06_DIR / "generate_design.py")


@pytest.fixture(scope="module")
def board07_mod():
    gp = _load_module("board_07_generate_pcb_band", BOARD07_DIR / "generate_pcb.py")
    sys.modules["generate_pcb"] = gp
    gs = _load_module("board_07_generate_schematic_band", BOARD07_DIR / "generate_schematic.py")
    sys.modules["generate_schematic"] = gs
    return _load_module("board_07_generate_design_band", BOARD07_DIR / "generate_design.py")


def _fixture(origin_x: float, origin_y: float) -> str:
    """Stranded target pad + far same-net primary pad, nothing else."""
    px, py = PRIMARY_AT
    return f"""(kicad_pcb
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
  (footprint "Test:Lands"
    (layer "F.Cu")
    (uuid "fp-band")
    (at {origin_x} {origin_y})
    (property "Reference" "U9" (at 0 0 0) (layer "F.SilkS") (uuid "ref-band"))
    (pad "1" smd circle (at {px:.4f} {py:.4f}) (size 0.3 0.3) (layers "F.Cu" "F.Mask") \
(net 1 "+1V2"))
    (pad "2" smd rect (at {TARGET_AT[0]:.4f} {TARGET_AT[1]:.4f}) \
(size {TARGET_SIZE[0]} {TARGET_SIZE[1]}) (layers "F.Cu" "F.Mask") (net 1 "+1V2"))
  )
)
"""


def _emitted_vias(text: str) -> list[tuple[float, float]]:
    """Centres of the vias the repair appended (3-decimal, as written)."""
    out = []
    for line in text.splitlines():
        m = re.search(r"\(via \(at (-?[\d.]+) (-?[\d.]+)\)", line)
        if m:
            out.append((float(m.group(1)), float(m.group(2))))
    return out


def _repair_and_find_target_via(mod, tmp_path: Path, origin: tuple[float, float]):
    pcb = tmp_path / "band.kicad_pcb"
    pcb.write_text(_fixture(*origin))
    mod._repair_pour_connectivity(pcb, ["+1V2"])
    after = pcb.read_text()
    added = _emitted_vias(after)
    audit = mod._audit_pour_nets(pcb, ["+1V2"])["+1V2"]
    return after, added, audit


@pytest.mark.parametrize(
    ("mod_fixture", "board_dir", "origin"),
    [
        ("board06_mod", BOARD06_DIR, (140.0, 90.0)),
        ("board07_mod", BOARD07_DIR, (140.0, 150.0)),
    ],
)
def test_repair_via_relocates_out_of_the_same_net_slit_band(
    mod_fixture, board_dir, origin, request, tmp_path
):
    mod = request.getfixturevalue(mod_fixture)
    tx, ty = origin
    after, added, audit = _repair_and_find_target_via(mod, tmp_path, origin)

    # The pad is repaired, not dropped: the copper-union audit connects it.
    assert audit["connected"], f"pad_groups={audit['pad_groups']}"
    assert not audit["stranded_pads"]

    # The pre-band ladder accepted the east 0.55 rung -- the parent's
    # stamped 0.0891 mm finding seat.  It must not be emitted any more.
    pre_band_seat = (tx + 0.55, ty)
    assert pre_band_seat not in added, (
        f"repair via seated in the same-net slit band (gap 0.0891 mm from its own pad): {added}"
    )

    # The first band-legal rung (east 0.65, fab gap 0.19 mm) is where the
    # via lands, with its stub walking out to it.
    assert (tx + 0.65, ty) in added, f"expected the 0.65 mm rung seat, got {added}"
    assert f"(start {tx:.3f} {ty:.3f})" in after and f"(end {tx + 0.65:.3f} {ty:.3f})" in after, (
        "pad-to-via stub missing"
    )

    # Independent witness: the emitted annulus keeps >= the resolved pad
    # error edge (0.15 mm) of true-shape pad copper.
    from shapely.geometry import Point

    via = Point(tx + 0.65, ty).buffer(0.225)
    pad_copper = Point(tx, ty).buffer(0.235)  # rect 0.47 wide -> half-width 0.235
    assert via.distance(pad_copper) >= 0.15 - 1e-9


def test_find_escape_honours_the_via_ok_seat_predicate(tmp_path):
    """``find_escape``'s new ``via_ok`` hook gates through-via nodes.

    The corridor scenario from ``tests/test_board06_pour_escape.py``: with
    the seat predicate rejecting every node the search fails closed (no
    partial path), and with it scoped to a region the default seat leaves,
    the returned via sits inside the accepted region.
    """
    spec = importlib.util.spec_from_file_location(
        "board06_pour_escape_band", BOARD06_DIR / "pour_escape.py"
    )
    escape = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = escape
    spec.loader.exec_module(escape)

    from shapely.geometry import Point, box

    start = (0, 0)
    pads = [(Point(start).buffer(0.15), "GND", {"F.Cu"}, 0, start)]
    segments = [
        (box(-1, 0.41, 18, 1), "other", "F.Cu"),
        (box(-1, -1, 18, -0.41), "other", "F.Cu"),
    ]
    primary = [(box(19, -2, 21, 2), {"In1.Cu"}, "fill")]
    args = (start, "GND", "F.Cu", pads, segments, [], primary, (-0.5, -0.5, 21, 0.5))

    default = escape.find_escape(*args, rules=escape.EscapeRules())
    assert default is not None and default.via

    # Reject every seat: no escape at all, never a partial path.
    assert escape.find_escape(*args, rules=escape.EscapeRules(), via_ok=lambda p: False) is None

    # Accept only seats in the primary region: the committed via satisfies it.
    scoped = escape.find_escape(*args, rules=escape.EscapeRules(), via_ok=lambda p: p[0] >= 19.0)
    assert scoped is not None and scoped.via
    assert scoped.via and scoped.points[-1][0] >= 19.0
