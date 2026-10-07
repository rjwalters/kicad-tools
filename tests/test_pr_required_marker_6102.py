"""pr_required keeps merge-gate tests out of ci_extended (Issue #6102)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
GATE = "tests/test_saved_fill_connectivity_6078.py"


def _load_updater():
    spec = importlib.util.spec_from_file_location(
        "update_ci_extended", REPO / "scripts" / "ci" / "update_ci_extended.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _junit(path: Path) -> Path:
    path.write_text(
        "<testsuites><testsuite>"
        '<testcase classname="tests.test_saved_fill_connectivity_6078" '
        'name="test_shipped_routed_board_saved_fill_is_not_fragmented[board-01]" time="6.0"/>'
        '<testcase classname="tests.test_saved_fill_connectivity_6078" '
        'name="test_other_slow" time="7.0"/>'
        "</testsuite></testsuites>"
    )
    return path


def test_updater_skips_pr_required_tests(tmp_path, monkeypatch):
    mod = _load_updater()
    gate_id = f"{GATE}::test_shipped_routed_board_saved_fill_is_not_fragmented[board-01]"
    monkeypatch.setattr(mod, "collect_pr_required_ids", lambda: frozenset({gate_id}))
    out = tmp_path / "ci_extended.txt"
    monkeypatch.setattr(mod, "OUT", out)
    monkeypatch.setattr(mod, "REPO", REPO)
    assert mod.main([str(_junit(tmp_path / "j.xml"))]) == 0
    ids = [ln for ln in out.read_text().splitlines() if not ln.startswith("#")]
    assert gate_id not in ids
    assert ids == [f"{GATE}::test_other_slow"]


def test_collect_pr_required_ids_finds_fleet_gate():
    mod = _load_updater()
    ids = mod.collect_pr_required_ids((GATE,))
    assert any(
        i.startswith(f"{GATE}::test_shipped_routed_board_saved_fill_is_not_fragmented") for i in ids
    ) or not list(REPO.glob("boards/*/output/*_routed.kicad_pcb"))


def test_conftest_does_not_mark_pr_required_as_ci_extended(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "real_tests_conftest", REPO / "tests" / "conftest.py"
    )
    conftest = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(conftest)

    class Item:
        def __init__(self, nodeid, pr_required):
            self.nodeid = nodeid
            self.pr_required = pr_required
            self.added = []

        def get_closest_marker(self, name):
            return object() if name == "pr_required" and self.pr_required else None

        def add_marker(self, marker):
            self.added.append(marker.name)

    gate, plain = Item("t.py::gate", True), Item("t.py::plain", False)
    monkeypatch.setattr(
        conftest, "_ci_extended_ids", lambda: frozenset({"t.py::gate", "t.py::plain"})
    )
    conftest.pytest_collection_modifyitems(None, [gate, plain])
    assert gate.added == []
    assert plain.added == ["ci_extended"]
