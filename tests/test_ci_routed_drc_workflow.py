"""Tests for the `routed-pcb-drc-check` CI job and its helper script.

Issue #2546 added a CI gate that validates committed `*_routed.kicad_pcb`
files via ``kct check --mfr jlcpcb --errors-only``. These tests pin the
critical structural properties of the workflow YAML, the allowlist file,
and the helper script's allowlist semantics so regressions in any of the
three layers are caught immediately rather than at the next CI run.

Out of scope (covered by other tests):
    * The DRC engine itself -- ``DRCChecker`` has its own test suite.
    * The ``kct check`` CLI surface -- ``test_check_cmd.py`` covers that.

What we DO assert here:
    * The `routed-pcb-drc-check` job exists in `.github/workflows/ci.yml`.
    * The job uses ``fetch-depth: 0`` (required for ``git diff origin/main...HEAD``).
    * The job invokes the helper script (so a refactor that drops the call
      is caught).
    * The allowlist YAML parses, has a ``tolerances`` mapping, and only
      contains paths matching the routed-PCB pattern.
    * The helper script's ``load_allowlist`` and ``check_file`` functions
      enforce the documented "allowed minus epsilon" semantic.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"
ALLOWLIST_PATH = REPO_ROOT / ".github" / "routed-drc-tolerance.yml"
HELPER_SCRIPT_PATH = REPO_ROOT / "scripts" / "ci" / "check_routed_drc.py"

JOB_NAME = "routed-pcb-drc-check"


# ---------------------------------------------------------------------------
# Helpers to load the helper script as a module without a package install.
# ---------------------------------------------------------------------------


def _load_helper_module():
    """Import scripts/ci/check_routed_drc.py as a module."""
    spec = importlib.util.spec_from_file_location("check_routed_drc", HELPER_SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_routed_drc"] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Workflow YAML structural tests
# ---------------------------------------------------------------------------


class TestWorkflowYAML:
    """The CI gate is only useful if the YAML is well-formed and the job
    is wired up correctly. These tests pin the load-bearing structure."""

    @pytest.fixture
    def workflow(self) -> dict:
        assert CI_WORKFLOW_PATH.is_file(), (
            f".github/workflows/ci.yml not found at {CI_WORKFLOW_PATH}"
        )
        with CI_WORKFLOW_PATH.open() as f:
            return yaml.safe_load(f)

    def test_workflow_parses(self, workflow: dict) -> None:
        """The workflow must be valid YAML with the expected top-level shape."""
        assert isinstance(workflow, dict)
        assert "jobs" in workflow
        assert isinstance(workflow["jobs"], dict)

    def test_routed_drc_job_exists(self, workflow: dict) -> None:
        """The `routed-pcb-drc-check` job must exist in ci.yml."""
        assert JOB_NAME in workflow["jobs"], (
            f"Expected job '{JOB_NAME}' in .github/workflows/ci.yml. "
            f"Found: {sorted(workflow['jobs'].keys())}"
        )

    def test_job_runs_on_ubuntu(self, workflow: dict) -> None:
        """Must use ubuntu-latest, NOT a kicad/kicad container -- the gate is
        pure-Python and we don't want to pay the container-pull cost."""
        job = workflow["jobs"][JOB_NAME]
        assert job["runs-on"] == "ubuntu-latest"
        # Defensive: a future refactor must not silently switch to a container.
        assert "container" not in job, (
            "routed-pcb-drc-check should NOT run in a container -- "
            "kct check is pure-Python and the job piggybacks on the cheap "
            "ubuntu-latest + uv pattern."
        )

    def test_job_uses_fetch_depth_zero(self, workflow: dict) -> None:
        """`git diff origin/main...HEAD` requires the merge-base, which is
        only present with fetch-depth: 0. Default fetch-depth: 1 silently
        breaks the diff with 'unknown revision'."""
        steps = workflow["jobs"][JOB_NAME]["steps"]
        checkout = next(
            (
                s
                for s in steps
                if isinstance(s, dict) and s.get("uses", "").startswith("actions/checkout")
            ),
            None,
        )
        assert checkout is not None, "routed-pcb-drc-check must use actions/checkout"
        assert checkout.get("with", {}).get("fetch-depth") == 0, (
            "actions/checkout must use fetch-depth: 0 so `git diff "
            "origin/main...HEAD` can resolve the merge-base."
        )

    def test_job_invokes_helper_script(self, workflow: dict) -> None:
        """Final step must invoke scripts/ci/check_routed_drc.py. Pinning
        the call site prevents an inadvertent refactor (e.g., to inline
        bash) from silently dropping the allowlist comparison."""
        steps = workflow["jobs"][JOB_NAME]["steps"]
        run_blocks = [s.get("run", "") for s in steps if isinstance(s, dict) and "run" in s]
        joined = "\n".join(run_blocks)
        assert "scripts/ci/check_routed_drc.py" in joined, (
            "routed-pcb-drc-check must invoke scripts/ci/check_routed_drc.py "
            "(the helper that reads the per-board allowlist and emits "
            "::error:: annotations)."
        )

    def test_job_has_short_circuit_on_no_files(self, workflow: dict) -> None:
        """Per the issue's acceptance criteria, the job must complete in
        <60s on PRs that don't touch routed PCBs. The implementation
        achieves this via a step-level ``if: steps.changed.outputs.files != ''``
        guard. Absence of that guard would cause every PR to pay the
        ``uv sync`` + ``kct check`` cost unnecessarily."""
        steps = workflow["jobs"][JOB_NAME]["steps"]
        guarded = [
            s for s in steps if isinstance(s, dict) and "if" in s and "files" in str(s["if"])
        ]
        assert guarded, (
            "Expected at least one step guarded by `if: "
            "steps.changed.outputs.files != ''` so the job short-circuits "
            "on PRs that don't touch routed PCBs."
        )

    def test_job_has_reasonable_timeout(self, workflow: dict) -> None:
        """A timeout prevents a runaway DRC check from blocking the queue.
        10 minutes is plenty for ~5 boards * <1min each."""
        job = workflow["jobs"][JOB_NAME]
        timeout = job.get("timeout-minutes")
        assert isinstance(timeout, int) and 1 <= timeout <= 30, (
            f"timeout-minutes must be set to a sane value (1-30); got {timeout!r}"
        )


# ---------------------------------------------------------------------------
# Allowlist YAML tests
# ---------------------------------------------------------------------------


class TestAllowlist:
    """The allowlist file is the gate's "minus epsilon" floor. A malformed
    file would silently break CI for every PR that touches a routed PCB."""

    @pytest.fixture
    def allowlist_data(self) -> dict:
        assert ALLOWLIST_PATH.is_file(), f"Allowlist file expected at {ALLOWLIST_PATH}"
        with ALLOWLIST_PATH.open() as f:
            return yaml.safe_load(f)

    def test_allowlist_parses(self, allowlist_data: dict) -> None:
        assert isinstance(allowlist_data, dict)
        assert "tolerances" in allowlist_data
        assert isinstance(allowlist_data["tolerances"], dict)

    def test_allowlist_paths_match_routed_pattern(self, allowlist_data: dict) -> None:
        """Every key in the allowlist must be a repo-relative path to a
        routed PCB. Catches typos like a stray space, a wrong board number,
        or accidentally listing the unrouted PCB."""
        import re

        pattern = re.compile(r"^boards/[^/]+/output/[^/]+_routed\.kicad_pcb$")
        for key in allowlist_data["tolerances"]:
            assert pattern.match(key), (
                f"Allowlist key {key!r} does not match the expected pattern "
                f"'boards/<name>/output/<file>_routed.kicad_pcb'."
            )

    def test_allowlist_values_are_non_negative_ints(self, allowlist_data: dict) -> None:
        for key, value in allowlist_data["tolerances"].items():
            assert isinstance(value, int) and not isinstance(value, bool), (
                f"Allowlist value for {key!r} must be an int, got {type(value).__name__}"
            )
            assert value >= 0, f"Allowlist value for {key!r} must be non-negative, got {value}"

    def test_allowlist_files_exist_on_disk(self, allowlist_data: dict) -> None:
        """A grandfather entry pointing at a missing file is dead config --
        likely the file was renamed or deleted and the allowlist wasn't
        updated. Keep the allowlist in sync with the tree."""
        for key in allowlist_data["tolerances"]:
            full_path = REPO_ROOT / key
            assert full_path.is_file(), (
                f"Allowlist references {key!r} but no such file exists "
                f"at {full_path}. Either remove the stale entry or fix "
                f"the path."
            )


# ---------------------------------------------------------------------------
# Helper script unit tests (allowlist semantics)
# ---------------------------------------------------------------------------


class TestHelperLoadAllowlist:
    """Unit tests for the helper's ``load_allowlist`` -- the parse layer."""

    def setup_method(self) -> None:
        self.helper = _load_helper_module()

    def test_missing_file_is_empty_allowlist(self, tmp_path: Path) -> None:
        """A missing allowlist file means strict 0-error gate for everything."""
        missing = tmp_path / "does-not-exist.yml"
        result = self.helper.load_allowlist(missing)
        assert result == {}

    def test_empty_file_is_empty_allowlist(self, tmp_path: Path) -> None:
        f = tmp_path / "empty.yml"
        f.write_text("")
        assert self.helper.load_allowlist(f) == {}

    def test_valid_allowlist_parses(self, tmp_path: Path) -> None:
        f = tmp_path / "valid.yml"
        f.write_text(
            "tolerances:\n"
            "  boards/03-x/output/x_routed.kicad_pcb: 1\n"
            "  boards/05-y/output/y_routed.kicad_pcb: 17\n"
        )
        result = self.helper.load_allowlist(f)
        assert result == {
            "boards/03-x/output/x_routed.kicad_pcb": 1,
            "boards/05-y/output/y_routed.kicad_pcb": 17,
        }

    def test_malformed_yaml_raises(self, tmp_path: Path) -> None:
        f = tmp_path / "broken.yml"
        f.write_text("tolerances:\n  -\n  bad: : :")
        with pytest.raises(ValueError, match="Malformed allowlist YAML"):
            self.helper.load_allowlist(f)

    def test_top_level_must_be_mapping(self, tmp_path: Path) -> None:
        f = tmp_path / "list.yml"
        f.write_text("- foo\n- bar\n")
        with pytest.raises(ValueError, match="must be a YAML mapping"):
            self.helper.load_allowlist(f)

    def test_tolerances_must_be_mapping(self, tmp_path: Path) -> None:
        f = tmp_path / "wrong-type.yml"
        f.write_text("tolerances:\n  - bad\n")
        with pytest.raises(ValueError, match="'tolerances' field must be a mapping"):
            self.helper.load_allowlist(f)

    def test_negative_value_rejected(self, tmp_path: Path) -> None:
        f = tmp_path / "neg.yml"
        f.write_text("tolerances:\n  foo: -1\n")
        with pytest.raises(ValueError, match="non-negative integer"):
            self.helper.load_allowlist(f)

    def test_bool_value_rejected(self, tmp_path: Path) -> None:
        """YAML implicitly maps `true`/`false` to bools; bool is also an
        int subclass in Python. The validator must reject this explicitly
        to avoid 'True == 1' confusion."""
        f = tmp_path / "bool.yml"
        f.write_text("tolerances:\n  foo: true\n")
        with pytest.raises(ValueError, match="non-negative integer"):
            self.helper.load_allowlist(f)


class TestHelperCheckFile:
    """Unit tests for ``check_file`` -- the per-PCB gate logic.

    These tests stub ``count_errors`` so we don't depend on the actual DRC
    engine here; the CI integration tests cover the end-to-end path.
    """

    def setup_method(self) -> None:
        self.helper = _load_helper_module()

    def test_zero_errors_zero_allowed_passes(self) -> None:
        with patch.object(self.helper, "count_errors", return_value=0):
            passed, msg = self.helper.check_file(Path("foo.kicad_pcb"), allowed=0)
        assert passed
        assert "0 errors" in msg

    def test_under_allowed_passes(self) -> None:
        """A board grandfathered at 17 errors that drops to 5 must pass --
        the gate is "allowed minus epsilon", not "exact match"."""
        with patch.object(self.helper, "count_errors", return_value=5):
            passed, msg = self.helper.check_file(Path("foo.kicad_pcb"), allowed=17)
        assert passed
        assert "5 errors" in msg
        # The pass message should nudge the contributor to lower the
        # allowlist if the count drops -- this prevents stale tolerances.
        assert "reduce the allowlist value" in msg

    def test_at_allowed_passes(self) -> None:
        """Equal-to-allowed is the steady state for a grandfathered board."""
        with patch.object(self.helper, "count_errors", return_value=17):
            passed, _ = self.helper.check_file(Path("foo.kicad_pcb"), allowed=17)
        assert passed

    def test_over_allowed_fails_grandfathered(self) -> None:
        """Regression on a grandfathered board: 17 -> 22. Must fail."""
        with patch.object(self.helper, "count_errors", return_value=22):
            passed, msg = self.helper.check_file(Path("foo.kicad_pcb"), allowed=17)
        assert not passed
        assert "regression" in msg.lower()
        assert "17" in msg
        assert "22" in msg

    def test_nonzero_with_zero_allowed_fails(self) -> None:
        """Default case: a board not in the allowlist (allowed=0) must
        report 0 errors. Any errors mean the gate fails. This is the
        critical assertion that PR #2538's merge state would have
        triggered (board 04 with 5 errors)."""
        with patch.object(self.helper, "count_errors", return_value=5):
            passed, msg = self.helper.check_file(
                Path("boards/04-x/output/x_routed.kicad_pcb"), allowed=0
            )
        assert not passed
        # Message must point the contributor at the allowlist for the
        # grandfathering escape hatch.
        assert "routed-drc-tolerance.yml" in msg
        assert "5 error" in msg


class TestHelperMain:
    """Smoke tests for the CLI entry-point -- empty-files short-circuit
    and end-to-end happy path."""

    def setup_method(self) -> None:
        self.helper = _load_helper_module()

    def test_no_files_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Per acceptance criteria #5: the gate must complete in <60s and
        exit 0 when no routed PCBs are modified."""
        rc = self.helper.main([])
        assert rc == 0
        captured = capsys.readouterr()
        assert "no-op" in captured.out.lower()

    def test_missing_file_is_warning_not_failure(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A file that was deleted in the PR (e.g., board removed) shows
        up in `git diff --name-only` but won't be on disk. Must warn,
        not fail."""
        # Use a path that can't possibly exist.
        rc = self.helper.main(
            [
                "--allowlist",
                str(tmp_path / "missing-allowlist.yml"),
                str(tmp_path / "nonexistent_routed.kicad_pcb"),
            ]
        )
        assert rc == 0
        captured = capsys.readouterr()
        assert "::warning" in captured.out
        assert "deleted in PR" in captured.out

    def test_malformed_allowlist_exits_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        bad = tmp_path / "bad.yml"
        bad.write_text("tolerances: [oops, this, is, a, list]\n")
        # Need at least one file argument so the allowlist is actually loaded.
        pcb = tmp_path / "foo_routed.kicad_pcb"
        pcb.touch()
        rc = self.helper.main(["--allowlist", str(bad), str(pcb)])
        assert rc == 1
        captured = capsys.readouterr()
        assert "::error" in captured.out


# ---------------------------------------------------------------------------
# Allowlist self-consistency: helper must accept the real allowlist file.
# ---------------------------------------------------------------------------


class TestRealAllowlistRoundtrip:
    """Sanity check: the live allowlist must load cleanly through the
    helper. If someone introduces a malformed entry, the test suite
    catches it before the CI run."""

    def setup_method(self) -> None:
        self.helper = _load_helper_module()

    def test_live_allowlist_loads(self) -> None:
        result = self.helper.load_allowlist(ALLOWLIST_PATH)
        # We don't assert specific board entries -- those will change as
        # boards reach 0 errors and entries are removed. Just assert the
        # types are right.
        assert isinstance(result, dict)
        for key, value in result.items():
            assert isinstance(key, str)
            assert isinstance(value, int)
            assert value >= 0
