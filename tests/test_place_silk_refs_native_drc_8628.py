"""kicad-cli agrees with the place-silk-refs plan at default clearance (#8628)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from kicad_tools.silkscreen.place_refs import SilkRefPlacer

pytest.importorskip("shapely")
pytestmark = pytest.mark.skipif(shutil.which("kicad-cli") is None, reason="kicad-cli not installed")

from tests.test_place_silk_refs_association_8628 import HEADER  # noqa: E402


def _silk_violations(path: Path) -> list[str]:
    out = path.with_suffix(".drc.json")
    subprocess.run(
        ["kicad-cli", "pcb", "drc", "--format", "json", "-o", str(out), str(path)],
        capture_output=True,
        check=False,
    )
    return [v["type"] for v in json.loads(out.read_text())["violations"] if "silk" in v["type"]]


def test_label_clear_in_old_model_is_clear_in_kicad_after_plan(tmp_path: Path):
    pad = '(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask") (net 0 ""))'
    # Label centre 1.7 mm from the pad: the box model (needs 1.625) calls it
    # clear, kicad-cli (needs 1.85) reports silk_over_copper.
    fp = (
        '(footprint "FP" (layer "F.Cu") (at 30 20) '
        '(fp_text reference "C22" (at 1.7 0) (layer "F.SilkS") '
        "(effects (font (size 1 1) (thickness 0.15)))) "
        f"{pad})"
    )
    path = tmp_path / "probe.kicad_pcb"
    path.write_text(HEADER + fp + ")\n")
    assert "silk_over_copper" in _silk_violations(path), "probe must reproduce the gap"

    placer = SilkRefPlacer(path)
    result = placer.plan(clearance_mm=0.0)
    placer.apply(result)
    placer.save()
    assert _silk_violations(path) == []
