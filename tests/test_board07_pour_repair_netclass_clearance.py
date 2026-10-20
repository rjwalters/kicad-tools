"""Board07's pour repair must hold its copper off foreign pads at the net-class clearance.

Issue #6736.  After PR #6713 a fresh Board 07 regen shipped one new kicad-cli
``clearance`` error::

    Clearance violation (rule 'Reviewed clearance - Power' clearance 0.2000 mm;
    actual 0.1913 mm)
      Track [+1V2] on F.Cu  (118.316, 60.496) -> (117.835, 60.977)
      Pad 24 [GND] of U1 on F.Cu  (117.9, 60.0)

That diagonal leg is not router copper: it is the 45-degree dogleg of a
step-7 pour-repair bridge (``_repair_pour_connectivity``, emitted through
``_emit_seg_45``).  The stage validated every stub / bridge leg against a
FLAT 0.15 mm (``CLEAR``) instead of the pour net's class clearance (Power,
0.2 mm, the value KiCad enforces), and measured foreign pads by their
circumscribed circle.  At a pad corner on a 45-degree leg that circle is the
pad, so the leg cleared 0.15 mm and shipped at 0.1913 mm.

The fixture is constructed directly: one stranded ``+1V2`` SMD pad, a
``+1V2`` In1.Cu fill strip (anchored by a through-hole ``+1V2`` pad so it is
the primary copper) that only the pad's 45-degree via candidates can reach,
and a ``GND`` pad (Power class, like ``+1V2``) beside that diagonal.  The
first candidate the stage prefers has a via that is legal on its own, but
its diagonal stub passes the GND pad's corner at between 0.15 and 0.2 mm.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

from kicad_tools.router.clearance_kernel import KSegment, KVia, copper_gap, make_pad

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_DIR = REPO_ROOT / "boards" / "07-matchgroup-test"

#: The Power class clearance of ``+1V2`` and ``GND`` (``NET_CLASS_POWER``).
POWER_CLEARANCE = 0.2
#: The stage's old flat clearance -- the gap the violating stub DOES clear.
OLD_FLAT_CLEARANCE = 0.15

STRANDED = (140.0, 140.0)
#: ``(shape, rratio, centre)`` of the foreign GND pad, per variant.  Both sit
#: beside the stranded pad's 45-degree stub, with their nearest corner facing
#: it: the circumscribed circle clears the stub by >= 0.15 mm, the true copper
#: by < 0.2 mm.  ``rect`` is Board 07's own U1 land; ``roundrect`` sits closer
#: by the corner relief its 0.075 mm radius buys.
GND_PADS = {
    "rect": ("rect", 0.25, (140.825, 140.165)),
    "roundrect": ("roundrect", 0.25, (140.8075, 140.1825)),
}
GND_SIZE = 0.3


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
    gp = _load_module("board_07_generate_pcb_6736", BOARD_DIR / "generate_pcb.py")
    sys.modules["generate_pcb"] = gp
    gs = _load_module("board_07_generate_schematic_6736", BOARD_DIR / "generate_schematic.py")
    sys.modules["generate_schematic"] = gs
    return _load_module("board_07_generate_design_6736", BOARD_DIR / "generate_design.py")


def _fixture(shape: str, rratio: float, centre: tuple[float, float]) -> str:
    gx, gy = centre[0] - STRANDED[0], centre[1] - STRANDED[1]
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
  (net 2 "GND")
  (footprint "Test:Lands"
    (layer "F.Cu")
    (uuid "fp-u1")
    (at {STRANDED[0]} {STRANDED[1]})
    (property "Reference" "U1" (at 0 0 0) (layer "F.SilkS") (uuid "ref-u1"))
    (pad "36" smd circle (at 0 0) (size 0.25 0.25) (layers "F.Cu" "F.Mask") \
(net 1 "+1V2"))
    (pad "24" smd {shape} (at {gx:.4f} {gy:.4f}) (size {GND_SIZE} {GND_SIZE}) \
(layers "F.Cu" "F.Mask") (roundrect_rratio {rratio}) (net 2 "GND"))
  )
  (footprint "Capacitor_THT:CP_Radial"
    (layer "F.Cu")
    (uuid "fp-c1")
    (at 150 150)
    (property "Reference" "C1" (at 0 0 0) (layer "F.SilkS") (uuid "ref-c1"))
    (pad "1" thru_hole circle (at 0 0) (size 1.0 1.0) (drill 0.5) \
(layers "*.Cu" "*.Mask") (net 1 "+1V2"))
  )
  (zone (net 1) (net_name "+1V2") (layer "In1.Cu")
    (uuid "zone-strip")
    (fill yes)
    (polygon
      (pts (xy 140.96 140.99) (xy 140.99 140.96) (xy 150.03 150) (xy 150 150.03))
    )
    (filled_polygon
      (layer "In1.Cu")
      (pts (xy 140.96 140.99) (xy 140.99 140.96) (xy 150.03 150) (xy 150 150.03))
    )
  )
)
"""


def _gnd_pad(variant: str):
    shape, rratio, (cx, cy) = GND_PADS[variant]
    return make_pad(shape, GND_SIZE, GND_SIZE, rratio, 0.0, cx, cy)


def _new_copper(before: str, after: str) -> tuple[list[KSegment], list[KVia]]:
    added = [line for line in after.splitlines() if line not in before.splitlines()]
    num = r"(-?[\d.]+)"
    segments = [
        KSegment(float(m[1]), float(m[2]), float(m[3]), float(m[4]), float(m[5]))
        for line in added
        if (m := re.search(rf"\(start {num} {num}\) \(end {num} {num}\) \(width {num}\)", line))
        and '"F.Cu"' in line
    ]
    vias = [
        KVia(float(m[1]), float(m[2]), float(m[3]))
        for line in added
        if (m := re.search(rf"\(via \(at {num} {num}\) \(size {num}\)", line))
    ]
    return segments, vias


@pytest.mark.parametrize("variant", sorted(GND_PADS))
def test_the_fixture_reproduces_the_issue_geometry(variant):
    """Control: the preferred stub clears the old model but not the Power rule."""
    pad = _gnd_pad(variant)
    stub = KSegment(*STRANDED, 140.99, 140.99, 0.15)
    # The via at the end of the stub is legal on its own ...
    assert copper_gap(KVia(140.99, 140.99, 0.45), pad) >= POWER_CLEARANCE
    # ... but its diagonal stub passes the GND pad's corner too close.
    assert OLD_FLAT_CLEARANCE <= copper_gap(stub, pad) < POWER_CLEARANCE


@pytest.mark.parametrize("variant", sorted(GND_PADS))
def test_repair_copper_keeps_the_power_clearance_from_a_foreign_pad(
    generate_design_mod, tmp_path, variant, capsys
):
    board = tmp_path / f"netclass_clearance_{variant}.kicad_pcb"
    board.write_text(_fixture(*GND_PADS[variant]))
    before = board.read_text()

    vias, _bridges = generate_design_mod._repair_pour_connectivity(board, ["+1V2"])
    capsys.readouterr()

    segments, new_vias = _new_copper(before, board.read_text())
    # The stranded pad still gets its repair: the fix relocates, never drops.
    assert vias >= 1 and len(new_vias) == vias
    assert segments, "the stage emitted no F.Cu stub for the stranded pad"

    pad = _gnd_pad(variant)
    for seg in segments:
        gap = copper_gap(seg, pad)
        assert gap >= POWER_CLEARANCE - 1e-4, (
            f"repair leg ({seg.x1}, {seg.y1}) -> ({seg.x2}, {seg.y2}) is {gap:.4f} mm "
            f"from GND U1.24 (Power clearance {POWER_CLEARANCE} mm)"
        )
    for via in new_vias:
        assert copper_gap(via, pad) >= POWER_CLEARANCE - 1e-4


def test_a_legal_diagonal_stub_is_still_taken(generate_design_mod, tmp_path, capsys):
    """The fix must not drop legal sites: with the GND pad moved off, the
    preferred 45-degree candidate onto the primary strip is placed as before."""
    board = tmp_path / "legal.kicad_pcb"
    board.write_text(_fixture("rect", 0.25, (141.2, 139.8)))
    before = board.read_text()

    assert generate_design_mod._repair_pour_connectivity(board, ["+1V2"]) == (1, 0)
    assert "UNREPAIRED" not in capsys.readouterr().out
    segments, new_vias = _new_copper(before, board.read_text())
    assert [(v.x, v.y) for v in new_vias] == [(140.99, 140.99)]
    assert [(s.x1, s.y1, s.x2, s.y2) for s in segments] == [(140.0, 140.0, 140.99, 140.99)]
