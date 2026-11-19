"""Opt-in wiring preserves the existing CI workload rather than replacing it."""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "scripts/ci/run_observed.py"
WORKFLOW_TEXT = (ROOT / ".github/workflows/ci.yml").read_text()
WORKFLOW = yaml.safe_load(WORKFLOW_TEXT)
JOB = WORKFLOW["jobs"]["test"]
GROUPS = {
    "board05": "Run Board05 manufacturing regression",
    "mask-copper": "Run native mask-to-copper gate",
    "stitch": "Run native physical-stitch acceptance",
    "zones": "Run native zone acceptance",
    "bulk": "Run tests",
}


def test_opt_in_and_resources_and_artifact_retention():
    assert JOB["env"]["PYTEST_XDIST_AUTO_NUM_WORKERS"] == "4"
    # Issue #9120: the opt-in is no longer a job-level substring test of the PR
    # body (a PR that merely quoted the marker opted itself in). A step runs the
    # line-anchored script and writes KCT_NATIVE_DIAGNOSTICS to $GITHUB_ENV.
    assert "KCT_NATIVE_DIAGNOSTICS" not in JOB["env"]
    assert "contains(github.event.pull_request.body" not in WORKFLOW_TEXT
    optin = {s.get("name"): s for s in JOB["steps"]}["Resolve native diagnostics opt-in"]
    assert optin["run"].strip() == "python3 scripts/ci/native_diagnostics_optin.py"
    assert "github.event.pull_request.body" not in optin["run"]
    names = [s.get("name") for s in JOB["steps"]]
    assert names.index("Resolve native diagnostics opt-in") < names.index("Run tests")
    # Issue #6013: one runner (GitHub-hosted) for every event, so one budget.
    # The fork-routing predicate and its split 45/90 budget (#5747) are gone
    # with the self-hosted branch they selected.
    assert JOB["runs-on"] == "ubuntu-latest"
    # #5240 stopgap (operator ruling Q28 = 1): 75 -> 120 until sharding lands.
    assert JOB["timeout-minutes"] == 120
    # The 80%-of-limit warning step must read the same limit (#7327).
    budget = {s.get("name"): s for s in JOB["steps"]}["Report job time budget"]
    assert int(budget["env"]["TIMEOUT_MINUTES"]) == JOB["timeout-minutes"]
    # #5682: the KiCad image is pinned by digest at the workflow level.
    # container.image cannot reference the env context (GitHub allows only
    # github/inputs/matrix/needs/strategy/vars there), so the kicad_pin
    # bridge job republishes the single KICAD_IMAGE literal into the needs
    # context every container job references.
    assert JOB["container"]["image"] == "${{ needs.kicad_pin.outputs.image }}"
    pinned = WORKFLOW["env"]["KICAD_IMAGE"]
    assert re.fullmatch(r"kicad/kicad@sha256:[0-9a-f]{64}", pinned), pinned
    bridge = WORKFLOW["jobs"]["kicad_pin"]
    assert bridge["outputs"]["image"] == "${{ steps.pin.outputs.image }}"
    assert "--memory 12g" in JOB["container"]["options"]
    steps = {s.get("name"): s for s in JOB["steps"]}
    for name in GROUPS.values():
        step = steps[name]
        assert not step.get("continue-on-error", False)
        assert "github.event.pull_request.body" not in step["run"]
        assert step["env"]["KCT_OBSERVER_CONTAINER_ID"] == "${{ job.container.id }}"
    artifact = steps["Retain native workload observations"]
    assert artifact["if"] == "${{ always() && env.KCT_NATIVE_DIAGNOSTICS == 'true' }}"
    assert artifact["uses"] == "actions/upload-artifact@v7"
    assert artifact["with"]["path"] == "${{ runner.temp }}/native-observer/"


@pytest.mark.parametrize("group", GROUPS)
@pytest.mark.parametrize("board07", ["true", "false"])
def test_existing_workflow_command_and_failure_propagation(tmp_path, group, board07):
    # Execute the actual YAML bash body, substituting ONLY the uv executable.
    # Since issue #9340 the step delegates the check to the shared job script
    # (scripts/ci/jobs/test.sh), so the body first runs the script's
    # `kct build-native` (exit 0 through the fake) and then the group's
    # check command, whose exit 7 must survive the wrapper and the serial
    # tee pipelines.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv_log = tmp_path / "argv.jsonl"
    uv = bin_dir / "uv"
    uv.write_text(
        f"#!{sys.executable}\n"
        "import sys, json\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        f"Path({str(argv_log)!r}).open('a').write(json.dumps(args) + chr(10))\n"
        "# The per-job script's native-backend build succeeds; every check\n"
        "# command fails with the sentinel exit the test asserts on.\n"
        "raise SystemExit(0 if 'build-native' in args else 7)\n"
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
    recorded = [json.loads(line) for line in argv_log.read_text().splitlines()]
    # First the shared script's native-backend build, then the check itself.
    assert recorded[0][:3] == ["run", "kct", "build-native"], recorded
    actual = recorded[-1]
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
            # Issue #7214: list skip/xfail reasons in the short summary.
            "-rfEsx",
            # Issue #6013: the ci_extended slow tail runs in "Test (extended)",
            # and the bulk pool's timings are kept for update_ci_extended.py.
            "-o",
            "addopts=",
            "--benchmark-disable",
            "--timeout=60",
            "-m",
            "not slow and not ci_extended",
            # Issue #7327: slowest-test visibility for the time-budget step.
            "--durations=30",
            *[
                "--ignore=tests/" + name
                for name in [
                    "test_board_05_drc_allowlist.py",
                    "test_mask_copper_native.py",
                    "test_stitch_physical_completion.py",
                    "test_zones_cmd.py",
                    "test_zones_hv_keepout.py",
                    # Epic #5509 Phase 1c (#5515): a whole directory rather
                    # than a file, and the only entry here whose ignore is
                    # load-bearing for *correctness* rather than only for
                    # resource contention -- ``tests/conformance`` has its own
                    # dedicated step, so without this the suite runs twice and
                    # the dedicated step is pure added wall time.
                    "conformance",
                ]
            ],
            # Issue #9340: the shared script owns the selection; the workflow
            # step forwards the artifact path, which lands last.
            "--junitxml",
            str(tmp_path / "junit/bulk.xml"),
        ],
    }[group]
    if group == "bulk" and board07 != "true":
        # The conditional ignore sits with the script's own ignore list,
        # before the workflow-forwarded junitxml pair.
        expected.insert(-2, "--ignore=tests/test_board_07_matchgroup_test.py")
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
        KICAD_IMAGE="kicad/kicad@sha256:" + "c" * 64,
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
        == subprocess.check_output(
            ["git", "-c", f"safe.directory={ROOT}", "-C", str(ROOT), "rev-parse", "HEAD"],
            text=True,
        ).strip()
    )
    assert identity["container_id"] == "a" * 64
    assert identity["image_digest"] is None
    assert identity["configured_image"] == "kicad/kicad@sha256:" + "c" * 64
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


# --- Issue #9120: line-anchored opt-in ------------------------------------

OPTIN = ROOT / "scripts/ci/native_diagnostics_optin.py"
MARKER = "<!-- kct:native-diagnostics -->"


def _optin(tmp_path, event_name, body, *, raw_event=None):
    event = tmp_path / "event.json"
    if raw_event is not None:
        event.write_text(raw_event)
    else:
        event.write_text(json.dumps({"pull_request": {"body": body}}))
    github_env = tmp_path / "github_env"
    github_env.write_text("")
    env = dict(
        os.environ,
        GITHUB_EVENT_NAME=event_name,
        GITHUB_EVENT_PATH=str(event),
        GITHUB_ENV=str(github_env),
    )
    result = subprocess.run(
        [sys.executable, "-I", str(OPTIN)], env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    # The body is never echoed: only the verdict is written, to both sinks.
    assert result.stdout.strip() == github_env.read_text().strip()
    return github_env.read_text().strip()


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        # Alone on a line -> opted in (LF, CRLF, trailing blanks, surrounded).
        (MARKER, "true"),
        (f"Summary\n\n{MARKER}\n\nMore", "true"),
        (f"Summary\r\n{MARKER}\r\nMore", "true"),
        (f"{MARKER}   \t", "true"),
        # The exact sentence that tripped PR #9109: inline in prose -> inert.
        (
            f"`KCT_NATIVE_DIAGNOSTICS` is only 'true' when the PR body carries {MARKER} (ci.yml)",
            "false",
        ),
        (f"see {MARKER}", "false"),
        (f"{MARKER} trailing words", "false"),
        (f"`{MARKER}`", "false"),
        (f"> {MARKER}", "false"),
        (f"- {MARKER}", "false"),
        # A fenced block is how a body quotes the marker verbatim -> inert.
        (f"Example:\n```\n{MARKER}\n```\n", "false"),
        (f"Example:\n~~~md\n{MARKER}\n~~~\n", "false"),
        # ... but a fence that has closed no longer protects a later line.
        (f"```\ncode\n```\n{MARKER}\n", "true"),
        # A longer fence is not closed by a shorter run of the same character.
        (f"````\n```\n{MARKER}\n```\n````\n", "false"),
        ("", "false"),
        ("no marker here", "false"),
    ],
)
def test_optin_marker_must_be_alone_on_its_own_line(tmp_path, body, expected):
    assert _optin(tmp_path, "pull_request", body) == f"KCT_NATIVE_DIAGNOSTICS={expected}"


def test_optin_is_inert_for_null_body_missing_event_and_push(tmp_path):
    assert _optin(tmp_path, "pull_request", None) == "KCT_NATIVE_DIAGNOSTICS=false"
    assert _optin(tmp_path, "pull_request", None, raw_event="{}") == "KCT_NATIVE_DIAGNOSTICS=false"
    assert _optin(tmp_path, "pull_request", None, raw_event="not json") == (
        "KCT_NATIVE_DIAGNOSTICS=false"
    )
    # push payloads carry no pull_request body; even a marker line is ignored.
    assert _optin(tmp_path, "push", MARKER) == "KCT_NATIVE_DIAGNOSTICS=false"
    assert _optin(tmp_path, "workflow_dispatch", MARKER) == "KCT_NATIVE_DIAGNOSTICS=false"


def test_optin_without_event_file_is_false(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GITHUB_")}
    result = subprocess.run(
        [sys.executable, "-I", str(OPTIN)], env=env, capture_output=True, text=True
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "KCT_NATIVE_DIAGNOSTICS=false"
