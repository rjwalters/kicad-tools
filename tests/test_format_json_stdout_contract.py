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
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from kicad_tools.cli.parser import create_parser, resolve_passthrough

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
    # Items file left missing on purpose: the JSON error document on stdout is
    # part of the contract (same shape as the pcb reannotate missing-map entry).
    "probe": ["{pcb}", "{dir}/probe_items.json"],
    "apply": ["{pcb}", "{dir}/probe_items.json", "--checked"],
    "creepage": ["{pcb}", "--min", "1.0"],
    "creepage-export-rules": ["{pro}"],
    "repair-creepage": ["{pcb}", "--min", "1.0", "--dry-run"],
    # Issue #9582: deliberately-absent inputs walk the JSON error path
    # ({"error": ..., "success": false}); no fixture files needed.
    "voltage-map-merge": ["{dir}/missing_a.json", "{dir}/missing_b.json", "-o", "{dir}/env.json"],
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
    # Issue #5994: without a package hint this parses up to 5000 global
    # footprints (~20-40 s); a library-name hint narrows it to ~1 s.
    "sch suggest-footprint": [
        "{sch}",
        "--ref",
        "R1",
        "--no-project-lib",
        "--package",
        "Resistor_SMD",
    ],
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
    "pcb prune": ["{pcb}", "--dry-run", "--no-drc"],
    "pcb reannotate": ["{pcb}", "--map", "{dir}/missing_map.json", "--dry-run"],
    "pcb sync-netlist": ["{pcb}", "--schematic", "{sch}", "--dry-run"],
    "pcb annotate-pintypes": ["{pcb}", "--schematic", "{sch}", "--dry-run"],
    "pcb zones": ["{pcb}"],
    "pcb add-3d-models": ["{pcb}", "--dry-run"],
    "pcb remove-footprint": ["{pcb}", "--ref", "R1", "--dry-run"],
    "pcb add-fiducials": ["{pcb}", "--dry-run"],
    "pcb move-footprint": ["{pcb}", "--ref", "R1", "--to", "1", "1", "--dry-run"],
    "pcb move-via": ["{pcb}", "--at", "1", "1", "--to", "2", "2", "--dry-run"],
    "pcb page-fit": ["{pcb}", "--dry-run"],
    "pcb center-on-sheet": ["{pcb}", "--dry-run"],
    "pcb lock-footprints": ["{pcb}", "--dry-run"],
    "pcb unlock-footprints": ["{pcb}", "--dry-run"],
    "pcb add-zone": ["{pcb}", "--net", "GND", "--layer", "B.Cu", "--dry-run"],
    "pcb add-board-info": ["{pcb}", "--text", "REV A", "--auto", "--dry-run"],
    "pcb snap-rotation": ["{pcb}", "--dry-run"],
    "pcb edit-outline": ["{pcb}", "--list"],
    "pcb net-audit": ["{pcb}"],
    "pcb model-audit": ["{pcb}"],
    "pcb diff": ["{pcb}", "{pcb}"],
    # Dry-run on the fixture board: one JSON document (a diff payload, or an
    # {"error": ...} document when the nets do not line up); nothing is written.
    "pcb transplant": ["{pcb}", "{pcb}", "--dry-run"],
    "pcb export-dsn": ["{pcb}"],
    "pcb import-ses": ["{pcb}", "{dir}/missing.ses"],
    # -- lib ----------------------------------------------------------------
    "lib list": [],
    # -- pinned 3D model subset (#7683) -------------------------------------
    # Hermetic: HOME is scratch and KCT_KICAD_3DMODEL_CACHE is popped, so it
    # reads an empty cache and prints one JSON document with exit 1.
    "models status": [],
    # Parametric bodies (#7687): placeholder mode needs no CadQuery extra.
    "models generate": [
        "chip",
        "--pcb",
        "{pcb}",
        "--ref",
        "R1",
        "--placeholder",
        "-o",
        "{dir}/_gen",
    ],
    "models families": [],
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
    # Issue #5994: the default route run spends ~45 s in kicad-cli (oracle
    # completion rounds, auto-pour zone refills, layer-escalation retries) which
    # blows the 50 s subprocess budget under load. None of that changes what is
    # written to stdout; post-route DRC is kept because it is a real stdout
    # writer. Measured ~45 s -> ~12 s.
    "route": [
        "{pcb}",
        "--no-current-paths",
        "--oracle-rounds",
        "0",
        "--no-auto-pour",
        "--no-auto-layers",
    ],
    # Issue #6093: a valid tiny net (NET1, 3 pads), so the success path's
    # document is checked, not just the missing-``--net`` error (~1.5 s).
    "route-auto": ["{pcb}", "--net", "NET1"],
    # Issue #8310: --probe reports the path (or blocker) per link and writes nothing.
    "route-link": [
        "{fixtures}/route_auto_6001/crossing.kicad_pcb",
        "--link",
        "/A R1.1 R2.1",
        "--probe",
    ],
    # Issue #6885: the refusal path (no net-class map on disk) is one JSON
    # document too; the success document is covered by tests/test_pcb_retune.py.
    "retune-lengths": [
        "{pcb}",
        "-o",
        "{dir}/retuned.kicad_pcb",
        "--net-class-map",
        "{dir}/absent_net_class_map.json",
        "--group",
        "G",
    ],
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
    # Issue #9803: the 3D ray-traces made this the heaviest native leaf
    # (~10 s own work measured on the pinned 10.0.6 image; 18-49 s wall in
    # extended CI under the 2-slot native gate), hugging the 50 s deadline.
    # ``--no-3d`` still performs both real ``pcb export svg`` invocations and
    # reports the written plots in its JSON document; the 3D flag mapping and
    # output contract keep their own coverage in tests/test_render_cmd.py.
    "render": ["{pcb}", "-o", "{dir}/render", "--no-3d"],
    "panel": ["{pcb}", "-o", "{dir}/panel.kicad_pcb"],
    "pipeline": ["{pcb}", "--dry-run"],
    "create-pcb": ["{sch}", "--dry-run"],
    "build": ["{dir}/missing.kct", "--dry-run"],
    "screenshot": ["{pcb}", "-o", "{dir}/shot.png"],
    # Issue #6316: matplotlib only, no kicad-cli (``list`` needs neither).
    "board-view overview": ["{pcb}", "-o", "{dir}/view.png"],
    "board-view net": ["{pcb}", "NET1", "-o", "{dir}/view.png"],
    "board-view layers": ["{pcb}", "NET1", "-o", "{dir}/view.png"],
    "board-view crop": ["{pcb}", "0", "0", "30", "20", "-o", "{dir}/view.png"],
    "board-view list": ["{pcb}"],
    "report generate": ["{pcb}", "-o", "{dir}/reports", "--skip-erc", "--no-figures"],
    "export": ["{pcb}", "-o", "{dir}/export", "--dry-run"],
    "export-formats": ["{pcb}", "-o", "{dir}/formats"],
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
    # Issue #6054: the gate's two lint runs must not leak onto stdout; the
    # verdict is merged into the command's one document as ``lint_gate``.
    # Narrowed like the ``route`` leaf (#5994) and to one net: the gate wraps the
    # whole route, so the test routes less instead of skipping the gate (#7129).
    "route --lint-gate": [
        "route",
        "{pcb}",
        "--no-current-paths",
        "--oracle-rounds",
        "0",
        "--no-auto-pour",
        "--no-auto-layers",
        "--nets",
        "NET1",
        "--lint-gate",
    ],
    "route-auto --lint-gate": [
        "route-auto",
        "{pcb}",
        "--net",
        "GND",
        "-o",
        "{dir}/auto_routed.kicad_pcb",
        "--lint-gate",
    ],
    # Issue #6001: a net that fails (it would short an already-routed net), so
    # the unrouted-cause pass really runs -- its board load and summary line
    # must stay off stdout.
    "route-auto --diagnose-unrouted-budget": [
        "route-auto",
        "{fixtures}/route_auto_6001/crossing.kicad_pcb",
        "--net",
        "/B",
        "--diagnose-unrouted-budget",
        "10",
    ],
}

# Leaves that cannot be run hermetically inside a unit test.  Keep each
# reason specific: an entry here is a hole in the contract, not a pass.
EXEMPT: dict[str, str] = {
    "parts lookup": "network: queries the LCSC/JLCPCB API",
    "parts search": "network: queries the LCSC/JLCPCB API",
    "parts availability": "network: queries the LCSC/JLCPCB API per BOM line",
    "parts suggest": "network: queries the LCSC/JLCPCB API per BOM line",
    "parts alternatives": "needs the ~620 MB jlcparts catalog from `parts sync-catalog` (unit-tested in tests/test_parts_alternatives.py)",
    "parts import": "network: searches and downloads datasheets (mocked JSON run in tests/test_parts_cmd_import.py)",
    "suggest alternatives": "network: queries the LCSC/JLCPCB API",
    "parts sync-catalog": "network: downloads the JLCPCB parts catalog",
    "datasheet search": "network: queries datasheet providers",
    "datasheet download": "network: downloads a datasheet PDF",
    "bench external": "network: fetches external benchmark boards (multi-minute)",
    "benchmark run": "runtime: routes the benchmark corpus (multi-minute)",
    "calibrate": "runtime: CPU/GPU micro-benchmarks (machine-dependent duration)",
    "models sync": "network: fetches the pinned kicad-packages3D subset (stubbed run in tests/test_models_sync.py)",
    "placement refine": "interactive: --format json is a stdin/stdout JSON Lines session",
    "submit-jlcpcb": "network: stock gate queries the LCSC/JLCPCB API and needs a tapeout bundle (JSON result unit-tested in tests/test_submission_workflow.py)",
    "tapeout-sign-off": "needs a signed tapeout bundle with a fresh readiness receipt (JSON result unit-tested in tests/test_submission_workflow.py)",
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
# Issue #9803: the most wall-clock seconds a queued child may be reimbursed
# for.  The extended-CI failures this addresses needed single-digit seconds
# (the passing distribution hugs the 50 s deadline); 30 s bounds worst-case
# hang detection at 50 s + 30 s while leaving 3x margin over that tail.
_SLOT_CREDIT_CAP_S = 30.0
_MPLCONFIGDIR = Path(tempfile.gettempdir()) / "kct-contract-mplconfig"


def _iter_parser_nodes(parser: argparse.ArgumentParser, path: tuple[str, ...] = ()):
    """Yield ``(path, parser)`` for every node, intermediate ones included."""
    yield path, parser
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                yield from _iter_parser_nodes(sub, (*path, name))


def _offers_json(parser: argparse.ArgumentParser) -> bool:
    return any(
        "--format" in action.option_strings and action.choices and "json" in action.choices
        for action in parser._actions
    )


def _iter_json_leaves(parser: argparse.ArgumentParser, path: tuple[str, ...] = ()):
    """Yield ``"a b"`` leaf paths whose ``--format`` offers ``json``."""
    for node_path, node in _iter_parser_nodes(parser, path):
        if _offers_json(node):
            yield " ".join(node_path)


def _discovered_json_leaves() -> set[str]:
    return set(_iter_json_leaves(resolve_passthrough(create_parser())))


# A stale-API crash (#7114/#7115 class) surfaces on stderr -- or in a JSON
# ``error`` field -- as one of these; ordinary user errors do not match.
_CRASH_RE = re.compile(
    r"Traceback"
    r"|(Type|Attribute|Import|ModuleNotFound|Key|Name|UnboundLocal)Error"
    r"|has no attribute|unexpected keyword argument|cannot import name"
)


def _help_nodes() -> list[tuple[str, ...]]:
    """Every parser node path, plus each ``footprint generate`` type."""
    from kicad_tools.cli.footprint_generate import GENERATORS

    paths = [p for p, _ in _iter_parser_nodes(resolve_passthrough(create_parser()))]
    return [*paths, *(("footprint", "generate", name) for name in sorted(GENERATORS))]


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
    # `mcp setup` reads $CLAUDE_CONFIG_DIR/.claude.json when it is set (#6418).
    env.pop("CLAUDE_CONFIG_DIR", None)
    # A developer's synced 3D model cache must not leak in (#7683).
    env.pop("KCT_KICAD_3DMODEL_CACHE", None)
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
            # matplotlib keeps its font cache under HOME and rebuilds it when
            # HOME is fresh: ~18 s per ``board-view`` image leaf on macOS
            # (issue #6316).  One cache outside the user's HOME, shared by
            # every leaf and xdist worker, keeps the run hermetic and fast.
            "MPLCONFIGDIR": str(_MPLCONFIGDIR),
        }
    )
    # Issue #9803: when the native-concurrency gate is active (extended CI
    # sets ``KCT_NATIVE_MAX_CONCURRENCY=2``), the child's ``kicad-cli``
    # launches queue for a shared slot and that queue time is charged to this
    # test's subprocess deadline -- the gate deliberately never credits
    # ``subprocess`` timeouts (#5572 credits only pytest-timeout's).  A private
    # permit log per child makes the waits measurable so the deadline can be
    # reimbursed (see ``_communicate_with_slot_credit``).  An explicit
    # ``KCT_NATIVE_SLOT_LOG``/observer configuration is left alone so the CI
    # native observer keeps collecting those records.
    if not (env.get("KCT_NATIVE_SLOT_LOG") or env.get("KCT_NATIVE_OBSERVER_OUTPUT")):
        env["KCT_NATIVE_SLOT_LOG"] = str(scratch / "_native-slot-log")
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


# CI runs pytest with ``--timeout=60``, which would preempt the 2 * 50 s inner
# guard below (a genuine hang then surfaces as a pytest-timeout traceback, not
# the "did not finish" message, and a slow variant fails under ~3x CI
# slowdown; ``route --lint-gate`` is narrowed to ~3.5 s for that reason). The per-test marker overrides the CLI value and leaves
# headroom so the inner guard, which kills the process group, stays authoritative.
@pytest.mark.timeout(2 * _SUBPROCESS_TIMEOUT_S + 30)
@pytest.mark.parametrize("variant", sorted(FLAG_VARIANT_ARGV))
def test_format_json_flag_variant_stdout_is_a_single_json_document(variant, tmp_path):
    scratch = tmp_path / "proj"
    shutil.copytree(PROJECT_FIXTURE, scratch)
    argv = _expand(FLAG_VARIANT_ARGV[variant], scratch)
    if "--format" not in argv:
        argv = [*argv, "--format", "json"]
    # ``--diff`` checks the board twice.
    _assert_single_json_document(variant, argv, scratch, timeout_s=2 * _SUBPROCESS_TIMEOUT_S)


# The marker rationale is the variant test's, plus #9803: the inner guard
# reimburses a queued child's measured native-slot wait into its deadline
# (capped at ``_SLOT_CREDIT_CAP_S``), so the per-test cap must leave that
# guard -- which kills the process group and names the leaf in its message --
# authoritative through ``_SUBPROCESS_TIMEOUT_S + _SLOT_CREDIT_CAP_S``.
@pytest.mark.timeout(2 * _SUBPROCESS_TIMEOUT_S + 30)
@pytest.mark.parametrize("leaf", sorted(CONTRACT_ARGV))
def test_format_json_stdout_is_a_single_json_document(leaf, tmp_path):
    if leaf in NEEDS_KICAD_CLI and not _kicad_cli_available():
        pytest.skip("kicad-cli not installed")

    scratch = tmp_path / "proj"
    shutil.copytree(PROJECT_FIXTURE, scratch)
    argv = [*leaf.split(), *_expand(CONTRACT_ARGV[leaf], scratch), "--format", "json"]
    doc = _assert_single_json_document(leaf, argv, scratch)
    check = _CONTENT_CHECKS.get(leaf)
    if check is not None:
        assert isinstance(doc, dict), (
            f"kct {leaf} --format json: expected a JSON object, got {doc!r}"
        )
        check(doc)


def _check_render_doc(doc: dict) -> None:
    """Issue #9803: the exercised path must report the plots it generated."""
    boards = doc.get("boards")
    assert isinstance(boards, list) and boards, doc
    outputs = boards[0].get("outputs", {})
    assert {"pcb-front", "pcb-back"} <= set(outputs), (
        f"render did not report both SVG plots as generated output: {doc}"
    )


def _check_zones_fill_doc(doc: dict) -> None:
    """Issue #9803: the exercised path is the real fill, not a dry-run/error."""
    assert doc.get("filled") is True, doc


# Leaves whose JSON payload must prove the real output path ran (#9803
# acceptance): an early error, a dry-run, or a narrowed argv that silently
# stopped generating output would otherwise still parse as one document.
_CONTENT_CHECKS: dict[str, Callable[[dict], None]] = {
    "render": _check_render_doc,
    "zones fill": _check_zones_fill_doc,
}


def _child_slot_queue_s(child: subprocess.Popen, env: dict[str, str]) -> float:
    """Seconds the child has spent waiting for a native permit, so far.

    Reads the gate's own JSONL permit records for the child's PID: completed
    waits arrive as ``acquire.waited_s``; the launch currently queued surfaces
    through its periodic ``waiting`` heartbeats (the last one's ``waited_s``
    is the in-flight total).  A torn tail line -- the child appends while we
    read -- is ignored; the next timeout cycle re-reads.
    """
    directory = env.get("KCT_NATIVE_SLOT_LOG") or env.get("KCT_NATIVE_OBSERVER_OUTPUT")
    if not directory:
        return 0.0
    path = Path(directory) / f"slots-{child.pid}.jsonl"
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return 0.0
    total = 0.0
    in_flight = 0.0
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        event = record.get("event")
        if event == "acquire":
            total += float(record.get("waited_s") or 0.0)
            in_flight = 0.0
        elif event == "waiting":
            in_flight = float(record.get("waited_s") or 0.0)
        elif event in ("release", "unbounded"):
            in_flight = 0.0
    return total + in_flight


def _communicate_with_slot_credit(
    child: subprocess.Popen, timeout_s: float, env: dict[str, str]
) -> tuple[str, str]:
    """``communicate()`` whose deadline measures the child's own work.

    Extended CI bounds native ``kicad-cli`` concurrency
    (``KCT_NATIVE_MAX_CONCURRENCY=2``) and the gate deliberately never credits
    ``subprocess`` timeouts (#5572 credits pytest-timeout only), so a contract
    child queued behind the shared slots was charged pool queue time against
    this test's 50 s deadline -- measured as the whole gap between ~3 s of own
    work and 18-49 s walls for ``zones fill`` (#9803).  This is the #5572
    pattern on the subprocess side: the deadline is extended by exactly the
    queue time the child's own permit records show, capped so hang detection
    stays bounded, and a child that merely hangs (no queue records) still hits
    the plain ``timeout_s`` deadline.
    """
    start = time.monotonic()
    budget = timeout_s
    while True:
        try:
            return child.communicate(timeout=budget)  # type: ignore[return-value]
        except subprocess.TimeoutExpired:
            waited = _child_slot_queue_s(child, env)
            allowed = timeout_s + min(waited, _SLOT_CREDIT_CAP_S)
            budget = allowed - (time.monotonic() - start)
            if budget <= 0:
                raise


def _sleep_child(seconds: float) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({seconds})"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )


def _write_slot_records(log_dir: Path, pid: int, events: list[dict]) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"time_ns": i * 1_000_000, "pid": pid, **e}) for i, e in enumerate(events)]
    (log_dir / f"slots-{pid}.jsonl").write_text("\n".join(lines) + "\n")


def test_child_slot_queue_s_sums_acquires_and_in_flight_heartbeat(tmp_path):
    class _Fake:  # noqa: D401 - minimal stand-in with the attribute actually read
        pid = 4242

    env = {"KCT_NATIVE_SLOT_LOG": str(tmp_path)}
    _write_slot_records(
        tmp_path,
        4242,
        [
            {"event": "acquire", "waited_s": 2.0},
            {"event": "release", "slot": 0},
            {"event": "waiting", "waited_s": 1.0},
            {"event": "waiting", "waited_s": 1.4},
        ],
    )
    # Completed waits plus the still-queued launch's latest heartbeat.
    assert _child_slot_queue_s(_Fake(), env) == pytest.approx(3.4)
    # A resolved wait replaces its heartbeats instead of adding to them.
    _write_slot_records(
        tmp_path,
        4242,
        [
            {"event": "waiting", "waited_s": 1.0},
            {"event": "acquire", "waited_s": 1.6},
        ],
    )
    assert _child_slot_queue_s(_Fake(), env) == pytest.approx(1.6)


def test_child_slot_queue_s_tolerates_missing_log_and_torn_tail(tmp_path):
    class _Fake:
        pid = 4243

    env = {"KCT_NATIVE_SLOT_LOG": str(tmp_path)}
    assert _child_slot_queue_s(_Fake(), env) == 0.0  # no records yet
    assert _child_slot_queue_s(_Fake(), {}) == 0.0  # nothing configured
    log = tmp_path / "slots-4243.jsonl"
    log.write_text('{"event": "waiting", "waited_s": 2.0}\n{"event": "wait')  # torn tail
    assert _child_slot_queue_s(_Fake(), env) == pytest.approx(2.0)


def test_communicate_without_queue_records_keeps_the_plain_deadline():
    child = _sleep_child(30)
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            _communicate_with_slot_credit(child, 1.0, {})
    finally:
        os.killpg(child.pid, signal.SIGKILL)
        child.communicate()


def test_communicate_extends_deadline_by_measured_queue_wait(tmp_path):
    child = _sleep_child(2.5)
    env = {"KCT_NATIVE_SLOT_LOG": str(tmp_path)}
    _write_slot_records(tmp_path, child.pid, [{"event": "acquire", "waited_s": 4.0}])
    start = time.monotonic()
    try:
        _communicate_with_slot_credit(child, 1.0, env)
        # 2.5 s of own work finished although the plain deadline was 1.0 s.
        assert time.monotonic() - start >= 2.0
    finally:
        child.communicate()


def test_communicate_credit_is_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "_SLOT_CREDIT_CAP_S", 2.0)
    child = _sleep_child(30)
    env = {"KCT_NATIVE_SLOT_LOG": str(tmp_path)}
    _write_slot_records(tmp_path, child.pid, [{"event": "acquire", "waited_s": 900.0}])
    start = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            # allowed wall = 1.0 + min(900, cap 2.0) = 3.0 s, not 900 s.
            _communicate_with_slot_credit(child, 1.0, env)
        assert time.monotonic() - start < 10.0
    finally:
        os.killpg(child.pid, signal.SIGKILL)
        child.communicate()


def _assert_single_json_document(
    leaf: str, argv: list[str], scratch: Path, timeout_s: int = _SUBPROCESS_TIMEOUT_S
) -> object | None:
    # Own process group: a hung grandchild (e.g. kicad-cli, #5877) would keep the
    # pipes open after killing only the direct child, blocking communicate()
    # forever. Kill the whole group on timeout instead.
    env = _hermetic_env(scratch)
    child = subprocess.Popen(
        [sys.executable, "-m", "kicad_tools.cli", "--verbose", *argv],
        cwd=scratch,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = _communicate_with_slot_credit(child, timeout_s, env)
    except subprocess.TimeoutExpired:
        waited = _child_slot_queue_s(child, env)
        os.killpg(child.pid, signal.SIGKILL)
        child.communicate()
        pytest.fail(
            f"kct {' '.join(argv)} did not finish within {timeout_s}s "
            f"(own-work deadline; native slot queue wait recorded: {waited:.1f}s)"
        )
    proc = subprocess.CompletedProcess(child.args, child.returncode, stdout, stderr)

    crash = _CRASH_RE.search(proc.stderr)
    if crash:
        pytest.fail(
            f"kct {leaf} --format json crashed ({crash.group(0)!r}); exit={proc.returncode}\n"
            f"--- stderr (tail) ---\n{proc.stderr[-1500:]}"
        )

    if proc.returncode != 0 and not proc.stdout.strip():
        return None  # user error reported on stderr only; stdout is not corrupted

    try:
        doc = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(
            f"kct {leaf} --format json wrote non-JSON to stdout ({exc}); "
            f"exit={proc.returncode}\n"
            f"--- stdout (head) ---\n{proc.stdout[:1500]}\n"
            f"--- stderr (tail) ---\n{proc.stderr[-1500:]}"
        )
    error = doc.get("error") if isinstance(doc, dict) else None
    if isinstance(error, str) and _CRASH_RE.search(error):
        pytest.fail(f"kct {leaf} --format json reported a crash in its error field: {error!r}")
    return doc


@pytest.mark.parametrize("path", _help_nodes(), ids=lambda p: " ".join(p))
def test_every_subcommand_help_is_clean(path, capsys):
    """``--help`` renders for every parser node without raising (#5028)."""
    if path[:2] == ("footprint", "generate"):
        # ``footprint generate`` owns its argv (passthrough); build its parser.
        from kicad_tools.cli.footprint_generate import main as generate_main

        run = lambda: generate_main([*path[2:], "--help"])  # noqa: E731
    else:
        parser = resolve_passthrough(create_parser())
        run = lambda: parser.parse_args([*path, "--help"])  # noqa: E731
    with pytest.raises(SystemExit) as exc:
        run()
    out = capsys.readouterr()
    assert exc.value.code == 0, f"{' '.join(path)} --help exited {exc.value.code}\n{out.err}"
    assert "Traceback" not in out.out + out.err
    assert out.out.strip(), f"{' '.join(path)} --help printed nothing"
