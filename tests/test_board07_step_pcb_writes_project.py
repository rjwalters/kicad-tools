"""``generate_design.py --step pcb`` writes the project file (Issue #7716).

``kct route`` takes its clearances from the ``.kicad_pro`` beside the input
PCB.  ``--step pcb`` used to write only the PCB, so ``--step pcb`` then
``--step route`` into a fresh directory routed board 07 with the router's
default rules instead of the project netclass.  The same seed then landed
different copper than ``--step all`` (MIPI_DAT1 0.045 vs 0.150 mm with
``--differential-pairs``), which read as load-dependent routing.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

RECIPE = (
    Path(__file__).resolve().parent.parent / "boards" / "07-matchgroup-test" / "generate_design.py"
)


def _step_pcb(out: Path) -> None:
    subprocess.run(
        [sys.executable, str(RECIPE), str(out), "--step", "pcb"],
        check=True,
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_step_pcb_writes_project_beside_pcb(tmp_path: Path) -> None:
    _step_pcb(tmp_path)
    assert (tmp_path / "matchgroup_test.kicad_pcb").is_file()
    assert (tmp_path / "matchgroup_test.kicad_pro").is_file()


def test_step_pcb_keeps_an_existing_project(tmp_path: Path) -> None:
    project = tmp_path / "matchgroup_test.kicad_pro"
    project.write_text('{"sentinel": true}\n')
    _step_pcb(tmp_path)
    assert project.read_text() == '{"sentinel": true}\n'
