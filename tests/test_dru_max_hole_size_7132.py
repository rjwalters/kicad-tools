"""``generate_dru`` emits a Max Hole Size rule (Issue #7132)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from kicad_tools.cli.kicad_toolchain import kicad_cli_env, scratch_config_home
from kicad_tools.manufacturers import get_profile
from kicad_tools.manufacturers.dru_generator import generate_dru

KICAD_CLI = shutil.which("kicad-cli")


@pytest.mark.parametrize("mfr,limit", [("jlcpcb", "6.3"), ("flashpcb", "6.35")])
def test_generate_dru_emits_max_hole_size(mfr: str, limit: str):
    profile = get_profile(mfr)
    rules = profile.get_design_rules(layers=2, copper_oz=1.0)

    content = generate_dru(rules, manufacturer_name=profile.name)

    assert (
        f'(rule "Max Hole Size - {profile.name}"\n  (constraint hole_size (max {limit}mm)))'
    ) in content


BOARD = """\
(kicad_pcb (version 20240108) (generator "pcbnew") (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (gr_rect (start 100 100) (end 140 140) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
  (footprint "Test:Big" (layer "F.Cu") (at 110 120)
    (pad "1" thru_hole circle (at 0 0) (size 8 8) (drill 6.5) (layers "*.Cu" "*.Mask") (net 0 ""))
  )
  (footprint "Test:AtLimit" (layer "F.Cu") (at 130 120)
    (pad "1" thru_hole circle (at 0 0) (size 8 8) (drill 6.3) (layers "*.Cu" "*.Mask") (net 0 ""))
  )
)
"""


@pytest.mark.skipif(KICAD_CLI is None, reason="kicad-cli not installed")
def test_kicad_cli_flags_only_oversized_hole(tmp_path: Path):
    profile = get_profile("jlcpcb")
    rules = profile.get_design_rules(layers=2, copper_oz=1.0)
    (tmp_path / "b.kicad_dru").write_text(generate_dru(rules, manufacturer_name=profile.name))
    pcb = tmp_path / "b.kicad_pcb"
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
    hits = [v for v in violations if "Max Hole Size" in v["description"]]
    assert len(hits) == 1, violations
    assert hits[0]["type"] == "drill_out_of_range"
    # Only the 6.5 mm pad (x=110) is flagged; the 6.3 mm one at the limit is not.
    assert "actual 6.5000 mm" in hits[0]["description"]
    assert [item["pos"]["x"] for item in hits[0]["items"]] == [110.0]
