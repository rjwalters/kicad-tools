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

import os
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


def add_gnd_stitching_vias(routed_path: Path) -> int:
    """Add GND F.Cu<->B.Cu stitching vias to bond fragmented GND islands.

    Issue #3787: this board carries two full-board GND planes (``GND`` on
    ``F.Cu`` and ``GND`` on ``B.Cu``, both priority 1 -- see
    ``generate_pcb.py:generate_power_pours()``) but the router emits *zero*
    GND vias (every via on the board is a signal-net layer change).  Without
    a via tying F.Cu-GND to B.Cu-GND, the J1 USB-C connector's F.Cu-only GND
    shield/return pads (A12 / B1) sit over the B.Cu GND pour with no
    galvanic path into it -- the F.Cu pour is carved away around the
    fine-pitch USB-C pad field, so those pads land in their own copper
    islands.  ``compare_copper_netlist`` correctly reports these as GND
    *opens* (KiCad's own DRC treats same-net zones as logically connected
    and so misses the missing physical bond).  This is the board-03 analogue
    of the universal "you must stitch your ground planes" rule.

    The fix is a recipe step -- run *after* :func:`route_pcb` and *before*
    :func:`fill_zones_in_routed_pcb` -- that finds every GND pad island that
    is **not** part of the main (largest) GND island and drops a GND-net
    ``(via ... (layers "F.Cu" "B.Cu"))`` at that island's pad so the
    subsequent re-fill bonds it into both GND planes.  The placement is
    derived dynamically from the routed copper partition
    (:meth:`ConnectivityValidator.extract_pad_partition`), so it stays
    correct if the deterministic route shifts which pads strand.

    The routed board reaching this step is *unfilled* (the zone fill runs in
    the next step, :func:`fill_zones_in_routed_pcb`), and copper-pour
    connectivity can only be measured against *filled* zones.  So the island
    detection is run against a throwaway **filled copy** of the board -- the
    same fill the real artifact will get -- while the vias are written into
    the real (unfilled) routed board, which Step 5.5 then fills for keeps.

    Mirrors the read-text / mutate / write-text / return-count style of
    :func:`fill_zones_in_routed_pcb`.  The via geometry (0.6mm size /
    0.3mm drill, F.Cu<->B.Cu) matches the signal-net vias already on the
    board and is jlcpcb-tier1 legal; the isolated GND pads are >=1.0mm from
    any neighbouring pad, leaving ample clearance for the annular ring.

    Returns the number of stitching vias added (0 if GND is already a
    single island -- e.g. on a re-run of an already-stitched board).
    """
    import re as _re
    import shutil as _shutil
    import tempfile as _tempfile
    import uuid as _uuid

    from kicad_tools.cli.runner import find_kicad_cli, run_fill_zones
    from kicad_tools.lvs.board_lvs import _schematic_pin_to_net
    from kicad_tools.validate.connectivity import ConnectivityValidator

    print("\n" + "=" * 60)
    print("Adding GND stitching vias (F.Cu<->B.Cu)...")
    print("=" * 60)

    sch_path = routed_path.parent / "usb_joystick.kicad_sch"

    text = routed_path.read_text()

    # Island detection needs *filled* zones (copper-pour connectivity is only
    # measurable against ``filled_polygon`` copper).  The routed board here is
    # still unfilled, so fill a throwaway copy and partition THAT; the vias
    # are written back into the real (unfilled) ``routed_path`` below.
    kicad_cli = find_kicad_cli()
    detect_path = routed_path
    _tmpdir: _tempfile.TemporaryDirectory[str] | None = None
    if kicad_cli is not None:
        _tmpdir = _tempfile.TemporaryDirectory()
        detect_path = Path(_tmpdir.name) / routed_path.name
        _shutil.copy(routed_path, detect_path)
        fill_result = run_fill_zones(detect_path, kicad_cli=kicad_cli)
        if not fill_result.success:
            print(
                "\n   WARNING: temp zone fill failed; partitioning the "
                "unfilled board (stitch placement may be conservative)."
            )
            detect_path = routed_path
    else:
        print(
            "\n   WARNING: kicad-cli not found; partitioning the unfilled "
            "board (stitch placement may be conservative)."
        )

    # Resolve the GND net id dynamically from the (net N "GND") table -- do
    # NOT hardcode (it is 3 on the current artifact but is route-dependent).
    net_table = dict(_re.findall(r'\(net (\d+) "([^"]*)"\)', text))
    gnd_net_id: int | None = None
    for nid, name in net_table.items():
        if name == "GND":
            gnd_net_id = int(nid)
            break
    if gnd_net_id is None:
        raise RuntimeError(
            f'{routed_path} has no (net N "GND") entry -- cannot place GND '
            "stitching vias (issue #3787)."
        )

    # Map each pad id -> schematic net so we can pick out the GND islands.
    schematic_net_of_pad = _schematic_pin_to_net(sch_path)
    gnd_pads = {f"{ref}.{pad}" for (ref, pad), net in schematic_net_of_pad.items() if net == "GND"}

    # Build the physical copper partition and the board-frame pad positions
    # from the SAME validator instance so islands and coordinates agree.
    #
    # NB: the parser normalises footprint positions by the detected board
    # origin (``PCB._board_origin``, e.g. (100, 100) here), so the partition
    # frame is origin-relative.  The raw ``.kicad_pcb`` ``(via (at ...))``
    # s-expressions we emit must be in *file* coordinates, so add the origin
    # offset back when computing each via's ``(at ...)``.
    validator = ConnectivityValidator(detect_path)
    origin_x, origin_y = validator.pcb._board_origin
    partition = validator.extract_pad_partition()
    pad_positions: dict[str, tuple[float, float]] = {}
    for fp in validator.pcb.footprints:
        if not fp.reference or fp.reference.startswith("#"):
            continue
        fp_x, fp_y = fp.position
        rotation = fp.rotation
        for pad in fp.pads:
            if pad.number is None or pad.number == "":
                continue
            pad_positions[f"{fp.reference}.{pad.number}"] = validator._transform_pad_position(
                pad.position, fp_x, fp_y, rotation
            )

    # Islands that contain at least one GND pad.
    gnd_islands = [
        sorted(p for p in island if p in gnd_pads)
        for island in partition
        if any(p in gnd_pads for p in island)
    ]
    gnd_islands = [isl for isl in gnd_islands if isl]
    if len(gnd_islands) <= 1:
        print("\n   GND is already a single copper island -- no vias needed.")
        return 0

    # The largest GND island is the bonded plane; every other island is a
    # fragment that needs a stitch via to reach it.  Pick a representative
    # pad per fragment (the lexicographically-first, for determinism) and
    # place the via at that pad's board-frame centre.
    gnd_islands.sort(key=len, reverse=True)
    main_island, fragments = gnd_islands[0], gnd_islands[1:]
    print(
        f"\n   GND fragmented into {len(gnd_islands)} island(s); "
        f"main island has {len(main_island)} pad(s), "
        f"{len(fragments)} fragment(s) to stitch."
    )

    vias = []
    for frag in fragments:
        anchor = frag[0]
        pos = pad_positions.get(anchor)
        if pos is None:
            print(f"   WARNING: no position for {anchor}; skipping stitch via")
            continue
        # Convert from the partition's origin-relative frame back to raw
        # file coordinates for the emitted ``(via (at ...))``.
        x, y = pos[0] + origin_x, pos[1] + origin_y
        vias.append((anchor, x, y))

    if not vias:
        print("\n   No placeable GND fragments found.")
        return 0

    via_blocks = []
    for anchor, x, y in vias:
        via_blocks.append(
            "\t(via\n"
            f"\t\t(at {x:g} {y:g})\n"
            "\t\t(size 0.6)\n"
            "\t\t(drill 0.3)\n"
            '\t\t(layers "F.Cu" "B.Cu")\n'
            f'\t\t(uuid "{_uuid.uuid4()}")\n'
            f"\t\t(net {gnd_net_id})\n"
            "\t)\n"
        )
        print(f"   + GND stitch via at {anchor} ({x:g}, {y:g})")

    # Insert the via s-expressions just before the file's final closing
    # paren (the top-level (kicad_pcb ...) close), mirroring how KiCad
    # appends board items.
    insert_at = text.rstrip().rfind(")")
    new_text = text[:insert_at] + "".join(via_blocks) + text[insert_at:]
    routed_path.write_text(new_text)

    print(f"\n   Added {len(vias)} GND stitching via(s).")
    return len(vias)


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
        # Issue #3799: route under an ITERATION budget instead of the
        # per-net WALL-CLOCK cutoff.  --seed only seeds Python's global
        # random; it does NOT control the per-net A* deadline checked in
        # the C++ loop, so on a loaded machine the wall-clock budget fires
        # mid-search and the net lands less copper -- same seed, different
        # copper.  --deterministic-budget (#3538) disables the per-net
        # wall-clock cutoff and pins a fixed node-expansion backstop, so
        # the seed-42 re-route is byte-identical (UUID-normalized) across
        # machines.  --timeout 600 below is then a SAFETY backstop only.
        # KEEP IN SYNC with tests/router/test_board03_routing_baseline.py.
        "--deterministic-budget",
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
    # Issue #3799: pin PYTHONHASHSEED for the route subprocess so any
    # string-keyed dict/set iteration in the negotiated router is
    # reproducible across runner environments.  Combined with --seed 42 +
    # --deterministic-budget this makes the full pipeline deterministic,
    # not just the A* loop.  Mirrors board-07's convention.
    _route_env = os.environ.copy()
    _route_env["PYTHONHASHSEED"] = "42"
    proc = _sp.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
        env=_route_env,
    )
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

        # Step 5.4: Add GND F.Cu<->B.Cu stitching vias (#3787).  The board
        # has GND planes on both copper layers but the router emits zero GND
        # vias, so the J1 USB-C F.Cu-only shield pads strand in their own
        # copper islands (copper-LVS GND opens).  Drop a stitch via at each
        # fragmented GND island BEFORE the fill so the re-fill below bonds
        # the new vias into both GND planes.
        add_gnd_stitching_vias(routed_path)

        # Step 5.5: Fill the zone polygons in the routed PCB so DRC's
        # ``connectivity`` rule sees the power-net pads as connected.  This
        # MUST run after Step 5.4 so the fill engine recomputes copper that
        # bonds through the freshly-added GND stitching vias.
        fill_zones_in_routed_pcb(routed_path)

        # Step 6: Run DRC
        drc_success = run_drc(routed_path)

        # Step 6.5: LVS (HARD copper gate, #3795 / #3780 Part 2) -- board 03
        # graduated out of ``ADVISORY_LVS_BOARDS``.  The residual GND
        # copper-opens were FIXED in #3787: Step 5.4 stitches the F.Cu/B.Cu
        # GND planes so the J1 USB-C F.Cu-only shield pads bond into the GND
        # net and the copper comparator reports 0 shorts / 0 opens.  A fresh
        # clean-room ``generate_design.py`` regen regenerates copper-clean,
        # so ``require_clean=True`` now hard-gates the copper leg: a
        # copper short/open raises :class:`BoardNetlistMismatch` and trips
        # the recipe exit gate (and the new ``board-03-end-to-end`` CI job
        # asserts ``lvs.json clean=true``).  ``write_lvs_report`` still
        # writes ``output/lvs.json`` so board.json / the gallery LVS chip
        # surface the green status.  ``run_label`` stays ``False``: the
        # copper comparator is the meaningful leg, and the USB-C fixture's
        # ``schematic_net=None`` label noise keeps the label leg advisory.
        write_lvs_report(
            sch_path,
            routed_path,
            output_dir,
            require_clean=True,
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
