"""Tests for the match-group routing-regression CI gate (Issue #2726).

Covers the pour-connectivity gate term added in Issue #3617 (the sibling
of board 06's #3509):

1. ``scripts/ci/check_matchgroup_coverage.py::measure_pour_connectivity``
   -- the copper-union pour audit term (all-connected -> empty failures;
   disjoint net / zero-fill zone / missing-net -> reported; missing audit
   function or audit crash -> RuntimeError, never a silent pass).
2. Board 07's recipe declares the pour contract the gate reads
   (``POUR_NETS`` / ``REQUIRE_POUR_CONNECTIVITY`` / ``MAX_POUR_REPAIR_ROUNDS``
   + the ``_audit_pour_nets`` function).
3. The ``matchgroup-routing-regression`` CI job runs in the
   kicad/kicad:10.0 container (so ``kct zones fill`` has a backend) with an
   early ``kicad-cli --version`` probe.

Architecture mirrors ``test_check_diffpair_coverage.py``: mock-heavy unit
tests for the assertion logic + a ``yaml.safe_load`` structural test for
the CI workflow.  Full end-to-end re-route of board 07 is NOT exercised
here (that is the CI job's job); the assertion logic is tested with
synthesised audit dictionaries so the test stays fast.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"
HELPER_SCRIPT_PATH = REPO_ROOT / "scripts" / "ci" / "check_matchgroup_coverage.py"

JOB_NAME = "matchgroup-routing-regression"


def _load_helper_module():
    """Import ``scripts/ci/check_matchgroup_coverage.py`` as a module."""
    spec = importlib.util.spec_from_file_location(
        "check_matchgroup_coverage_test_module", HELPER_SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_matchgroup_coverage_test_module"] = module
    spec.loader.exec_module(module)
    return module


def _load_board_recipe_module():
    """Import board 07's generate_design.py via the gate's loader."""
    mod = _load_helper_module()
    return mod.load_board_recipe_module(REPO_ROOT / "boards" / "07-matchgroup-test")


# ---------------------------------------------------------------------------
# Issue #3828: zero-violation assertion (symmetric with the diff-pair gate)
# ---------------------------------------------------------------------------


class TestCheckZeroViolations:
    """The gate must FAIL when match_group_length_skew fires beyond its
    documented baseline -- even when the total error count fits the (large)
    allowlist floor.  This is the assertion the old gate lacked."""

    def test_zero_violations_strict_baseline_passes(self):
        mod = _load_helper_module()
        assert mod.check_zero_violations({"match_group_length_skew": 0}, baseline=0) == []

    def test_nonzero_violations_strict_baseline_fails(self):
        mod = _load_helper_module()
        failures = mod.check_zero_violations({"match_group_length_skew": 5}, baseline=0)
        assert len(failures) == 1
        assert "match_group_length_skew=5" in failures[0]

    def test_regression_under_large_allowlist_floor_still_fails(self):
        """The exact blindness #3828 closes: board 07's 120-error allowlist
        floor would absorb a few new skew errors, but the zero-violation
        assertion (independent of the allowlist) still fails."""
        mod = _load_helper_module()
        assert mod.check_zero_violations({"match_group_length_skew": 3}, baseline=0)

    def test_at_documented_baseline_passes(self):
        mod = _load_helper_module()
        assert mod.check_zero_violations({"match_group_length_skew": 4}, baseline=4) == []

    def test_one_above_baseline_fails(self):
        mod = _load_helper_module()
        assert mod.check_zero_violations({"match_group_length_skew": 3}, baseline=2)

    def test_board_07_documented_via_skew_baseline(self):
        """Board 07's documented match-group baseline is 2 (Issue #7339).

        Reroute path: 1x MIPI_CSI_LANES pair-only skew (#3916) plus
        1x DDR_DATA_BYTE_0 (16.717 mm vs 0.100 mm tol, seed 42), which fires
        now that DQ0 is routed (#7331).  ADDR_BUS is compensated by the #3931
        via-aware tuner and HDMI_TMDS_LANES is excluded by the unrouted-leg
        gate.  The ``--skip-route`` committed artifact counts 1 and so passes
        under the same value.  Drop to 1 once the tuner fixes DDR_DATA_BYTE_0
        (#5798 / #6447)."""
        mod = _load_helper_module()
        key = "boards/07-matchgroup-test/regression-fixture/matchgroup_test_routed.kicad_pcb"
        assert mod.MATCHGROUP_VIOLATION_BASELINE.get(key, 0) == 2


# ---------------------------------------------------------------------------
# Issue #3617: measure_pour_connectivity unit tests
# ---------------------------------------------------------------------------


def _fake_recipe(audit_result=None, crash=False, omit_audit_fn=False):
    """Build a stand-in recipe module for measure_pour_connectivity."""
    import types

    mod = types.SimpleNamespace()
    if omit_audit_fn:
        return mod

    def _audit_pour_nets(pcb_path, net_names):
        if crash:
            raise ValueError("synthetic audit crash")
        return audit_result

    mod._audit_pour_nets = _audit_pour_nets
    return mod


def _net_ok(n_pads=4):
    return {
        "connected": True,
        "pad_groups": [[(f"U1.{i}", False) for i in range(n_pads)]],
        "zero_fill_zones": 0,
    }


class TestMeasurePourConnectivity:
    """Unit tests for the Issue #3617 pour-connectivity gate term.

    Before this gate term board 07's recipe never even filled its pours,
    so the routed artifact shipped zone outlines with zero fill geometry
    and the job stayed green by omission.
    """

    def test_all_connected_returns_empty(self, tmp_path):
        mod = _load_helper_module()
        recipe = _fake_recipe({"GND": _net_ok(), "+1V2": _net_ok()})
        failures = mod.measure_pour_connectivity(recipe, tmp_path / "x.kicad_pcb", {"GND", "+1V2"})
        assert failures == []

    def test_disjoint_net_reported(self, tmp_path):
        mod = _load_helper_module()
        audit = {
            "GND": {
                "connected": False,
                "pad_groups": [
                    [("U1.1", False), ("U1.2", False)],
                    [("J1.5", False)],
                ],
                "zero_fill_zones": 0,
            }
        }
        failures = mod.measure_pour_connectivity(
            _fake_recipe(audit), tmp_path / "x.kicad_pcb", {"GND"}
        )
        assert len(failures) == 1
        assert "GND" in failures[0]
        assert "2 disjoint pad groups" in failures[0]
        assert "largest 2/3" in failures[0]

    def test_zero_fill_zone_reported(self, tmp_path):
        """A zero-fill zone fails the audit even when the pads happen to
        be connected by stitching copper -- a dead pour is the #3482
        boundary-test illusion this audit exists to catch."""
        mod = _load_helper_module()
        audit = {"+1V8": {**_net_ok(), "zero_fill_zones": 1}}
        failures = mod.measure_pour_connectivity(
            _fake_recipe(audit), tmp_path / "x.kicad_pcb", {"+1V8"}
        )
        assert len(failures) == 1
        assert "ZERO filled polygons" in failures[0]

    def test_missing_net_in_audit_reported(self, tmp_path):
        mod = _load_helper_module()
        failures = mod.measure_pour_connectivity(
            _fake_recipe({}), tmp_path / "x.kicad_pcb", {"GND"}
        )
        assert failures == ["GND: missing from audit result"]

    def test_missing_audit_fn_raises(self, tmp_path):
        """A recipe that declares the contract but lost its audit function
        is a tool failure, never a silent pass (the PR #3481 lesson)."""
        mod = _load_helper_module()
        with pytest.raises(RuntimeError, match="_audit_pour_nets"):
            mod.measure_pour_connectivity(
                _fake_recipe(omit_audit_fn=True), tmp_path / "x.kicad_pcb", {"GND"}
            )

    def test_audit_crash_raises(self, tmp_path):
        mod = _load_helper_module()
        with pytest.raises(RuntimeError, match="crashed"):
            mod.measure_pour_connectivity(
                _fake_recipe(crash=True), tmp_path / "x.kicad_pcb", {"GND"}
            )


# ---------------------------------------------------------------------------
# Issue #3617: board 07 recipe declares the pour contract the gate reads
# ---------------------------------------------------------------------------


class TestBoard07PourContract:
    def test_board_07_declares_pour_connectivity_contract(self):
        """Board 07 must opt in -- the explicit gate, never silent."""
        mod = _load_board_recipe_module()
        assert mod is not None
        assert mod.REQUIRE_POUR_CONNECTIVITY is True
        # The repair loop budget must mirror board 06's #3509 floor.
        assert mod.MAX_POUR_REPAIR_ROUNDS >= 6
        # The audit function the gate calls must exist.
        assert callable(mod._audit_pour_nets)

    def test_board_07_pour_nets(self):
        mod = _load_board_recipe_module()
        assert set(mod.POUR_NETS) == {"GND", "+1V2", "+1V8"}


# ---------------------------------------------------------------------------
# Issue #7319: reach contract (board 06's #3413 phase 5 shape)
# ---------------------------------------------------------------------------

# Two signal nets: SIG_A routed pad-to-pad, SIG_B unrouted.
_REACH_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "SIG_A")
  (net 2 "SIG_B")

  (footprint "R_0402"
    (layer "F.Cu")
    (at 10 10)
    (property "Reference" "R1")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "SIG_A"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 2 "SIG_B"))
  )

  (footprint "R_0402"
    (layer "F.Cu")
    (at 20 10)
    (property "Reference" "R2")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "SIG_A"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 2 "SIG_B"))
  )

  (segment (start 9.5 10) (end 19.5 10) (width 0.25) (layer "F.Cu") (net 1))
)
"""


class TestSignalReachContract:
    """The gate must fail (exit 2, open nets named) below the contract."""

    @pytest.fixture
    def reach_pcb(self, tmp_path):
        p = tmp_path / "reach.kicad_pcb"
        p.write_text(_REACH_PCB)
        return p

    def test_below_contract_fails_and_names_open_nets(self, reach_pcb, tmp_path, capsys):
        mod = _load_helper_module()
        rc = mod.check_signal_reach(tmp_path, reach_pcb, 2, set())
        assert rc == 2
        out = capsys.readouterr().out
        assert "::error" in out
        assert "1/2 signal nets complete" in out
        assert "SIG_B" in out

    def test_at_contract_passes(self, reach_pcb, tmp_path):
        mod = _load_helper_module()
        assert mod.check_signal_reach(tmp_path, reach_pcb, 1, set()) == 0

    def test_pour_nets_excluded(self, reach_pcb, tmp_path):
        mod = _load_helper_module()
        # SIG_B as a pour net leaves 1/1 complete: contract of 1 passes.
        assert mod.check_signal_reach(tmp_path, reach_pcb, 1, {"SIG_B"}) == 0

    def test_no_contract_skips(self, reach_pcb, tmp_path):
        mod = _load_helper_module()
        assert mod.check_signal_reach(tmp_path, reach_pcb, None, set()) == 0

    def test_measurement_failure_is_tool_failure(self, tmp_path):
        mod = _load_helper_module()
        missing = tmp_path / "missing.kicad_pcb"
        assert mod.check_signal_reach(tmp_path, missing, 1, set()) == 1

    def test_gate_shares_board_06_reach_counter(self):
        """One counter for both gates -- no copy (#7319)."""
        mod = _load_helper_module()
        sys.path.insert(0, str(REPO_ROOT / "scripts" / "ci"))
        try:
            import board_recipe_artifacts
        finally:
            sys.path.pop(0)
        assert mod.measure_signal_reach is board_recipe_artifacts.measure_signal_reach

    def test_board_07_declares_reach_contract(self):
        mod = _load_board_recipe_module()
        assert mod.REQUIRED_SIGNAL_REACH == 30

    def test_committed_fixture_fails_reach_contract(self, tmp_path, capsys):
        """The committed 27/31 fixture (#6730 / #7283) is below 30: it bites."""
        mod = _load_helper_module()
        recipe = _load_board_recipe_module()
        board_dir = REPO_ROOT / "boards" / "07-matchgroup-test"
        pcb = board_dir / "regression-fixture" / "matchgroup_test_routed.kicad_pcb"
        rc = mod.check_signal_reach(
            board_dir, pcb, recipe.REQUIRED_SIGNAL_REACH, set(recipe.POUR_NETS)
        )
        assert rc == 2
        out = capsys.readouterr().out
        for net in ("DQ3", "DQ4", "TMDS_D0_N", "TMDS_D1_N"):
            assert net in out


# ---------------------------------------------------------------------------
# Issue #3617: CI job runs in the kicad/kicad:10.0 container
# ---------------------------------------------------------------------------


def _matchgroup_job() -> dict:
    workflow = yaml.safe_load(CI_WORKFLOW_PATH.read_text())
    jobs = workflow.get("jobs", {})
    assert JOB_NAME in jobs, f"{JOB_NAME} job missing from ci.yml"
    return jobs[JOB_NAME]


class TestMatchgroupJobContainer:
    def test_job_runs_in_kicad_container(self):
        """Issue #3617: the job needs kicad-cli (zone fills), so it must
        run in the workflow's pinned KiCad container (workflow-level
        KICAD_IMAGE digest via the kicad_pin bridge, #5682) like the
        diffpair job."""
        job = _matchgroup_job()
        container = job.get("container")
        assert container is not None, "matchgroup job must declare a container"
        assert container["image"] == "${{ needs.kicad_pin.outputs.image }}"

    def test_job_probes_kicad_cli_early(self):
        """An early ``kicad-cli --version`` probe makes a lost filler
        backend fail setup attributably instead of surfacing later as
        zero-fill pours."""
        job = _matchgroup_job()
        steps = job.get("steps", [])
        assert steps, "matchgroup job must declare steps"
        # The prerequisites/probe step must come before the re-route step.
        probe_idx = next(
            (i for i, s in enumerate(steps) if "kicad-cli --version" in (s.get("run") or "")),
            None,
        )
        assert probe_idx is not None, "no kicad-cli --version probe step found"
        reroute_idx = next(
            (
                i
                for i, s in enumerate(steps)
                if "check_matchgroup_coverage.py" in (s.get("run") or "")
            ),
            None,
        )
        assert reroute_idx is not None, "no re-route step found"
        assert probe_idx < reroute_idx

    def test_job_timeout_has_container_headroom(self):
        """Container pull + apt + real fills need more than the prior
        15-min ceiling (Issue #3617 re-measure)."""
        job = _matchgroup_job()
        assert job.get("timeout-minutes", 0) >= 20
