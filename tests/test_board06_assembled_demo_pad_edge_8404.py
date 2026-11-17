"""Board 06's assembled-demo recipe clears the mask-dam zone itself (Issue #8404).

``generate.py`` drops each power via 1.2 mm beside its pin, 0.075 mm from the
pad's mask aperture -- under JLCPCB's 0.100 mm dam.  ``finish.py`` must slide
those vias out, otherwise ``build.py``'s ``kct check`` gate fails on
``via_at_pad_edge`` warnings.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

DEMO = Path(__file__).resolve().parent.parent / "boards" / "06-diffpair-test" / "assembled-demo"


def _load_finish():
    spec = importlib.util.spec_from_file_location("board06_finish_8404", DEMO / "finish.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_finish_relocates_generator_power_vias_off_the_mask_dam(tmp_path):
    subprocess.run(
        [sys.executable, str(DEMO / "generate.py"), str(tmp_path)],
        check=True,
        capture_output=True,
    )
    pcb = tmp_path / "diffpair_test.kicad_pcb"
    finish = _load_finish()
    assert finish.relocate_pad_edge_vias(pcb) == 16
    # Fixed point: nothing left in the dam zone to move.
    assert finish.relocate_pad_edge_vias(pcb) == 0
