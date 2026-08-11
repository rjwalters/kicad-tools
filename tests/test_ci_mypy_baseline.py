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
import os
import shutil
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

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


def test_baseline_keeps_both_nanobind_signatures() -> None:
    """Both env-dependent nanobind signatures must survive any regeneration.

    PR #3879 / issue #3877: the nanobind import diagnostic differs by whether
    nanobind is installed (``import-not-found`` in a bare env vs
    ``import-untyped`` in a native-built one).  The baseline deliberately
    carries BOTH so the gate is green either way.  A ``--update`` run in a
    native-built worktree drops the ``import-not-found`` line and breaks CI
    Type Check, so pin the pair here rather than relying on review to catch it.
    """
    text = BASELINE_PATH.read_text()
    for code in ("import-not-found", "import-untyped"):
        expected = f"src/kicad_tools/cli/build_native_cmd.py\t{code}\t"
        assert expected in text, (
            f"baseline lost the nanobind {code!r} signature -- most likely a "
            "`--update` run in an environment-specific worktree. Restore both "
            "lines from origin/main before merging."
        )


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


# ---------------------------------------------------------------------------
# Mypy version-drift guard (issue #4558)
# ---------------------------------------------------------------------------

_FAKE_LOCK = """\
[[package]]
name = "librt"
version = "0.1.0"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "mypy"
version = "1.19.1"
source = { registry = "https://pypi.org/simple" }
dependencies = [
    { name = "mypy-extensions" },
]

[[package]]
name = "mypy-extensions"
version = "1.0.0"
source = { registry = "https://pypi.org/simple" }
"""


def test_locked_mypy_version_parses_pin(mod, tmp_path: Path) -> None:
    lock = _write(tmp_path, "uv.lock", _FAKE_LOCK)
    assert mod.locked_mypy_version(lock) == "1.19.1"


def test_locked_mypy_version_ignores_similar_names(mod, tmp_path: Path) -> None:
    """``mypy-extensions`` must not satisfy the ``mypy`` block lookup."""
    lock_text = _FAKE_LOCK.replace('name = "mypy"\nversion = "1.19.1"\n', "")
    lock = _write(tmp_path, "uv.lock", lock_text)
    assert mod.locked_mypy_version(lock) is None


def test_locked_mypy_version_missing_lock_is_none(mod, tmp_path: Path) -> None:
    assert mod.locked_mypy_version(tmp_path / "no-such-uv.lock") is None


def test_locked_mypy_version_unparseable_lock_is_none(mod, tmp_path: Path) -> None:
    lock = _write(tmp_path, "uv.lock", "this is not a lockfile at all\n")
    assert mod.locked_mypy_version(lock) is None


def test_repo_uv_lock_pin_is_parseable(mod) -> None:
    """The real uv.lock must yield a version -- otherwise the guard is dead code."""
    version = mod.locked_mypy_version(REPO_ROOT / "uv.lock")
    assert version is not None and version[0].isdigit()


def test_parse_installed_mypy_version(mod) -> None:
    assert mod.parse_installed_mypy_version("mypy 1.19.1 (compiled: yes)") == "1.19.1"
    assert mod.parse_installed_mypy_version("mypy 1.20.2") == "1.20.2"
    assert mod.parse_installed_mypy_version("garbage output") is None


def test_mypy_version_drift_mismatch(mod, tmp_path: Path, monkeypatch) -> None:
    lock = _write(tmp_path, "uv.lock", _FAKE_LOCK)
    monkeypatch.setattr(mod, "installed_mypy_version", lambda: "1.20.2")
    assert mod.mypy_version_drift(lock) == ("1.19.1", "1.20.2")


def test_mypy_version_drift_match_is_silent(mod, tmp_path: Path, monkeypatch) -> None:
    lock = _write(tmp_path, "uv.lock", _FAKE_LOCK)
    monkeypatch.setattr(mod, "installed_mypy_version", lambda: "1.19.1")
    assert mod.mypy_version_drift(lock) is None


def test_mypy_version_drift_skips_on_unparseable_lock(mod, tmp_path: Path, monkeypatch) -> None:
    """Guard must degrade gracefully -- never a tool failure, never a query."""
    lock = _write(tmp_path, "uv.lock", "not a lockfile\n")

    def _boom() -> str:
        raise AssertionError("installed version must not be queried without a parsed pin")

    monkeypatch.setattr(mod, "installed_mypy_version", _boom)
    assert mod.mypy_version_drift(lock) is None


def test_mypy_version_drift_skips_when_mypy_unqueryable(mod, tmp_path: Path, monkeypatch) -> None:
    lock = _write(tmp_path, "uv.lock", _FAKE_LOCK)
    monkeypatch.setattr(mod, "installed_mypy_version", lambda: None)
    assert mod.mypy_version_drift(lock) is None


def test_emit_drift_warning_names_versions_and_remedy(mod, capsys) -> None:
    mod.emit_drift_warning("1.19.1", "1.20.2", has_new_errors=False)
    out = capsys.readouterr().out
    assert "MYPY VERSION DRIFT" in out
    assert "1.19.1" in out and "1.20.2" in out
    assert "uv sync --frozen --extra dev" in out
    assert "::warning::" in out
    # No banner when the gate is otherwise clean.
    assert "TOOLCHAIN NOISE" not in out


def test_emit_drift_warning_banner_when_new_errors(mod, capsys) -> None:
    mod.emit_drift_warning("1.19.1", "1.20.2", has_new_errors=True)
    out = capsys.readouterr().out
    assert "TOOLCHAIN NOISE" in out
    assert "uv sync --frozen --extra dev" in out
    # The banner must not steer agents toward --update as a drift "fix".
    assert "Do NOT run --update" in out


def test_main_synthetic_output_skips_drift_guard(mod, tmp_path: Path, monkeypatch, capsys) -> None:
    """--mypy-output runs never invoked the PATH mypy, so no drift check."""

    def _boom(*_args, **_kwargs):
        raise AssertionError("drift guard must not run for --mypy-output runs")

    monkeypatch.setattr(mod, "mypy_version_drift", _boom)
    raw = "src/a.py:10: error: bad thing  [misc]\nFound 1 error in 1 file\n"
    mypy_out = _write(tmp_path, "mypy.txt", raw)
    baseline = tmp_path / "baseline.txt"
    assert mod.main(["--baseline", str(baseline), "--update", "--mypy-output", str(mypy_out)]) == 0
    assert mod.main(["--baseline", str(baseline), "--mypy-output", str(mypy_out)]) == 0
    out = capsys.readouterr().out
    assert "MYPY VERSION DRIFT" not in out


# ---------------------------------------------------------------------------
# Cold-by-default incremental-cache control (issue #4767)
#
# CI is structurally always-cold (no actions/cache; setup-uv pins
# enable-cache: false), while a local run is incremental by default.  Two
# Judges (PRs #4746, #4762) burned a review cycle each on a stale-cache
# phantom error, so the gate now disables the cache read by default.
# ---------------------------------------------------------------------------


class _RecordingRun:
    """Stand-in for ``subprocess.run`` that records argv and fakes a clean run."""

    def __init__(self, stdout: str = "Success: no issues found in 1 source file\n") -> None:
        self.argv: list[str] | None = None
        self._stdout = stdout

    def __call__(self, argv, *_args, **_kwargs):
        self.argv = list(argv)
        return SimpleNamespace(stdout=self._stdout, stderr="")


def test_run_mypy_is_cold_by_default(mod, monkeypatch) -> None:
    """The default invocation must disable the incremental cache read."""
    rec = _RecordingRun()
    monkeypatch.setattr(mod.subprocess, "run", rec)
    mod.run_mypy("src/")
    assert rec.argv == ["mypy", "--no-incremental", "src/"]


def test_run_mypy_incremental_opt_out_drops_cold_flag(mod, monkeypatch) -> None:
    """``incremental=True`` restores the warm-cache invocation verbatim."""
    rec = _RecordingRun()
    monkeypatch.setattr(mod.subprocess, "run", rec)
    mod.run_mypy("src/", incremental=True)
    assert rec.argv == ["mypy", "src/"]
    assert "--no-incremental" not in rec.argv


def test_main_runs_cold_by_default(mod, tmp_path: Path, monkeypatch) -> None:
    """A real (non ``--mypy-output``) run threads the cold default through."""
    seen: dict[str, object] = {}

    def _fake_run_mypy(target: str, *, incremental: bool = False) -> str:
        seen["target"] = target
        seen["incremental"] = incremental
        return "Success: no issues found in 1 source file\n"

    monkeypatch.setattr(mod, "run_mypy", _fake_run_mypy)
    monkeypatch.setattr(mod, "mypy_version_drift", lambda: None)
    baseline = tmp_path / "baseline.txt"
    assert mod.main(["--baseline", str(baseline)]) == 0
    assert seen["incremental"] is False


def test_main_incremental_flag_opts_into_warm_cache(mod, tmp_path: Path, monkeypatch) -> None:
    """``--incremental`` / ``--warm-cache`` reach ``run_mypy``."""
    seen: list[bool] = []

    def _fake_run_mypy(target: str, *, incremental: bool = False) -> str:
        seen.append(incremental)
        return "Success: no issues found in 1 source file\n"

    monkeypatch.setattr(mod, "run_mypy", _fake_run_mypy)
    monkeypatch.setattr(mod, "mypy_version_drift", lambda: None)
    baseline = tmp_path / "baseline.txt"
    assert mod.main(["--baseline", str(baseline), "--incremental"]) == 0
    assert mod.main(["--baseline", str(baseline), "--warm-cache"]) == 0
    assert seen == [True, True]


def test_incremental_flag_is_documented_in_help(mod, capsys) -> None:
    """The escape hatch is useless if `--help` does not explain the trade-off."""
    with pytest.raises(SystemExit):
        mod.main(["--help"])
    help_text = capsys.readouterr().out
    assert "--incremental" in help_text
    assert "--warm-cache" in help_text
    assert ".mypy_cache" in help_text


def test_mypy_output_runs_never_invoke_mypy(mod, tmp_path: Path, monkeypatch) -> None:
    """Synthetic ``--mypy-output`` runs must not touch the new flag path."""

    def _boom(*_args, **_kwargs):
        raise AssertionError("run_mypy must not be invoked for --mypy-output runs")

    monkeypatch.setattr(mod, "run_mypy", _boom)
    out = _write(tmp_path, "mypy.txt", "src/a.py:10: error: bad thing  [misc]\n")
    baseline = tmp_path / "baseline.txt"
    assert mod.main(["--baseline", str(baseline), "--update", "--mypy-output", str(out)]) == 0
    assert mod.main(["--baseline", str(baseline), "--mypy-output", str(out)]) == 0


# Same byte length in both states, so the file's (mtime, size) validation key
# is identical -- exactly the condition under which mypy replays a cached
# result instead of re-checking.
_STALE_STATE_A = 'def f() -> int:\n    return "a"\n'  # errors: str returned as int
_STALE_STATE_B = 'def f() -> str:\n    return "a"\n'  # clean


@pytest.mark.skipif(shutil.which("mypy") is None, reason="mypy binary not on PATH")
def test_cold_run_ignores_a_poisoned_cache(mod, tmp_path: Path, monkeypatch) -> None:
    """The property that matters: a stale cache cannot change the cold verdict.

    Seeds ``.mypy_cache/`` from a state that errors, then swaps in a clean
    state of identical byte length while restoring the original mtime.  The
    warm run replays the stale error (this is the phantom failure from PRs
    #4746/#4762 in miniature); the cold run must report the truth.
    """
    project = tmp_path / "proj"
    project.mkdir()
    module = project / "mod_a.py"
    module.write_text(_STALE_STATE_A)
    monkeypatch.chdir(project)

    seeded = mod.run_mypy("mod_a.py", incremental=True)
    assert "Incompatible return value type" in seeded, (
        f"test premise broken: state A should error, got:\n{seeded}"
    )
    assert (project / ".mypy_cache").is_dir(), "warm run did not write a cache to poison"

    stat_before = module.stat()
    module.write_text(_STALE_STATE_B)
    assert module.stat().st_size == stat_before.st_size, "states must be the same byte length"
    os.utime(module, (stat_before.st_atime, stat_before.st_mtime))

    warm = mod.run_mypy("mod_a.py", incremental=True)
    cold = mod.run_mypy("mod_a.py")

    assert "Incompatible return value type" in warm, (
        f"test premise broken: the cache was not actually poisoned, got:\n{warm}"
    )
    assert "Incompatible return value type" not in cold, (
        f"cold run replayed the stale cached error -- the gate is still trapped:\n{cold}"
    )
    assert mod.parse_mypy_output(cold) == Counter(), cold
