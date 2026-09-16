"""Opt-in wiring preserves the existing CI workload rather than replacing it."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "scripts/ci/run_observed.py"
JOB = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]["test"]
GROUPS = {
    "board05": "Run Board05 manufacturing regression",
    "mask-copper": "Run native mask-to-copper gate",
    "stitch": "Run native physical-stitch acceptance",
    "zones": "Run native zone acceptance",
    "bulk": "Run tests",
}


def test_opt_in_and_resources_and_artifact_retention():
    assert JOB["env"]["PYTEST_XDIST_AUTO_NUM_WORKERS"] == "4"
    expression = JOB["env"]["KCT_NATIVE_DIAGNOSTICS"]
    assert "github.event_name == 'pull_request'" in expression
    assert (
        "contains(github.event.pull_request.body, '<!-- kct:native-diagnostics -->')" in expression
    )
    assert JOB["timeout-minutes"] == 45
    assert JOB["container"]["image"] == "kicad/kicad:10.0"
    assert "--memory 12g" in JOB["container"]["options"]
    steps = {s.get("name"): s for s in JOB["steps"]}
    for name in GROUPS.values():
        step = steps[name]
        assert not step.get("continue-on-error", False)
        assert "github.event.pull_request.body" not in step["run"]
        assert step["env"]["KCT_OBSERVER_CONTAINER_ID"] == "${{ job.container.id }}"
    artifact = steps["Retain native workload observations"]
    assert artifact["if"] == "${{ always() && env.KCT_NATIVE_DIAGNOSTICS == 'true' }}"
    assert artifact["uses"] == "actions/upload-artifact@v4"
    assert artifact["with"]["path"] == "${{ runner.temp }}/native-observer/"


@pytest.mark.parametrize("group", GROUPS)
@pytest.mark.parametrize("board07", ["true", "false"])
def test_existing_workflow_command_and_failure_propagation(tmp_path, group, board07):
    # Execute the actual YAML bash body, substituting ONLY the uv executable.
    # Its exit7 must survive serial tee pipelines and the disabled wrapper.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    args_file = tmp_path / "argv.json"
    uv = bin_dir / "uv"
    uv.write_text(
        f"#!{sys.executable}\nimport sys,json\nfrom pathlib import Path\nPath({str(args_file)!r}).write_text(json.dumps(sys.argv[1:]))\nraise SystemExit(7)\n"
    )
    uv.chmod(0o755)
    step = next(s for s in JOB["steps"] if s.get("name") == GROUPS[group])
    env = dict(
        os.environ,
        PATH=str(bin_dir) + os.pathsep + os.environ["PATH"],
        RUNNER_TEMP=str(tmp_path),
        KCT_NATIVE_DIAGNOSTICS="false",
        BOARD_07_CI_ENABLED=board07,
    )
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", step["run"]],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 7, result.stdout + result.stderr
    actual = json.loads(args_file.read_text())
    common = ["-o", "addopts=", "--benchmark-disable", "--timeout=60", "-m", "not slow"]
    expected = {
        "board05": ["run", "pytest", "tests/test_board_05_drc_allowlist.py", *common],
        "mask-copper": [
            "run",
            "--frozen",
            "python",
            "scripts/ci/check_mask_copper_native.py",
            "--artifacts",
            str(tmp_path / "mask-copper-native"),
        ],
        "stitch": [
            "run",
            "pytest",
            "tests/test_stitch_physical_completion.py",
            *common,
            "--basetemp",
            str(tmp_path / "stitch-physical-native/pytest"),
            "--junitxml",
            str(tmp_path / "stitch-physical-native/junit.xml"),
        ],
        "zones": [
            "run",
            "pytest",
            "tests/test_zones_cmd.py",
            "tests/test_zones_hv_keepout.py",
            *common,
            "--basetemp",
            str(tmp_path / "zone-native/pytest"),
            "--junitxml",
            str(tmp_path / "zone-native/junit.xml"),
        ],
        "bulk": [
            "run",
            "pytest",
            "-n",
            "auto",
            *common,
            *[
                "--ignore=tests/" + name
                for name in [
                    "test_board_05_drc_allowlist.py",
                    "test_mask_copper_native.py",
                    "test_stitch_physical_completion.py",
                    "test_zones_cmd.py",
                    "test_zones_hv_keepout.py",
                ]
            ],
        ],
    }[group]
    if group == "bulk" and board07 != "true":
        expected.append("--ignore=tests/test_board_07_matchgroup_test.py")
    assert actual == expected
    assert not (tmp_path / "native-observer").exists()


def test_enabled_identity_redacts_and_forwards_exact_command(tmp_path):
    # The reviewed observer is independently runtime-tested; intercept just
    # its entry point here to verify activation and argv without Linux /proc.
    wrapper = tmp_path / "run_observed.py"
    shutil.copyfile(WRAPPER, wrapper)
    output = tmp_path / "received.json"
    (tmp_path / "native_observer.py").write_text(
        f"import sys,json\nfrom pathlib import Path\nPath({str(output)!r}).write_text(json.dumps(sys.argv[1:]))\nraise SystemExit(7)\n"
    )
    env = dict(
        os.environ,
        RUNNER_TEMP=str(tmp_path),
        KCT_NATIVE_DIAGNOSTICS="true",
        KCT_OBSERVER_CONTAINER_ID="a" * 64,
        KCT_OBSERVER_PR_HEAD="b" * 40,
        GITHUB_RUN_ID="123",
        GITHUB_RUN_ATTEMPT="2",
        SECRET="do-not-record",
    )
    command = ["fake-workload", "--secret", "do-not-record", "space argument"]
    result = subprocess.run(
        [sys.executable, str(wrapper), "bulk", "--", *command], cwd=ROOT, env=env
    )
    assert result.returncode == 7
    received = json.loads(output.read_text())
    assert received == ["--output", str(tmp_path / "native-observer/bulk"), "--", *command]
    identity_text = (tmp_path / "native-observer/bulk-identity.json").read_text()
    identity = json.loads(identity_text)
    assert "do-not-record" not in identity_text and "--secret" not in identity_text
    assert (
        identity["source_sha"]
        == subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    )
    assert identity["container_id"] == "a" * 64
    assert identity["image_digest"] is None
    assert identity["run_id"] == "123"
    assert len(identity["argv_sha256"]) == 64


def test_absent_opt_in_executes_without_ci_environment(tmp_path):
    env = dict(os.environ)
    for name in ("KCT_NATIVE_DIAGNOSTICS", "RUNNER_TEMP"):
        env.pop(name, None)
    result = subprocess.run(
        [sys.executable, str(WRAPPER), "bulk", "--", sys.executable, "-c", "raise SystemExit(7)"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 7
    assert list(tmp_path.iterdir()) == []


def test_foreign_owned_checkout_identity_uses_only_invocation_scoped_trust(tmp_path):
    repo = tmp_path / "foreign-checkout"
    repo.mkdir()
    config = tmp_path / "global.gitconfig"
    config.write_text("[user]\n\tname = Test\n\temail = test@example.invalid\n")
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=str(config))
    subprocess.run(["git", "init", "-q", str(repo)], env=env, check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "fixture",
        ],
        env=env,
        check=True,
    )
    expected = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], env=env, text=True
    ).strip()
    baseline_config = config.read_bytes()
    (repo / ".github/workflows").mkdir(parents=True)
    (repo / ".github/workflows/ci.yml").write_text("fixture workflow")
    wrapper = repo / "run_observed.py"
    shutil.copyfile(WRAPPER, wrapper)
    (repo / "native_observer.py").write_text("raise SystemExit(7)\n")
    env.update(
        GIT_TEST_ASSUME_DIFFERENT_OWNER="1",
        RUNNER_TEMP=str(tmp_path),
        KCT_NATIVE_DIAGNOSTICS="true",
        GITHUB_WORKSPACE=str(repo),
    )
    baseline = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, env=env, capture_output=True, text=True
    )
    assert baseline.returncode == 128 and "dubious ownership" in baseline.stderr
    successor = subprocess.run(
        [sys.executable, str(wrapper), "bulk", "--", "unused-workload"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )
    assert successor.returncode == 7, successor.stderr
    identity = json.loads((tmp_path / "native-observer/bulk-identity.json").read_text())
    assert identity["source_sha"] == expected
    assert config.read_bytes() == baseline_config
    # Trust was not persisted: the same ordinary Git invocation still fails.
    after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, env=env, capture_output=True, text=True
    )
    assert after.returncode == 128 and "dubious ownership" in after.stderr
    # A different directory cannot borrow the workflow's checkout trust.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    wrong = subprocess.run(
        [sys.executable, str(wrapper), "other", "--", "unused-workload"],
        cwd=elsewhere,
        env=env,
        capture_output=True,
        text=True,
    )
    assert wrong.returncode != 0
    assert not (tmp_path / "native-observer/other-identity.json").exists()
