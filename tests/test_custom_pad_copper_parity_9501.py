"""Custom-pad primitive copper parity with the pinned image's KiCad (#9501).

``router.fixed_copper.custom_pad_copper`` now builds ``gr_circle`` primitives
(filled disc and stroked ring) and stroked ``gr_poly`` outlines (filled
outline buffered by half the stroke width, and unfilled outline bands).  The
area acceptance criterion (Issue #9501, AC2) is that each built shape matches
the pad polygon KiCad itself computes on the pinned image to within 1e-4 mm^2
at 0/90/180/30 degrees, front and back.

The oracle is the image's own ``pcbnew``: ``PAD::GetEffectivePolygon`` with
``m_MaxError`` forced to 1 nm, so KiCad's own tessellation is within ~1.5e-5
mm^2 of the analytic shape and the 1e-4 budget measures the builder, not the
oracle's chord error.  (The default 5000 nm error tessellates a 1.1 mm disc
40 times and is 1.6e-2 mm^2 from its own ideal -- far too coarse to be a
1e-4 reference.)

Requires the pinned image (``KCT_KICAD_DOCKER=1`` with docker) or an
interpreter that imports ``pcbnew`` (the slow-test workflow runs inside the
kicad/kicad container).  Skips everywhere else.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from pathlib import Path

import pytest

from kicad_tools.router.fixed_copper import custom_pad_copper

#: Probe placements: 4 rotations x front/back (Issue #9501 AC2).
PLACEMENTS = [
    pytest.param(0.0, "F", id="0-front"),
    pytest.param(90.0, "F", id="90-front"),
    pytest.param(180.0, "F", id="180-front"),
    pytest.param(30.0, "F", id="30-front"),
    pytest.param(0.0, "B", id="0-back"),
    pytest.param(90.0, "B", id="90-back"),
    pytest.param(180.0, "B", id="180-back"),
    pytest.param(30.0, "B", id="30-back"),
]

#: One fixture per newly supported primitive kind, each disjoint from the
#: 0.4 x 0.4 rect anchor and from the other primitives so the pad's copper
#: is exactly the anchor plus the sum of the primitives.
PRIMITIVE_CASES = {
    "filled_circle": "(gr_circle (center -6 -4) (end -5 -4) (width 0.2) (fill yes))",
    "ring_circle": "(gr_circle (center -6 4) (end -5 4) (width 0.15) (fill none))",
    "stroked_poly": (
        "(gr_poly (pts (xy 3 -3) (xy 5 -3) (xy 5 -1) (xy 3 -1)) (width 0.3) (fill yes))"
    ),
    "outlined_poly": "(gr_poly (pts (xy 3 2) (xy 5 2) (xy 4 4)) (width 0.25) (fill none))",
}

PAD_AT = "0.6 -0.4"

# The oracle worker, executed by the image's own python3 (which imports
# pcbnew).  Prints the pad's effective-polygon rings as JSON.
_ORACLE_WORKER = r"""
import json, sys
import pcbnew

board = pcbnew.LoadBoard(sys.argv[1])
# 1 nm: KiCad's own tessellation is then within ~1.5e-5 mm^2 of the analytic
# shape, so the 1e-4 budget measures the builder (module docstring).
board.GetDesignSettings().m_MaxError = 1
out = []
for fp in board.GetFootprints():
    for pad in fp.Pads():
        if pad.GetShape() != pcbnew.PAD_SHAPE_CUSTOM:
            continue
        poly = pad.GetEffectivePolygon(0)
        rings = []
        for i in range(poly.OutlineCount()):
            outline = poly.Outline(i)
            rings.append(
                [(outline.CPoint(j).x / 1e6, outline.CPoint(j).y / 1e6)
                 for j in range(outline.PointCount())]
            )
        out.append(rings)
print(json.dumps(out))
"""


def _oracle_command(stage: Path) -> list[str] | None:
    """Interpreter argv for the image's pcbnew python, or None when absent."""
    from kicad_tools.cli.kicad_toolchain import (
        docker_fallback_enabled,
        docker_kicad_cli_prefix,
        process_config_home,
    )
    from kicad_tools.zones.placement_fill import find_kicad_python

    if docker_fallback_enabled() and shutil.which("docker"):
        return docker_kicad_cli_prefix([stage], process_config_home(), entrypoint="python3")
    found = find_kicad_python(probe="import pcbnew")
    return [str(found)] if found is not None else None


def _pad_block(primitives: str, rotation: float, side: str) -> str:
    cu, mask = f"{side}.Cu", f"{side}.Mask"
    return (
        f'(pad "1" smd custom (at {PAD_AT} {rotation}) (size 0.4 0.4) '
        f'(layers "{cu}" "{mask}") (net 1 "GND") '
        f"(options (clearance outline) (anchor rect)) "
        f"(primitives {primitives}))"
    )


def _board_text(pad: str, rotation: float, side: str) -> str:
    cu, silk = f"{side}.Cu", f"{side}.SilkS"
    return f"""(kicad_pcb (version 20240108) (generator "test")
 (general (thickness 1.6))
 (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
 (setup (pad_to_mask_clearance 0))
 (net 0 "") (net 1 "GND")
 (gr_rect (start 90 90) (end 130 130) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
 (footprint "parity" (layer "{cu}") (at 110 110 {rotation})
  (property "Reference" "U1" (at 0 -3.5) (layer "{silk}"))
  {pad}))"""


def _oracle_pad_area(command: list[str], stage: Path, board: Path) -> float:
    """Board-frame copper area of the pad, per KiCad's own effective polygon."""
    worker = stage / "oracle_worker.py"
    worker.write_text(_ORACLE_WORKER)
    result = subprocess.run(
        [*command, str(worker), str(board)],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=600,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    pads = json.loads(result.stdout.strip().splitlines()[-1])
    assert pads, "the oracle found no custom pad on the board"
    assert len(pads) == 1, f"expected exactly one custom pad, found {len(pads)}"
    total = 0.0
    for ring in pads[0]:
        shoelace = 0.0
        for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1], strict=False):
            shoelace += x1 * y2 - x2 * y1
        # A hole is a reversed traversal inside the same outline, so the
        # signed shoelace nets it out of the pad's copper.
        total += shoelace / 2.0
    return abs(total)


@pytest.mark.slow
@pytest.mark.parametrize(("rotation", "side"), PLACEMENTS)
@pytest.mark.parametrize("case", sorted(PRIMITIVE_CASES), ids=sorted(PRIMITIVE_CASES))
def test_primitive_copper_matches_kicad_pad_polygon_area(tmp_path, rotation, side, case):
    """Each supported primitive's copper is within 1e-4 mm^2 of KiCad's (#9501).

    The board-frame placement (rotation, side, pad offset) goes through the
    whole ``custom_pad_copper`` transform, not just the local-frame builder.
    Area is the issue's acceptance metric, so it cannot by itself catch a
    mirrored or doubled rotation (those preserve area); it pins the builder's
    shape semantics -- radius, stroke growth, join rounding and hole -- which
    is what this issue changed.
    """
    command = _oracle_command(tmp_path)
    if command is None:
        pytest.skip("pinned-image pcbnew unavailable (KCT_KICAD_DOCKER=1 or KiCad python needed)")
    pad = _pad_block(PRIMITIVE_CASES[case], rotation, side)
    board = tmp_path / f"parity-{case}-{rotation}-{side}.kicad_pcb"
    board.write_text(_board_text(pad, rotation, side))

    # The pad's board position: its footprint-local offset rotated about the
    # footprint origin by KiCad's negated angle (the file already stores the
    # pad's ABSOLUTE angle, issue #3902).
    theta = math.radians(-rotation)
    px = 110 + 0.6 * math.cos(theta) - (-0.4) * math.sin(theta)
    py = 110 + 0.6 * math.sin(theta) + (-0.4) * math.cos(theta)
    copper = custom_pad_copper(
        pad,
        reference="U1",
        pad_number="1",
        x=px,
        y=py,
        rotation=rotation,
        source_net="GND",
        source_net_id=1,
    )
    oracle = _oracle_pad_area(command, tmp_path, board)
    assert abs(copper.geometry.area - oracle) < 1e-4, (
        f"{case} at {rotation} deg {side}: built {copper.geometry.area:.8f} mm^2, "
        f"kicad {oracle:.8f} mm^2, difference {abs(copper.geometry.area - oracle):.3e} mm^2"
    )
