"""Native controls for complete copper component extraction."""

import subprocess
import sys
from pathlib import Path

import pytest

from kicad_tools.cli import relocation_components
from kicad_tools.zones.placement_fill import find_kicad_python


def test_native_holes_layers_and_foreign_net_copper():
    python = find_kicad_python()
    if python is None:
        pytest.skip("Native KiCad Python is required")
    script = Path(__file__).parent / "fixtures/via_relocation/native_component_controls.py"
    worker = Path(relocation_components.__file__).with_name("_native_components.py")
    command = [str(python), str(script), str(worker)]
    if sys.platform == "darwin":
        command.extend(["-ApplePersistenceIgnoreState", "YES"])
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert '"hole_excluded_and_foreign_net_zone_contact_connected": true' in result.stdout
