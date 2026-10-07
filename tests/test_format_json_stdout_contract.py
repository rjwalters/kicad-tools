"""Contract: ``--format json`` puts *only* JSON on stdout (issue #5938).

Agents and MCP callers (Claude, Codex CLI, opencode, ...) parse ``kct ...
--format json`` stdout strictly with ``json.loads``.  A single human progress
line ahead of the document (``Running DRC on: board.kicad_pcb``) breaks every
one of them, and #5938 found three commands doing exactly that.

Rather than pinning commands one at a time, this module *discovers* every
leaf command in the real ``kct`` argument parser that offers ``--format``
with a ``json`` choice and requires each to be classified:

* ``CONTRACT_ARGV`` -- the leaf is run in a subprocess on a scratch copy of a
  small fixture project, and ``json.loads(stdout)`` must succeed.  The exit
  code is *not* asserted.  The one tolerated non-JSON stdout is an *empty*
  stdout on a non-zero exit (error reported on stderr only): a strict parser
  sees a failed command, not a corrupted document.  Prose on stdout is never
  tolerated, whatever the exit code.
* ``EXEMPT`` -- the leaf cannot be run hermetically (network, multi-minute
  runtime, long-running server/stdin protocol); the reason is recorded.

``test_every_json_command_is_classified`` fails when a new ``--format json``
command lands without being added to one of the two tables, so the contract
cannot silently erode command by command.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from kicad_tools.cli.parser import create_parser

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures"
PROJECT_FIXTURE = FIXTURES / "projects"

# Per-leaf argv *after* the leaf path and *before* ``--format json``.
# Placeholders are expanded by ``_expand``:
#   {pcb} {sch} {pro}  scratch copies of tests/fixtures/projects/test_project.*
#   {dir}              the scratch directory (also the subprocess cwd)
#   {fixtures}         tests/fixtures (read-only inputs)
CONTRACT_ARGV: dict[str, list[str]] = {
    # -- schematic / netlist readers ---------------------------------------
    "symbols": ["{sch}"],
    "nets": ["{sch}"],
    "netlist analyze": ["{sch}"],
    "netlist list": ["{sch}"],
    "netlist show": ["{sch}", "--net", "/GND"],
    "netlist check": ["{sch}"],
    "netlist compare": ["{sch}", "{sch}"],
    "netlist export": ["{sch}"],
    "bom": ["{sch}"],
    # -- ERC / DRC (the #5938 repros) ---------------------------------------
    "erc parse": ["{sch}"],
    "erc explain": ["{fixtures}/sample_erc.json"],
    "drc": ["{pcb}"],
    "detect-mistakes": ["{pcb}"],
    "check": ["{pcb}"],
    "creepage": ["{pcb}", "--min", "1.0"],
    "creepage-export-rules": ["{pro}"],
    # -- sch family ---------------------------------------------------------
    "sch summary": ["{sch}"],
    "sch hierarchy": ["{sch}"],
    "sch labels": ["{sch}"],
    "sch validate": ["{sch}"],
    "sch preflight": ["{sch}"],
    "sch wires": ["{sch}"],
    "sch info": ["{sch}", "R1"],
    "sch pins": ["{sch}", "R1"],
    "sch pin-map": ["{sch}"],
    "sch connections": ["{sch}"],
    "sch unconnected": ["{sch}"],
    "sch replace": ["{sch}", "R1", "Device:R_Small", "--dry-run"],
    "sch set-footprint": ["{sch}", "--ref", "R1", "--footprint", "Resistor_SMD:R_0402"],
    "sch assign-footprints": ["{sch}"],
    "sch suggest-footprint": ["{sch}", "--ref", "R1", "--no-project-lib"],
    "sch set-value": ["{sch}", "--ref", "R1", "--value", "10k"],
    "sch set-reference": ["{sch}", "--ref", "R1", "--new-ref", "R9"],
    "sch set-symbol-property": [
        "{sch}",
        "--ref",
        "R1",
        "--property",
        "dnp",
        "--value",
        "yes",
    ],
    "sch sync-hierarchy": ["{sch}"],
    "sch rename-signal": ["{sch}", "--from", "VIN", "--to", "VBUS", "--dry-run"],
    "sch set-label-direction": ["{sch}", "--name", "VIN", "--shape", "input"],
    "sch add-no-connect": ["{sch}", "--auto", "--dry-run"],
    "sch add-component": ["{sch}", "--lib-id", "Device:R", "--at", "100", "100", "--dry-run"],
    "sch add-bypass-cap": ["{sch}", "--ref", "R1", "--pin", "1", "--dry-run"],
    "sch add-pull-resistor": [
        "{sch}",
        "--ref",
        "R1",
        "--pin",
        "1",
        "--direction",
        "up",
        "--value",
        "10k",
        "--dry-run",
    ],
    "sch add-wire": ["{sch}", "--from", "100", "100", "--to", "120", "100", "--dry-run"],
    "sch add-junction": ["{sch}", "--at", "100", "100", "--dry-run"],
    "sch add-label": [
        "{sch}",
        "--type",
        "global",
        "--name",
        "NET9",
        "--at",
        "100",
        "100",
        "--dry-run",
    ],
    "sch cleanup-wires": ["{sch}", "--dry-run"],
    "sch remove-wire": ["{sch}", "--near", "100", "100", "--dry-run"],
    "sch insert-inline": ["{sch}", "--lib-id", "Device:R", "--near", "100", "40", "--dry-run"],
    "sch disconnect": ["{sch}", "--ref", "R1", "--pin", "1", "--dry-run"],
    "sch fix-wire-stubs": ["{sch}", "--dry-run"],
    "sch reconnect-pin": ["{sch}", "--ref", "R1", "--pin", "1", "--to-net", "GND", "--dry-run"],
    "sch move-component": ["{sch}", "--ref", "R1", "--to", "110", "110", "--dry-run"],
    "sch tidy": ["{sch}", "--dry-run"],
    "sch remove-component": ["{sch}", "--ref", "R1", "--dry-run"],
    "sch re-annotate": ["{sch}", "--dry-run"],
    "sch repair-instances": ["{sch}", "--dry-run"],
    "sch fix-annotation": ["{sch}", "--dry-run"],
    # -- pcb family ---------------------------------------------------------
    # Installs into a scratch dir: exercises the real write path, not just --list.
    "skills install": ["--target", "{dir}/_skills"],
    "agent-guide": [],
    "pcb summary": ["{pcb}"],
    "pcb footprints": ["{pcb}"],
    "pcb nets": ["{pcb}"],
    "pcb padmap": ["{pcb}"],
    "pcb traces": ["{pcb}"],
    "pcb stackup": ["{pcb}"],
    "pcb strip": ["{pcb}", "--dry-run"],
    "pcb reinforce": ["{pcb}", "--net", "VIN", "--dry-run"],
    "pcb current-paths-audit": ["{pcb}", "--current-paths", "{dir}/missing_current_paths.yaml"],
    "pcb dedupe": ["{pcb}", "--dry-run"],
    "pcb reannotate": ["{pcb}", "--map", "{dir}/missing_map.json", "--dry-run"],
    "pcb sync-netlist": ["{pcb}", "--schematic", "{sch}", "--dry-run"],
    "pcb annotate-pintypes": ["{pcb}", "--schematic", "{sch}", "--dry-run"],
    "pcb zones": ["{pcb}"],
    "pcb add-3d-models": ["{pcb}", "--dry-run"],
    "pcb remove-footprint": ["{pcb}", "--ref", "R1", "--dry-run"],
    "pcb move-footprint": ["{pcb}", "--ref", "R1", "--to", "1", "1", "--dry-run"],
    "pcb page-fit": ["{pcb}", "--dry-run"],
    "pcb center-on-sheet": ["{pcb}", "--dry-run"],
    "pcb lock-footprints": ["{pcb}", "--dry-run"],
    "pcb unlock-footprints": ["{pcb}", "--dry-run"],
    "pcb add-zone": ["{pcb}", "--net", "GND", "--layer", "B.Cu", "--dry-run"],
    "pcb snap-rotation": ["{pcb}", "--dry-run"],
    "pcb edit-outline": ["{pcb}", "--list"],
    "pcb net-audit": ["{pcb}"],
    "pcb export-dsn": ["{pcb}"],
    "pcb import-ses": ["{pcb}", "{dir}/missing.ses"],
    # -- lib ----------------------------------------------------------------
    "lib list": [],
    "lib symbols": ["{fixtures}/multiunit_test.kicad_sym"],
    "lib footprints": ["{fixtures}/Test_Library.pretty"],
    "lib footprint": ["{fixtures}/Test_Library.pretty/SOT-23-5.kicad_mod"],
    "lib symbol-info": ["{fixtures}/multiunit_test.kicad_sym", "missing_symbol"],
    "lib footprint-info": ["{fixtures}/Test_Library.pretty", "SOT-23-5"],
    "lib create-symbol-lib": ["{dir}/new.kicad_sym"],
    "lib create-footprint-lib": ["{dir}/new.pretty"],
    "lib generate-footprint": ["{dir}/gen.pretty", "soic"],
    "lib export": ["{fixtures}/Test_Library.pretty/SOT-23-5.kicad_mod"],
    "lib purge": ["{dir}"],
    # -- mfr ----------------------------------------------------------------
    "mfr list": [],
    "mfr info": ["jlcpcb"],
    "mfr rules": ["jlcpcb"],
    "mfr compare": [],
    "mfr apply-rules": ["{pcb}", "jlcpcb"],
    "mfr validate": ["{pcb}", "jlcpcb"],
    "mfr export-dru": ["jlcpcb"],
    "mfr import-dru": ["{fixtures}/issue-5362-witness/usb_joystick_routed.kicad_dru"],
    # -- zones / copper -----------------------------------------------------
    "zones add": ["{pcb}", "--net", "GND", "--layer", "B.Cu", "--dry-run"],
    "zones list": ["{pcb}"],
    "zones batch": ["{pcb}", "--power-nets", "GND:B.Cu", "--dry-run"],
    "zones hv-keepout": ["{pcb}", "--clearance", "2.0", "--dry-run"],
    "zones fill": ["{pcb}"],
    "stitch": ["{pcb}", "--dry-run"],
    # -- routing / repair drivers ------------------------------------------
    "route": ["{pcb}", "--no-current-paths"],
    "route-auto": ["{pcb}"],
    "reason": ["{pcb}"],
    "optimize-traces": ["{pcb}", "--dry-run"],
    "validate-footprints": ["{pcb}"],
    "fix-footprints": ["{pcb}", "--dry-run"],
    "fix-vias": ["{pcb}", "--dry-run"],
    "fix-silkscreen": ["{pcb}", "--dry-run"],
    "place-silk-refs": ["{pcb}", "--dry-run"],
    "repair-clearance": ["{pcb}", "--dry-run"],
    "fix-drc": ["{pcb}", "--dry-run"],
    "fix-erc": ["{sch}", "--dry-run"],
    # -- parts / datasheet (offline surfaces only) -------------------------
    "parts cache": ["stats"],
    "datasheet list": [],
    "datasheet cache": ["stats"],
    "datasheet convert": ["{dir}/missing.pdf"],
    "datasheet extract-images": ["{dir}/missing.pdf", "-o", "{dir}/images"],
    "datasheet extract-tables": ["{dir}/missing.pdf"],
    "datasheet info": ["{dir}/missing.pdf"],
    # -- decisions / placement ---------------------------------------------
    "decisions show": ["{pcb}"],
    "decisions list": ["{pcb}"],
    "decisions explain-placement": ["{pcb}", "R1"],
    "decisions explain-route": ["{pcb}", "GND"],
    "placement check": ["{pcb}"],
    "placement fix": ["{pcb}", "--dry-run"],
    "placement nudge": ["{pcb}", "--dry-run"],
    "placement optimize": ["{pcb}", "--dry-run"],
    "placement snap": ["{pcb}", "--dry-run"],
    "placement align": ["{pcb}", "--components", "R1,C1", "--dry-run"],
    "placement distribute": ["{pcb}", "--components", "R1,C1,D1", "--dry-run"],
    "placement suggest": ["{pcb}"],
    "optimize-placement": ["{pcb}", "--dry-run"],
    # -- analysis -----------------------------------------------------------
    "config": ["--show"],
    "validate": ["--sync", "{pro}"],
    "analyze congestion": ["{pcb}"],
    "analyze trace-lengths": ["{pcb}"],
    "analyze signal-integrity": ["{pcb}"],
    "analyze thermal": ["{pcb}"],
    "analyze complexity": ["{pcb}"],
    "analyze current-sense": ["{pcb}"],
    "analyze electrical-rating": ["{sch}"],
    "analyze component-stress": [
        "{fixtures}/component_stress/mosfets.kicad_sch",
        "--states",
        "{fixtures}/component_stress/states.yaml",
    ],
    "constraints check": ["{pcb}"],
    "estimate cost": ["{pcb}"],
    "audit": ["{pro}", "--skip-erc"],
    "net-status": ["{pcb}"],
    "readiness": ["{pcb}"],
    "board-metrics": ["{dir}", "--dry-run"],
    "clean": ["{pro}"],
    "impedance stackup": ["--preset", "jlcpcb-4"],
    "impedance width": ["--preset", "jlcpcb-4", "--target", "50", "--layer", "F.Cu"],
    "impedance calculate": ["--preset", "jlcpcb-4", "--width", "0.2", "--layer", "F.Cu"],
    "impedance diffpair": [
        "--preset",
        "jlcpcb-4",
        "--width",
        "0.2",
        "--gap",
        "0.2",
        "--layer",
        "F.Cu",
    ],
    "impedance crosstalk": ["--preset", "jlcpcb-4", "--layer", "F.Cu"],
    "explain": ["--list"],
    "sync": ["{pro}", "--analyze"],
    # -- registry / environment --------------------------------------------
    "fleet status": ["--boards-dir", "{dir}"],
    "fleet ship-ready": ["--boards-dir", "{dir}"],
    "ecosystem list": [],
    "ecosystem show": ["kicadroutingtools"],
    "ecosystem where-we-sit": [],
    "mcp setup": ["--dry-run"],
    "ipc status": [],
    "ipc connect": [],
    "ipc push-routes": ["{pcb}"],
    "build-native": ["--check"],
    "doctor": ["--root", "{dir}"],
    "init": ["{dir}/newproj", "--mfr", "jlcpcb", "--dry-run"],
    "spec init": ["demo", "-o", "{dir}/demo.kct"],
    "spec validate": ["{dir}/missing.kct"],
    "spec status": ["{dir}/missing.kct"],
    "spec decide": [
        "{dir}/missing.kct",
        "--topic",
        "t",
        "--choice",
        "c",
        "--rationale",
        "r",
    ],
    "spec check": ["{dir}/missing.kct", "item"],
    "benchmark list": [],
    "benchmark compare": ["--baseline", "{dir}/missing_baseline.json"],
    "benchmark report": ["{dir}/missing_results.json"],
    # -- artifact producers (error / dry-run paths are hermetic) -----------
    "render": ["{pcb}", "-o", "{dir}/render"],
    "panel": ["{pcb}", "-o", "{dir}/panel.kicad_pcb"],
    "pipeline": ["{pcb}", "--dry-run"],
    "create-pcb": ["{sch}", "--dry-run"],
    "build": ["{dir}/missing.kct", "--dry-run"],
    "screenshot": ["{pcb}", "-o", "{dir}/shot.png"],
    "report generate": ["{pcb}", "-o", "{dir}/reports", "--skip-erc", "--no-figures"],
    "export": ["{pcb}", "-o", "{dir}/export", "--dry-run"],
    "optim fom-debug": ["{pcb}"],
}

# Flag variants of an already-registered leaf that change what the JSON
# document is (not just its contents), e.g. ``kct check --diff OLD NEW`` emits
# a diff document instead of a check report and runs the leaf twice with its
# own stdout diverted (issue #5946).  Full argv after ``kct``, before
# ``--format json``; same placeholders as CONTRACT_ARGV.  A variant whose argv
# already names ``--format`` (another JSON-based format such as ``sarif``,
# issue #6006) is run as given.
FLAG_VARIANT_ARGV: dict[str, list[str]] = {
    "check --diff": ["check", "--diff", "{pcb}", "{pcb}"],
    "check --format sarif": ["check", "{pcb}", "--format", "sarif"],
    "check --diff --format sarif": ["check", "--diff", "{pcb}", "{pcb}", "--format", "sarif"],
    "detect-mistakes --format sarif": [
        "detect-mistakes",
        "{dir}/multilayer_zones.kicad_pcb",
        "--format",
        "sarif",
    ],
    "detect-mistakes --waive": [
        "detect-mistakes",
        "{dir}/multilayer_zones.kicad_pcb",
        "--waive",
        "mistake.bypass_cap_distance|C1,U1||",
        "--waive-reason",
        "contract test",
        "--waive-reviewer",
        "pytest",
    ],
    "check --waive": [
        "check",
        "{pcb}",
        "--waive",
        "connectivity|D1.2|GND|",
        "--waive-reason",
        "contract test",
        "--waive-reviewer",
        "pytest",
    ],
}

# Leaves that cannot be run hermetically inside a unit test.  Keep each
# reason specific: an entry here is a hole in the contract, not a pass.
EXEMPT: dict[str, str] = {
    "parts lookup": "network: queries the LCSC/JLCPCB API",
    "parts search": "network: queries the LCSC/JLCPCB API",
    "parts availability": "network: queries the LCSC/JLCPCB API per BOM line",
    "parts suggest": "network: queries the LCSC/JLCPCB API per BOM line",
    "suggest alternatives": "network: queries the LCSC/JLCPCB API",
    "parts sync-catalog": "network: downloads the JLCPCB parts catalog",
    "datasheet search": "network: queries datasheet providers",
    "datasheet download": "network: downloads a datasheet PDF",
    "bench external": "network: fetches external benchmark boards (multi-minute)",
    "benchmark run": "runtime: routes the benchmark corpus (multi-minute)",
    "calibrate": "runtime: CPU/GPU micro-benchmarks (machine-dependent duration)",
    "placement refine": "interactive: --format json is a stdin/stdout JSON Lines session",
}

# Leaves whose fixture run shells out to kicad-cli; skipped when it is absent
# (the CI ``test`` job ships kicad-cli 10).
NEEDS_KICAD_CLI: frozenset[str] = frozenset(
    {
        "erc parse",
        "drc",
        "fix-drc",
        "repair-clearance",
        "fix-erc",
        "render",
        "screenshot",
        "zones fill",
    }
)

_SUBPROCESS_TIMEOUT_S = 50


def _iter_json_leaves(parser: argparse.ArgumentParser, path: tuple[str, ...] = ()):
    """Yield ``"a b"`` leaf paths whose ``--format`` offers ``json``."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                yield from _iter_json_leaves(sub, (*path, name))
    for action in parser._actions:
        if "--format" in action.option_strings and action.choices and "json" in action.choices:
            yield " ".join(path)
            return


def _discovered_json_leaves() -> set[str]:
    return set(_iter_json_leaves(create_parser()))


def _kicad_cli_available() -> bool:
    from kicad_tools.cli.runner import find_kicad_cli

    return find_kicad_cli() is not None


def _expand(argv: list[str], scratch: Path) -> list[str]:
    values = {
        "pcb": str(scratch / "test_project.kicad_pcb"),
        "sch": str(scratch / "test_project.kicad_sch"),
        "pro": str(scratch / "test_project.kicad_pro"),
        "dir": str(scratch),
        "fixtures": str(FIXTURES),
    }
    return [arg.format(**values) for arg in argv]


def _hermetic_env(scratch: Path) -> dict[str, str]:
    env = dict(os.environ)
    home = scratch / "_home"
    home.mkdir(exist_ok=True)
    env.update(
        {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
            "NO_COLOR": "1",
            "PYTHONIOENCODING": "utf-8",
        }
    )
    return env


def test_registry_tables_are_disjoint():
    assert not set(CONTRACT_ARGV) & set(EXEMPT)


def test_every_json_command_is_classified():
    """A new ``--format json`` command must join CONTRACT_ARGV or EXEMPT."""
    discovered = _discovered_json_leaves()
    classified = set(CONTRACT_ARGV) | set(EXEMPT)
    unclassified = sorted(discovered - classified)
    stale = sorted(classified - discovered)
    assert not unclassified, (
        "These commands offer --format json but are not covered by the stdout "
        "contract; add them to CONTRACT_ARGV (preferred) or EXEMPT with a "
        f"reason: {unclassified}"
    )
    assert not stale, f"Registry names commands that no longer offer --format json: {stale}"


def test_discovery_finds_the_issue_5938_repros():
    discovered = _discovered_json_leaves()
    assert {"drc", "erc parse", "detect-mistakes"} <= discovered
    # Guard the walker itself: a silent regression to zero leaves would make
    # the classification test vacuous.
    assert len(discovered) > 150


def test_flag_variants_extend_registered_leaves():
    for variant, argv in FLAG_VARIANT_ARGV.items():
        leaf = variant.split(" --", 1)[0]
        assert leaf in CONTRACT_ARGV, variant
        assert argv[: len(leaf.split())] == leaf.split(), variant


@pytest.mark.parametrize("variant", sorted(FLAG_VARIANT_ARGV))
def test_format_json_flag_variant_stdout_is_a_single_json_document(variant, tmp_path):
    scratch = tmp_path / "proj"
    shutil.copytree(PROJECT_FIXTURE, scratch)
    argv = _expand(FLAG_VARIANT_ARGV[variant], scratch)
    if "--format" not in argv:
        argv = [*argv, "--format", "json"]
    # ``--diff`` checks the board twice.
    _assert_single_json_document(variant, argv, scratch, timeout_s=2 * _SUBPROCESS_TIMEOUT_S)


@pytest.mark.parametrize("leaf", sorted(CONTRACT_ARGV))
def test_format_json_stdout_is_a_single_json_document(leaf, tmp_path):
    if leaf in NEEDS_KICAD_CLI and not _kicad_cli_available():
        pytest.skip("kicad-cli not installed")

    scratch = tmp_path / "proj"
    shutil.copytree(PROJECT_FIXTURE, scratch)
    argv = [*leaf.split(), *_expand(CONTRACT_ARGV[leaf], scratch), "--format", "json"]
    _assert_single_json_document(leaf, argv, scratch)


def _assert_single_json_document(
    leaf: str, argv: list[str], scratch: Path, timeout_s: int = _SUBPROCESS_TIMEOUT_S
) -> None:
    # Own process group: a hung grandchild (e.g. kicad-cli, #5877) would keep the
    # pipes open after killing only the direct child, blocking communicate()
    # forever. Kill the whole group on timeout instead.
    child = subprocess.Popen(
        [sys.executable, "-m", "kicad_tools.cli", *argv],
        cwd=scratch,
        env=_hermetic_env(scratch),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = child.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.communicate()
        pytest.fail(f"kct {' '.join(argv)} did not finish within {timeout_s}s")
    proc = subprocess.CompletedProcess(child.args, child.returncode, stdout, stderr)

    if proc.returncode != 0 and not proc.stdout.strip():
        return  # failure reported on stderr only; stdout is not corrupted

    try:
        json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(
            f"kct {leaf} --format json wrote non-JSON to stdout ({exc}); "
            f"exit={proc.returncode}\n"
            f"--- stdout (head) ---\n{proc.stdout[:1500]}\n"
            f"--- stderr (tail) ---\n{proc.stderr[-1500:]}"
        )
