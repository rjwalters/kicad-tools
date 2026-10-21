"""Board04 must pass its reviewed paid mechanical-drilling process.

The current board uses ordinary through vias, not laser microvias. Its
0.15 mm drills, 0.30 mm lands and 0.075 mm rings require the explicitly
selected factory option and matching native floors. The board-local checker
validates that evidence and the physical fingerprint before running all rules.
No generic manufacturer profile or error allowance is relaxed by these tests.
"""

from __future__ import annotations

import json
import os
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_DIR = REPO_ROOT / "boards/04-stm32-devboard"
BOARD_04_ROUTED_PCB = BOARD_DIR / "output/stm32_devboard_routed.kicad_pcb"
TOLERANCE_YAML = REPO_ROOT / ".github/routed-drc-tolerance.yml"
BOARD_04_RELPATH = BOARD_04_ROUTED_PCB.relative_to(REPO_ROOT).as_posix()


# Issues #7220 / #7295: the nine 0805s whose +3.3V/+5V terminal the starved-
# thermal repair used to force solid now take a narrower thermal spoke, so the
# reviewed gate accepts no warnings at all (``BOARD04_KNOWN_WARNINGS`` is
# empty; tests/test_board_known_warnings.py).
KNOWN_7220_WARNINGS: list[tuple[str, str]] = []


def _warnings(report: dict) -> list[tuple[str, str]]:
    return sorted(
        (v["rule_id"], v["items"][0]) for v in report["violations"] if v["severity"] == "warning"
    )


#: Issue #7682: the gate must not depend on the host's 3D model library.  The
#: CI runner and the pinned toolchain image ship none (#7683), so the gate is
#: also run with every model variable pointing nowhere -- a simulated CI host.
_NO_MODEL_LIBRARY_ENV = {
    f"KICAD{v}_3DMODEL_DIR": "/nonexistent/kct-7682-no-models" for v in (6, 7, 8, 9, 10)
}


@pytest.fixture(scope="module", params=["host", "no_model_library"])
def board_04_mfr_check(request, tmp_path_factory) -> dict:
    report = tmp_path_factory.mktemp("board04-process") / "check.json"
    env = None
    if request.param == "no_model_library":
        env = {**os.environ, **_NO_MODEL_LIBRARY_ENV}
    proc = subprocess.run(
        [
            sys.executable,
            str(BOARD_DIR / "check_manufacturing.py"),
            str(BOARD_04_ROUTED_PCB),
            str(report),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=env,
    )
    assert proc.returncode == 0 and report.is_file(), proc.stdout + proc.stderr
    return json.loads(report.read_text())


def test_board_04_reviewed_process_is_strictly_clean(board_04_mfr_check: dict) -> None:
    report = board_04_mfr_check
    assert report["summary"]["errors"] == 0
    assert _warnings(report) == KNOWN_7220_WARNINGS
    meta = report["meta_checks"]
    for gate in ("drc", "erc", "lvs", "manifest"):
        assert meta[gate]["status"] == "PASSED", gate
    assert meta["drc"]["detail"] == "0 errors, 0 warnings"
    assert report["fabrication_overrides"]["suppressed_findings"] == 0
    assert report["fabrication_overrides"]["paid_factory_option"] == "0.15 mm minimum via hole size"
    assert report["fabrication_overrides"]["via_type"] == "through"
    assert report["fabrication_overrides"]["via_in_pad"] is False
    assert not [v for v in report["violations"] if v["rule_id"] == "connectivity"]
    # Issue #7685: 3D coverage is default-on.  The board is fully covered, so it
    # raises no warning on any host; a host with no model library (the
    # ``no_model_library`` run) only adds the informational "not evaluated" line.
    assert not [
        v
        for v in report["violations"]
        if v["rule_id"] == "model_coverage" and v["severity"] != "info"
    ]


def test_board_04_mfr_gate_flags_uninitialized_kicad_libraries(tmp_path: Path) -> None:
    """Issue #5860: a host with no KiCad global library tables must get a
    loud, distinct diagnostic -- not a bare "N warnings (strict)" failure
    that reads identically to a genuine footprint/symbol-link defect.

    Points ``KICAD_CONFIG_HOME`` at an empty directory (the same technique
    ``_configured_model_variables()`` in ``runner.py`` uses for a related
    lookup) so kicad-cli's ERC run has no ``fp-lib-table``/``sym-lib-table``
    to consult, reproducing the exact condition a fresh worktree or
    uninitialized dev host hits before ``scripts/ci/init_kicad_libraries.py``
    has run.
    """
    report = tmp_path / "check.json"
    empty_kicad_config_home = tmp_path / "kicad-config-home"
    empty_kicad_config_home.mkdir()
    env = {**os.environ, "KICAD_CONFIG_HOME": str(empty_kicad_config_home)}
    proc = subprocess.run(
        [
            sys.executable,
            str(BOARD_DIR / "check_manufacturing.py"),
            str(BOARD_04_ROUTED_PCB),
            str(report),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        check=False,
    )
    # check_manufacturing.py exits 2 when meta_checks.overall != PASSED --
    # an uninitialized host genuinely cannot produce a trustworthy ERC
    # result, so the gate still fails; what must change is *how* it fails.
    assert proc.returncode == 2, proc.stdout + proc.stderr
    data = json.loads(report.read_text())
    erc = data["meta_checks"]["erc"]
    assert erc["status"] == "FAILED"
    assert data["meta_checks"]["overall"] == "FAILED"
    assert "KiCad global library tables not initialized" in erc["detail"]
    assert "scripts/ci/init_kicad_libraries.py" in erc["detail"]
    # The old, indistinguishable-from-a-real-defect message must not be the
    # whole story any more.
    assert erc["detail"] != "0 error(s), 30 warning(s) (strict)"


def test_board_04_no_release_error_allowance() -> None:
    tolerance = yaml.safe_load(TOLERANCE_YAML.read_text())
    assert tolerance["tolerances"].get(BOARD_04_RELPATH, 0) == 0
    assert tolerance["manufacturers"][BOARD_04_RELPATH] == "jlcpcb-tier1"


@pytest.mark.parametrize("change", ["missing_option", "wrong_option", "hole", "diameter", "ring"])
def test_board_04_rejects_unselected_process_or_wrong_native_floor(tmp_path: Path, change: str):
    for name in (
        "stm32_devboard_routed.kicad_pcb",
        "stm32_devboard_routed.kicad_pro",
        "manufacturing-requirements.json",
    ):
        shutil.copy2(BOARD_DIR / "output" / name, tmp_path / name)
    pcb = tmp_path / BOARD_04_ROUTED_PCB.name
    options = tmp_path / "manufacturing-requirements.json"
    if change == "missing_option":
        options.unlink()
    elif change == "wrong_option":
        data = json.loads(options.read_text())
        data["paid_factory_option"] = "standard drilling"
        options.write_text(json.dumps(data))
    else:
        project = pcb.with_suffix(".kicad_pro")
        data = json.loads(project.read_text())
        key = {
            "hole": "min_via_hole",
            "diameter": "min_via_diameter",
            "ring": "min_via_annular_width",
        }[change]
        data["board"]["design_settings"]["rules"][key] = 0.01
        project.write_text(json.dumps(data))
    process = runpy.run_path(str(BOARD_DIR / "manufacturing_process.py"))
    with pytest.raises((ValueError, FileNotFoundError)):
        process["make_checker"](pcb)
