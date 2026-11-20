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
    # Issue #9548: generate.py now clears the mask dam at the source -- the
    # tangential power-via offset spans the pad aperture plus the annulus
    # plus clearance, so the flat 1.2 mm drop that used to leave 16 SOIC
    # power vias 0.075 mm inside their pad's aperture is gone and finish.py
    # has nothing to slide.  The old contract this test pinned (16 moves)
    # was the symptom the aperture clipping produced.
    assert finish.relocate_pad_edge_vias(pcb) == 0
    # Fixed point: nothing left in the dam zone to move.
    assert finish.relocate_pad_edge_vias(pcb) == 0


def test_generator_power_vias_clear_their_pad_apertures(tmp_path):
    """Every authored power via clears every pad's mask aperture by 0.25 mm (#9548).

    ``mask_to_copper`` measures the via annulus against the pad apertures
    (pad copper; this board's mask expansion is 0) with 0.09 mm required and
    a warning band to 0.15 mm, so the authored offset keeps 0.25 mm.  The
    flat 1.2 mm drop this replaced left the 16 SOIC power vias' annulus
    0.075 mm inside their own pad's aperture.
    """
    import math
    import re

    subprocess.run(
        [sys.executable, str(DEMO / "generate.py"), str(tmp_path)],
        check=True,
        capture_output=True,
    )
    text = (tmp_path / "diffpair_test.kicad_pcb").read_text()
    nets = {int(m.group(1)): m.group(2) for m in re.finditer(r'\(net (\d+) "([^"]+)"\)', text)}
    pads = []
    for block in re.finditer(r"\n\t\(footprint \"[^\"]+\"([\s\S]*?)(?=\n\t\(footprint |\Z)", text):
        body = block.group(1)
        ref = re.search(r'\(property "Reference" "([^"]+)"', body).group(1)
        at = re.search(r"\(at ([\d.-]+) ([\d.-]+)(?: [\d.-]+)?\)\n", body)
        fx, fy = float(at.group(1)), float(at.group(2))
        for pad in re.finditer(
            r'\(pad "([^"]+)"[^\n]*\n\s*\(at ([\d.-]+) ([\d.-]+)[^\n]*\n\s*\(size ([\d.]+) ([\d.]+)\)',
            body,
        ):
            # Every part carrying power vias sits unrotated, so the local
            # pad frame is the board frame.
            pads.append(
                (
                    ref,
                    fx + float(pad.group(2)),
                    fy + float(pad.group(3)),
                    float(pad.group(4)),
                    float(pad.group(5)),
                )
            )
    vias = [
        (float(v.group(1)), float(v.group(2)), float(v.group(3)) / 2, nets[int(v.group(4))])
        for v in re.finditer(
            r"\(via\s*\(at ([\d.-]+) ([\d.-]+)\)\s*\(size ([\d.]+)\)[\s\S]*?\(net (\d+)\)", text
        )
    ]
    power = [v for v in vias if v[3] in ("+3V3", "GND")]
    # 16 SOIC power vias + 32 C1-C16 + 2 C17 + 4 R5-R8 GND vias.
    assert len(power) == 54
    for vx, vy, radius, _net in power:
        for _ref, px, py, pw, ph in pads:
            gap = math.hypot(
                max(abs(vx - px) - pw / 2, 0.0),
                max(abs(vy - py) - ph / 2, 0.0),
            )
            assert gap - radius >= 0.25 - 1e-9, (_ref, vx, vy, gap)
