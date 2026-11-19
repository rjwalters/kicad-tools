"""Staging-helper contracts for scripts/ci/jobs/_lib.sh (issue #9412).

``scripts/ci/jobs/_lib.sh`` (issue #9340) stages a freshly generated artifact
over its committed repo path while a check runs, then restores the committed
path afterwards -- pass, fail or signal. Two jobs lean on it:
``board-03-end-to-end.sh`` stages a fresh routed PCB over
``boards/<board>/routed.kicad_pcb`` through ``check_drc_over_committed_path``
and ``board-04-end-to-end.sh`` stages a whole output directory through
``stage_over_committed_dir``. When this logic first shipped it was broken and
only a hand run caught it, because the CI jobs that exercise it are gated off
for PRs; this module is the regression net that closes that gap.

The tests never regenerate a board: they source a byte-identical copy of the
real ``_lib.sh`` inside a ``tmp_path`` sandbox laid out as a miniature repo
(the script derives ``REPO_ROOT`` from its own location, so the copy must sit
at ``<sandbox>/scripts/ci/jobs/_lib.sh``), and the one hard-coded inner
command, ``uv run python scripts/ci/check_routed_drc_internal.py``, resolves
inside the sandbox to a deterministic stub that records what it saw and exits
a code the test chooses.

To check a mutated ``_lib.sh`` instead of the repo copy (the PR-body mutation
check), point the module at a replacement:

    CI_JOBS_LIB_UNDER_TEST=/tmp/mutated_lib.sh uv run pytest tests/test_ci_jobs_lib.py --no-cov
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LIB_SH = REPO_ROOT / "scripts" / "ci" / "jobs" / "_lib.sh"

COMMITTED_PCB = b"(kicad_pcb committed 9412)\n"
FRESH_PCB = b"(kicad_pcb fresh 9412)\n"
COMMITTED_JSON = b'{"manifest": "committed"}\n'
FRESH_JSON = b'{"manifest": "fresh"}\n'
COMMITTED_MANIFEST = b"committed-manifest\n"
FRESH_MANIFEST = b"fresh-manifest\n"

# Deterministic stand-in for scripts/ci/check_routed_drc_internal.py: log the
# sha256 of the committed path as the inner command sees it, then exit with
# the code the test staged in the environment.
DRC_STUB = """\
import hashlib
import os
import pathlib
import sys

target = pathlib.Path(sys.argv[1])
digest = hashlib.sha256(target.read_bytes()).hexdigest()
with open(os.environ["CI_JOBS_DRC_STUB_LOG"], "a") as fh:
    fh.write(f"{digest} {target.name}\\n")
sys.exit(int(os.environ.get("CI_JOBS_DRC_STUB_EXIT", "0")))
"""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _snapshot(tree: Path) -> dict[str, bytes]:
    """Byte-identity snapshot of every file under ``tree`` (relative path)."""
    return {
        str(p.relative_to(tree)): p.read_bytes() for p in sorted(tree.rglob("*")) if p.is_file()
    }


@pytest.fixture()
def sandbox(tmp_path: Path) -> Path:
    """Miniature repo whose scripts/ tree makes _lib.sh's REPO_ROOT local."""
    repo = tmp_path / "repo"
    jobs = repo / "scripts" / "ci" / "jobs"
    jobs.mkdir(parents=True)
    shutil.copyfile(LIB_SH, jobs / "_lib.sh")
    stub = repo / "scripts" / "ci" / "check_routed_drc_internal.py"
    stub.write_text(DRC_STUB)
    (tmp_path / "tmp").mkdir()
    return repo


def _run_lib(
    sandbox: Path, tmp_path: Path, snippet: str, **env_overrides
) -> subprocess.CompletedProcess[str]:
    """Run a bash snippet that sources the sandbox copy of _lib.sh.

    The snippet is written next to the _lib.sh copy as a miniature job
    script: _lib.sh derives JOBS_DIR/REPO_ROOT from ``BASH_SOURCE[1]`` (the
    sourcing script's path), so sourcing it directly from a ``bash -c``
    string leaves REPO_ROOT unresolved (its ``BASH_SOURCE[1]`` is unset
    there). Sourcing a real file inside ``scripts/ci/jobs/`` is exactly how
    the real job scripts are consumed. TMPDIR points at the per-test
    directory so every ci-jobs-stage-bak.* backup the helpers allocate is
    created (and, per the contract, cleaned up) somewhere this test alone
    can inspect.
    """
    lib = Path(os.environ.get("CI_JOBS_LIB_UNDER_TEST", LIB_SH))
    probe = sandbox / "scripts" / "ci" / "jobs" / "probe.sh"
    probe.write_text(f"set -uo pipefail\nsource '{lib}'\n{snippet}")
    env = os.environ | {
        "TMPDIR": str(tmp_path / "tmp"),
        "CI_JOBS_DRC_STUB_LOG": str(tmp_path / "drc-stub.log"),
        **env_overrides,
    }
    return subprocess.run(
        ["bash", "-c", f". '{probe}'"],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )


def _assert_committed_tree_restored(
    sandbox: Path, tmp_path: Path, before: dict[str, bytes]
) -> None:
    """The committed output tree must be byte-identical to the snapshot."""
    committed = sandbox / "boards" / "probe" / "output"
    assert _snapshot(committed) == before


def _assert_no_backup_left(tmp_path: Path) -> None:
    leftovers = list((tmp_path / "tmp").glob("ci-jobs-stage-bak.*"))
    assert leftovers == [], f"staging backups leaked into TMPDIR: {leftovers}"


def _seed_dir_trees(sandbox: Path, tmp_path: Path) -> tuple[Path, Path, dict[str, bytes]]:
    committed = sandbox / "boards" / "probe" / "output"
    fresh = tmp_path / "board04-ci"
    for base, files in (
        (committed, {"board.json": COMMITTED_JSON, "only-committed.txt": b"c\n"}),
        (fresh, {"board.json": FRESH_JSON, "only-fresh.txt": b"f\n"}),
    ):
        (base / "manifests").mkdir(parents=True)
        for name, data in files.items():
            (base / name).write_bytes(data)
        (base / "manifests" / "m.txt").write_bytes(
            COMMITTED_MANIFEST if base is committed else FRESH_MANIFEST
        )
    return fresh, committed, _snapshot(committed)


def test_stage_over_committed_dir_inner_sees_fresh_and_tree_restored(
    sandbox: Path, tmp_path: Path
) -> None:
    fresh, committed, before = _seed_dir_trees(sandbox, tmp_path)
    # stage_over_committed_dir runs the inner command in a child `bash -c`
    # (board-04-end-to-end.sh usage), so the inner command is an executable
    # script, not a shell function. It inherits the repo root as cwd.
    inner = tmp_path / "inner.sh"
    inner.write_text(
        f"""\
#! /usr/bin/env bash
{{
  find boards/probe/output -mindepth 1 -printf '%P\\n' | sort
  printf -- '--\\n'
  cat boards/probe/output/board.json
  cat boards/probe/output/manifests/m.txt
}} > '{tmp_path / "inner.log"}'
"""
    )
    inner.chmod(0o755)
    result = _run_lib(
        sandbox,
        tmp_path,
        f"""
stage_over_committed_dir '{fresh}' '{committed}' '{inner}'
rc=$?
echo "RC=$rc"
""",
    )
    assert result.returncode == 0, result.stderr
    assert "RC=0" in result.stdout
    inner = (tmp_path / "inner.log").read_text()
    # The inner command ran against the staged tree: the fresh tree is merged
    # over the committed one, so fresh contents (and the fresh-only file)
    # travel with the committed-only file during the window.
    assert sorted(inner.splitlines()) == sorted(
        [
            "board.json",
            "only-fresh.txt",
            "only-committed.txt",
            "manifests",
            "manifests/m.txt",
            "--",
            FRESH_JSON.decode().rstrip("\n"),
            FRESH_MANIFEST.decode().rstrip("\n"),
        ]
    )
    # Afterwards: staged-over contents (incl. the fresh-only file) dropped,
    # committed tree byte-identical to before.
    _assert_committed_tree_restored(sandbox, tmp_path, before)
    _assert_no_backup_left(tmp_path)


def test_stage_over_committed_dir_propagates_inner_failure_and_restores(
    sandbox: Path, tmp_path: Path
) -> None:
    fresh, committed, before = _seed_dir_trees(sandbox, tmp_path)
    inner = tmp_path / "inner-fail.sh"
    inner.write_text("#! /usr/bin/env bash\nexit 7\n")
    inner.chmod(0o755)
    result = _run_lib(
        sandbox,
        tmp_path,
        f"""
stage_over_committed_dir '{fresh}' '{committed}' '{inner}'
rc=$?
echo "RC=$rc"
""",
    )
    assert result.returncode == 0, result.stderr
    assert "RC=7" in result.stdout
    _assert_committed_tree_restored(sandbox, tmp_path, before)
    _assert_no_backup_left(tmp_path)


def test_check_drc_over_committed_path_inner_sees_fresh_and_file_restored(
    sandbox: Path, tmp_path: Path
) -> None:
    committed = sandbox / "boards" / "probe" / "routed.kicad_pcb"
    committed.parent.mkdir(parents=True)
    committed.write_bytes(COMMITTED_PCB)
    fresh = tmp_path / "routed-fresh.kicad_pcb"
    fresh.write_bytes(FRESH_PCB)
    result = _run_lib(
        sandbox,
        tmp_path,
        f"""
check_drc_over_committed_path '{fresh}' '{committed}'
rc=$?
echo "RC=$rc"
""",
    )
    assert result.returncode == 0, result.stderr
    assert "RC=0" in result.stdout
    # The stub (the job's real inner command position) saw the fresh PCB.
    log = (tmp_path / "drc-stub.log").read_text().splitlines()
    assert log == [f"{_sha(FRESH_PCB)} routed.kicad_pcb"]
    # The committed file is restored exactly, byte for byte.
    assert committed.read_bytes() == COMMITTED_PCB
    _assert_no_backup_left(tmp_path)


def test_check_drc_over_committed_path_propagates_inner_failure_and_restores(
    sandbox: Path, tmp_path: Path
) -> None:
    committed = sandbox / "boards" / "probe" / "routed.kicad_pcb"
    committed.parent.mkdir(parents=True)
    committed.write_bytes(COMMITTED_PCB)
    fresh = tmp_path / "routed-fresh.kicad_pcb"
    fresh.write_bytes(FRESH_PCB)
    result = _run_lib(
        sandbox,
        tmp_path,
        f"""
check_drc_over_committed_path '{fresh}' '{committed}'
rc=$?
echo "RC=$rc"
""",
        CI_JOBS_DRC_STUB_EXIT="7",
    )
    assert result.returncode == 0, result.stderr
    assert "RC=7" in result.stdout
    assert committed.read_bytes() == COMMITTED_PCB
    _assert_no_backup_left(tmp_path)


def test_helpers_fall_back_when_tmpdir_unset(sandbox: Path, tmp_path: Path) -> None:
    """TMPDIR unset must not break the backup allocation; nothing leaks."""
    fresh, committed, before = _seed_dir_trees(sandbox, tmp_path)
    lib = Path(os.environ.get("CI_JOBS_LIB_UNDER_TEST", LIB_SH))
    probe = sandbox / "scripts" / "ci" / "jobs" / "probe.sh"
    probe.write_text(
        f"set -uo pipefail\nsource '{lib}'\n"
        f"stage_over_committed_dir '{fresh}' '{committed}' true\n"
        'echo "RC=$?"\n'
    )
    env = os.environ.copy()
    env.pop("TMPDIR", None)
    result = subprocess.run(
        ["bash", "-c", f". '{probe}'"],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    assert "RC=0" in result.stdout
    _assert_committed_tree_restored(sandbox, tmp_path, before)
