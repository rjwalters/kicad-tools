"""Tests for the mypy baseline gate and its CI wiring (issue #3512).

Issue #3512 un-masked the Type Check job: the `Run mypy` step used to carry
``continue-on-error: true``, so mypy failures were invisible and type rot
accumulated (1512 errors in 269 files on main).  The fix mirrors the DRC
tolerance allowlist (``scripts/ci/check_routed_drc.py`` /
``.github/routed-drc-tolerance.yml``): a committed baseline records the known
errors, and ``scripts/ci/check_mypy_baseline.py`` fails CI ONLY on errors
beyond that baseline.

These tests pin the critical structural and behavioural properties so a
regression in any layer (workflow YAML, baseline file, or wrapper script) is
caught immediately rather than at the next CI run.

What we DO assert here:
    * The Type Check job's mypy step no longer has continue-on-error.
    * The Type Check job invokes the baseline wrapper (a refactor that drops
      the call is caught).
    * The committed baseline file exists and parses.
    * The wrapper's signature normalization is line-number-independent.
    * The diff semantics: new errors fail (exit 2), baseline errors pass,
      fixed errors warn but pass, duplicate-of-baseline errors fail.

Out of scope:
    * mypy itself -- not re-tested here.
"""

from __future__ import annotations

import importlib.util
import sys
from collections import Counter
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"
BASELINE_PATH = REPO_ROOT / ".github" / "mypy-baseline.txt"
HELPER_SCRIPT_PATH = REPO_ROOT / "scripts" / "ci" / "check_mypy_baseline.py"


def _load_helper_module():
    """Import scripts/ci/check_mypy_baseline.py as a module."""
    spec = importlib.util.spec_from_file_location("check_mypy_baseline", HELPER_SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_mypy_baseline"] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Workflow YAML structural tests
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(CI_WORKFLOW_PATH.read_text())


def test_typecheck_job_exists(workflow: dict) -> None:
    assert "typecheck" in workflow["jobs"], "Type Check job missing from ci.yml"


def test_typecheck_mypy_step_has_no_continue_on_error(workflow: dict) -> None:
    """The whole point of #3512: the mypy step must gate, not be advisory."""
    steps = workflow["jobs"]["typecheck"]["steps"]
    mypy_steps = [
        s
        for s in steps
        if "mypy" in str(s.get("name", "")).lower() or "mypy" in str(s.get("run", "")).lower()
    ]
    assert mypy_steps, "no mypy step found in the typecheck job"
    for step in mypy_steps:
        assert "continue-on-error" not in step, (
            f"mypy step {step.get('name')!r} still has continue-on-error -- "
            "the Type Check gate is masked again (regresses issue #3512)"
        )


def test_typecheck_job_invokes_baseline_wrapper(workflow: dict) -> None:
    """A refactor that drops the wrapper call (e.g. reverts to bare mypy with
    its 1500+ errors) would make the job permanently red; pin the call."""
    steps = workflow["jobs"]["typecheck"]["steps"]
    run_blobs = " ".join(str(s.get("run", "")) for s in steps)
    assert "check_mypy_baseline.py" in run_blobs, (
        "typecheck job no longer invokes scripts/ci/check_mypy_baseline.py"
    )


def test_lint_job_continue_on_error_removed(workflow: dict) -> None:
    """Gate guard: issue #3464 REMOVED continue-on-error from the Lint &
    Format job so ruff format/check failures now fail CI.

    The two lint steps -- ``Check formatting`` (``ruff format --check``)
    and ``Lint`` (``ruff check .``) -- must NOT carry
    ``continue-on-error: true``; if either is re-added, the gate goes
    advisory again and ~860 lint / ~510 format regressions become
    invisible (the exact failure mode #3464 fixed)."""
    steps = workflow["jobs"]["lint"]["steps"]
    advisory = [s for s in steps if s.get("continue-on-error") is True]
    assert advisory == [], (
        "the Lint & Format steps must gate (issue #3464 removed "
        "continue-on-error), but these still carry continue-on-error: true: "
        + ", ".join(repr(s.get("name")) for s in advisory)
    )
    # Sanity: the two ruff steps that do the gating are present and run ruff.
    run_blobs = " ".join(str(s.get("run", "")) for s in steps)
    assert "ruff format" in run_blobs and "ruff check" in run_blobs, (
        "expected the Lint & Format job to invoke `ruff format --check` and "
        "`ruff check .` as the gating steps"
    )


# ---------------------------------------------------------------------------
# Baseline file structural tests
# ---------------------------------------------------------------------------


def test_baseline_file_exists() -> None:
    assert BASELINE_PATH.exists(), (
        f"committed baseline {BASELINE_PATH} missing -- the gate would run in "
        "strict mode and fail on every existing error"
    )


def test_baseline_loads_and_is_nonempty() -> None:
    mod = _load_helper_module()
    counts = mod.load_baseline(BASELINE_PATH)
    # The baseline captures real debt; it should be non-trivial on main.
    assert sum(counts.values()) > 0, "baseline parsed to zero entries"


def test_baseline_lines_have_signature_shape() -> None:
    """Each non-comment baseline line must be a 3-field tab signature."""
    for raw in BASELINE_PATH.read_text().splitlines():
        line = raw.rstrip("\n")
        if not line or line.lstrip().startswith("#"):
            continue
        parts = line.split("\t")
        assert len(parts) == 3, f"baseline line is not a 3-field signature: {line!r}"


# ---------------------------------------------------------------------------
# Wrapper-script unit tests
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def mod():
    return _load_helper_module()


def test_normalize_message_masks_numbers(mod) -> None:
    a = mod.normalize_message('Argument 1 has incompatible type "int"')
    b = mod.normalize_message('Argument 3 has incompatible type "int"')
    assert a == b, "numeric tokens should be masked so counts/indices don't drift signatures"


def test_normalize_message_preserves_identifiers(mod) -> None:
    """Quoted identifiers carry semantic content -- distinct errors must stay
    distinct so a new error can't hide behind an unrelated baseline entry."""
    a = mod.normalize_message('"object" has no attribute "labels"')
    b = mod.normalize_message('"object" has no attribute "global_labels"')
    assert a != b


def test_parse_mypy_output_ignores_notes_and_summary(mod) -> None:
    raw = (
        'src/pkg/mod.py:248: error: Function "builtins.any" is not valid  [valid-type]\n'
        'src/pkg/mod.py:248: note: Perhaps you meant "typing.Any"?\n'
        "Found 1 error in 1 file (checked 5 source files)\n"
    )
    counts = mod.parse_mypy_output(raw)
    assert sum(counts.values()) == 1, "notes and the Found-N summary must not count as errors"


def test_parse_mypy_output_counts_duplicates(mod) -> None:
    raw = (
        'src/pkg/a.py:1: error: Need type annotation for "x"  [var-annotated]\n'
        'src/pkg/a.py:50: error: Need type annotation for "x"  [var-annotated]\n'
    )
    counts = mod.parse_mypy_output(raw)
    # Same file + code + (number-masked) message -> same signature, count 2.
    assert sum(counts.values()) == 2
    assert len(counts) == 1


def test_signature_is_line_independent(mod) -> None:
    line_a = "src/pkg/m.py:10: error: bad thing  [misc]"
    line_b = "src/pkg/m.py:9999: error: bad thing  [misc]"
    ca = mod.parse_mypy_output(line_a)
    cb = mod.parse_mypy_output(line_b)
    assert ca == cb, "moving an error to a different line must not change its signature"


def test_gate_passes_when_within_baseline(mod) -> None:
    baseline = Counter({"src/a.py\tmisc\tbad thing": 1})
    current = Counter({"src/a.py\tmisc\tbad thing": 1})
    new, fixed = mod.diff_against_baseline(current, baseline)
    assert not new and not fixed


def test_gate_fails_on_net_new_error(mod) -> None:
    baseline = Counter({"src/a.py\tmisc\tbad thing": 1})
    current = Counter({"src/a.py\tmisc\tbad thing": 1, "src/b.py\tname-defined\tundefined name": 1})
    new, _ = mod.diff_against_baseline(current, baseline)
    assert sum(new.values()) == 1
    assert "src/b.py\tname-defined\tundefined name" in new


def test_gate_fails_on_extra_duplicate(mod) -> None:
    """A second occurrence of an existing error in the same file is new debt."""
    baseline = Counter({"src/a.py\tmisc\tbad thing": 1})
    current = Counter({"src/a.py\tmisc\tbad thing": 2})
    new, _ = mod.diff_against_baseline(current, baseline)
    assert sum(new.values()) == 1


def test_fixed_errors_surface_but_do_not_fail(mod) -> None:
    baseline = Counter({"src/a.py\tmisc\tbad thing": 2})
    current = Counter({"src/a.py\tmisc\tbad thing": 1})
    new, fixed = mod.diff_against_baseline(current, baseline)
    assert not new, "dropping an error must not fail the gate"
    assert sum(fixed.values()) == 1


# ---------------------------------------------------------------------------
# End-to-end main() tests via synthetic mypy output
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text)
    return p


def test_main_passes_on_baseline_output(mod, tmp_path: Path) -> None:
    raw = "src/a.py:10: error: bad thing  [misc]\nFound 1 error in 1 file\n"
    mypy_out = _write(tmp_path, "mypy.txt", raw)
    baseline = tmp_path / "baseline.txt"
    # Generate baseline from the same output, then gate against it.
    assert mod.main(["--baseline", str(baseline), "--update", "--mypy-output", str(mypy_out)]) == 0
    assert mod.main(["--baseline", str(baseline), "--mypy-output", str(mypy_out)]) == 0


def test_main_fails_on_new_error(mod, tmp_path: Path) -> None:
    baseline_out = _write(tmp_path, "base.txt", "src/a.py:10: error: bad thing  [misc]\n")
    baseline = tmp_path / "baseline.txt"
    assert (
        mod.main(["--baseline", str(baseline), "--update", "--mypy-output", str(baseline_out)]) == 0
    )

    new_out = _write(
        tmp_path,
        "new.txt",
        "src/a.py:10: error: bad thing  [misc]\n"
        'src/b.py:5: error: Name "frob" is not defined  [name-defined]\n',
    )
    assert mod.main(["--baseline", str(baseline), "--mypy-output", str(new_out)]) == 2


def test_main_missing_baseline_is_strict(mod, tmp_path: Path) -> None:
    """No baseline file -> every error is new (exit 2)."""
    out = _write(tmp_path, "mypy.txt", "src/a.py:10: error: bad thing  [misc]\n")
    missing = tmp_path / "does-not-exist.txt"
    assert mod.main(["--baseline", str(missing), "--mypy-output", str(out)]) == 2
