"""Tests for scripts/install-kct.sh (Installer MVP, issue #4055).

These drive a throwaway ``git init``-ed target repo through the installer's
dry-run, real path-mode install, idempotent re-run, skill-selection, and
loom-coexistence paths (see the issue's Acceptance / Test Plan).

Hermeticity: the tests stub ``uv`` with a fake on PATH that emulates
``uv add``'s pyproject/[tool.uv.sources] write-back. This keeps the suite
network-free and fast in CI while still exercising the installer's real
idempotency logic (existing-dependency detection, marker-block replacement,
metadata emission) against the resulting pyproject content -- exactly the
"git-mode dep addition asserted on pyproject content rather than resolved"
approach the issue calls for.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALLER = REPO_ROOT / "scripts" / "install-kct.sh"
SKILLS_SRC = REPO_ROOT / ".claude" / "commands" / "kct"
CI_GATES_SRC = REPO_ROOT / "scripts" / "ci"

# The three portable CI gates vendored into the consumer's .kct/ci/ (#4056).
# Must stay in sync with CI_GATE_FILES in scripts/install-kct.sh.
VENDORED_CI_GATES = (
    "check_copper_lvs.py",
    "check_routed_drc.py",
    "net_class_map_resolver.py",
)


# --- fake uv -----------------------------------------------------------------
# Emulates just enough of `uv add <path> --editable --frozen` to write the
# dependency + [tool.uv.sources] path entry the installer's idempotency logic
# reads back. No venv, no network, no resolution.
FAKE_UV = r"""#!/usr/bin/env bash
set -euo pipefail
# We only implement: uv add <path-or-name> [--editable] [--frozen] [--git URL]
#                    [--tag T] [--rev R]
[[ "${1:-}" == "add" ]] || { echo "fake-uv: only 'add' is stubbed" >&2; exit 2; }
shift
PKG=""
IS_GIT=false
GIT_URL=""
PIN=""
PATH_SRC=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --editable|--frozen) shift ;;
    --git) IS_GIT=true; GIT_URL="$2"; shift 2 ;;
    --tag|--rev) PIN="$2"; shift 2 ;;
    -*) shift ;;
    *) PKG="$1"; shift ;;
  esac
done
PYPROJECT="pyproject.toml"
[[ -f "$PYPROJECT" ]] || { echo "fake-uv: no pyproject.toml" >&2; exit 1; }

python3 - "$PYPROJECT" "$PKG" "$IS_GIT" "$GIT_URL" "$PIN" <<'PYEOF'
import re, sys
pyproject, pkg, is_git, git_url, pin = sys.argv[1:6]
text = open(pyproject).read()

# 1. Ensure "kicad-tools" is in [project.dependencies] exactly once.
if '"kicad-tools"' not in text and '"kicad-tools ' not in text:
    m = re.search(r'(dependencies\s*=\s*\[)(.*?)(\])', text, re.S)
    if m:
        head, body, tail = m.groups()
        entry = '\n    "kicad-tools",'
        text = text[:m.start()] + head + body.rstrip() + entry + "\n" + tail + text[m.end():]

# 2. Compute the [tool.uv.sources] entry line.
if is_git == "true":
    key = "tag" if pin else "rev"  # test passes tag by convention
    src = f'kicad-tools = {{ git = "{git_url}", {key} = "{pin}" }}'
else:
    # path mode: pkg is an absolute path; record it verbatim (installer
    # resolves it back to absolute, so verbatim absolute is fine for the test).
    src = f'kicad-tools = {{ path = "{pkg}", editable = true }}'

if "[tool.uv.sources]" in text:
    # replace any existing kicad-tools = line, else append under the table.
    lines = text.splitlines()
    out, in_sources, replaced = [], False, False
    for ln in lines:
        if ln.strip() == "[tool.uv.sources]":
            in_sources = True
            out.append(ln)
            continue
        if in_sources and ln.strip().startswith("kicad-tools"):
            out.append(src)
            replaced = True
            continue
        if in_sources and ln.startswith("[") and ln.strip() != "[tool.uv.sources]":
            if not replaced:
                out.append(src)
                replaced = True
            in_sources = False
        out.append(ln)
    if not replaced:
        out.append(src)
    text = "\n".join(out) + "\n"
else:
    text = text.rstrip() + f"\n\n[tool.uv.sources]\n{src}\n"

open(pyproject, "w").write(text)
PYEOF
"""


@pytest.fixture
def fake_uv_env(tmp_path: Path) -> dict[str, str]:
    """A PATH-front-loaded env whose ``uv`` is the stub above."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    uv = bindir / "uv"
    uv.write_text(FAKE_UV)
    uv.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    return env


@pytest.fixture
def target_repo(tmp_path: Path) -> Path:
    """A throwaway git repo with a minimal uv-style pyproject + CLAUDE.md."""
    target = tmp_path / "board-repo"
    target.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=target, check=True)
    (target / "pyproject.toml").write_text(
        "[project]\n"
        'name = "board-repo"\n'
        'version = "0.1.0"\n'
        'requires-python = ">=3.10"\n'
        "dependencies = []\n"
    )
    (target / "CLAUDE.md").write_text("# Board Repo\n\nExisting content stays.\n")
    # Seed a loom skill to prove the installer never touches it.
    loom = target / ".claude" / "commands" / "loom"
    loom.mkdir(parents=True)
    (loom / "seed.md").write_text("LOOM SEED — DO NOT TOUCH\n")
    return target


def run_installer(
    target: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(INSTALLER), *args, str(target)],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env=env,
    )


def _snapshot(target: Path) -> dict[str, str]:
    return {
        str(p.relative_to(target)): p.read_text(errors="replace")
        for p in target.rglob("*")
        if p.is_file() and ".git" not in p.parts
    }


# --- dry-run: writes nothing -------------------------------------------------


def test_dry_run_writes_nothing(target_repo: Path, fake_uv_env: dict[str, str]) -> None:
    before = _snapshot(target_repo)
    result = run_installer(target_repo, "--dry-run", "--path", str(REPO_ROOT), env=fake_uv_env)
    assert result.returncode == 0, result.stderr
    assert _snapshot(target_repo) == before, "dry-run must not modify any file"
    assert not (target_repo / ".kct").exists()
    assert not (target_repo / ".claude" / "commands" / "kct").exists()
    assert "dry-run" in result.stdout


# --- real path-mode install --------------------------------------------------


def test_path_install_produces_all_artifacts(
    target_repo: Path, fake_uv_env: dict[str, str]
) -> None:
    result = run_installer(target_repo, "--path", str(REPO_ROOT), env=fake_uv_env)
    assert result.returncode == 0, result.stderr

    # Dependency + path source in pyproject.
    pyproject = (target_repo / "pyproject.toml").read_text()
    assert '"kicad-tools"' in pyproject
    assert "[tool.uv.sources]" in pyproject
    assert "path" in pyproject.split("[tool.uv.sources]", 1)[1]

    # Vendored skills byte-identical to source.
    for name in ("README.md", "ee-review.md"):
        dst = target_repo / ".claude" / "commands" / "kct" / name
        assert dst.exists()
        assert dst.read_bytes() == (SKILLS_SRC / name).read_bytes()

    # Exactly one guarded CLAUDE.md block, slimmed to a pointer (the
    # load-bearing conventions now live in .kct/CONVENTIONS.md, not inlined).
    claude = (target_repo / "CLAUDE.md").read_text()
    assert claude.count("<!-- BEGIN KICAD-TOOLS -->") == 1
    assert claude.count("<!-- END KICAD-TOOLS -->") == 1
    assert ".kct/CONVENTIONS.md" in claude  # slim pointer references the vendored file
    assert ".claude/commands/kct/README.md" in claude  # pointer references the skills README
    # The numbered convention list must NOT be inlined into CLAUDE.md anymore.
    assert "### Conventions (load-bearing" not in claude
    assert "refill-zones" not in claude
    assert "Existing content stays." in claude  # pre-existing content preserved

    # The three conventions live verbatim in the vendored .kct/CONVENTIONS.md.
    conventions = (target_repo / ".kct" / "CONVENTIONS.md").read_text()
    assert "build-native" in conventions
    assert "refill-zones" in conventions
    assert "Artifact-first" in conventions

    # Metadata is valid JSON with the schema fields.
    meta = json.loads((target_repo / ".kct" / "install-metadata.json").read_text())
    assert meta["kct_version"]
    assert meta["source_mode"] == "path"
    assert meta["source_ref"] == str(REPO_ROOT)
    assert "ee-review" in meta["skills_selected"]
    assert ".claude/commands/kct/ee-review.md" in meta["installed_files"]
    assert ".claude/commands/kct/README.md" in meta["installed_files"]
    assert ".kct/CONVENTIONS.md" in meta["installed_files"]


# --- idempotent re-run -------------------------------------------------------


def test_rerun_is_idempotent(target_repo: Path, fake_uv_env: dict[str, str]) -> None:
    first = run_installer(target_repo, "--path", str(REPO_ROOT), env=fake_uv_env)
    assert first.returncode == 0, first.stderr
    second = run_installer(target_repo, "--path", str(REPO_ROOT), env=fake_uv_env)
    assert second.returncode == 0, second.stderr

    pyproject = (target_repo / "pyproject.toml").read_text()
    assert pyproject.count('"kicad-tools"') == 1, "dependency must not duplicate"

    claude = (target_repo / "CLAUDE.md").read_text()
    assert claude.count("<!-- BEGIN KICAD-TOOLS -->") == 1
    assert claude.count("<!-- END KICAD-TOOLS -->") == 1

    # The second run must recognize the up-to-date dependency and no-op it.
    assert "already present and up to date" in second.stdout


# --- loom coexistence --------------------------------------------------------


def test_loom_dir_untouched(target_repo: Path, fake_uv_env: dict[str, str]) -> None:
    loom_file = target_repo / ".claude" / "commands" / "loom" / "seed.md"
    before = loom_file.read_bytes()
    result = run_installer(target_repo, "--path", str(REPO_ROOT), env=fake_uv_env)
    assert result.returncode == 0, result.stderr
    assert loom_file.read_bytes() == before, "installer must never touch loom skills"


# --- malformed CLAUDE.md markers: error, never silent data loss --------------


def test_unterminated_begin_marker_errors_without_data_loss(
    target_repo: Path, fake_uv_env: dict[str, str]
) -> None:
    """A BEGIN with no matching END must abort and leave the file byte-unchanged.

    Regression for the PR #4062 judge finding: the in-place line rebuild dropped
    every line after a stale BEGIN (in_block never cleared), then clobbered the
    original — silent data loss on exit 0.
    """
    claude = target_repo / "CLAUDE.md"
    claude.write_text(
        "# My Repo\n"
        "IMPORTANT USER CONTENT\n"
        "<!-- BEGIN KICAD-TOOLS -->\n"
        "stale half block\n"
        "MORE IMPORTANT CONTENT AFTER STALE BEGIN\n"
    )
    before = claude.read_bytes()

    result = run_installer(target_repo, "--path", str(REPO_ROOT), env=fake_uv_env)

    assert result.returncode != 0, "unterminated BEGIN must fail, not exit 0"
    assert "unterminated" in result.stderr.lower()
    assert claude.read_bytes() == before, "malformed target must be byte-unchanged"


def test_unterminated_begin_marker_dry_run_errors(
    target_repo: Path, fake_uv_env: dict[str, str]
) -> None:
    """--dry-run must surface the malformed-marker error, not report an in-place replace."""
    claude = target_repo / "CLAUDE.md"
    claude.write_text("# My Repo\n<!-- BEGIN KICAD-TOOLS -->\nstale half block\nUSER CONTENT\n")
    before = claude.read_bytes()

    result = run_installer(target_repo, "--dry-run", "--path", str(REPO_ROOT), env=fake_uv_env)

    assert result.returncode != 0
    assert "unterminated" in result.stderr.lower()
    assert "replace existing kicad-tools block" not in result.stdout
    assert claude.read_bytes() == before


def test_end_before_begin_marker_errors_without_data_loss(
    target_repo: Path, fake_uv_env: dict[str, str]
) -> None:
    """Inverse malformation (END before BEGIN): also errors out, file unchanged.

    Documented behavior: an END that precedes its BEGIN is treated as malformed
    and aborts (the grep gate still fires because a BEGIN exists later). Either
    an error or a safe no-op is acceptable per the issue; we choose to error so
    the operator fixes the markers. Never silent corruption.
    """
    claude = target_repo / "CLAUDE.md"
    claude.write_text(
        "# My Repo\n"
        "USER CONTENT ABOVE\n"
        "<!-- END KICAD-TOOLS -->\n"
        "USER CONTENT BETWEEN\n"
        "<!-- BEGIN KICAD-TOOLS -->\n"
        "block body\n"
        "<!-- END KICAD-TOOLS -->\n"
    )
    before = claude.read_bytes()

    result = run_installer(target_repo, "--path", str(REPO_ROOT), env=fake_uv_env)

    assert result.returncode != 0
    err = result.stderr.lower()
    assert "before any" in err or "unterminated" in err
    assert claude.read_bytes() == before


# --- skill selection ---------------------------------------------------------


def test_skills_filter_selects_named_skill(target_repo: Path, fake_uv_env: dict[str, str]) -> None:
    result = run_installer(
        target_repo, "--skills=ee-review", "--path", str(REPO_ROOT), env=fake_uv_env
    )
    assert result.returncode == 0, result.stderr
    kct_dir = target_repo / ".claude" / "commands" / "kct"
    assert (kct_dir / "ee-review.md").exists()
    # README is always vendored (documents the namespace).
    assert (kct_dir / "README.md").exists()
    meta = json.loads((target_repo / ".kct" / "install-metadata.json").read_text())
    assert meta["skills_selected"] == ["ee-review"]


def test_unknown_skill_errors(target_repo: Path, fake_uv_env: dict[str, str]) -> None:
    result = run_installer(
        target_repo, "--skills=does-not-exist", "--path", str(REPO_ROOT), env=fake_uv_env
    )
    assert result.returncode != 0
    assert "unknown skill" in result.stderr


# --- prerequisite failure ----------------------------------------------------


def test_missing_pyproject_fails_clearly(tmp_path: Path, fake_uv_env: dict[str, str]) -> None:
    target = tmp_path / "no-pyproject"
    target.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=target, check=True)
    result = run_installer(target, "--path", str(REPO_ROOT), env=fake_uv_env)
    assert result.returncode != 0
    assert "no pyproject.toml" in result.stderr


# --- git mode (asserted on pyproject content, no network) --------------------


def test_git_mode_writes_git_source(target_repo: Path, fake_uv_env: dict[str, str]) -> None:
    result = run_installer(target_repo, "--tag", "v0.14.0", env=fake_uv_env)
    assert result.returncode == 0, result.stderr
    pyproject = (target_repo / "pyproject.toml").read_text()
    assert '"kicad-tools"' in pyproject
    src = pyproject.split("[tool.uv.sources]", 1)[1]
    assert "git" in src
    assert "rjwalters/kicad-tools" in src
    assert "v0.14.0" in src
    meta = json.loads((target_repo / ".kct" / "install-metadata.json").read_text())
    assert meta["source_mode"] == "git"


# --- portable CI gates vendored into .kct/ci/ (#4056) ------------------------


def test_ci_gates_vendored_byte_identical_and_executable(
    target_repo: Path, fake_uv_env: dict[str, str]
) -> None:
    """The 3 portable gates land in .kct/ci/, byte-identical + executable."""
    result = run_installer(target_repo, "--path", str(REPO_ROOT), env=fake_uv_env)
    assert result.returncode == 0, result.stderr

    ci_dir = target_repo / ".kct" / "ci"
    for name in VENDORED_CI_GATES:
        dst = ci_dir / name
        assert dst.exists(), f"{name} not vendored into .kct/ci/"
        assert dst.read_bytes() == (CI_GATES_SRC / name).read_bytes(), (
            f"{name} must be a verbatim copy of scripts/ci/{name}"
        )
        # Executable so a consumer can invoke the gate directly.
        assert dst.stat().st_mode & 0o111, f"{name} must be executable"

    # A README documenting consumer-side invocation ships alongside the gates.
    readme = ci_dir / "README.md"
    assert readme.exists()
    readme_text = readme.read_text()
    assert "--allow" in readme_text, "README must document the --allow N pattern"
    assert "check_copper_lvs.py" in readme_text
    assert "check_routed_drc.py" in readme_text


def test_ci_gates_not_over_vendored(target_repo: Path, fake_uv_env: dict[str, str]) -> None:
    """Repo-internal / board-specific gates must NOT be vendored."""
    result = run_installer(target_repo, "--path", str(REPO_ROOT), env=fake_uv_env)
    assert result.returncode == 0, result.stderr
    ci_dir = target_repo / ".kct" / "ci"
    for excluded in (
        "check_board_00_e2e.py",
        "check_board_05_blocking.py",
        "check_diffpair_coverage.py",
        "check_matchgroup_coverage.py",
    ):
        assert not (ci_dir / excluded).exists(), f"{excluded} must not be vendored"


def test_ci_gates_recorded_in_metadata(target_repo: Path, fake_uv_env: dict[str, str]) -> None:
    """install-metadata.json installed_files lists every vendored gate + README."""
    result = run_installer(target_repo, "--path", str(REPO_ROOT), env=fake_uv_env)
    assert result.returncode == 0, result.stderr
    meta = json.loads((target_repo / ".kct" / "install-metadata.json").read_text())
    for name in VENDORED_CI_GATES:
        assert f".kct/ci/{name}" in meta["installed_files"]
    assert ".kct/ci/README.md" in meta["installed_files"]


def test_ci_gates_dry_run_writes_nothing(target_repo: Path, fake_uv_env: dict[str, str]) -> None:
    """--dry-run must not create .kct/ci/ or any gate file."""
    result = run_installer(target_repo, "--dry-run", "--path", str(REPO_ROOT), env=fake_uv_env)
    assert result.returncode == 0, result.stderr
    assert not (target_repo / ".kct" / "ci").exists()
    assert "vendor .kct/ci/check_copper_lvs.py" in result.stdout


def test_ci_gates_rerun_idempotent(target_repo: Path, fake_uv_env: dict[str, str]) -> None:
    """A second install re-copies the gates without duplication or drift."""
    first = run_installer(target_repo, "--path", str(REPO_ROOT), env=fake_uv_env)
    assert first.returncode == 0, first.stderr
    second = run_installer(target_repo, "--path", str(REPO_ROOT), env=fake_uv_env)
    assert second.returncode == 0, second.stderr

    ci_dir = target_repo / ".kct" / "ci"
    # Still exactly the expected set (3 gates + README), all byte-identical.
    vendored = sorted(p.name for p in ci_dir.iterdir())
    assert vendored == sorted([*VENDORED_CI_GATES, "README.md"])
    for name in VENDORED_CI_GATES:
        assert (ci_dir / name).read_bytes() == (CI_GATES_SRC / name).read_bytes()

    # Metadata still lists each gate exactly once.
    meta = json.loads((target_repo / ".kct" / "install-metadata.json").read_text())
    for name in VENDORED_CI_GATES:
        assert meta["installed_files"].count(f".kct/ci/{name}") == 1


def test_vendored_gates_run_standalone_from_consumer(
    target_repo: Path, fake_uv_env: dict[str, str]
) -> None:
    """Functional smoke: the vendored gates operate standalone from .kct/ci/.

    Proves (a) check_routed_drc.py's sibling `from net_class_map_resolver
    import ...` still resolves after relocation (its --help importing the
    module is the tell), (b) the zero-files no-op exits 0, and (c) copper-LVS
    gates a JSON verdict with no repo-relative path leakage.
    """
    result = run_installer(target_repo, "--path", str(REPO_ROOT), env=fake_uv_env)
    assert result.returncode == 0, result.stderr
    ci_dir = target_repo / ".kct" / "ci"

    # (a) check_routed_drc.py --help succeeds ONLY if the sibling import of
    # net_class_map_resolver resolves at module load (it is a top-level import).
    drc_help = subprocess.run(
        ["python3", str(ci_dir / "check_routed_drc.py"), "--help"],
        capture_output=True,
        text=True,
    )
    assert drc_help.returncode == 0, drc_help.stderr
    assert "usage: check_routed_drc" in drc_help.stdout

    # (b) zero files passed is a documented no-op that exits 0.
    drc_noop = subprocess.run(
        ["python3", str(ci_dir / "check_routed_drc.py"), "--allow", "0"],
        capture_output=True,
        text=True,
    )
    assert drc_noop.returncode == 0, drc_noop.stderr

    # (c) copper-LVS gate: clean verdict -> 0, dirty verdict -> 2, from a path
    # outside this repo (the tmp target), proving no boards/-relative assumption.
    clean = target_repo / "clean.json"
    clean.write_text('{"clean": true, "mismatches": []}')
    clean_run = subprocess.run(
        ["python3", str(ci_dir / "check_copper_lvs.py"), str(clean)],
        capture_output=True,
        text=True,
    )
    assert clean_run.returncode == 0, clean_run.stderr

    dirty = target_repo / "dirty.json"
    dirty.write_text('{"clean": false, "mismatches": [{"net": "N", "kind": "short"}]}')
    dirty_run = subprocess.run(
        ["python3", str(ci_dir / "check_copper_lvs.py"), str(dirty)],
        capture_output=True,
        text=True,
    )
    assert dirty_run.returncode == 2, dirty_run.stdout


# --- .kct/CONVENTIONS.md vendoring (#4190) -----------------------------------


def test_conventions_vendored_with_verbatim_conventions(
    target_repo: Path, fake_uv_env: dict[str, str]
) -> None:
    """The three load-bearing Epic #4054 conventions are vendored verbatim."""
    result = run_installer(target_repo, "--path", str(REPO_ROOT), env=fake_uv_env)
    assert result.returncode == 0, result.stderr

    conventions = target_repo / ".kct" / "CONVENTIONS.md"
    assert conventions.exists()
    text = conventions.read_text()
    # (1) native backend build, (2) cross-gate DRC, (3) artifact-first.
    assert "build-native" in text
    assert "refill-zones" in text
    assert "Artifact-first" in text

    # Recorded in the metadata manifest alongside the other vendored artifacts.
    meta = json.loads((target_repo / ".kct" / "install-metadata.json").read_text())
    assert meta["installed_files"].count(".kct/CONVENTIONS.md") == 1


def test_conventions_dry_run_writes_nothing(target_repo: Path, fake_uv_env: dict[str, str]) -> None:
    """--dry-run reports the planned .kct/CONVENTIONS.md write without creating it."""
    result = run_installer(target_repo, "--dry-run", "--path", str(REPO_ROOT), env=fake_uv_env)
    assert result.returncode == 0, result.stderr
    assert not (target_repo / ".kct" / "CONVENTIONS.md").exists()
    assert "write .kct/CONVENTIONS.md" in result.stdout


def test_conventions_rerun_idempotent(target_repo: Path, fake_uv_env: dict[str, str]) -> None:
    """A second install refreshes .kct/CONVENTIONS.md in place, no duplication."""
    first = run_installer(target_repo, "--path", str(REPO_ROOT), env=fake_uv_env)
    assert first.returncode == 0, first.stderr
    conventions = target_repo / ".kct" / "CONVENTIONS.md"
    first_text = conventions.read_text()

    second = run_installer(target_repo, "--path", str(REPO_ROOT), env=fake_uv_env)
    assert second.returncode == 0, second.stderr
    assert conventions.read_text() == first_text  # refreshed in place, identical

    meta = json.loads((target_repo / ".kct" / "install-metadata.json").read_text())
    assert meta["installed_files"].count(".kct/CONVENTIONS.md") == 1
