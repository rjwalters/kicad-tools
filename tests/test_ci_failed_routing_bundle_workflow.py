"""Structural tests for the failed-routing-bundle CI steps (Issue #5067).

Four long-running CI jobs re-route/validate a board from scratch and
previously discarded the generated PCB/schematic/DRC output on failure,
making failures hard to reproduce (#5044 / PR #5045: a Board07 failure
needed a full ~30-minute reroute to diagnose because run 34524705221's
Actions artifacts API reported ``total_count: 0``).

These tests pin the load-bearing structure of the fix:

* Each of the four jobs gains a collection step + an
  ``actions/upload-artifact`` step, both gated on ``if: failure()``.
* The upload step has an explicit ``name`` (distinguishing board + job +
  commit + run), ``path`` (scoped to a staging dir, never repo-root or
  ``.venv``/``.git``), and ``retention-days``.
* No existing route/validate step in these jobs gained
  ``continue-on-error: true`` as a side effect (that would hide the
  original gate result, violating the issue's acceptance criteria).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"

JOB_IDS = (
    "diffpair-routing-regression",
    "matchgroup-routing-regression",
    "board-06-end-to-end",
    "board-07-end-to-end",
)

# Steps that existed in these jobs before Issue #5067; used to confirm no
# pre-existing step was retroactively given `continue-on-error: true`.
PRE_EXISTING_STEP_NAME_SUBSTRINGS = (
    "Re-route board",
    "Regenerate board",
    "Copper-LVS",
    "Independent DRC re-check",
    "Assert plane-net completion",
    "Assert artifacts",
)


@pytest.fixture(scope="module")
def workflow() -> dict:
    assert CI_WORKFLOW_PATH.is_file(), f".github/workflows/ci.yml not found at {CI_WORKFLOW_PATH}"
    with CI_WORKFLOW_PATH.open() as f:
        return yaml.safe_load(f)


def _steps(workflow: dict, job_id: str) -> list[dict]:
    job = workflow["jobs"][job_id]
    return [s for s in job["steps"] if isinstance(s, dict)]


def _find_upload_step(steps: list[dict]) -> dict | None:
    return next(
        (s for s in steps if str(s.get("uses", "")).startswith("actions/upload-artifact")),
        None,
    )


def _find_collection_step(steps: list[dict]) -> dict | None:
    return next(
        (s for s in steps if "collect_failed_routing_bundle.py" in str(s.get("run", ""))),
        None,
    )


class TestFourJobsExist:
    def test_all_four_target_jobs_are_present(self, workflow: dict) -> None:
        missing = [j for j in JOB_IDS if j not in workflow["jobs"]]
        assert not missing, f"Expected jobs missing from ci.yml: {missing}"


@pytest.mark.parametrize("job_id", JOB_IDS)
class TestFailedBundleCollectionStep:
    def test_collection_step_exists(self, workflow: dict, job_id: str) -> None:
        steps = _steps(workflow, job_id)
        collection = _find_collection_step(steps)
        assert collection is not None, (
            f"{job_id}: expected a step invoking scripts/ci/collect_failed_routing_bundle.py"
        )

    def test_collection_step_gated_on_failure(self, workflow: dict, job_id: str) -> None:
        steps = _steps(workflow, job_id)
        collection = _find_collection_step(steps)
        assert collection is not None
        assert collection.get("if") == "failure()", (
            f"{job_id}: the collection step must be gated on `if: failure()` "
            "so it never runs (and never masks the gate) on a green job."
        )


@pytest.mark.parametrize("job_id", JOB_IDS)
class TestFailedBundleUploadStep:
    def test_upload_step_exists(self, workflow: dict, job_id: str) -> None:
        steps = _steps(workflow, job_id)
        upload = _find_upload_step(steps)
        assert upload is not None, f"{job_id}: expected an actions/upload-artifact step"

    def test_upload_step_gated_on_failure_not_always(self, workflow: dict, job_id: str) -> None:
        """These four jobs are hard gates (not warn-only), so the upload
        step uses `if: failure()`, NOT `if: always()` -- uploading on
        every green run would be needless churn. (Contrast with
        nightly-ship-ready.yml, a warn-only job where `always()` is
        appropriate.)"""
        steps = _steps(workflow, job_id)
        upload = _find_upload_step(steps)
        assert upload is not None
        assert upload.get("if") == "failure()", (
            f"{job_id}: upload-artifact step must be gated on `if: failure()`"
        )

    def test_upload_step_has_explicit_name_path_retention(
        self, workflow: dict, job_id: str
    ) -> None:
        steps = _steps(workflow, job_id)
        upload = _find_upload_step(steps)
        assert upload is not None
        with_block = upload.get("with", {})
        assert with_block.get("name"), f"{job_id}: upload-artifact must set an explicit `name`"
        assert with_block.get("path"), f"{job_id}: upload-artifact must set an explicit `path`"
        assert isinstance(with_block.get("retention-days"), int), (
            f"{job_id}: upload-artifact must set an explicit integer `retention-days`"
        )

    def test_upload_artifact_name_distinguishes_board_job_and_commit(
        self, workflow: dict, job_id: str
    ) -> None:
        steps = _steps(workflow, job_id)
        upload = _find_upload_step(steps)
        assert upload is not None
        name = upload["with"]["name"]
        assert "${{ github.sha }}" in name, (
            f"{job_id}: artifact name must include the commit SHA so bundles "
            "from different commits never collide."
        )
        assert "${{ github.run_id }}" in name, (
            f"{job_id}: artifact name should include the run id for extra "
            "disambiguation within a single commit's re-runs."
        )

    def test_upload_path_is_scoped_not_repo_root(self, workflow: dict, job_id: str) -> None:
        """The uploaded path must be a bounded staging directory -- never
        the whole checkout (repo root, `.`, `.venv/`, `.git/`)."""
        steps = _steps(workflow, job_id)
        upload = _find_upload_step(steps)
        assert upload is not None
        path = upload["with"]["path"]
        disallowed = {".", "./", ".venv", ".venv/", ".git", ".git/", "/", ""}
        assert path not in disallowed, (
            f"{job_id}: upload path {path!r} must not be the whole checkout"
        )
        assert not path.strip().startswith(("..", "/home", "/root")), (
            f"{job_id}: upload path {path!r} looks unbounded/unexpected"
        )


@pytest.mark.parametrize("job_id", JOB_IDS)
def test_no_existing_route_or_validate_step_gained_continue_on_error(
    workflow: dict, job_id: str
) -> None:
    """`if: failure()` on the new steps naturally preserves the job's
    already-failed conclusion. Confirm the fix did NOT also add
    `continue-on-error: true` to any pre-existing route/validate step --
    that would flip the job to success and hide the original gate result,
    which the issue's acceptance criteria explicitly forbid."""
    steps = _steps(workflow, job_id)
    for step in steps:
        name = str(step.get("name", ""))
        if any(marker in name for marker in PRE_EXISTING_STEP_NAME_SUBSTRINGS):
            assert step.get("continue-on-error") is not True, (
                f"{job_id}: step {name!r} must not have continue-on-error: "
                "true -- that would hide the job's real pass/fail result."
            )
