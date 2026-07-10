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
        or accidentally listing the unrouted PCB.

        The board directory may be nested (e.g.
        ``boards/external/softstart/output/softstart_routed.kicad_pcb``,
        grandfathered by Issue #3527), so the pattern allows one or more
        path components between ``boards/`` and ``output/``."""
        import re

        pattern = re.compile(r"^boards/(?:[^/]+/)+output/[^/]+_routed\.kicad_pcb$")
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


class TestHelperLoadManufacturers:
    """Unit tests for ``load_manufacturers`` -- the optional per-board
    ``--mfr`` profile override map (issue #3033 / PR #3038).  The map is
    a separate top-level mapping in the same YAML file so the original
    ``load_allowlist`` API and its tests stay backward-compatible."""

    def setup_method(self) -> None:
        self.helper = _load_helper_module()

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        """A missing YAML file means every board uses the default profile."""
        assert self.helper.load_manufacturers(tmp_path / "does-not-exist.yml") == {}

    def test_no_manufacturers_key_returns_empty(self, tmp_path: Path) -> None:
        """A YAML file with only ``tolerances:`` (the common case) returns
        an empty manufacturers map; every board uses the default profile."""
        f = tmp_path / "tolerances-only.yml"
        f.write_text("tolerances:\n  boards/x/output/x_routed.kicad_pcb: 1\n")
        assert self.helper.load_manufacturers(f) == {}

    def test_valid_manufacturers_parses(self, tmp_path: Path) -> None:
        f = tmp_path / "with-mfr.yml"
        f.write_text(
            "tolerances:\n"
            "  boards/04-y/output/y_routed.kicad_pcb: 4\n"
            "manufacturers:\n"
            "  boards/04-y/output/y_routed.kicad_pcb: jlcpcb-tier1\n"
        )
        assert self.helper.load_manufacturers(f) == {
            "boards/04-y/output/y_routed.kicad_pcb": "jlcpcb-tier1",
        }

    def test_manufacturers_must_be_mapping(self, tmp_path: Path) -> None:
        f = tmp_path / "bad-mfr.yml"
        f.write_text("manufacturers:\n  - oops\n")
        with pytest.raises(ValueError, match="'manufacturers' field must be a mapping"):
            self.helper.load_manufacturers(f)

    def test_empty_string_profile_rejected(self, tmp_path: Path) -> None:
        f = tmp_path / "empty-profile.yml"
        f.write_text("manufacturers:\n  boards/x/output/x_routed.kicad_pcb: ''\n")
        with pytest.raises(ValueError, match="non-empty string profile name"):
            self.helper.load_manufacturers(f)

    def test_non_string_profile_rejected(self, tmp_path: Path) -> None:
        f = tmp_path / "int-profile.yml"
        f.write_text("manufacturers:\n  boards/x/output/x_routed.kicad_pcb: 42\n")
        with pytest.raises(ValueError, match="non-empty string profile name"):
            self.helper.load_manufacturers(f)

    def test_live_allowlist_manufacturers_load(self) -> None:
        """The real allowlist must load cleanly through the manufacturers
        reader too (mirrors TestRealAllowlistRoundtrip for the new field)."""
        result = self.helper.load_manufacturers(ALLOWLIST_PATH)
        assert isinstance(result, dict)
        for key, value in result.items():
            assert isinstance(key, str)
            assert isinstance(value, str) and value


class TestHelperCheckFile:
    """Unit tests for ``check_file`` -- the per-PCB gate logic.

    These tests stub ``count_errors`` so we don't depend on the actual DRC
    engine here; the CI integration tests cover the end-to-end path.
    """

    def setup_method(self) -> None:
        self.helper = _load_helper_module()

    def test_zero_errors_zero_allowed_passes(self) -> None:
        # Issue #3074: count_errors now returns (blocking_errors,
        # advisory_by_rule).  Stub with the no-advisory tuple to keep
        # the historical semantics of "0 blocking + 0 advisory".
        with patch.object(self.helper, "count_errors", return_value=(0, {})):
            passed, msg, errors = self.helper.check_file(Path("foo.kicad_pcb"), allowed=0)
        assert passed
        assert "0 errors" in msg
        assert errors == 0

    def test_under_allowed_passes(self) -> None:
        """A board grandfathered at 17 errors that drops to 5 must pass --
        the gate is "allowed minus epsilon", not "exact match"."""
        with patch.object(self.helper, "count_errors", return_value=(5, {})):
            passed, msg, errors = self.helper.check_file(Path("foo.kicad_pcb"), allowed=17)
        assert passed
        assert "5 errors" in msg
        # The pass message should nudge the contributor to lower the
        # allowlist if the count drops -- this prevents stale tolerances.
        assert "reduce the allowlist value" in msg
        assert errors == 5

    def test_at_allowed_passes(self) -> None:
        """Equal-to-allowed is the steady state for a grandfathered board."""
        with patch.object(self.helper, "count_errors", return_value=(17, {})):
            passed, _, errors = self.helper.check_file(Path("foo.kicad_pcb"), allowed=17)
        assert passed
        assert errors == 17

    def test_over_allowed_fails_grandfathered(self) -> None:
        """Regression on a grandfathered board: 17 -> 22. Must fail."""
        with patch.object(self.helper, "count_errors", return_value=(22, {})):
            passed, msg, errors = self.helper.check_file(Path("foo.kicad_pcb"), allowed=17)
        assert not passed
        assert "regression" in msg.lower()
        assert "17" in msg
        assert "22" in msg
        assert errors == 22

    def test_nonzero_with_zero_allowed_fails(self) -> None:
        """Default case: a board not in the allowlist (allowed=0) must
        report 0 errors. Any errors mean the gate fails. This is the
        critical assertion that PR #2538's merge state would have
        triggered (board 04 with 5 errors)."""
        with patch.object(self.helper, "count_errors", return_value=(5, {})):
            passed, msg, errors = self.helper.check_file(
                Path("boards/04-x/output/x_routed.kicad_pcb"), allowed=0
            )
        assert not passed
        # Message must point the contributor at the allowlist for the
        # grandfathering escape hatch.
        assert "routed-drc-tolerance.yml" in msg
        assert "5 " in msg and "error" in msg
        assert errors == 5

    def test_mfr_override_forwards_to_count_errors(self) -> None:
        """Issue #3033 / PR #3038: the per-board `manufacturers:` override
        must reach ``count_errors`` as the ``mfr`` keyword (not be silently
        dropped).  Pin this so a future refactor of the call chain doesn't
        regress board-04's tier-aware measurement."""
        with patch.object(self.helper, "count_errors", return_value=(4, {})) as mock:
            passed, msg, errors = self.helper.check_file(
                Path("foo.kicad_pcb"), allowed=4, mfr="jlcpcb-tier1"
            )
        assert passed
        assert errors == 4
        # The mfr must propagate verbatim to count_errors.
        mock.assert_called_once()
        call_kwargs = mock.call_args.kwargs
        assert call_kwargs.get("mfr") == "jlcpcb-tier1"
        # And surface in the human-readable message so contributors
        # can tell which profile produced the count.
        assert "jlcpcb-tier1" in msg

    def test_mfr_default_is_jlcpcb(self) -> None:
        """When no override is supplied, ``check_file`` must fall back to
        the strict ``jlcpcb`` profile (the historical default)."""
        with patch.object(self.helper, "count_errors", return_value=(0, {})) as mock:
            self.helper.check_file(Path("foo.kicad_pcb"), allowed=0)
        call_kwargs = mock.call_args.kwargs
        # Default may be passed explicitly or omitted; both routes resolve
        # to "jlcpcb" via the function signature default.
        assert call_kwargs.get("mfr", self.helper.DEFAULT_MANUFACTURER) == "jlcpcb"


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
# Drift warning tests (issue #2590)
# ---------------------------------------------------------------------------


class TestHelperDriftWarning:
    """Unit tests for ``annotate_drift_warning`` -- the warning-annotation
    helper that surfaces stale allowlist entries (slack > 0) to PR
    reviewers via the GitHub Files-changed view (issue #2590)."""

    def setup_method(self) -> None:
        self.helper = _load_helper_module()

    def test_warning_format(self, capsys: pytest.CaptureFixture[str]) -> None:
        """The annotation must use the ``::warning file=...::`` form so
        GitHub anchors it to the file in the Files-changed view (matches
        the deleted-file precedent at check_routed_drc.py's main loop)."""
        path = "boards/05-bldc-motor-controller/output/bldc_controller_routed.kicad_pcb"
        self.helper.annotate_drift_warning(path, errors=15, allowed=17)
        captured = capsys.readouterr()
        # GitHub annotation prefix with the right file= target.
        assert f"::warning file={path}::" in captured.out

    def test_warning_includes_actual_and_allowed_counts(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The warning text must include both numbers so reviewers can
        read the slack at a glance without opening the allowlist."""
        path = "boards/05-bldc-motor-controller/output/bldc_controller_routed.kicad_pcb"
        self.helper.annotate_drift_warning(path, errors=15, allowed=17)
        captured = capsys.readouterr()
        assert "is 17" in captured.out  # allowlist value
        assert "actual is 15" in captured.out  # actual count

    def test_warning_includes_recommended_new_value(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Per acceptance criteria #2: the warning must tell the contributor
        what to set the allowlist to (= the actual count). This is the
        load-bearing 'tighten to <N>' phrasing."""
        path = "boards/05-bldc-motor-controller/output/bldc_controller_routed.kicad_pcb"
        self.helper.annotate_drift_warning(path, errors=15, allowed=17)
        captured = capsys.readouterr()
        assert "Tighten to 15" in captured.out

    def test_warning_points_at_allowlist_file(self, capsys: pytest.CaptureFixture[str]) -> None:
        """The warning must reference the allowlist file path so the
        reviewer can navigate to where the fix belongs."""
        path = "boards/03-usb-joystick/output/usb_joystick_routed.kicad_pcb"
        self.helper.annotate_drift_warning(path, errors=0, allowed=1)
        captured = capsys.readouterr()
        assert ".github/routed-drc-tolerance.yml" in captured.out


class TestMainDriftWarningEmission:
    """Integration tests for ``main()``'s drift-warning emission flow
    (issue #2590).

    These stub ``count_errors`` so we exercise the gate without invoking
    ``kct check``. The point is to pin the conditions under which a
    warning IS or IS NOT emitted, since the silent stdout drift was the
    original bug.
    """

    def setup_method(self) -> None:
        self.helper = _load_helper_module()

    def _make_pcb_and_allowlist(
        self, tmp_path: Path, allowlist_value: int | None
    ) -> tuple[Path, Path]:
        """Create a placeholder .kicad_pcb file in a routed-pattern path
        and an allowlist YAML pointing at it (or empty if value is None).

        Returns ``(pcb_path, allowlist_path)``.
        """
        # Mirror the boards/<name>/output/<file>_routed.kicad_pcb pattern
        # so the path looks like a real routed PCB.
        pcb_dir = tmp_path / "boards" / "99-test" / "output"
        pcb_dir.mkdir(parents=True)
        pcb = pcb_dir / "test_routed.kicad_pcb"
        pcb.touch()

        allowlist = tmp_path / "tolerance.yml"
        if allowlist_value is None:
            allowlist.write_text("tolerances: {}\n")
        else:
            # Use the path as passed on argv (the helper resolves it
            # relative to cwd before lookup, but raw form works for
            # tmp_path-rooted absolute paths too -- we set it here so the
            # lookup_key matches what main() computes).
            allowlist.write_text(f"tolerances:\n  '{pcb}': {allowlist_value}\n")
        return pcb, allowlist

    def test_slack_positive_emits_drift_warning(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The PR #2583 scenario: actual=15, allowlist=17. Gate passes,
        but a ``::warning file=...::`` MUST be emitted so the reviewer
        sees the stale entry. This is the regression test for #2590."""
        pcb, allowlist = self._make_pcb_and_allowlist(tmp_path, allowlist_value=17)
        # Issue #3074: count_errors returns (blocking, advisory_by_rule).
        with patch.object(self.helper, "count_errors", return_value=(15, {})):
            rc = self.helper.main(["--allowlist", str(allowlist), str(pcb)])
        assert rc == 0  # gate still passes
        captured = capsys.readouterr()
        assert "::warning file=" in captured.out
        assert "Tighten to 15" in captured.out
        assert "actual is 15" in captured.out
        assert "is 17" in captured.out

    def test_slack_zero_no_drift_warning(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Steady state: actual=17, allowlist=17. No warning -- the
        floor matches reality. (The OK message still goes to stdout.)"""
        pcb, allowlist = self._make_pcb_and_allowlist(tmp_path, allowlist_value=17)
        with patch.object(self.helper, "count_errors", return_value=(17, {})):
            rc = self.helper.main(["--allowlist", str(allowlist), str(pcb)])
        assert rc == 0
        captured = capsys.readouterr()
        # The "deleted in PR" warning is unrelated; we assert the drift
        # warning specifically is absent.
        assert "Tighten to" not in captured.out
        assert "::warning file=" not in captured.out

    def test_slack_negative_failure_no_drift_warning(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Regression case: actual=22, allowlist=17. Gate FAILS with
        ::error::, and we must NOT also emit a drift warning -- the
        problem here is too many errors, not a stale floor."""
        pcb, allowlist = self._make_pcb_and_allowlist(tmp_path, allowlist_value=17)
        with patch.object(self.helper, "count_errors", return_value=(22, {})):
            rc = self.helper.main(["--allowlist", str(allowlist), str(pcb)])
        assert rc == 2  # gate fails (regression)
        captured = capsys.readouterr()
        assert "::error file=" in captured.out
        assert "Tighten to" not in captured.out

    def test_unlisted_board_zero_errors_no_drift_warning(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A board not in the allowlist (allowed=0) with 0 errors is the
        common clean-state case (boards 01/02/04). Slack is 0, but more
        importantly the ``allowed > 0`` guard suppresses the warning so
        every clean PR doesn't get nagged."""
        pcb, allowlist = self._make_pcb_and_allowlist(tmp_path, allowlist_value=None)
        with patch.object(self.helper, "count_errors", return_value=(0, {})):
            rc = self.helper.main(["--allowlist", str(allowlist), str(pcb)])
        assert rc == 0
        captured = capsys.readouterr()
        assert "Tighten to" not in captured.out
        assert "::warning file=" not in captured.out

    def test_drift_warning_does_not_change_exit_code(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Per acceptance criteria #3 + #5: warnings are advisory. A PR
        that triggers the drift warning must still exit 0 so the gate
        doesn't become accidentally blocking."""
        pcb, allowlist = self._make_pcb_and_allowlist(tmp_path, allowlist_value=10)
        with patch.object(self.helper, "count_errors", return_value=(3, {})):
            rc = self.helper.main(["--allowlist", str(allowlist), str(pcb)])
        assert rc == 0  # warning emitted, but gate still passes
        captured = capsys.readouterr()
        assert "::warning file=" in captured.out
        assert "Tighten to 3" in captured.out

    def test_stale_allowlist_via_subprocess(self, tmp_path: Path) -> None:
        """End-to-end via subprocess on a real allowlist file with a stale
        entry. Exercises the full main() path including argv parsing,
        allowlist load, and stdout flushing -- the path the CI workflow
        actually takes.

        We mock count_errors via a small shim script that imports the
        helper, monkey-patches count_errors, and calls main(). This avoids
        invoking the real `kct check` (which needs KiCad and is slow).
        """
        import subprocess
        import textwrap

        pcb_dir = tmp_path / "boards" / "05-x" / "output"
        pcb_dir.mkdir(parents=True)
        pcb = pcb_dir / "x_routed.kicad_pcb"
        pcb.touch()

        allowlist = tmp_path / "tolerance.yml"
        # The shim runs from tmp_path; use the same pcb path that argv
        # gets so lookup_key resolves correctly.
        rel_pcb = "boards/05-x/output/x_routed.kicad_pcb"
        allowlist.write_text(f"tolerances:\n  {rel_pcb}: 17\n")

        shim = tmp_path / "shim.py"
        shim.write_text(
            textwrap.dedent(
                f"""
                import importlib.util
                import sys
                from unittest.mock import patch

                spec = importlib.util.spec_from_file_location(
                    "check_routed_drc",
                    {str(HELPER_SCRIPT_PATH)!r},
                )
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)

                # Issue #3074: count_errors returns (blocking, advisory_by_rule).
                with patch.object(module, "count_errors", return_value=(15, {{}})):
                    sys.exit(module.main([
                        "--allowlist", {str(allowlist)!r},
                        {rel_pcb!r},
                    ]))
                """
            )
        )

        proc = subprocess.run(
            [sys.executable, str(shim)],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, f"subprocess exited {proc.returncode}; stderr:\n{proc.stderr}"
        # The drift warning must appear in stdout exactly as GitHub
        # Actions would see it.
        assert "::warning file=" in proc.stdout
        assert rel_pcb in proc.stdout
        assert "Tighten to 15" in proc.stdout

    def test_allow_flag_overrides_yaml_and_passes(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``--allow N`` gates the FRESH-regen path independently of the YAML.

        Board 04's recipe-vs-artifact divergence (#4017): the committed
        artifact is strict-0 (no YAML entry), but the fresh regen still
        routes the legacy 0.350mm drill pair (2 errors).  ``--allow 2`` on
        the fresh-regen invocation tolerates exactly those 2 while the YAML
        stays entry-free for the committed-artifact ratchet.
        """
        pcb, allowlist = self._make_pcb_and_allowlist(tmp_path, allowlist_value=None)
        with patch.object(self.helper, "count_errors", return_value=(2, {})):
            rc = self.helper.main(["--allowlist", str(allowlist), "--allow", "2", str(pcb)])
        assert rc == 0  # 2 <= 2 (explicit override), gate passes

    def test_allow_flag_still_fails_on_excess(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``--allow N`` is a ceiling, not a bypass: N+1 blocking errors fail."""
        pcb, allowlist = self._make_pcb_and_allowlist(tmp_path, allowlist_value=None)
        with patch.object(self.helper, "count_errors", return_value=(3, {})):
            rc = self.helper.main(["--allowlist", str(allowlist), "--allow", "2", str(pcb)])
        assert rc == 2  # 3 > 2, gate fails
        captured = capsys.readouterr()
        assert "::error file=" in captured.out

    def test_allow_flag_suppresses_drift_warning(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """No drift nag under ``--allow``: the tolerance is an intentional
        recipe-vs-artifact divergence, not a stale YAML floor to tighten."""
        pcb, allowlist = self._make_pcb_and_allowlist(tmp_path, allowlist_value=None)
        with patch.object(self.helper, "count_errors", return_value=(0, {})):
            rc = self.helper.main(["--allowlist", str(allowlist), "--allow", "2", str(pcb)])
        assert rc == 0  # 0 <= 2
        captured = capsys.readouterr()
        assert "::warning file=" not in captured.out
        assert "Tighten to" not in captured.out

    def test_allow_flag_rejects_negative(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A negative ``--allow`` is a config error -> exit 1."""
        pcb, allowlist = self._make_pcb_and_allowlist(tmp_path, allowlist_value=None)
        rc = self.helper.main(["--allowlist", str(allowlist), "--allow", "-1", str(pcb)])
        assert rc == 1
        captured = capsys.readouterr()
        assert "--allow must be a non-negative integer" in captured.out


# ---------------------------------------------------------------------------
# main() integration: per-board manufacturer overrides (issue #3033 / PR #3038).
# ---------------------------------------------------------------------------


class TestMainManufacturerOverride:
    """Integration tests for ``main()`` honoring the optional ``manufacturers:``
    map (issue #3033 / PR #3038).  Pins that a board listed under the
    overrides actually has its overridden profile reach ``count_errors``,
    and that boards NOT listed continue to use the default ``jlcpcb``
    profile (the historical behavior)."""

    def setup_method(self) -> None:
        self.helper = _load_helper_module()

    def _make_pcb(self, tmp_path: Path) -> Path:
        pcb_dir = tmp_path / "boards" / "04-test" / "output"
        pcb_dir.mkdir(parents=True)
        pcb = pcb_dir / "test_routed.kicad_pcb"
        pcb.touch()
        return pcb

    def test_per_board_mfr_override_reaches_count_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A board with a ``manufacturers:`` entry must have its overridden
        profile passed to ``count_errors`` via the ``mfr`` keyword."""
        # Create the placeholder PCB file on disk; main() will look it up
        # via Path(rel).is_file() relative to cwd.
        self._make_pcb(tmp_path)
        # main() resolves the file via Path(...).is_file() from cwd and
        # then strips cwd via Path.resolve().relative_to(Path.cwd()) for
        # the allowlist lookup key.  chdir into tmp_path so the rel path
        # resolves correctly on disk AND the lookup key matches the
        # allowlist entry.
        monkeypatch.chdir(tmp_path)
        rel = "boards/04-test/output/test_routed.kicad_pcb"
        allowlist = tmp_path / "tolerance.yml"
        allowlist.write_text(f"tolerances:\n  {rel}: 4\nmanufacturers:\n  {rel}: jlcpcb-tier1\n")
        # Issue #3074: count_errors returns (blocking, advisory_by_rule).
        with patch.object(self.helper, "count_errors", return_value=(4, {})) as mock:
            rc = self.helper.main(["--allowlist", str(allowlist), rel])
        assert rc == 0
        mock.assert_called_once()
        assert mock.call_args.kwargs.get("mfr") == "jlcpcb-tier1"

    def test_unlisted_board_uses_default_mfr(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A board with no ``manufacturers:`` entry must continue to use
        the default ``jlcpcb`` profile (backward compatibility)."""
        self._make_pcb(tmp_path)
        monkeypatch.chdir(tmp_path)
        rel = "boards/04-test/output/test_routed.kicad_pcb"
        allowlist = tmp_path / "tolerance.yml"
        # tolerances entry only; no manufacturers section at all.
        allowlist.write_text(f"tolerances:\n  {rel}: 4\n")
        with patch.object(self.helper, "count_errors", return_value=(4, {})) as mock:
            rc = self.helper.main(["--allowlist", str(allowlist), rel])
        assert rc == 0
        mock.assert_called_once()
        assert mock.call_args.kwargs.get("mfr") == self.helper.DEFAULT_MANUFACTURER


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
