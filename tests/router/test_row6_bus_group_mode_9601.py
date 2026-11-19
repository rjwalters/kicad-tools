"""Row 6 (issue #9601, PCBJam audit): a group mode for routing N nets
together already exists and routes detected bus members as a group.

The audit row reported "no way to route N nets as a group of similar length".
The grouping half is refuted as filed for *named* buses: ``kct route
--strategy basic --bus-routing`` (``router/bus.py`` detection +
``router/bus_routing.py`` coordination) detects bus members by naming
convention and routes each group together before the non-bus nets.
``--strategy basic`` is required: the bus driver
(``BusRouter.route_all_with_buses``) is dispatched only on the basic strategy
(``route_cmd.py``), so under the default ``negotiated`` strategy
``--bus-routing`` only changes the option banner.  This test pins that
capability end-to-end: a DATA[0..3] bus on a small synthetic board must route
completely with bus mode enabled, and the run log must name the bus phase and
the per-group routing line.

The length-window grouping (similar-length nets that do not share a naming
convention) and the escape-order choice are NOT part of the existing mode;
those stay with the row-6 child issue (a new mode must not be built here).
"""

from __future__ import annotations

import contextlib
import io
import re
from pathlib import Path

from kicad_tools.cli import route_cmd

BUS_NETS = ("DATA[0]", "DATA[1]", "DATA[2]", "DATA[3]")


def _board_text() -> str:
    # U1: four bus pads in a row on F.Cu; U2: the four partners 20mm south.
    u1 = "\n".join(
        f'\t\t(pad "P{i}" smd rect (at {8.0 + i * 1.2:.2f} 6.00) (size 0.5 0.5) '
        f'(layers "F.Cu" "F.Paste" "F.Mask") (net {i + 1} "{name}"))'
        for i, name in enumerate(BUS_NETS)
    )
    u2 = "\n".join(
        f'\t\t(pad "P{i}" smd rect (at {8.0 + i * 1.2:.2f} 26.00) (size 0.5 0.5) '
        f'(layers "F.Cu" "F.Paste" "F.Mask") (net {i + 1} "{name}"))'
        for i, name in enumerate(BUS_NETS)
    )
    nets = "\n".join(f'\t(net {i + 1} "{name}")' for i, name in enumerate(BUS_NETS))
    return f"""(kicad_pcb
	(version 20240108)
	(generator "row6_audit_fixture")
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
{nets}

	(gr_rect (start 0 0) (end 30 32) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts") (uuid "edge"))

	(footprint "Audit:U1"
		(layer "F.Cu")
		(uuid "fp-u1")
		(at 0 0)
		(attr smd)
		(fp_text reference "U1" (at 8 4) (layer "F.SilkS"))
		(fp_text value "BUS" (at 8 7) (layer "F.Fab"))
{u1}
	)

	(footprint "Audit:U2"
		(layer "F.Cu")
		(uuid "fp-u2")
		(at 0 0)
		(attr smd)
		(fp_text reference "U2" (at 8 24) (layer "F.SilkS"))
		(fp_text value "BUS" (at 8 27) (layer "F.Fab"))
{u2}
	)
)
"""


def test_bus_group_mode_routes_all_bus_members(tmp_path: Path):
    source = tmp_path / "bus.kicad_pcb"
    source.write_text(_board_text())
    output = tmp_path / "bus_routed.kicad_pcb"

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = route_cmd._in_process_main(
            [
                str(source),
                "-o",
                str(output),
                "--no-auto-layers",
                "--strategy",
                "basic",
                "--bus-routing",
                "--bus-mode",
                "parallel",
            ]
        )
    assert rc == 0, f"route --bus-routing failed with exit {rc}"

    # The bus phase ran (header + bus-aware routing markers in the log).
    log = buf.getvalue()
    assert re.search(r"Bus:\s+enabled", log), f"bus mode header missing from:\n{log}"
    # The bus-group router itself ran: route_all_with_buses prints one line
    # per detected group before routing it (bus_routing.py).  Under the
    # default negotiated strategy this line never appears -- the negotiated
    # dispatch never reaches the bus driver.
    assert re.search(r"Routing bus DATA\[3:0\] \(parallel mode, spacing=[0-9.]+mm\)", log), (
        f"bus-group routing line missing from:\n{log}"
    )

    # Every bus member reached its partner: each net has committed copper.
    text = output.read_text()
    seg_re = re.compile(r"\(segment\s.*?\(net (\d+)\)", re.S)
    routed_net_ids = {int(m.group(1)) for m in seg_re.finditer(text)}
    for net_id, name in enumerate(BUS_NETS, start=1):
        assert net_id in routed_net_ids, f"bus net {net_id} ({name}) left unrouted"
