"""Issue #8278: the audit's ampacity gate follows the board's declared stackup copper.

Board 05 declares 1 oz inner copper.  The audit used to evaluate ampacity at the
JLCPCB profile's 0.5 oz inner default, producing 1186 spurious ``ampacity``
errors that ``kct check`` (which honours the stackup) never reported.
"""

from pathlib import Path

import pytest

from kicad_tools.audit import ManufacturingAudit

BOARD = Path(__file__).resolve().parent.parent / "boards" / "05-bldc-motor-controller" / "output"


@pytest.mark.skipif(
    not (BOARD / "bldc_controller_routed.kicad_pcb").exists(), reason="board 05 not present"
)
def test_audit_ampacity_uses_declared_stackup_copper():
    result = ManufacturingAudit(
        BOARD / "bldc_controller_routed.kicad_pcb",
        manufacturer="jlcpcb",
        skip_erc=True,
        net_class_map_path=BOARD / "net_class_map.json",
    ).run()
    assert result.drc.blocking_count == 0
