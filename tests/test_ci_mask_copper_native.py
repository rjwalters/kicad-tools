"""The native CI gate must reject unavailable prerequisites and incomplete runs."""

import importlib.util
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "mask_native_gate", ROOT / "scripts/ci/check_mask_copper_native.py"
)
assert SPEC and SPEC.loader
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)


def report(tmp_path, *, bad_tag=None, missing=False):
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite")
    names = sorted(helper.REQUIRED_WITNESSES)
    if missing:
        names.pop()
    for name in names:
        case = ET.SubElement(suite, "testcase", name=name)
        if bad_tag:
            ET.SubElement(case, bad_tag)
    path = tmp_path / "report.xml"
    ET.ElementTree(root).write(path)
    return path


@pytest.mark.parametrize("tag", ["skipped", "failure", "error"])
def test_result_guard_rejects_nonpassing_cases(tmp_path, tag):
    with pytest.raises(ValueError, match="did not pass"):
        helper.validate_results(report(tmp_path, bad_tag=tag))


def test_result_guard_requires_material_witnesses_and_nonempty_collection(tmp_path):
    with pytest.raises(ValueError, match="witnesses"):
        helper.validate_results(report(tmp_path, missing=True))
    path = tmp_path / "empty.xml"
    path.write_text("<testsuites/>")
    with pytest.raises(ValueError, match="no test cases"):
        helper.validate_results(path)


def test_result_guard_accepts_additional_parameterizations(tmp_path):
    path = report(tmp_path)
    root = ET.parse(path).getroot()
    ET.SubElement(root[0], "testcase", name="future_case[extra]")
    ET.ElementTree(root).write(path)
    assert helper.validate_results(path) == 3


@pytest.mark.parametrize("command", [None, '"/usr/bin/python3"', "[]", "[1]", '[""]'])
def test_invalid_or_missing_native_argv_fails_before_tests(tmp_path, monkeypatch, command):
    monkeypatch.delenv("KCT_MASK_NATIVE_PYTHON", raising=False)
    if command is not None:
        monkeypatch.setenv("KCT_MASK_NATIVE_PYTHON", command)
    assert helper.main(["--artifacts", str(tmp_path)]) == 1
    assert list(tmp_path.glob("run-*/gate-error.txt"))
    assert not list(tmp_path.glob("run-*/junit.xml"))


def test_unavailable_cli_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("KCT_MASK_NATIVE_PYTHON", json.dumps([sys.executable]))
    monkeypatch.setattr(helper, "find_kicad_cli", lambda: None)
    assert helper.main(["--artifacts", str(tmp_path)]) == 1


def test_unavailable_oracle_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("KCT_MASK_NATIVE_PYTHON", json.dumps([sys.executable]))
    monkeypatch.setattr(helper, "find_kicad_cli", lambda: Path("/unused/kicad-cli"))
    monkeypatch.setitem(sys.modules, "gerbonara", None)
    assert helper.main(["--artifacts", str(tmp_path)]) == 1


@pytest.mark.parametrize(
    "failure", ["assert False", "import missing_collection_dependency", "skip"]
)
def test_real_pytest_failure_is_rejected(tmp_path, failure):
    test = tmp_path / "test_failure.py"
    if failure.startswith("import"):
        test.write_text(failure + "\n")
    elif failure == "skip":
        test.write_text("import pytest\ndef test_skip():\n    pytest.skip('deliberate')\n")
    else:
        test.write_text("def test_failure():\n    " + failure + "\n")
    path = tmp_path / "failed.xml"
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-o", "addopts=", str(test), "--junitxml", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert (run.returncode == 0) is (failure == "skip")
    with pytest.raises(ValueError, match="failures|did not pass"):
        helper.validate_results(path)


def _stub_probe_environment(monkeypatch, *, native_version, cli_version):
    """Answer probe()'s two version subprocesses without a real KiCad install."""
    cli = Path("/unused/kicad-cli")
    monkeypatch.setenv("KCT_MASK_NATIVE_PYTHON", json.dumps([sys.executable]))
    monkeypatch.setattr(helper, "find_kicad_cli", lambda: cli)

    def check_output(command, **kwargs):
        if command[0] == str(cli):
            return cli_version + "\n"
        Path(command[-1]).write_text("shared")  # the shared-scratch marker probe() asserts
        return native_version + "\n"

    monkeypatch.setattr(helper.subprocess, "check_output", check_output)


@pytest.mark.parametrize("version", sorted(helper.QUALIFIED_NATIVE_VERSIONS))
def test_probe_accepts_every_qualified_version(tmp_path, monkeypatch, version):
    _stub_probe_environment(monkeypatch, native_version=version, cli_version=version)
    info = helper.probe(tmp_path)
    assert info["pcbnew_version"] == info["kicad_cli_version"] == version


@pytest.mark.parametrize(
    ("native_version", "cli_version"),
    [
        ("10.0.4", "10.0.4"),  # older than anything qualified
        ("10.1.0", "10.1.0"),  # newer, not yet compared against the material oracle
        ("10.0.5", "10.0.6"),  # qualified individually, but a mismatched pair
        ("", ""),  # no version reported at all
    ],
)
def test_probe_rejects_unqualified_or_mismatched_versions(
    tmp_path, monkeypatch, native_version, cli_version
):
    _stub_probe_environment(monkeypatch, native_version=native_version, cli_version=cli_version)
    with pytest.raises(ValueError, match="qualified"):
        helper.probe(tmp_path)


def test_gate_and_checker_share_one_qualified_version_set():
    """The CI gate imports the checker's set; it must never restate its own."""
    from kicad_tools.validate.mask_copper_geometry import QUALIFIED_NATIVE_VERSIONS

    assert helper.QUALIFIED_NATIVE_VERSIONS is QUALIFIED_NATIVE_VERSIONS
    source = (ROOT / "scripts/ci/check_mask_copper_native.py").read_text()
    assert "QUALIFIED_NATIVE_VERSIONS" in source
    assert not re.search(r'"10\.\d+\.\d+"', source), "gate must not hard-code a KiCad version"


def test_workflow_runs_full_gate_once_and_retains_diagnostics():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    steps = workflow["jobs"]["test"]["steps"]
    gate = next(s for s in steps if s.get("name") == "Run native mask-to-copper gate")
    assert json.loads(gate["env"]["KCT_MASK_NATIVE_PYTHON"]) == ["/usr/bin/python3"]
    assert "check_mask_copper_native.py" in gate["run"]
    assert not gate.get("continue-on-error")
    general = next(s for s in steps if s.get("name") == "Run tests")
    assert "--ignore=tests/test_mask_copper_native.py" in general["run"]
    artifact = next(s for s in steps if s.get("name") == "Retain native mask diagnostics")
    assert "failure()" in artifact["if"]
    assert "mask-copper-native/" in artifact["with"]["path"]
