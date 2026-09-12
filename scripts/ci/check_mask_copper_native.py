#!/usr/bin/env python3
"""Run the complete native mask suite with mandatory prerequisites and no skips."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from packaging.version import Version

from kicad_tools.cli.runner import find_kicad_cli

ROOT = Path(__file__).resolve().parents[2]
SUITE = "tests/test_mask_copper_native.py"
REQUIRED_WITNESSES = {
    "test_native_merged_bridge_exposes_third_copper_with_multiple_contributors",
    "test_native_repeated_pad_numbers_do_not_share_owner_exclusion",
}


def probe(directory: Path) -> dict:
    """Check the actual interpreters, oracle, versions, and shared scratch path."""
    command = json.loads(os.environ.get("KCT_MASK_NATIVE_PYTHON", "null"))
    if (
        not isinstance(command, list)
        or not command
        or any(not isinstance(arg, str) or not arg for arg in command)
    ):
        raise ValueError("KCT_MASK_NATIVE_PYTHON must be a nonempty JSON argv")
    cli = find_kicad_cli()
    if cli is None:
        raise ValueError("KiCad CLI is unavailable")
    import gerbonara  # noqa: F401 -- prove importability, not just distribution metadata

    oracle = importlib.metadata.version("gerbonara")
    if Version(oracle) < Version("1.6.3"):
        raise ValueError(f"Gerbonara >=1.6.3 required; found {oracle}")
    marker = directory / "native-path-probe.txt"
    native = subprocess.check_output(
        [
            *command,
            "-c",
            "import pcbnew,sys; from pathlib import Path; "
            "Path(sys.argv[1]).write_text('shared'); "
            "print(pcbnew.GetBuildVersion())",
            str(marker),
        ],
        text=True,
        stderr=subprocess.STDOUT,
        timeout=30,
    ).strip()
    cli_version = subprocess.check_output(
        [str(cli), "--version"], text=True, stderr=subprocess.STDOUT, timeout=30
    ).strip()
    info = {
        "pytest_python": sys.executable,
        "native_python_argv": command,
        "pcbnew_version": native,
        "kicad_cli": str(cli),
        "kicad_cli_version": cli_version,
        "gerbonara_version": oracle,
    }
    print(json.dumps(info, indent=2), flush=True)
    # Match the checker profile in validate/mask_copper_geometry.py. A newer
    # image must be qualified there before this dedicated gate can pass.
    if native.split()[:1] != ["10.0.5"] or cli_version.split()[:1] != native.split()[:1]:
        raise ValueError("Native mask checks require a matching KiCad 10.0.5 CLI/pcbnew pair")
    if marker.read_text() != "shared":
        raise ValueError("Native Python does not share the pytest scratch path")
    return info


def validate_results(report: Path) -> int:
    """Require executed, successful cases and both named material witnesses."""
    root = ET.parse(report).getroot()
    for suite in root.iter("testsuite"):
        if any(int(suite.get(key, "0")) for key in ("failures", "errors", "skipped")):
            raise ValueError("Native suite reports failures, errors, or skips")
    cases = list(root.iter("testcase"))
    if not cases:
        raise ValueError("Native suite produced no test cases")
    for case in cases:
        if any(case.find(tag) is not None for tag in ("failure", "error", "skipped")):
            raise ValueError(f"Native case did not pass: {case.get('name')}")
    names = {case.get("name", "").split("[")[0] for case in cases}
    missing = REQUIRED_WITNESSES - names
    if missing:
        raise ValueError(f"Required material witnesses were not executed: {sorted(missing)}")
    return len(cases)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args(argv)
    args.artifacts.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="run-", dir=args.artifacts.resolve()))
    print(f"Native mask artifacts: {directory}", flush=True)
    report = directory / "junit.xml"
    try:
        info = probe(directory)
        (directory / "prerequisites.json").write_text(json.dumps(info, indent=2))
        env = dict(os.environ)
        env.pop("PYTEST_ADDOPTS", None)
        command = [
            sys.executable,
            "-m",
            "pytest",
            SUITE,
            "-o",
            "addopts=",
            "--no-cov",
            "--benchmark-disable",
            "--timeout=180",
            "--junitxml",
            str(report),
            "--basetemp",
            str(directory / "pytest"),
        ]
        with (directory / "pytest.log").open("w") as output:
            result = subprocess.run(
                command,
                cwd=ROOT,
                env=env,
                stdout=output,
                stderr=subprocess.STDOUT,
                timeout=900,
                check=False,
            )
        print((directory / "pytest.log").read_text(), flush=True)
        if result.returncode:
            raise ValueError(f"Native pytest exited {result.returncode}")
        count = validate_results(report)
        print(f"Native mask gate passed: {count} cases, no skips", flush=True)
        return 0
    except (OSError, ValueError, ImportError, subprocess.SubprocessError, ET.ParseError) as exc:
        message = f"Native mask gate failed: {exc}"
        (directory / "gate-error.txt").write_text(message + "\n")
        print(message, file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
