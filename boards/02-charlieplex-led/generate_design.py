#!/usr/bin/env python3
"""
Charlieplex LED Grid - Complete Design Generation

This script demonstrates the complete PCB design workflow:
1. Create project file
2. Create schematic with MCU, resistors, and LED matrix
3. Run ERC validation
4. Generate PCB with component placement
5. Route PCB traces
6. Run DRC validation

The design is a 3x3 charlieplexed LED grid driven by 4 GPIO pins,
demonstrating how N pins can drive N*(N-1) LEDs.

Usage:
    python generate_design.py [output_dir]

If no output directory is specified, files are written to ./output/
"""

import os
import subprocess
import sys
import uuid
from pathlib import Path

from design_spec import (
    LED_CONNECTIONS,
)

from kicad_tools.core.project_file import create_minimal_project, save_project
from kicad_tools.dev import warn_if_stale
from kicad_tools.lvs import write_lvs_report
from kicad_tools.recipes.gate import evaluate_pipeline_gate
from kicad_tools.recipes.precondition import require_spec

# Warn if running source scripts with stale pipx install
warn_if_stale()


def generate_uuid() -> str:
    """Generate a KiCad-format UUID."""
    return str(uuid.uuid4())


# =============================================================================
# Schematic Generation
# =============================================================================


def create_charlieplex_schematic(output_dir: Path) -> Path:
    from hardware_design import write_schematic

    return write_schematic(output_dir / "charlieplex_3x3.kicad_sch")


def create_charlieplex_pcb(output_dir: Path) -> Path:
    from hardware_design import write_pcb

    return write_pcb(output_dir / "charlieplex_3x3.kicad_pcb")


# =============================================================================
# Project, ERC, Routing, DRC
# =============================================================================


def create_project(output_dir: Path, project_name: str) -> Path:
    """Create a KiCad project file."""
    print("\n" + "=" * 60)
    print("Creating Project File...")
    print("=" * 60)

    output_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{project_name}.kicad_pro"
    project_data = create_minimal_project(filename)

    project_path = output_dir / filename
    save_project(project_data, project_path)
    print(f"\n   Project: {project_path}")

    return project_path


def run_erc(sch_path: Path) -> bool:
    """Run ERC on the schematic."""
    from kicad_tools.cli.runner import find_kicad_cli
    from kicad_tools.cli.runner import run_erc as kicad_run_erc
    from kicad_tools.erc import ERCReport

    print("\n" + "=" * 60)
    print("Running ERC...")
    print("=" * 60)

    kicad_cli = find_kicad_cli()
    if not kicad_cli:
        print("\n   WARNING: kicad-cli not found - skipping ERC")
        return True

    result = kicad_run_erc(sch_path)

    if not result.success:
        print(f"\n   Error running ERC: {result.stderr}")
        return False

    try:
        report = ERCReport.load(result.output_path)
    except Exception as e:
        print(f"\n   Error parsing ERC report: {e}")
        return False
    finally:
        if result.output_path:
            result.output_path.unlink(missing_ok=True)

    violations = [v for v in report.violations if not v.excluded]
    error_count = sum(1 for v in violations if v.is_error)

    if error_count > 0:
        print(f"\n   Found {error_count} ERC errors:")
        for v in [v for v in violations if v.is_error][:5]:
            print(f"      - [{v.type_str}] {v.description}")
        return False
    else:
        print("\n   No ERC errors found!")
        return True


def route_pcb(input_path: Path, output_path: Path) -> bool:
    """Route all twelve physical nets, apply reviewed escapes, require native DRC.

    Uses the standalone CLI parser until the main parser exposes --no-auto-pour
    (issue #4988). The seed and iteration budget are shared with route_demo.py.
    """
    print("\n" + "=" * 60)
    print("Routing PCB...")
    print("=" * 60)

    skip_nets = []  # Revision B routes both power rails explicitly.

    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli.route_cmd",
        str(input_path),
        "--output",
        str(output_path),
        "--strategy",
        "negotiated",
        "--iterations",
        "30",
        # Issue #3799: route under an ITERATION budget instead of the
        # per-net WALL-CLOCK cutoff.  --seed only seeds Python's global
        # random; it does NOT control the per-net A* deadline checked in
        # the C++ loop.  On a loaded machine that wall-clock budget fires
        # mid-search and the net lands less copper -- same seed, different
        # copper.  --deterministic-budget (#3538) disables the per-net
        # wall-clock cutoff and pins a fixed node-expansion backstop, so
        # the seed-42 re-route is byte-identical (UUID-normalized) across
        # machines.  --timeout 240 below is retained only as a SAFETY
        # backstop (the normalizer warns if it would bind).
        "--deterministic-budget",
        "--timeout",
        "240",
        "--seed",
        "42",
        "--no-auto-pour",
        "--no-auto-layers",
        "--grid",
        "0.1",
        # Issue #3112: pass the manufacturer through so the post-route
        # ``drc_verify_and_nudge`` sweep can consult
        # ``via_in_pad_supported`` and slide any same-net via-in-pad
        # escape vias off the offending pad.  The default jlcpcb profile
        # does NOT support via-in-pad, so this is the case that exercises
        # the new sweep.
        "--manufacturer",
        "jlcpcb",
    ]

    # Issue #3799: pin PYTHONHASHSEED for the route subprocess so any
    # string-keyed dict/set iteration in the negotiated router is
    # reproducible across runner environments (CPython randomizes string
    # hashing per-process otherwise).  Combined with --seed 42 +
    # --deterministic-budget this makes the full pipeline deterministic,
    # not just the A* loop.  Mirrors board-07's convention.
    env = os.environ.copy()
    env["PYTHONHASHSEED"] = "42"

    print(f"\n1. Input: {input_path}")
    print(f"   Output: {output_path}")
    print(f"   Skipping nets: {skip_nets}")
    print(f"   Command: PYTHONHASHSEED={env['PYTHONHASHSEED']} {' '.join(cmd)}")
    print("\n2. Routing...")

    result = subprocess.run(cmd, capture_output=False, text=True, env=env)

    # ``kct route`` returns 0 on full success and a non-zero code on
    # partial / failed routing.  Either way it writes a routed PCB to
    # ``output_path``; downstream DRC + manufacturing checks decide if
    # the partial output is acceptable.
    success = result.returncode == 0

    if not output_path.exists():
        print(f"\n   ERROR: ``kct route`` did not produce {output_path}", file=sys.stderr)
        return False

    if success:
        print("\n   SUCCESS: ``kct route`` reports all signal nets routed!")
    else:
        print(
            f"\n   PARTIAL: ``kct route`` exited with code {result.returncode} "
            "(partial routing; downstream DRC will continue)"
        )

    from finalize_routing import finalize_routing

    return finalize_routing(output_path)


def run_drc(pcb_path: Path) -> bool:
    """Run DRC on the PCB.

    Issue #3779: pass ``--allow-incomplete`` (mirroring boards 00/01's
    ``run_drc``) because this DRC pass runs BEFORE
    ``export_manufacturing_bundle`` writes ``output/manufacturing/
    manifest.json`` and BEFORE ``write_lvs_report`` writes ``output/
    lvs.json``.  Without the opt-in the Manifest/LVS meta sub-checks
    (#3755) report ``NOT RUN`` (-> Overall INCOMPLETE -> exit 2) and the
    recipe would fail here on a fresh regen even though DRC itself passes.
    LVS is run separately by ``write_lvs_report`` further down the recipe.
    """
    print("\n" + "=" * 60)
    print("Running DRC (via kct check)...")
    print("=" * 60)

    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "kicad_tools.cli",
                "check",
                str(pcb_path),
                "--allow-incomplete",
            ],
            capture_output=True,
            text=True,
        )

        if result.stdout:
            for line in result.stdout.strip().split("\n"):
                print(f"   {line}")

        if result.returncode == 0:
            return True
        else:
            if result.stderr:
                print(f"\n   Error: {result.stderr}")
            return False

    except Exception as e:
        print(f"\n   Error running DRC: {e}")
        return False


def export_manufacturing_bundle(routed_path: Path, output_dir: Path) -> bool:
    """Export the manufacturing bundle (gerbers, BOM, CPL, report, manifest).

    Issue #3147: ``kct fleet status`` flags a board ``ship_ready=false``
    with the ``"artifacts stale"`` blocker whenever the routed PCB is
    newer than ``output/manufacturing/manifest.json``.  Re-running this
    recipe always rewrites the routed PCB, so the recipe must also
    regenerate the manufacturing bundle to keep the manifest current.

    ``kct export`` runs the standard JLCPCB recipe (gerbers + drill + BOM
    + CPL + report.{md,pdf} + manifest.json).  ``--skip-preflight`` skips
    the strict pre-flight DRC/ERC gate so the bundle is produced even for
    boards that ship with allowlisted tolerances (mirrors boards
    03/04/05); for clean boards it is harmless.
    """
    print("\n" + "=" * 60)
    print("Exporting manufacturing bundle...")
    print("=" * 60)

    mfg_dir = output_dir / "manufacturing"
    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "export",
        str(routed_path),
        "--output",
        str(mfg_dir),
        "--mfr",
        "jlcpcb",
        "--skip-preflight",
    ]
    print(f"\n   Command: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout:
        for line in result.stdout.strip().split("\n")[-15:]:
            print(f"   {line}")
    if result.returncode != 0:
        if result.stderr:
            print(f"\n   Error: {result.stderr}")
        return False
    manifest = mfg_dir / "manifest.json"
    if manifest.exists():
        print(f"\n   Manifest: {manifest}")
        return True
    print("\n   WARNING: manifest.json not produced")
    return False


# =============================================================================
# Main Entry Point
# =============================================================================


def main() -> int:
    """Main entry point."""
    # Precondition (#4539): refuse to mutate a board that carries no captured
    # intent.  Anchored on ``__file__`` -- NOT ``Path.cwd()`` and NOT the
    # output dir -- because CI invokes this recipe from the repo root with an
    # out-of-tree output dir, so a cwd-anchored search would find nothing and
    # fire spuriously.  Advisory by default (L1: project.kct exists, is
    # non-empty, and parses); ``KCT_REQUIRE_SPEC=1`` upgrades it to a hard L2
    # gate that exits non-zero BEFORE any file is written.
    require_spec(__file__)

    if len(sys.argv) > 1:
        output_dir = Path(sys.argv[1])
    else:
        output_dir = Path(__file__).parent / "output"

    try:
        # Step 1: Create project file
        project_path = create_project(output_dir, "charlieplex_3x3")

        # Step 2: Create schematic
        sch_path = create_charlieplex_schematic(output_dir)

        # Step 3: Run ERC
        erc_success = run_erc(sch_path)

        # Step 4: Create PCB
        pcb_path = create_charlieplex_pcb(output_dir)

        # Step 5: Route PCB
        routed_path = output_dir / "charlieplex_3x3_routed.kicad_pcb"
        route_success = route_pcb(pcb_path, routed_path)

        # Step 5.5: route_success fast-fail gate (#4066, mirrors board 03's
        # ``route_success`` gate after ``route_pcb`` in
        # boards/03-usb-joystick/generate_design.py).  route_pcb
        # runs under a wall-clock ``--timeout`` SAFETY backstop layered above
        # the load-independent per-net ``--deterministic-budget`` iteration
        # cap, so on a loaded machine that outer deadline can fire before every
        # signal net lands and ``route_pcb`` returns ``False``.  If we fall
        # through, the downstream ``write_lvs_report(require_clean=True)`` sees
        # a genuinely unrouted signal net as a copper OPEN and raises
        # ``BoardNetlistMismatch``, which the broad ``except`` below reports as
        # exit 1 -- misdirecting the reviewer to the LVS subsystem when the
        # true cause is upstream route truncation.  Raise a DISTINCT,
        # clearly-worded error here instead.  The "PARTIAL: Routed N/M signal
        # nets" line is already printed above by ``route_pcb``.
        if not route_success:
            raise RuntimeError(
                "partial route -- likely wall-clock budget exhaustion under "
                "load (the --timeout safety backstop fired before every "
                "signal net landed; see the 'PARTIAL: Routed N/M signal nets' "
                "line above for the exact count). This is NOT a copper-LVS / "
                "GND-stitching failure -- the pipeline stopped before the LVS "
                "gate. Re-run boards/02-charlieplex-led/generate_design.py in "
                "isolation on a quiet machine, or raise the --timeout in "
                "route_pcb() if this recurs on an unloaded host."
            )

        # Step 6: Run DRC
        drc_success = run_drc(routed_path)

        # Step 6.5: LVS (#3779) -- assert the routed PCB's nets match the
        # schematic's design intent via the shared
        # ``kicad_tools.lvs.write_lvs_report`` helper.  Board 02 is verified
        # clean on both the copper-extracted and label-based comparators, so
        # it hard-gates on both (``require_clean=True``): a dirty result
        # raises ``BoardNetlistMismatch`` and the recipe exits non-zero.
        # Writes ``output/lvs.json`` so ``kct board-metrics`` surfaces
        # ``lvs_clean: true`` and the gallery LVS chip turns green.
        copper_clean, label_clean = write_lvs_report(
            sch_path,
            routed_path,
            output_dir,
            require_clean=True,
            run_copper=True,
            run_label=True,
        )
        lvs_success = copper_clean and label_clean

        # Step 7: Export manufacturing bundle (#3147) so ``kct fleet
        # status`` reports ``ship_ready=true`` (the bundle's manifest
        # mtime must be newer than the freshly routed PCB).
        mfg_success = export_manufacturing_bundle(routed_path, output_dir)

        # Issue #3912: derive BOTH the SUMMARY and the exit code from ONE
        # shared PipelineGateResult so they can never diverge.  The gate's
        # DRC leg is AUTHORITATIVE -- it runs ``kicad-cli pcb drc
        # --refill-zones`` (run_geometric_drc) from scratch, catching copper
        # shorts the stale-zone-fill ``kct check`` engine misses -- and the
        # recipe's own ``run_drc`` verdict (``drc_success``) is threaded in
        # as ``supplemental_drc_ok`` so it can only TIGHTEN the DRC verdict.
        # Board 02 routes fully (the fast-fail above already raised on a
        # partial route) with no allowance; its copper-LVS verdict
        # (``lvs_success``) is the LVS leg.  Board 02 is manufacturable-clean,
        # so the gate passes and the recipe exits 0.
        gate = evaluate_pipeline_gate(
            routed_path,
            route_ok=route_success,
            route_allowance=0,
            lvs_ok=lvs_success,
            supplemental_drc_ok=drc_success,
            supplemental_reason=(
                "kct check reported error-level DRC findings (see DRC output above)"
            ),
        )

        # Summary
        print("\n" + "=" * 60)
        print("SUMMARY")
        print("=" * 60)
        print(f"\nOutput directory: {output_dir.absolute()}")
        print("\nGenerated files:")
        print(f"  1. Project: {project_path.name}")
        print(f"  2. Schematic: {sch_path.name}")
        print(f"  3. PCB (unrouted): {pcb_path.name}")
        print(f"  4. PCB (routed): {routed_path.name}")
        print("\nResults:")
        print(f"  ERC: {'PASS' if erc_success else 'FAIL'}")
        for line in gate.summary_lines():
            print(line)
        # #3912: mfg_success means ``kct export`` WROTE a bundle (exit 0),
        # NOT that the board is DRC-clean -- cleanliness is the ``DRC``/
        # ``Overall`` verdict above.  Label "WRITTEN" so PASS is never
        # mistaken for a DRC pass.
        print(f"  MFG bundle: {'WRITTEN' if mfg_success else 'FAILED'}")
        print("\nCharlieplex LED mapping:")
        print("  LED   Anode    Cathode")
        for led_conn in LED_CONNECTIONS:
            print(f"  {led_conn.ref}    {led_conn.anode_node}  {led_conn.cathode_node}")

        # #3912: the exit code derives from the SAME PipelineGateResult that
        # drove the SUMMARY above (route / DRC / LVS legs).  ERC remains an
        # independent gating leg.  (``write_lvs_report`` already raises on a
        # dirty LVS so a False ``lvs_ok`` here would mean LVS short-circuited.)
        return 0 if erc_success and gate.passed else 1

    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
