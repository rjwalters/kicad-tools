"""Issue #7708: auto-pour on a pre-KiCad-6 board with ``Top``/``VDD``/``Bottom`` copper.

``PCB.copper_layers`` counts legacy-named signal layers, so the pour allocator
planned an ``In1.Cu`` zone that ``add_zone`` rejected with ``Layer 'In1.Cu' is
not available ... Declared copper layers: .`` -- aborting the whole route
(PocketBeagle, first external benchmark run).
"""

from __future__ import annotations

import pytest

from kicad_tools.router.net_class import NetClass
from kicad_tools.zones.generator import auto_create_zones_for_pour_nets

_BOARD = """\
(kicad_pcb
  (version 20170123)
  (host pcbnew "(2017-08-02 revision 9760937)-master")
  (general (thickness 1.6))
  (layers
    (0 Top signal)
    (1 VDD signal)
    (2 DigitalGND signal)
    (31 Bottom signal)
    (44 Edge.Cuts user)
  )
  (net 0 "")
  (net 1 "GND")
  (net 2 "+3V3")
  (gr_line (start 0 0) (end 40 0) (layer Edge.Cuts) (width 0.15))
  (gr_line (start 40 0) (end 40 30) (layer Edge.Cuts) (width 0.15))
  (gr_line (start 40 30) (end 0 30) (layer Edge.Cuts) (width 0.15))
  (gr_line (start 0 30) (end 0 0) (layer Edge.Cuts) (width 0.15))
)
"""


def test_legacy_named_copper_skips_pour_instead_of_raising(tmp_path, capsys):
    pcb_path = tmp_path / "legacy.kicad_pcb"
    pcb_path.write_text(_BOARD)
    pour_nets = [("GND", NetClass.POWER), ("+3V3", NetClass.POWER)]

    count = auto_create_zones_for_pour_nets(pcb_path, pour_nets)

    assert count == 0
    assert "legacy layer names" in capsys.readouterr().err


def test_add_zone_still_rejects_in1_on_legacy_board(tmp_path):
    """Loosening the validator is not the fix: explicit requests still fail."""
    from kicad_tools.zones.generator import ZoneGenerator

    pcb_path = tmp_path / "legacy.kicad_pcb"
    pcb_path.write_text(_BOARD)
    gen = ZoneGenerator.from_pcb(pcb_path)
    with pytest.raises(ValueError, match="not available"):
        gen.add_zone(net="GND", layer="In1.Cu")
