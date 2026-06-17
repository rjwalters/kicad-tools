#!/usr/bin/env python3
"""
USB Joystick Controller - Complete Design Generation

This script demonstrates the complete PCB design workflow:
1. Create project file
2. Create schematic with MCU, USB, joystick, and buttons
3. Run ERC validation
4. Generate PCB with component placement
5. Route PCB traces
6. Run DRC validation

The design is a USB game controller with:
- 32-pin QFP microcontroller
- USB Type-C connector
- 2-axis analog joystick
- 4 tactile buttons
- Crystal oscillator

Usage:
    python generate_design.py [output_dir]

If no output directory is specified, files are written to ./output/
"""

import subprocess
import sys
from pathlib import Path

from kicad_tools.core.project_file import create_minimal_project, save_project
from kicad_tools.dev import warn_if_stale
from kicad_tools.lvs import write_lvs_report

# Warn if running source scripts with stale pipx install
warn_if_stale()


# =============================================================================
# Schematic Generation
# =============================================================================


def create_usb_joystick_schematic(output_dir: Path) -> Path:
    """Create the USB Joystick schematic by delegating to ``generate_schematic.py``.

    Issue #3764: this recipe previously carried its OWN inline, simplified
    schematic (4-pin ``Conn_01x04`` USB stub, generic connectors, a
    ``power:+5V`` rail, and an MCU pinout that disagreed with the PCB).
    That third, divergent net model meant the canonical end-to-end recipe
    never reproduced the schematic that actually ships, so the
    schematic↔PCB netlist could not reconcile.

    Mirroring the way :func:`create_usb_joystick_pcb` already delegates to
    ``generate_pcb.py``, the schematic step now delegates to
    ``generate_schematic.py`` so there is exactly ONE schematic generator.
    The PCB's 16-net model is the source of truth and the shared schematic
    generator is aligned to it pad-for-pad.

    Returns the path to the generated schematic file.
    """
    print("\n" + "=" * 60)
    print("Creating USB Joystick Schematic (delegates to generate_schematic.py)...")
    print("=" * 60)

    sys.path.insert(0, str(Path(__file__).parent))
    import generate_schematic as _sch_gen

    output_dir.mkdir(parents=True, exist_ok=True)
    sch_path = output_dir / "usb_joystick.kicad_sch"
    _sch_gen.create_usb_joystick_schematic(sch_path)
    print(f"   Schematic: {sch_path}")

    return sch_path


# =============================================================================
# PCB Generation
# =============================================================================
#
# Issue #3410: this script previously carried its OWN copy of the PCB
# layout (60x40mm board, pre-#2527 USB pin assignment) which had drifted
# from the canonical generator ``generate_pcb.py`` (80x60mm, schematic-
# aligned pin map).  Because ``kct build --step pcb`` and the CI
# ``regenerated_board`` fixture both run ``generate_pcb.py`` while this
# script's main() wrote its stale internal copy over the committed
# artifact, the two paths measured DIFFERENT boards (the #3402 audit's
# "77% reach" was the stale 60x40 board).  Mirroring the #3308 route-
# recipe consolidation, the PCB step now DELEGATES to ``generate_pcb.py``
# so there is exactly one copy of the layout.
#
# Copper pours (GND/VCC/VBUS) are likewise emitted by ``generate_pcb.py:
# generate_power_pours()`` -- see ``create_zones_for_pcb`` below.


def create_usb_joystick_pcb(output_dir: Path) -> Path:
    """Create the PCB by delegating to the canonical ``generate_pcb.py``.

    Returns the path to the generated PCB file.
    """
    print("\n" + "=" * 60)
    print("Creating USB Joystick PCB (delegates to generate_pcb.py)...")
    print("=" * 60)

    sys.path.insert(0, str(Path(__file__).parent))
    import generate_pcb as _pcb_gen

    pcb_path = output_dir / "usb_joystick.kicad_pcb"
    output_dir.mkdir(parents=True, exist_ok=True)
    pcb_path.write_text(_pcb_gen.generate_pcb())
    print(f"   PCB: {pcb_path}")
    print(f"\n   Board size: {_pcb_gen.BOARD_WIDTH}mm x {_pcb_gen.BOARD_HEIGHT}mm")
    return pcb_path


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

    # Issue #3764: persist the ERC report as ``output/erc_report.json``
    # (the location ``fleet_cmd._detect_erc`` looks for) so the board-03
    # ERC leg in ``kct fleet ship-ready`` is a captured artifact instead
    # of ``n/a``.  Previously this report was parsed then immediately
    # deleted.
    erc_report_path = sch_path.parent / "erc_report.json"
    try:
        report = ERCReport.load(result.output_path)
    except Exception as e:
        print(f"\n   Error parsing ERC report: {e}")
        return False
    finally:
        if result.output_path:
            try:
                erc_report_path.write_text(Path(result.output_path).read_text())
                print(f"\n   ERC report: {erc_report_path}")
            except OSError as exc:
                print(f"\n   WARNING: could not persist ERC report: {exc}")
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


def create_zones_for_pcb(pcb_path: Path) -> int:
    """Verify the power-net copper pours exist in *pcb_path*.

    Issue #3410 consolidation: the GND (F.Cu + B.Cu) / VCC / VBUS zone
    definitions are now emitted by the canonical generator
    (``generate_pcb.py:generate_power_pours``) so the layout and its
    pour regions cannot drift apart again (the pre-#3410 version of
    this function carried pour rectangles hand-tuned to a 60x40mm
    layout this script no longer generates).  This step now only
    VERIFIES the pours are present so a future generator regression
    fails loudly here rather than as 40 stranded-pad connectivity
    errors at DRC time.

    Returns the number of zones found.
    """
    print("\n" + "=" * 60)
    print("Verifying copper-pour zones...")
    print("=" * 60)

    text = pcb_path.read_text()
    zone_count = text.count("(zone")
    required = ("GND", "VCC", "VBUS")
    missing = [n for n in required if f'(net_name "{n}")' not in text]
    if missing:
        raise RuntimeError(
            f"Generated PCB {pcb_path} is missing pour zone(s) for "
            f"{', '.join(missing)} -- generate_pcb.py:generate_power_pours() "
            "must emit GND/VCC/VBUS zones (issue #3410)."
        )
    print(f"\n   {zone_count} zone(s) present (GND/VCC/VBUS pours OK)")
    return zone_count


def fill_zones_in_routed_pcb(routed_path: Path) -> int:
    """Fill copper zones in the routed PCB via ``kicad-cli``.

    Zone *definitions* (created by :func:`create_zones_for_pcb`) only carry
    a polygon outline + net + layer.  The actual ``(filled_polygon ...)``
    copper is computed by KiCad's fill engine -- without this step the
    routed PCB ships with empty zones, and DRC reports the power-net pads
    as stranded.  Mirrors board-05's ``fill_zones_in_routed_pcb`` at
    ``boards/05-bldc-motor-controller/design.py:2233``.

    Returns the number of zones in the routed PCB after fill.
    """
    from kicad_tools.cli.runner import find_kicad_cli, run_fill_zones

    print("\n" + "=" * 60)
    print("Filling copper zones...")
    print("=" * 60)

    kicad_cli = find_kicad_cli()
    if kicad_cli is None:
        print("\n   WARNING: kicad-cli not found - skipping zone fill")
        return 0

    print(f"\n1. Filling zones in: {routed_path}")
    result = run_fill_zones(routed_path, kicad_cli=kicad_cli)

    if not result.success:
        print(f"\n   WARNING: Zone fill failed: {result.stderr or '(no stderr)'}")
        return 0

    try:
        text = routed_path.read_text()
        zone_count = text.count("(zone ")
        print(f"\n2. Zones present: {zone_count}")
        return zone_count
    except Exception:
        return 0


def route_pcb(input_path: Path, output_path: Path) -> bool:
    """Route the PCB with the production ``kct route`` recipe.

    Issue #3410 (recipe consolidation, round 2): the previous version of
    this function carried an in-process ``Autorouter.route_all()`` recipe
    (0.05mm grid, in-pad rescues on U1, CoupledPathfinder disabled).
    That simple per-net strategy tops out at 11-12/13 on this board: the
    USB-C escape belt packs four signal columns into 3.5mm, and without
    the negotiated two-phase strategy's rip-up/retry and fine-pitch
    escape regions, whichever USB net routes last is left stranded
    (USB_CC2 under current HEAD).

    The production ``kct route`` invocation -- the SAME one pinned by
    ``tests/router/test_board03_routing_baseline.py`` and used by the
    fleet (board-05 precedent: "bake proven kct route flag recipe into
    design.py", PR #2981) -- reaches 13/13 at 2 layers on the
    regenerated board.  Delegating to it means the demo, the build
    pipeline, and the reach-floor CI tests all measure ONE code path.

    The function also emits the ``net_class_map.json`` sidecar next to
    the routed PCB so the validate-side diff-pair rules
    (``routing_continuity`` / ``length_skew``) can engage from
    ``kct check --net-class-map`` (Issue #2684).

    Returns True when every signal net is fully routed (the DRC gate is
    reported separately by the caller via ``run_drc``).
    """
    import json as _json
    import re as _re
    import subprocess as _sp
    from dataclasses import replace as _dc_replace

    from kicad_tools.router import create_net_class_map
    from kicad_tools.router.rules import net_class_map_to_dict

    print("\n" + "=" * 60)
    print("Routing PCB (production `kct route` recipe)...")
    print("=" * 60)

    # ------------------------------------------------------------------
    # Net-class sidecar (Issue #2684): annotate the USB diff pair so the
    # validate-side rules can re-derive engagement/skew state.  The
    # intra_pair_clearance widening to 0.15mm mirrors #3095 (JLCPCB
    # ``diffpair_clearance_intra`` threshold is 0.127mm).
    # ------------------------------------------------------------------
    net_class_map = create_net_class_map(
        power_nets=["VCC", "VBUS", "GND"],
        high_speed_nets=["USB_D+", "USB_D-"],
        clock_nets=["XTAL1", "XTAL2"],
    )
    if "USB_D+" in net_class_map and "USB_D-" in net_class_map:
        net_class_map["USB_D+"] = _dc_replace(
            net_class_map["USB_D+"],
            diffpair_partner="USB_D-",
            intra_pair_clearance=0.15,
        )
        net_class_map["USB_D-"] = _dc_replace(
            net_class_map["USB_D-"],
            diffpair_partner="USB_D+",
            intra_pair_clearance=0.15,
        )
    sidecar_path = output_path.parent / "net_class_map.json"
    sidecar_path.write_text(_json.dumps(net_class_map_to_dict(net_class_map), indent=2))
    print(f"   Wrote net-class-map sidecar: {sidecar_path}")

    # ------------------------------------------------------------------
    # Production routing recipe.  EXACTLY the invocation pinned by
    # tests/router/test_board03_routing_baseline.py::_run_kct_route --
    # if you change a flag here, change it there in the same commit.
    # ------------------------------------------------------------------
    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "route",
        str(input_path),
        "--output",
        str(output_path),
        "--seed",
        "42",
        "--manufacturer",
        "jlcpcb-tier1",
        "--backend",
        "cpp",
        "--timeout",
        "600",
        # Issues #3507/#3454: ``--raw`` (skip TraceOptimizer) was
        # LOAD-BEARING for the 0-DRC acceptance until the grid-staleness
        # fix.  The optimize pass used to replace Route objects without
        # re-marking the routing grid, so the optimizer's collision
        # checking ran against pre-optimization copper and the
        # segment-merge pass deterministically re-introduced one
        # ``clearance_segment_via`` violation (XTAL1's merged B.Cu run
        # vs XTAL2's via, 0.006mm gap, seeds 42/43).  With the
        # grid-transactional optimize (``optimize_routes_grid_synced``)
        # the optimizer is safe here: 13/13 nets, 0 DRC errors at
        # jlcpcb-tier1 WITH optimization ON (verified seed 42 against
        # the sidecar-aware ``kct check``).
    ]
    print(f"   $ {' '.join(cmd[1:])}")
    proc = _sp.run(cmd, capture_output=True, text=True, timeout=1800, check=False)
    # Echo the route output so reach parsers (and humans) see the full log.
    print(proc.stdout)
    if proc.returncode in (1, 5):
        print(f"   ERROR: kct route failed with exit code {proc.returncode}")
        if proc.stderr:
            print(proc.stderr[-2000:])
        return False

    # Parse the LAST "Nets routed: N/M" occurrence (escalation mode can
    # emit several; the last reflects the final saved state).
    matches = _re.findall(r"Nets routed:\s+(\d+)/(\d+)", proc.stdout)
    if not matches:
        print("   ERROR: could not parse 'Nets routed: N/M' from kct route output")
        return False
    routed, total = (int(matches[-1][0]), int(matches[-1][1]))

    success = routed == total
    if success:
        print("\n   SUCCESS: All signal nets routed!")
    else:
        print(f"\n   PARTIAL: Routed {routed}/{total} signal nets")

    return success


def export_manufacturing_bundle(routed_path: Path, output_dir: Path) -> bool:
    """Export the manufacturing bundle (gerbers, BOM, CPL, report).

    Issue #3095: AC requires the routed PCB to produce a manufacturing
    bundle (`fleet status` checks for ``manufacturing/`` directory with
    ``manifest.json``).  ``kct export`` runs the standard JLCPCB recipe
    (gerbers + drill + BOM + CPL + report.{md,pdf} + manifest.json) but
    skips the strict pre-flight DRC/ERC gate so the bundle can be
    produced even with the small allowlisted USB-C tolerance errors.
    """
    print("\n" + "=" * 60)
    print("Exporting manufacturing bundle...")
    print("=" * 60)

    mfg_dir = output_dir / "manufacturing"
    # Issue #3150: board 03 is ROUTED/DRC-gated against jlcpcb-tier1
    # (Capability-Plus permits the standard via-in-pad on U1-28 / USB_D-
    # that tier-0 forbids; see the manufacturers: override in
    # .github/routed-drc-tolerance.yml).  The `kct export` fab-spec layer,
    # however, only recognises the base `jlcpcb` profile name for CPL /
    # spec-overlay generation (tier-1 is a routing/DRC capability tier, not
    # a distinct fab house), so the bundle exports against `jlcpcb` --
    # exactly mirroring board-04's split (#3033/#3038): route+check at
    # tier-1, export at jlcpcb.
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


def run_drc(pcb_path: Path) -> bool:
    """Run DRC on the routed PCB and write ``drc_report.json`` beside it.

    Issue #3764: capture the DRC result as ``output/drc_report.json`` so
    the board-03 DRC leg in ``kct fleet ship-ready`` becomes a real
    artifact instead of ``n/a``.  The report is written next to the
    routed PCB (``<routed>.parent/drc_report.json``) — exactly where
    ``fleet_cmd._detect_drc`` looks for it.

    Uses ``--drc-only`` so the gate reflects geometric DRC (clearance /
    connectivity / via rules) rather than the copper-LVS sub-check, which
    reports pour-served power-net pads (VCC / VBUS / GND) as "open"
    because the router deliberately skips those nets and serves them via
    copper pours.  Schematic↔PCB netlist equivalence is asserted
    separately and exactly by ``compare_netlists`` (issue #3764), so the
    DRC leg here is correctly scoped to manufacturing geometry.
    """
    print("\n" + "=" * 60)
    print("Running DRC (via kct check --drc-only)...")
    print("=" * 60)

    report_path = pcb_path.parent / "drc_report.json"
    try:
        # Issue #3150: align the local DRC summary with the jlcpcb-tier1
        # profile this board ships and is gated against (see
        # export_manufacturing_bundle and the manufacturers: override in
        # .github/routed-drc-tolerance.yml).
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "kicad_tools.cli",
                "check",
                str(pcb_path),
                "--mfr",
                "jlcpcb-tier1",
                "--drc-only",
                "--output",
                str(report_path),
            ],
            capture_output=True,
            text=True,
        )

        if result.stdout:
            for line in result.stdout.strip().split("\n"):
                print(f"   {line}")

        if report_path.is_file():
            print(f"\n   DRC report: {report_path}")

        if result.returncode == 0:
            return True
        else:
            if result.stderr:
                print(f"\n   Error: {result.stderr}")
            return False

    except Exception as e:
        print(f"\n   Error running DRC: {e}")
        return False


# =============================================================================
# Main Entry Point
# =============================================================================


def main() -> int:
    """Main entry point."""
    if len(sys.argv) > 1:
        output_dir = Path(sys.argv[1])
    else:
        output_dir = Path(__file__).parent / "output"

    try:
        # Step 1: Create project file
        project_path = create_project(output_dir, "usb_joystick")

        # Step 2: Create schematic
        sch_path = create_usb_joystick_schematic(output_dir)

        # Step 3: Run ERC
        erc_success = run_erc(sch_path)

        # Step 4: Create PCB
        pcb_path = create_usb_joystick_pcb(output_dir)

        # Step 4.5: Create copper-pour zones for GND/VCC/VBUS so the
        # power-net pads land on filled copper instead of being stranded
        # by the router's ``skip_nets`` list (#3095).
        create_zones_for_pcb(pcb_path)

        # Step 5: Route PCB
        routed_path = output_dir / "usb_joystick_routed.kicad_pcb"
        route_success = route_pcb(pcb_path, routed_path)

        # Step 5.5: Fill the zone polygons in the routed PCB so DRC's
        # ``connectivity`` rule sees the power-net pads as connected.
        fill_zones_in_routed_pcb(routed_path)

        # Step 6: Run DRC
        drc_success = run_drc(routed_path)

        # Step 6.5: LVS (advisory, #3780) -- board 03 is in
        # ``ADVISORY_LVS_BOARDS`` and is genuinely copper-dirty today (the
        # J1 USB-C connector's F.Cu-only pads sit over the B.Cu pour with no
        # stitching via, leaving residual opens), so ``require_clean=False``:
        # ``write_lvs_report`` logs the mismatch summary and writes
        # ``output/lvs.json`` but does NOT raise.  This surfaces
        # ``lvs_clean=false`` (with ``copper_mismatches`` detail) in
        # board.json / the gallery LVS chip without gating CI.  ``run_label``
        # is off because the board is label-dirty too and the copper
        # comparator is the meaningful leg.  Graduation to a hard gate is
        # deferred (see #3785 + the board-03 residual-opens follow-up).
        write_lvs_report(
            sch_path,
            routed_path,
            output_dir,
            require_clean=False,
            run_copper=True,
            run_label=False,
        )

        # Step 7: Export manufacturing bundle (gerbers, BOM, CPL,
        # report).  Required by AC of #3095 so ``kct fleet status``
        # reports ``ship_ready=true``.
        mfg_success = export_manufacturing_bundle(routed_path, output_dir)

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
        print(f"  Routing: {'SUCCESS' if route_success else 'PARTIAL'}")
        print(f"  DRC: {'PASS' if drc_success else 'FAIL'}")
        print(f"  MFG bundle: {'PASS' if mfg_success else 'FAIL'}")
        print("\nBoard description:")
        print("  - USB game controller with analog joystick")
        print("  - 32-pin QFP MCU")
        print("  - USB Type-C connector")
        print("  - 4 tactile buttons")

        # For this complex demo board, partial routing is acceptable
        # Success if ERC passes and DRC has no errors (warnings OK)
        return 0 if erc_success and drc_success else 1

    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
