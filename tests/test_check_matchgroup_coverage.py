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

    def test_board_07_documented_via_skew_baseline(self):
        """Board 07's documented match-group baseline is 1 -- a single value
        that satisfies BOTH the reroute and ``--skip-route`` paths.

        On the reroute path (the Match-Group Routing Regression CI job) the
        #3931 via-aware match-group tuner compensates ADDR_BUS's via-count
        imbalance (A4/A6 escape behind a full-stack via; the tuner adds F.Cu
        meander to the flat members), so ADDR_BUS's via-inclusive skew
        converges to within tolerance and no longer fires.  Only MIPI_CSI_LANES'
        pair-only skew (the group #3916 newly makes checkable) remains, giving a
        reroute count of 1.  HDMI_TMDS_LANES does not fire -- its
        TMDS_D0_N/TMDS_D1_N legs stay unrouted after the reroute, so the
        unrouted-leg gate excludes it.

        On the ``--skip-route`` committed-artifact path the count is also 1: the
        #3931 router fix does NOT regenerate the committed artifact (an artifact
        refresh is the separately-tracked #3925 churn work), so that artifact
        still carries the pre-fix via-imbalanced ADDR_BUS (which fires) while
        MIPI/HDMI/DDR are gated out by the unrouted-leg rule.  Neither count is
        silently absorbed by the large error-count allowlist floor."""
        mod = _load_helper_module()
        key = "boards/07-matchgroup-test/output/matchgroup_test_routed.kicad_pcb"
        assert mod.MATCHGROUP_VIOLATION_BASELINE.get(key, 0) == 1


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
        run in the kicad/kicad:10.0 container like the diffpair job."""
        job = _matchgroup_job()
        container = job.get("container")
        assert container is not None, "matchgroup job must declare a container"
        assert container["image"] == "kicad/kicad:10.0"

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
