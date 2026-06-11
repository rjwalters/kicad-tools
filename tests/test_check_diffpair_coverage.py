"""Tests for the diff-pair routing-regression CI gate (Issue #2660).

Covers:

1. ``scripts/ci/check_diffpair_coverage.py`` helper functions
   (``check_rule_coverage``, ``load_allowlist``, ``find_routed_pcb``).
2. The ``diffpair-routing-regression`` CI job in
   ``.github/workflows/ci.yml`` (job exists, runs on ubuntu-latest,
   matrix is parametric over board, calls the helper, no diff-driven
   short-circuit).
3. The new ``rules_checked_by_rule`` field in ``DRCResults`` /
   ``kct check --format json`` output (Phase 4N JSON schema extension).
4. The three diff-pair rules populate ``rules_checked_by_rule`` with
   their rule_id when they actually run (i.e., on a board with engaged
   diff pairs).

Out of scope (covered by sibling tests):

* The DRC rules themselves -- see ``test_cli_check_diffpair_*.py``.
* The diff-driven ``routed-pcb-drc-check`` job -- see
  ``test_ci_routed_drc_workflow.py``.

Architecture: Mock-heavy unit tests for the assertion logic + a
``yaml.safe_load`` structural test for the CI workflow.  Full
end-to-end re-route of board 06 is NOT exercised here (it's the CI
job's job to do that); the assertion logic is tested with synthesised
``rules_checked_by_rule`` dictionaries so the test remains fast.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"
HELPER_SCRIPT_PATH = REPO_ROOT / "scripts" / "ci" / "check_diffpair_coverage.py"
ALLOWLIST_PATH = REPO_ROOT / ".github" / "routed-drc-tolerance.yml"

JOB_NAME = "diffpair-routing-regression"


def _load_helper_module():
    """Import ``scripts/ci/check_diffpair_coverage.py`` as a module."""
    spec = importlib.util.spec_from_file_location(
        "check_diffpair_coverage_test_module", HELPER_SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_diffpair_coverage_test_module"] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Unit tests for the assertion logic
# ---------------------------------------------------------------------------


class TestCheckRuleCoverage:
    """The "rule exercised" assertion is the critical correctness check.

    A regression in this function would silently let a defective router
    pass the gate, so the truth table is pinned exhaustively here.
    """

    def test_all_three_rules_exercised_returns_empty(self):
        mod = _load_helper_module()
        rules_by_rule = {
            "diffpair_clearance_intra": 9,
            "diffpair_length_skew": 2,
            "diffpair_routing_continuity": 4,
        }
        assert mod.check_rule_coverage(rules_by_rule) == []

    def test_missing_rule_id_returns_missing(self):
        mod = _load_helper_module()
        rules_by_rule = {
            "diffpair_clearance_intra": 9,
            # diffpair_length_skew omitted entirely (no entry)
            "diffpair_routing_continuity": 4,
        }
        missing = mod.check_rule_coverage(rules_by_rule)
        assert missing == ["diffpair_length_skew"]

    def test_zero_count_returns_missing(self):
        mod = _load_helper_module()
        rules_by_rule = {
            "diffpair_clearance_intra": 9,
            "diffpair_length_skew": 0,  # explicit zero
            "diffpair_routing_continuity": 4,
        }
        missing = mod.check_rule_coverage(rules_by_rule)
        assert missing == ["diffpair_length_skew"]

    def test_all_three_missing_returns_all_three(self):
        mod = _load_helper_module()
        missing = mod.check_rule_coverage({})
        assert missing == [
            "diffpair_clearance_intra",
            "diffpair_length_skew",
            "diffpair_routing_continuity",
        ]

    def test_returned_order_is_stable(self):
        """Stable order so error messages don't churn run-to-run."""
        mod = _load_helper_module()
        # Two missing rules; expect deterministic order matching
        # DIFFPAIR_RULE_IDS tuple.
        rules_by_rule = {"diffpair_routing_continuity": 1}
        missing = mod.check_rule_coverage(rules_by_rule)
        assert missing == [
            "diffpair_clearance_intra",
            "diffpair_length_skew",
        ]

    def test_extra_rule_ids_ignored(self):
        """A rule outside the required set doesn't satisfy the gate.

        e.g., ``clearance_segment_segment`` ran 50 times but no
        diffpair rule ran.  Gate must still fail.
        """
        mod = _load_helper_module()
        rules_by_rule = {
            "clearance_segment_segment": 50,
            "impedance": 10,
        }
        missing = mod.check_rule_coverage(rules_by_rule)
        assert set(missing) == set(mod.DIFFPAIR_RULE_IDS)

    def test_custom_required_set(self):
        """The function accepts a custom required_rule_ids for future reuse."""
        mod = _load_helper_module()
        rules_by_rule = {"clearance_segment_segment": 1}
        missing = mod.check_rule_coverage(
            rules_by_rule, required_rule_ids=("clearance_segment_segment",)
        )
        assert missing == []


class TestAllowlistLoading:
    """Sanity check that the helper's allowlist loader matches the
    sibling ``check_routed_drc.py`` semantic.  Without this, a divergence
    in the two scripts could let one gate pass while the other fails
    on the same allowlist file."""

    def test_load_real_allowlist(self):
        """The committed allowlist must parse with the helper's loader."""
        mod = _load_helper_module()
        data = mod.load_allowlist(ALLOWLIST_PATH)
        assert isinstance(data, dict)
        # Today board 06 IS in the allowlist (28 errors); curator notes
        # the eventual goal is to remove it once #2672 and #2677 close.
        # Pin the schema, not the value: the value can change as the
        # baseline tightens.
        key = "boards/06-diffpair-test/output/diffpair_test_routed.kicad_pcb"
        assert key in data
        assert isinstance(data[key], int)
        assert data[key] >= 0

    def test_missing_file_returns_empty(self, tmp_path):
        mod = _load_helper_module()
        result = mod.load_allowlist(tmp_path / "does-not-exist.yml")
        assert result == {}

    def test_empty_file_returns_empty(self, tmp_path):
        mod = _load_helper_module()
        f = tmp_path / "empty.yml"
        f.write_text("")
        assert mod.load_allowlist(f) == {}

    def test_malformed_yaml_raises(self, tmp_path):
        mod = _load_helper_module()
        f = tmp_path / "bad.yml"
        f.write_text("this: is: not: valid:")
        with pytest.raises(ValueError, match="Malformed allowlist YAML"):
            mod.load_allowlist(f)

    def test_non_mapping_raises(self, tmp_path):
        mod = _load_helper_module()
        f = tmp_path / "list.yml"
        f.write_text("- not\n- a\n- mapping\n")
        with pytest.raises(ValueError, match="must be a YAML mapping"):
            mod.load_allowlist(f)

    def test_non_int_value_raises(self, tmp_path):
        mod = _load_helper_module()
        f = tmp_path / "bad-val.yml"
        f.write_text('tolerances:\n  "some/path_routed.kicad_pcb": "not-a-number"\n')
        with pytest.raises(ValueError, match="non-negative integer"):
            mod.load_allowlist(f)


class TestFindRoutedPcb:
    """The locator must find the right artifact without false positives."""

    def test_returns_none_for_missing_dir(self, tmp_path):
        mod = _load_helper_module()
        assert mod.find_routed_pcb(tmp_path / "nope") is None

    def test_returns_none_for_empty_output_dir(self, tmp_path):
        mod = _load_helper_module()
        (tmp_path / "output").mkdir()
        assert mod.find_routed_pcb(tmp_path) is None

    def test_returns_routed_pcb(self, tmp_path):
        mod = _load_helper_module()
        out = tmp_path / "output"
        out.mkdir()
        target = out / "foo_routed.kicad_pcb"
        target.write_text("(kicad_pcb)")
        result = mod.find_routed_pcb(tmp_path)
        assert result == target

    def test_ignores_unrouted_pcb(self, tmp_path):
        mod = _load_helper_module()
        out = tmp_path / "output"
        out.mkdir()
        (out / "foo.kicad_pcb").write_text("(kicad_pcb)")
        # No *_routed.kicad_pcb -- locator should return None.
        assert mod.find_routed_pcb(tmp_path) is None


# ---------------------------------------------------------------------------
# CI workflow YAML structural tests
# ---------------------------------------------------------------------------


class TestWorkflowJob:
    """Pin the structure of the new ``diffpair-routing-regression`` job."""

    @pytest.fixture
    def workflow(self) -> dict:
        assert CI_WORKFLOW_PATH.is_file()
        return yaml.safe_load(CI_WORKFLOW_PATH.read_text())

    def test_job_exists(self, workflow: dict) -> None:
        assert JOB_NAME in workflow["jobs"], (
            f"Expected job '{JOB_NAME}' in .github/workflows/ci.yml. "
            f"Found: {sorted(workflow['jobs'].keys())}"
        )

    def test_job_runs_on_ubuntu_no_container(self, workflow: dict) -> None:
        """Pure-Python; don't pay container-pull cost."""
        job = workflow["jobs"][JOB_NAME]
        assert job["runs-on"] == "ubuntu-latest"
        assert "container" not in job

    def test_job_has_reasonable_timeout(self, workflow: dict) -> None:
        """Issue #2660 AC #5: <= 5 min on ubuntu-latest.  Timeout-minutes
        is the upper-bound safety net; 10 min matches the sibling
        ``routed-pcb-drc-check`` job."""
        job = workflow["jobs"][JOB_NAME]
        timeout = job.get("timeout-minutes")
        assert isinstance(timeout, int) and 1 <= timeout <= 30

    def test_job_is_matrix_parametric_over_board(self, workflow: dict) -> None:
        """Per the curator: must be parametric over board name so board 03
        can be added cheaply when #2589 + #2513 close."""
        job = workflow["jobs"][JOB_NAME]
        strategy = job.get("strategy", {})
        matrix = strategy.get("matrix", {})
        assert "board" in matrix, (
            f"Expected ``matrix.board`` in {JOB_NAME} strategy so additional "
            "boards can be added without job duplication."
        )
        boards = matrix["board"]
        assert isinstance(boards, list) and boards
        # Board 06 must be in the initial matrix.
        assert any("06-diffpair-test" in str(b) for b in boards)

    def test_job_invokes_helper_script(self, workflow: dict) -> None:
        steps = workflow["jobs"][JOB_NAME]["steps"]
        run_blocks = [s.get("run", "") for s in steps if isinstance(s, dict) and "run" in s]
        joined = "\n".join(run_blocks)
        assert "scripts/ci/check_diffpair_coverage.py" in joined

    def test_job_passes_seed_42(self, workflow: dict) -> None:
        """Curator-mandated: ``--seed 42`` for determinism (Issue #2589)."""
        steps = workflow["jobs"][JOB_NAME]["steps"]
        run_blocks = [s.get("run", "") for s in steps if isinstance(s, dict) and "run" in s]
        joined = "\n".join(run_blocks)
        assert "--seed 42" in joined or "--seed=42" in joined, (
            f"Expected ``--seed 42`` in {JOB_NAME} run step (Issue #2589 "
            "/ Phase 3X.2 determinism)."
        )

    def test_job_has_no_diff_driven_short_circuit(self, workflow: dict) -> None:
        """The job runs on every PR, no ``files`` short-circuit (per issue
        scope: catches algorithmic regressions even when no committed PCB
        is touched)."""
        steps = workflow["jobs"][JOB_NAME]["steps"]
        # No ``if: steps.changed.outputs.files`` guards on any step.
        for s in steps:
            if isinstance(s, dict) and "if" in s:
                assert "files" not in str(s["if"]), (
                    "diffpair-routing-regression must NOT be diff-driven; "
                    "it must run on every PR to catch routing-algorithm "
                    "regressions."
                )

    def test_no_kicad_cli_dependency(self, workflow: dict) -> None:
        """Like sibling job: pure Python.  No kicad-cli setup steps."""
        steps = workflow["jobs"][JOB_NAME]["steps"]
        for s in steps:
            if not isinstance(s, dict):
                continue
            uses = s.get("uses", "")
            run = s.get("run", "")
            assert "kicad" not in uses.lower(), (
                f"Unexpected kicad-cli setup step: {s}"
            )
            assert "apt-get install" not in run or "kicad" not in run.lower()


# ---------------------------------------------------------------------------
# rules_checked_by_rule field (validate + JSON output extension)
# ---------------------------------------------------------------------------


class TestRulesCheckedByRule:
    """Pin the per-rule counter field added in Phase 4N (#2660)."""

    def test_drc_results_has_per_rule_counter_field(self):
        from kicad_tools.validate.violations import DRCResults

        results = DRCResults()
        # Initially empty, never None.
        assert isinstance(results.rules_checked_by_rule, dict)
        assert results.rules_checked_by_rule == {}

    def test_merge_sums_per_rule_counters(self):
        from kicad_tools.validate.violations import DRCResults

        a = DRCResults()
        a.rules_checked_by_rule["foo"] = 3
        a.rules_checked_by_rule["bar"] = 1

        b = DRCResults()
        b.rules_checked_by_rule["foo"] = 5
        b.rules_checked_by_rule["baz"] = 2

        a.merge(b)
        assert a.rules_checked_by_rule == {
            "foo": 8,
            "bar": 1,
            "baz": 2,
        }

    def test_to_dict_includes_per_rule_counter(self):
        from kicad_tools.validate.violations import DRCResults

        results = DRCResults()
        results.rules_checked_by_rule["foo"] = 7
        d = results.to_dict()
        assert "rules_checked_by_rule" in d
        assert d["rules_checked_by_rule"] == {"foo": 7}

    def test_cli_json_output_contains_per_rule_counter(self, tmp_path):
        """``kct check --format json`` MUST emit ``summary.rules_checked_by_rule``
        so the CI script can read it.  Uses an existing committed routed PCB.
        """
        # Use board 03's routed PCB (smaller / faster than board 06).
        pcb = REPO_ROOT / "boards" / "03-usb-joystick" / "output" / "usb_joystick_routed.kicad_pcb"
        if not pcb.is_file():
            pytest.skip(f"Board 03 routed PCB not found at {pcb}")

        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "kicad_tools.cli.check_cmd",
                str(pcb),
                "--mfr",
                "jlcpcb",
                "--format",
                "json",
            ],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        # Exit code 0 or 2 is fine (errors are tolerated, what we care about
        # is JSON structure).
        assert proc.returncode in (0, 2), (
            f"kct check exited {proc.returncode}; stderr:\n{proc.stderr}"
        )
        data = json.loads(proc.stdout)
        assert "summary" in data
        assert "rules_checked_by_rule" in data["summary"], (
            f"Expected ``summary.rules_checked_by_rule`` in JSON output.  "
            f"Got keys: {sorted(data['summary'].keys())}"
        )
        assert isinstance(data["summary"]["rules_checked_by_rule"], dict)


# ---------------------------------------------------------------------------
# CLI entry-point exit code tests
# ---------------------------------------------------------------------------


class TestCliExitCodes:
    """End-to-end check that the helper's main() returns the right codes
    for the documented contract.  Uses --skip-route to avoid spinning up
    the full route pipeline in unit tests."""

    def test_missing_board_dir_returns_1(self, tmp_path):
        mod = _load_helper_module()
        rc = mod.main([str(tmp_path / "nope"), "--skip-route"])
        assert rc == 1

    def test_skip_route_without_routed_pcb_returns_1(self, tmp_path):
        """Skip-route can't run if there's no committed routed PCB."""
        mod = _load_helper_module()
        # Synthesise a minimal board dir with generate_design.py but no
        # output/.  Must return tool-failure (1), not gate-failure (2),
        # because no DRC ran.
        (tmp_path / "generate_design.py").write_text("def build_net_class_map(): return {}\n")
        rc = mod.main([str(tmp_path), "--skip-route"])
        assert rc == 1


# ---------------------------------------------------------------------------
# Issue #3413 phase 5: reach assertion
# ---------------------------------------------------------------------------

# Two-net PCB: VCC fully routed (R1.1 -> R2.1), GND unrouted.  Borrowed
# from tests/test_analysis_net_status.py's fixture shape.
_REACH_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "VCC")
  (net 2 "GND")

  (footprint "R_0402"
    (layer "F.Cu")
    (at 10 10)
    (property "Reference" "R1")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "VCC"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 2 "GND"))
  )

  (footprint "R_0402"
    (layer "F.Cu")
    (at 20 10)
    (property "Reference" "R2")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "VCC"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 2 "GND"))
  )

  (segment (start 9.5 10) (end 19.5 10) (width 0.25) (layer "F.Cu") (net 1))
)
"""


class TestMeasureSignalReach:
    """Unit tests for the Issue #3413 phase-5 reach measurement.

    Without a reach assertion the gate was green at ANY reach as long as
    connectivity errors fit under the allowlist floor (the 18/21 shape
    from the #3413 re-measure).
    """

    @pytest.fixture
    def reach_pcb(self, tmp_path):
        p = tmp_path / "reach.kicad_pcb"
        p.write_text(_REACH_PCB)
        return p

    def test_counts_complete_signal_nets(self, reach_pcb):
        mod = _load_helper_module()
        complete, total, incomplete = mod.measure_signal_reach(reach_pcb, set())
        assert (complete, total) == (1, 2)
        assert incomplete == ["GND"]

    def test_pour_nets_excluded_from_signal_universe(self, reach_pcb):
        mod = _load_helper_module()
        complete, total, incomplete = mod.measure_signal_reach(reach_pcb, {"GND"})
        assert (complete, total) == (1, 1)
        assert incomplete == []

    def test_board_06_declares_reach_contract(self):
        """Board 06's recipe must expose the constants the gate reads."""
        import sys as _sys
        from pathlib import Path as _Path

        repo_root = _Path(__file__).resolve().parent.parent
        ci_dir = repo_root / "scripts" / "ci"
        _sys.path.insert(0, str(ci_dir))
        try:
            from net_class_map_resolver import load_board_recipe_module
        finally:
            _sys.path.pop(0)

        mod = load_board_recipe_module(repo_root / "boards" / "06-diffpair-test")
        assert mod is not None
        assert mod.REQUIRED_SIGNAL_REACH == 21
        assert set(mod.POUR_NETS) == {"GND", "VBUS_USB", "+3V3", "+1V8", "+1V2"}
