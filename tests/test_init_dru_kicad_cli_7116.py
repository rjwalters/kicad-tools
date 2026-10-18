"""``kct init`` DRU is enforced by kicad-cli (Issue #7116).

``create_dru_content`` used to hand-write rules that mapped silkscreen width to
``silk_clearance`` and hole-to-edge to ``hole_to_hole``.  It now delegates to
``generate_dru``; these tests guard against drift and check that kicad-cli 10
loads the generated file and flags undersized silkscreen text with it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from kicad_tools.cli.init_cmd import create_dru_content, init_project
from kicad_tools.cli.kicad_toolchain import kicad_cli_env, scratch_config_home
from kicad_tools.manufacturers import get_profile
from kicad_tools.manufacturers.dru_generator import generate_dru

KICAD_CLI = shutil.which("kicad-cli")

#: Board 20x20 mm outline with one silkscreen text far thinner/smaller than JLCPCB allows.
BOARD = """\
(kicad_pcb (version 20240108) (generator "pcbnew") (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (36 "B.SilkS" user "B.Silkscreen")
    (37 "F.SilkS" user "F.Silkscreen")
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (gr_rect (start 100 100) (end 120 120) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
  (gr_text "TINY" (at 110 110) (layer "F.SilkS")
    (effects (font (size 0.5 0.5) (thickness 0.05))))
)
"""


@pytest.mark.parametrize("mfr", ["jlcpcb", "seeed"])
@pytest.mark.parametrize("layers", [2, 4])
def test_init_dru_embeds_generate_dru_output(mfr: str, layers: int):
    """Drift guard: the init DRU body is exactly ``generate_dru`` output."""
    rules = get_profile(mfr).get_design_rules(layers)

    content = create_dru_content(rules, mfr, layers)

    expected = generate_dru(rules, mfr.upper())
    _, _, expected_rules = expected.partition("\n")
    assert content.startswith("(version 1)\n")
    assert content.endswith(expected_rules)


def test_init_dru_uses_correct_constraints():
    rules = get_profile("jlcpcb").get_design_rules(2)

    content = create_dru_content(rules, "jlcpcb", 2)

    assert "text_thickness" in content
    assert "text_height" in content
    assert "F.Silkscreen" in content and "B.Silkscreen" in content
    assert "physical_hole_clearance" in content
    assert "hole_to_hole" not in content
    assert "physical_clearance" not in content
    assert "Minimum Silkscreen Width" not in content
    assert "Minimum Solder Mask Dam" not in content


@pytest.mark.skipif(KICAD_CLI is None, reason="kicad-cli not installed")
@pytest.mark.parametrize("mfr,layers", [("jlcpcb", 2), ("jlcpcb", 4), ("seeed", 2), ("seeed", 4)])
def test_kicad_cli_flags_undersized_silk_with_init_dru(tmp_path: Path, mfr: str, layers: int):
    project = tmp_path / "proj"
    assert init_project(target=str(project), manufacturer=mfr, layers=layers) == 0
    pcb = tmp_path / "proj.kicad_pcb"
    pcb.write_text(BOARD)
    report = tmp_path / "drc.json"

    with scratch_config_home() as home:
        result = subprocess.run(
            [
                KICAD_CLI,
                "pcb",
                "drc",
                "--format",
                "json",
                "--severity-all",
                "-o",
                str(report),
                str(pcb),
            ],
            env=kicad_cli_env(home),
            capture_output=True,
            text=True,
            timeout=120,
        )

    assert report.exists(), result.stderr
    violations = json.loads(report.read_text())["violations"]
    types = {v["type"] for v in violations}
    assert "text_thickness" in types, violations
    assert "text_height" in types, violations
