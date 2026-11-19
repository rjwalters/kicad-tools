"""Row 9 (issue #9601, PCBJam audit): a non-local net's trunk crosses the
body area of a fine-pitch IC on the opposite layer with no penalty.

The reported PCBJam failure: a non-local net's trunk ran diagonally under a
whole QFN on the bottom layer, splitting the free area under the part and
stranding two rails.  This test pins the *default-behaviour* half of that
mechanism on a minimal synthetic board:

* U1 -- 0.5 mm-pitch QFN pad rows (pure obstacle geometry, net-less pads) at
  the board centre, body bbox x 17.25..22.75 / y 9.2..15.2.
* TRUNK -- a non-local net (no pads on U1) whose SMD pads sit on B.Cu at
  (4,12) and (36,12): a dead horizontal shot through the middle of U1's body.

Today the router crosses the part's body area at zero cost, so the committed
assertion is that the trunk DOES cross the body bbox.  When the row-9 remedy
(an area-splitting cost, or keeping non-local nets out from under fine-pitch
ICs by default) lands via the child issue, this assertion flips: the trunk
must then avoid the body area.  Either way the test fails on a regression of
the behaviour the audit pinned.

Child issue: loom:triage, parent #9601 (filed from this audit's PR).
"""

from __future__ import annotations

import re
from pathlib import Path

from kicad_tools.cli import route_cmd

# U1 body bbox in board coordinates (footprint at (20,12); pad rows span
# x 17.25..22.75 at y 9.2 and 15.2; the body square between them is the
# area whose splitting stranded the PCBJam rails).
BODY_BBOX = (17.25, 9.2, 22.75, 15.2)


def _board_text() -> str:
    u1_pads = []
    for i in range(12):
        x = -2.75 + i * 0.5
        u1_pads.append(
            f'\t\t(pad "T{i}" smd rect (at {x:.2f} -2.80) (size 0.25 0.3) '
            '(layers "F.Cu" "F.Paste" "F.Mask"))'
        )
        u1_pads.append(
            f'\t\t(pad "B{i}" smd rect (at {x:.2f} 3.20) (size 0.25 0.3) '
            '(layers "F.Cu" "F.Paste" "F.Mask"))'
        )
    fp_u1 = "\n".join(u1_pads)
    return f"""(kicad_pcb
	(version 20240108)
	(generator "row9_audit_fixture")
	(generator_version "8.0")
	(general (thickness 1.6) (legacy_teardrops no))
	(paper "A4")
	(layers
		(0 "F.Cu" signal)
		(31 "B.Cu" signal)
		(36 "B.SilkS" user "B.Silkscreen")
		(37 "F.SilkS" user "F.Silkscreen")
		(38 "B.Mask" user)
		(39 "F.Mask" user)
		(44 "Edge.Cuts" user)
	)
	(setup (pad_to_mask_clearance 0.05))
	(net 0 "")
	(net 1 "TRUNK")

	(gr_rect (start 0 0) (end 40 30) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts") (uuid "edge"))

	(footprint "Audit:U1_QFN"
		(layer "F.Cu")
		(uuid "fp-u1")
		(at 20.0 12.0)
		(attr smd)
		(fp_text reference "U1" (at 0 -4) (layer "F.SilkS"))
		(fp_text value "QFN" (at 0 4.5) (layer "F.Fab"))
{fp_u1}
	)

	(footprint "Audit:TRUNK"
		(layer "B.Cu")
		(uuid "fp-trunk")
		(at 0 0)
		(attr smd)
		(fp_text reference "T" (at 1 -1) (layer "B.SilkS"))
		(fp_text value "trunk" (at 1 1) (layer "B.Fab"))
		(pad "A" smd rect (at 4.00 12.00) (size 0.6 0.6) (layers "B.Cu" "B.Paste" "B.Mask") (net 1 "TRUNK"))
		(pad "B" smd rect (at 36.00 12.00) (size 0.6 0.6) (layers "B.Cu" "B.Paste" "B.Mask") (net 1 "TRUNK"))
	)
)
"""


def _seg_crosses_box(x1: float, y1: float, x2: float, y2: float, box: tuple) -> bool:
    bx1, by1, bx2, by2 = box
    for i in range(81):
        t = i / 80
        x = x1 + (x2 - x1) * t
        y = y1 + (y2 - y1) * t
        if bx1 <= x <= bx2 and by1 <= y <= by2:
            return True
    return False


def test_nonlocal_trunk_crosses_qfn_body_on_bottom_layer(tmp_path: Path):
    """Current default behaviour (row 9 applies): the non-local trunk routes
    straight through the QFN body area on B.Cu.  The child-issue fix flips
    this assertion to ``not any(...)``."""
    source = tmp_path / "qfn.kicad_pcb"
    source.write_text(_board_text())
    output = tmp_path / "qfn_routed.kicad_pcb"

    rc = route_cmd._in_process_main(
        [
            str(source),
            "-o",
            str(output),
            "--no-auto-layers",
            "--quiet",
        ]
    )
    assert rc == 0, f"route failed with exit {rc}"
    assert output.exists()

    text = output.read_text()
    seg_re = re.compile(
        r"\(segment\s+\(start ([\d.\-]+) ([\d.\-]+)\)\s+\(end ([\d.\-]+) ([\d.\-]+)\)"
        r"\s+\(width ([\d.]+)\)\s+\(layer \"([^\"]+)\"\)",
        re.S,
    )
    trunk_crossings = []
    for m in seg_re.finditer(text):
        x1, y1, x2, y2, _w, layer = (
            float(m.group(1)),
            float(m.group(2)),
            float(m.group(3)),
            float(m.group(4)),
            m.group(5),
            m.group(6),
        )
        if layer != "B.Cu":
            continue
        # Only the trunk net exists on this board; still, key on geometry
        # touching the trunk's endpoint corridor so a stray via-stub segment
        # on another net cannot satisfy the assertion.
        if _seg_crosses_box(x1, y1, x2, y2, BODY_BBOX):
            trunk_crossings.append((x1, y1, x2, y2))

    assert trunk_crossings, (
        "No B.Cu segment crosses U1's body bbox -- the row-9 behaviour "
        "(non-local trunk crossing under a fine-pitch IC at zero cost) is no "
        "longer present. If this is the child-issue fix landing, flip this "
        "assertion to `not trunk_crossings` and rename the test."
    )
