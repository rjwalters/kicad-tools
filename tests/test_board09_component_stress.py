"""Board09 wiring: component-stress FAIL/UNRESOLVED rows become readiness
blockers (issue #5170).

``evaluate_component_stress`` in ``boards/09-usbc-pd-power/check_design.py``
is exercised directly (mirroring the ``importlib``-loading pattern already
used by ``test_board09_critical_routing.py`` / ``test_board09_telemetry.py``)
so this suite never needs ``kicad-cli`` or a full board pipeline run.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

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
