"""Board09 wiring: component-stress FAIL/UNRESOLVED rows become readiness
blockers (issue #5170).

``evaluate_component_stress`` in ``boards/09-usbc-pd-power/check_design.py``
is exercised directly (mirroring the ``importlib``-loading pattern already
used by ``test_board09_critical_routing.py`` / ``test_board09_telemetry.py``)
so this suite never needs ``kicad-cli`` or a full board pipeline run.
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from kicad_tools.cli.board_readiness import read_readiness

BOARD_ROOT = Path(__file__).resolve().parents[1] / "boards/09-usbc-pd-power"
STRESS_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "component_stress"
SCH = STRESS_FIXTURES / "mosfets.kicad_sch"
STATES = STRESS_FIXTURES / "states.yaml"

EMPTY_SCH = (
    '(kicad_sch (version 20250114) (generator "eeschema") '
    '(uuid "11111111-1111-1111-1111-111111111111") (paper "A4") (lib_symbols))'
)


@pytest.fixture
def checker(monkeypatch):
    # check_design.py imports `engineering.calculate`, a sibling package that
    # only resolves once the board directory is on sys.path (see
    # test_board09_critical_routing.py for the same pattern).
    monkeypatch.syspath_prepend(str(BOARD_ROOT))
    spec = importlib.util.spec_from_file_location(
        "board09_check_design_component_stress", BOARD_ROOT / "check_design.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_no_manifest_is_unaffected(checker, tmp_path):
    """(a) No manifest present -> no status, no blockers -- readiness untouched."""
    missing_manifest = tmp_path / "operating_states.yaml"
    assert not missing_manifest.exists()
    clean, blockers = checker.evaluate_component_stress(SCH, missing_manifest)
    assert clean is None
    assert blockers == []


def test_manifest_with_fail_or_unresolved_row_becomes_a_blocker(checker):
    """(b) A FAIL/UNRESOLVED row -> a matching blocker and a non-ready status."""
    clean, blockers = checker.evaluate_component_stress(SCH, STATES)
    assert clean is False
    assert blockers
    assert all(b.startswith("Component stress ") for b in blockers)
    # The blocker text carries enough detail (reference/check/state) to act on.
    assert any("Q" in b for b in blockers)


def test_manifest_with_no_findings_is_clean(checker, tmp_path):
    """(c) A manifest producing zero FAIL/UNRESOLVED rows -> no new blocker."""
    empty_sch = tmp_path / "empty.kicad_sch"
    empty_sch.write_text(EMPTY_SCH)
    clean, blockers = checker.evaluate_component_stress(empty_sch, STATES)
    assert clean is True
    assert blockers == []


def test_malformed_manifest_is_a_blocker_not_a_crash(checker, tmp_path):
    """A manifest that fails to load degrades to a blocker, never an exception."""
    bad_manifest = tmp_path / "operating_states.yaml"
    bad_manifest.write_text("version: 1\n")  # no 'states' key -> ValueError
    clean, blockers = checker.evaluate_component_stress(SCH, bad_manifest)
    assert clean is False
    assert len(blockers) == 1
    assert "could not be loaded" in blockers[0]


def test_readiness_wiring_matches_pattern(checker):
    """The helper's contract mirrors what check() feeds into readiness.json."""
    clean, blockers = checker.evaluate_component_stress(SCH, STATES)
    checks_entry = {"name": "component_stress", "status": "passed" if clean else "failed"}
    assert checks_entry["status"] == "failed"
    assert blockers


# --- Report-writing regression: exercise check() itself, not just the helper ---
#
# The wiring above proves evaluate_component_stress() behaves correctly in
# isolation, but check() is what actually assembles readiness.json, and its
# `inputs` hashing is where a real defect lived: the manifest was consulted
# to produce component_stress but never hashed, so editing or deleting it
# after a passing report left every recorded hash unchanged and the site
# readiness loader (read_readiness) kept presenting the stale check as
# verified. These tests run check()'s real report-writing path -- with only
# the native ERC/DRC/LVS/tool-report subprocess calls and the two pure
# geometry helpers stubbed -- against a throwaway copy of the board
# directory, never the tracked one.


@dataclasses.dataclass
class _FakeLabelLVS:
    clean: bool


@dataclasses.dataclass
class _FakeCopperLVS:
    clean: bool
    bound_pad_count: int = 0


def _fake_subprocess_run(cmd, *args, **kwargs):
    if cmd[:3] == ["kicad-cli", "sch", "erc"]:
        Path(cmd[cmd.index("--output") + 1]).write_text(json.dumps({"violations": []}))
        return subprocess.CompletedProcess(cmd, 0)
    if cmd[:3] == ["kicad-cli", "pcb", "drc"]:
        Path(cmd[cmd.index("--output") + 1]).write_text(
            json.dumps({"violations": [], "unconnected_items": []})
        )
        return subprocess.CompletedProcess(cmd, 0)
    if len(cmd) > 3 and cmd[3] == "check":
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"summary": {"passed": True}}), stderr=""
        )
    if len(cmd) > 3 and cmd[3] == "net-status":
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"summary": {"passed": True}}), stderr=""
        )
    raise AssertionError(f"unexpected subprocess invocation in test: {cmd}")


@pytest.fixture
def board_copy(tmp_path):
    """A throwaway copy of the real board directory.

    check() writes into output/ in place; this suite must never mutate the
    tracked boards/09-usbc-pd-power tree.
    """
    dest = tmp_path / "board09"
    shutil.copytree(BOARD_ROOT, dest)
    return dest


def _run_check(monkeypatch, board_root, manifest_yaml, schematic_text=None):
    """Load check_design.py against *board_root* and run its report path.

    Swaps in the small stress fixture schematic (in place of the board's
    real, unrelated schematic) so the manifest/analyzer pairing under test
    stays independent of board09's actual MOSFET wiring, then runs check()
    for real with only native-tool subprocess calls and the two pure
    geometry helpers stubbed.
    """
    monkeypatch.syspath_prepend(str(board_root))
    spec = importlib.util.spec_from_file_location(
        "board09_check_design_readiness_wiring", board_root / "check_design.py"
    )
    checker = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = checker
    assert spec.loader is not None
    spec.loader.exec_module(checker)

    schematic_path = board_root / "output" / "usbc_pd_power.kicad_sch"
    schematic_path.write_text(schematic_text if schematic_text is not None else SCH.read_text())

    manifest_path = board_root / "operating_states.yaml"
    if manifest_yaml is None:
        manifest_path.unlink(missing_ok=True)
    else:
        manifest_path.write_text(manifest_yaml)

    monkeypatch.setattr(checker, "ROOT", board_root)
    monkeypatch.setattr(checker, "COMPONENT_STRESS_MANIFEST", manifest_path)
    monkeypatch.setattr(checker.subprocess, "run", _fake_subprocess_run)
    monkeypatch.setattr(checker, "compare_netlists", lambda *a, **k: _FakeLabelLVS(clean=True))
    monkeypatch.setattr(
        checker, "compare_copper_netlist", lambda *a, **k: _FakeCopperLVS(clean=True)
    )
    monkeypatch.setattr(checker, "critical_route_opens", lambda *a, **k: [])
    monkeypatch.setattr(checker, "inner_ground_planes_valid", lambda *a, **k: True)

    checker.check(board_root / "output")
    readiness = json.loads((board_root / "output" / "readiness.json").read_text())
    return checker, readiness


def test_absent_manifest_leaves_readiness_untouched_through_check(monkeypatch, board_copy):
    """(a) No manifest -> no component_stress check, no new input, through check()."""
    _, readiness = _run_check(monkeypatch, board_copy, manifest_yaml=None)
    assert "operating_states.yaml" not in readiness["inputs"]
    assert not any(c["name"] == "component_stress" for c in readiness["checks"])


def test_present_manifest_is_bound_into_inputs_and_edit_is_rejected(monkeypatch, board_copy):
    """(b) Present manifest is hashed into inputs; editing it later must go stale."""
    _, readiness = _run_check(monkeypatch, board_copy, manifest_yaml=STATES.read_text())
    manifest_path = board_copy / "operating_states.yaml"
    expected_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    assert readiness["inputs"]["operating_states.yaml"] == expected_hash
    checks_entry = next(c for c in readiness["checks"] if c["name"] == "component_stress")
    assert checks_entry["status"] == "failed"  # STATES fixture has a FAIL/UNRESOLVED row
    assert any("Component stress" in b for b in readiness["blockers"])

    # Consumer control: before this fix, editing the manifest left every
    # recorded hash unchanged and read_readiness kept accepting the report.
    manifest_path.write_text(STATES.read_text() + "\n# tampered\n")
    result = read_readiness(board_copy)
    assert result["status"] == "unverified"
    assert any("operating_states.yaml" in b for b in result["blockers"])


def test_manifest_deletion_after_passing_report_is_rejected(monkeypatch, board_copy):
    """(c) Deleting a manifest that produced a passing check must go stale, not stay clean."""
    _, readiness = _run_check(
        monkeypatch, board_copy, manifest_yaml=STATES.read_text(), schematic_text=EMPTY_SCH
    )
    checks_entry = next(c for c in readiness["checks"] if c["name"] == "component_stress")
    assert checks_entry["status"] == "passed"
    assert "operating_states.yaml" in readiness["inputs"]

    (board_copy / "operating_states.yaml").unlink()
    result = read_readiness(board_copy)
    assert result["status"] == "unverified"


def test_malformed_manifest_is_recorded_as_a_blocker_and_still_hashed(monkeypatch, board_copy):
    """A present-but-malformed manifest still participates in the input binding."""
    _, readiness = _run_check(monkeypatch, board_copy, manifest_yaml="version: 1\n")
    checks_entry = next(c for c in readiness["checks"] if c["name"] == "component_stress")
    assert checks_entry["status"] == "failed"
    assert any("could not be loaded" in b for b in readiness["blockers"])
    manifest_path = board_copy / "operating_states.yaml"
    assert (
        readiness["inputs"]["operating_states.yaml"]
        == hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    )
