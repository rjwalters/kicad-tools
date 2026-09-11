"""Behavioral coverage for CI selection, real Git diffs and job wiring."""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ci_selection", ROOT / "scripts/ci/select_jobs.py")
assert SPEC and SPEC.loader
selector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(selector)


def chosen(paths):
    return {job for job, selected in selector.select_jobs(paths).items() if selected}


def test_docs_keep_actual_test_consumers_without_unrelated_routing():
    assert chosen(["docs/guides/routing.md"]) == {"test"}


@pytest.mark.parametrize("board,jobs", selector.BOARD_JOBS.items())
def test_board_change_selects_all_and_only_its_consumers(board, jobs):
    assert chosen([f"boards/{board}/generate_design.py"]) == {"test", *jobs}


def test_multiple_boards_select_union():
    assert chosen(
        ["boards/00-simple-led/output/a.kicad_pcb", "boards/06-diffpair-test/design.py"]
    ) == {
        "test",
        "kicad-cli-smoke",
        "board-00-end-to-end",
        "board-06-end-to-end",
        "diffpair-routing-regression",
    }


@pytest.mark.parametrize(
    "path",
    [
        "src/kicad_tools/router/core.py",
        "scripts/ci/check_diffpair_coverage.py",
        "scripts/ci/select_jobs.py",
        ".github/workflows/ci.yml",
        "pyproject.toml",
        "uv.lock",
        "cpp/src/router.cpp",
        "tests/test_copper_lvs.py",
        "boards/shared.py",
        "boards/99-new-board/recipe.py",
        "new-config",
        "docs/../src/core.py",
    ],
)
def test_shared_unknown_and_selection_changes_keep_full_coverage(path):
    assert chosen([path]) == set(selector.JOBS)


def test_absent_diff_is_not_an_empty_diff():
    assert chosen(None) == set(selector.JOBS)
    assert chosen([]) == set()


@pytest.fixture
def repo(tmp_path):
    def git(*args):
        return (
            subprocess.check_output(["git", *args], cwd=tmp_path, stderr=subprocess.PIPE)
            .decode()
            .strip()
        )

    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "CI selection test")
    (tmp_path / "initial").write_text("initial")
    git("add", ".")
    git("commit", "-qm", "initial")
    return tmp_path, git


def commit_file(repo, path, content="content"):
    root, git = repo
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    git("add", ".")
    git("commit", "-qm", "fixture")
    return git("rev-parse", "HEAD")


def test_push_range_includes_all_commits_and_both_rename_sides(repo):
    root, git = repo
    old = "boards/00-simple-led/with space\nand newline.kicad_pcb"
    base = commit_file(repo, old)
    new = "boards/06-diffpair-test/moved.kicad_pcb"
    (root / new).parent.mkdir(parents=True)
    git("mv", old, new)
    git("commit", "-qm", "rename")
    head = commit_file(repo, "docs/new.md")
    paths = selector.changed_paths({"before": base, "after": head}, "push", root)
    assert set(paths) == {old, new, "docs/new.md"}
    assert chosen(paths) == {
        "test",
        "board-00-end-to-end",
        "board-06-end-to-end",
        "diffpair-routing-regression",
    }


def test_pr_diff_uses_merge_base_excluding_unrelated_base_changes(repo):
    root, git = repo
    base = git("rev-parse", "HEAD")
    git("checkout", "-qb", "topic")
    head = commit_file(repo, "docs/topic.md")
    git("checkout", "--detach", base)
    base = commit_file(repo, "src/unrelated.py")
    event = {"pull_request": {"base": {"sha": base}, "head": {"sha": head}}}
    assert selector.changed_paths(event, "pull_request", root) == ["docs/topic.md"]


@pytest.mark.parametrize(
    "event,event_name",
    [
        ({"before": "0" * 40, "after": "a" * 40}, "push"),
        ({"before": "a" * 40, "after": "b" * 40}, "push"),
        ({"before": "$(touch sentinel)", "after": "b" * 40}, "push"),
        ({}, "push"),
        ({}, "workflow_dispatch"),
        ([], "pull_request"),
    ],
)
def test_failed_or_invalid_discovery_selects_full_coverage(repo, event, event_name):
    root, _ = repo
    path = root / "event.json"
    path.write_text(json.dumps(event))
    assert all(selector.plan(path, event_name, root).values())
    assert not (root / "sentinel").exists()


def test_invalid_event_file_selects_full_coverage(tmp_path):
    path = tmp_path / "event.json"
    path.write_text("{")
    assert all(selector.plan(path, "push", tmp_path).values())
    assert all(selector.plan(tmp_path / "missing", "push", tmp_path).values())


def test_workflow_wires_every_heavy_consumer_and_retains_operator_gate():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    jobs = workflow["jobs"]
    heavy = set(jobs) - {"changes", "lint", "typecheck", "routed-pcb-drc-check"}
    assert heavy == set(selector.JOBS)
    assert set(jobs["changes"]["outputs"]) == heavy
    for job in heavy:
        assert set(jobs[job]["needs"]) == {"changes", "lint", "typecheck"}
        assert f"needs.changes.outputs['{job}'] == 'true'" in jobs[job]["if"]
        assert "always()" not in jobs[job]["if"]  # Failed cheap gates must stop work.
        assert jobs["changes"]["outputs"][job] == "${{ steps.plan.outputs['" + job + "'] }}"
    for job in ("board-07-end-to-end", "matchgroup-routing-regression"):
        assert "vars.BOARD_07_CI_ENABLED == 'true'" in jobs[job]["if"]
    for job in ("test", "board-06-end-to-end"):
        assert "github.event_name == 'push'" in jobs[job]["runs-on"]
    triggers = workflow.get("on", workflow.get(True))  # YAML 1.1's on coercion.
    assert set(triggers) == {"push", "pull_request"}
    assert not any("paths" in config or "paths-ignore" in config for config in triggers.values())


def test_cli_emits_workflow_outputs_from_event_file(repo, monkeypatch):
    root, git = repo
    base = git("rev-parse", "HEAD")
    head = commit_file(repo, "docs/guide.md")
    event_path = root / "event.json"
    event_path.write_text(json.dumps({"before": base, "after": head}))
    output = root / "output"
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.chdir(root)
    selector.main()
    actual = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert set(actual) == set(selector.JOBS)
    assert actual.pop("test") == "true"
    assert set(actual.values()) == {"false"}


@pytest.mark.parametrize("board,jobs", selector.BOARD_JOBS.items())
def test_saved_board_outputs_retain_standalone_native_smoke(board, jobs):
    assert chosen([f"boards/{board}/output/board.kicad_sch"]) == {"test", "kicad-cli-smoke", *jobs}
