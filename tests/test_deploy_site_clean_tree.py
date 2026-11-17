"""deploy-site.sh restores tracked renders and checks for a clean tree (Issue #8220)."""

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/deploy-site.sh"
RENDER = "boards/b1/output/renders/top.png"


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


def _repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "boards/b1/output/renders").mkdir(parents=True)
    (repo / RENDER).write_text("committed")
    (repo / ".gitignore").write_text(".wrangler/\n")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    return repo


def _run(repo, body):
    source = SCRIPT.read_text()
    helpers = source[
        source.index("# --- Logging helpers") : source.index("# --- Cloudflare account")
    ]
    clean = source[source.index("# --- Clean-tree helpers") : source.index("# --- Parse flags")]
    script = f'REPO_ROOT="{repo}"\n{helpers}\n{clean}\n{body}'
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True)


def test_gitignore_covers_wrangler(tmp_path):
    root = Path(SCRIPT).parents[1]
    r = subprocess.run(["git", "-C", str(root), "check-ignore", "-q", ".wrangler/x"])
    assert r.returncode == 0


def test_modified_tracked_render_is_restored(tmp_path):
    repo = _repo(tmp_path)
    (repo / RENDER).write_text("fresh")
    r = _run(repo, "restore_tracked_renders")
    assert r.returncode == 0, r.stderr
    assert (repo / RENDER).read_text() == "committed"
    assert RENDER in r.stdout
    assert _git(repo, "status", "--porcelain") == ""


def test_untracked_ignored_files_do_not_trip_check(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".wrangler").mkdir()
    (repo / ".wrangler/cache").write_text("x")
    r = _run(repo, "restore_tracked_renders; warn_if_dirty_tree")
    assert r.returncode == 0
    assert "not clean" not in r.stderr


def test_unrelated_change_is_warned_not_clobbered(tmp_path):
    repo = _repo(tmp_path)
    (repo / "notes.txt").write_text("operator")
    (repo / RENDER).write_text("fresh")
    r = _run(repo, "restore_tracked_renders; warn_if_dirty_tree")
    assert r.returncode == 0
    assert (repo / "notes.txt").read_text() == "operator"
    assert "not clean" in r.stderr and "notes.txt" in r.stderr
    assert (repo / RENDER).read_text() == "committed"


def test_outside_git_is_skipped(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    r = _run(plain, "restore_tracked_renders; warn_if_dirty_tree")
    assert r.returncode == 0
    assert r.stderr == ""
