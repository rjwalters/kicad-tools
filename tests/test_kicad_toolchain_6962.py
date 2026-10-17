"""Native evidence runs on the pinned KiCad toolchain (Issue #6962).

Board 03's readiness bundle gained 37 ``lib_footprint_issues`` warnings when
it was regenerated on a host with no global ``fp-lib-table``.  These tests pin
the four parts of the fix: committed library tables in an isolated config
home, the Docker fallback defaulting to CI's image digest, a refusal to write
evidence when kicad-cli could not resolve a library, and canonical reports.
"""

from __future__ import annotations

import json
import os
import random
import re
import shutil
import stat
import sys
from pathlib import Path

import pytest
import yaml

from kicad_tools.cli import kicad_toolchain as toolchain
from kicad_tools.cli import readiness_cmd, runner
from tests.test_kct_readiness_cmd import FakeEngines, make_board

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / ".github" / "workflows"

MISSING = "The current configuration does not include the footprint library 'Resistor_SMD'"


def _violation(kind: str, description: str, x: float, severity: str = "warning") -> dict:
    return {
        "type": kind,
        "severity": severity,
        "description": description,
        "items": [{"description": f"item at {x}", "pos": {"x": x, "y": 1.0}, "uuid": f"u{x}"}],
    }


def _report(violations: list[dict], date: str = "2026-10-17T10:00:00") -> dict:
    return {
        "$schema": "https://schemas.kicad.org/drc.v1.json",
        "coordinate_units": "mm",
        "date": date,
        "kicad_version": "10.0.6",
        "source": "board.kicad_pcb",
        "violations": violations,
        "unconnected_items": [],
        "schematic_parity": [],
    }


# ---------------------------------------------------------------------------
# Pinned image
# ---------------------------------------------------------------------------


def test_fallback_image_is_ci_pinned_digest(monkeypatch):
    workflow = yaml.safe_load((WORKFLOWS / "ci.yml").read_text())
    ci_image = workflow["env"]["KICAD_IMAGE"]
    assert re.fullmatch(r"kicad/kicad@sha256:[0-9a-f]{64}", ci_image)
    # The packaged mirror (used from a wheel) and the live read agree with CI.
    assert ci_image == toolchain.PINNED_KICAD_IMAGE
    assert toolchain.ci_workflow_image(WORKFLOWS / "ci.yml") == ci_image
    assert toolchain.pinned_kicad_image() == ci_image
    assert ci_image == runner.DEFAULT_KICAD_DOCKER_IMAGE

    monkeypatch.delenv(toolchain.KICAD_DOCKER_IMAGE_ENV, raising=False)
    cmd = runner.build_docker_kicad_cmd(["--version"], Path("/tmp/work"))
    assert cmd[cmd.index("--entrypoint") + 2] == ci_image
    assert "kicad/kicad:10.0" not in cmd


def test_docker_fallback_carries_pinned_config_home(monkeypatch):
    monkeypatch.setenv(toolchain.KICAD_DOCKER_IMAGE_ENV, "example/kicad@sha256:" + "a" * 64)
    cmd = runner.build_docker_kicad_cmd(["pcb", "drc", "b.kicad_pcb"], Path("/tmp/work"))
    assert cmd[cmd.index("--entrypoint") + 2] == "example/kicad@sha256:" + "a" * 64
    assert "KICAD_CONFIG_HOME=/kct-kicad-config" in cmd
    mount = next(v for v in cmd if v.endswith(":/kct-kicad-config"))
    host_home = Path(mount.rsplit(":", 1)[0])
    assert toolchain.check_config_home(host_home) == []


def test_ci_workflow_image_requires_exactly_one_pin(tmp_path):
    workflow = tmp_path / "ci.yml"
    workflow.write_text("env:\n  KICAD_IMAGE: a\n  KICAD_IMAGE: b\n")
    assert toolchain.ci_workflow_image(workflow) is None
    workflow.write_text("env:\n  KICAD_IMAGE: kicad/kicad@sha256:" + "b" * 64 + "\n")
    assert toolchain.ci_workflow_image(workflow) == "kicad/kicad@sha256:" + "b" * 64


# ---------------------------------------------------------------------------
# Pinned library tables
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,variable", [("fp-lib-table", "FOOTPRINT"), ("sym-lib-table", "SYMBOL")]
)
def test_pinned_tables_are_flat_stock_tables(name, variable):
    text = (toolchain.library_tables_dir() / name).read_text()
    libs = re.findall(r'\(lib \(name "([^"]+)"\) \(type "([^"]+)"\) \(uri "([^"]+)"\)', text)
    assert len(libs) > 100
    assert len({lib for lib, _, _ in libs}) == len(libs)
    # Flat: no `(type "Table")` pointer into some install, every URI through
    # the KiCad 10 stock-library variable.
    assert {kind for _, kind, _ in libs} == {"KiCad"}
    assert all(uri.startswith(f"${{KICAD10_{variable}_DIR}}/") for _, _, uri in libs)


def test_board03_stock_nicknames_are_pinned():
    text = (toolchain.library_tables_dir() / "fp-lib-table").read_text()
    for nickname in (
        "Button_Switch_SMD",
        "Capacitor_SMD",
        "Connector_JST",
        "Connector_USB",
        "Crystal",
        "Fuse",
        "Package_QFP",
        "Package_TO_SOT_SMD",
        "Resistor_SMD",
    ):
        assert f'(name "{nickname}")' in text


def test_populate_and_env(tmp_path):
    home = toolchain.populate_config_home(tmp_path / "home")
    assert toolchain.check_config_home(home) == []
    (home / "10.0" / "fp-lib-table").write_text("(fp_lib_table)")
    assert toolchain.check_config_home(home) == [
        f"{home / '10.0' / 'fp-lib-table'} differs from the pinned fp-lib-table"
    ]
    # Re-populating replaces whatever an earlier run left behind.
    toolchain.populate_config_home(home)
    assert toolchain.check_config_home(home) == []
    env = toolchain.kicad_cli_env(home, base={"PATH": "/bin"})
    assert env == {"PATH": "/bin", "KICAD_CONFIG_HOME": str(home)}


# ---------------------------------------------------------------------------
# Evidence toolchain: isolation + qualified version
# ---------------------------------------------------------------------------


def _fake_kicad_cli(directory: Path, version: str, log: Path | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "kicad-cli"
    record = f'echo "$KICAD_CONFIG_HOME" >> {log}\n' if log else ""
    script.write_text(f"#!/bin/sh\n{record}echo {version}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


@pytest.fixture
def isolated_path(monkeypatch, tmp_path):
    """PATH holding only a fake kicad-cli directory (plus /bin for sh)."""
    bindir = tmp_path / "bin"
    monkeypatch.setenv("PATH", os.pathsep.join([str(bindir), "/usr/bin", "/bin"]))
    monkeypatch.delenv(toolchain.KICAD_DOCKER_ENV, raising=False)
    monkeypatch.setenv("KICAD_CONFIG_HOME", str(tmp_path / "user-config"))
    monkeypatch.setattr(runner, "_kicad_cli_candidates", lambda: [bindir / "kicad-cli"])
    runner.find_kicad_cli.cache_clear()
    yield bindir
    runner.find_kicad_cli.cache_clear()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell fake")
def test_unqualified_kicad_cli_is_refused(isolated_path):
    _fake_kicad_cli(isolated_path, "10.0.7")
    with pytest.raises(toolchain.KiCadEnvironmentError, match=r"10\.0\.7.*KCT_KICAD_DOCKER=1"):
        with toolchain.evidence_toolchain():
            pytest.fail("an unqualified kicad-cli must not run the block")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell fake")
def test_qualified_kicad_cli_gets_isolated_config_home(isolated_path, tmp_path):
    log = tmp_path / "homes.log"
    _fake_kicad_cli(isolated_path, toolchain.PINNED_KICAD_VERSION, log)
    with toolchain.evidence_toolchain() as active:
        assert active.version == toolchain.PINNED_KICAD_VERSION
        assert active.docker_image is None
        home = Path(os.environ["KICAD_CONFIG_HOME"])
        assert home == active.config_home
        assert toolchain.check_config_home(home) == []
    # kicad-cli saw the scratch home, never the user's; the env is restored.
    assert set(log.read_text().split()) == {str(home)}
    assert os.environ["KICAD_CONFIG_HOME"] == str(tmp_path / "user-config")
    assert not home.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell fake")
def test_docker_route_runs_pinned_image_with_config_home(isolated_path, tmp_path, monkeypatch):
    calls = tmp_path / "docker.log"
    docker = isolated_path / "docker"
    isolated_path.mkdir(parents=True, exist_ok=True)
    docker.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$@" > {calls}\necho {toolchain.PINNED_KICAD_VERSION}\n'
    )
    docker.chmod(0o755)
    _fake_kicad_cli(isolated_path, "10.0.7")  # a host build that must NOT be used
    monkeypatch.setenv(toolchain.KICAD_DOCKER_ENV, "1")
    monkeypatch.delenv(toolchain.KICAD_DOCKER_IMAGE_ENV, raising=False)
    # PATH first, as the real lookup does: the shim must win over the host build.
    monkeypatch.setattr(runner, "_kicad_cli_candidates", lambda: [Path(shutil.which("kicad-cli"))])
    with toolchain.evidence_toolchain(mounts=[tmp_path]) as active:
        assert active.version == toolchain.PINNED_KICAD_VERSION
        assert active.docker_image == toolchain.pinned_kicad_image()
        argv = calls.read_text().split("\n")
        assert argv[argv.index("--entrypoint") + 2] == toolchain.pinned_kicad_image()
        assert f"KICAD_CONFIG_HOME={active.config_home}" in argv
        assert "--user" in argv
        mounts = [argv[i + 1].split(":", 1)[0] for i, v in enumerate(argv) if v == "-v"]
        assert any(tmp_path.is_relative_to(m) for m in mounts), mounts


# ---------------------------------------------------------------------------
# Fail closed on library findings
# ---------------------------------------------------------------------------


def test_library_findings_are_refused():
    clean = _report([_violation("clearance", "Clearance violation", 1.0, "error")])
    toolchain.require_library_environment(clean)
    dirty = _report([*clean["violations"], _violation("lib_footprint_issues", MISSING, 2.0)])
    with pytest.raises(toolchain.KiCadEnvironmentError, match="Resistor_SMD"):
        toolchain.require_library_environment(dirty)


def test_finalize_deletes_refused_report(tmp_path):
    path = tmp_path / "native-drc.json"
    path.write_text(json.dumps(_report([_violation("lib_footprint_issues", MISSING, 1.0)])))
    with pytest.raises(toolchain.KiCadEnvironmentError):
        toolchain.finalize_native_drc_report(path)
    assert not path.exists()


def _native_kicad_cli(report_factory):
    def fake(args, raw):
        raw.write_text(json.dumps(report_factory()))
        return readiness_cmd.EngineRun(True, returncode=0)

    return fake


def test_readiness_refuses_missing_library_and_writes_no_evidence(tmp_path, monkeypatch):
    board = make_board(tmp_path)
    fake = FakeEngines(board)
    engines = fake.bundle()
    engines.native_drc = readiness_cmd._run_native_drc
    monkeypatch.setattr(
        readiness_cmd,
        "_run_kicad_cli",
        _native_kicad_cli(lambda: _report([_violation("lib_footprint_issues", MISSING, 1.0)])),
    )
    before = sorted(p.relative_to(board).as_posix() for p in board.rglob("*"))

    code = readiness_cmd.main([str(board), "--mfr", "jlcpcb", "--verify"], engines=engines)

    assert code == 2
    assert not (board / "output" / "readiness-verification").exists()
    assert not (board / "output" / "readiness.json").exists()
    assert sorted(p.relative_to(board).as_posix() for p in board.rglob("*")) == before


# ---------------------------------------------------------------------------
# Canonical reports
# ---------------------------------------------------------------------------


def test_canonical_report_is_order_and_date_independent():
    findings = [_violation("clearance", f"gap {i}", float(i), "error") for i in range(12)]
    shuffled = findings[:]
    random.Random(6962).shuffle(shuffled)
    a = toolchain.canonical_drc_report(_report(findings, "2026-10-17T10:00:00"), date="X")
    b = toolchain.canonical_drc_report(_report(shuffled, "2026-10-18T11:11:11"), date="X")
    assert json.dumps(a) == json.dumps(b)
    assert a["date"] == "X"
    # Items inside a finding keep their order (A/B roles are meaningful).
    assert a["violations"][0]["items"] == findings[0]["items"]


def test_readiness_evidence_is_byte_identical_across_runs(tmp_path, monkeypatch):
    """Two verify runs with the same --checked-at write the same bytes."""
    board = make_board(tmp_path)
    # A finished generic package to verify (generated with clean fake engines).
    assert (
        readiness_cmd.main(
            [str(board), "--mfr", "jlcpcb", "--generate"], engines=FakeEngines(board).bundle()
        )
        == 0
    )
    findings = [_violation("silk_overlap", f"silk {i}", float(i)) for i in range(8)]
    snapshots = []
    for seed, date in ((1, "2026-10-17T01:00:00"), (2, "2026-10-17T02:00:00")):
        shuffled = findings[:]
        random.Random(seed).shuffle(shuffled)
        monkeypatch.setattr(
            readiness_cmd,
            "_run_kicad_cli",
            _native_kicad_cli(lambda s=shuffled, d=date: _report(s, d)),
        )
        engines = FakeEngines(board).bundle()
        engines.native_drc = readiness_cmd._run_native_drc
        readiness_cmd.main(
            [str(board), "--mfr", "jlcpcb", "--verify", "--checked-at", "2026-10-17T00:00:00Z"],
            engines=engines,
        )
        evidence = board / "output" / "readiness-verification"
        snapshots.append(
            {
                p.relative_to(evidence).as_posix(): p.read_bytes()
                for p in sorted(evidence.rglob("*"))
                if p.is_file()
            }
        )
    assert json.loads(snapshots[0]["readiness.json"])["status"] == "ready"
    assert "native-drc-raw.json" in snapshots[0]
    assert json.loads(snapshots[0]["native-drc-raw.json"])["date"] == "2026-10-17T00:00:00Z"
    assert json.loads(snapshots[0]["readiness.json"])["checked_at"] == "2026-10-17T00:00:00Z"
    assert snapshots[0] == snapshots[1]


# ---------------------------------------------------------------------------
# Guards over committed evidence, CI and producer scripts
# ---------------------------------------------------------------------------


def _committed_native_reports() -> list[Path]:
    reports = []
    for path in sorted((REPO / "boards").rglob("*.json")):
        relative = path.relative_to(REPO / "boards").parts
        if "diagnostics" in relative or "external" in relative[:1]:
            continue
        reports.append(path)
    return reports


def test_no_committed_report_records_library_findings():
    offenders = [
        path.relative_to(REPO).as_posix()
        for path in _committed_native_reports()
        if "lib_footprint_issues" in path.read_text(errors="replace")
    ]
    assert offenders == [], (
        "committed native DRC evidence records lib_footprint_issues -- it was produced "
        "without the pinned library tables (Issue #6962); regenerate it through the "
        f"pinned toolchain: {offenders}"
    )


_INIT = "init_kicad_libraries.py"
#: Anything that can run kicad-cli on a board: a kicad-cli subcommand, the
#: test suite, or project code.
_KICAD_WORK = re.compile(r"kicad-cli\s+(?:pcb|sch|fp|sym|jobset)\b|pytest|uv run|\bkct\b")


def _kicad_container(job: dict) -> bool:
    container = job.get("container")
    image = container.get("image", "") if isinstance(container, dict) else str(container or "")
    return "kicad" in image


def _kicad_docker_steps(job: dict) -> list[str]:
    runs = [str(step.get("run", "")) for step in job.get("steps", [])]
    return [run for run in runs if "docker run" in run and "KICAD_IMAGE" in run]


def _step_text(step: dict) -> str:
    return " ".join(str(step.get(key, "")) for key in ("run", "uses", "with"))


def _code(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


@pytest.mark.parametrize("workflow", ["ci.yml", "slow-tests.yml"])
def test_every_kicad_ci_job_initialises_pinned_tables_first(workflow):
    jobs = yaml.safe_load((WORKFLOWS / workflow).read_text())["jobs"]
    containers = {name: job for name, job in jobs.items() if _kicad_container(job)}
    assert containers, f"{workflow}: no KiCad container jobs found -- guard is vacuous"
    for name, job in containers.items():
        texts = [_step_text(step) for step in job.get("steps", [])]
        init = next((i for i, text in enumerate(texts) if _INIT in text), None)
        assert init is not None, f"{workflow}:{name} runs KiCad without {_INIT}"
        for text in texts[:init]:
            # Only prerequisites, checkout and the version probe may precede it.
            assert not _KICAD_WORK.search(_code(text)), (
                f"{workflow}:{name} uses KiCad before {_INIT}: {text[:120]!r}"
            )
    for name, job in jobs.items():
        for run in _kicad_docker_steps(job):
            # A `docker run "$KICAD_IMAGE"` step must hand its work to a script
            # that initialises the pinned config home inside the container.
            scripts = re.findall(r"scripts/ci/[\w./-]+\.sh", run)
            assert scripts, f"{workflow}:{name}: KiCad docker step runs no repo script"
            for script in scripts:
                text = (REPO / script).read_text()
                assert f'eval "$(python3 scripts/ci/{_INIT} --shell)"' in text, (
                    f"{workflow}:{name}: {script} runs kicad-cli without {_INIT}"
                )


def test_routed_drc_container_script_exports_pinned_config_home():
    script = (REPO / "scripts/ci/run_native_routed_drc.sh").read_text()
    assert f'eval "$(python3 scripts/ci/{_INIT} --shell)"' in script


def test_every_board_evidence_script_runs_on_the_pinned_toolchain():
    offenders = []
    for path in sorted((REPO / "scripts" / "boards").glob("*.py")):
        if path.name.startswith("_"):
            continue
        text = path.read_text()
        if "evidence_toolchain" not in text and "run_on_evidence_toolchain" not in text:
            offenders.append(path.name)
    assert offenders == []
