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
from pathlib import Path

from design_spec import (
    LED_CONNECTIONS,
)

from kicad_tools.core.project_file import create_minimal_project, save_project
from kicad_tools.dev import warn_if_stale
from kicad_tools.lvs import write_lvs_report
from kicad_tools.operations.pintype import annotate_pcb_file_pintypes
from kicad_tools.recipes.gate import evaluate_pipeline_gate
from kicad_tools.recipes.precondition import require_spec

# Warn if running source scripts with stale pipx install
warn_if_stale()


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


# Declared supply currents from project.kct (A), mapped onto nets (Issue #7206).
AMPACITY_TARGETS_A = {"VCC": 0.1}


def _copper_signature(path: Path) -> tuple:
    """Uuid-insensitive geometry signature of a board's segments and vias."""
    from kicad_tools.schema.pcb import PCB

    pcb = PCB.load(str(path))
    segments = sorted(
        (
            seg.layer,
            seg.net_number,
            round(seg.start[0], 6),
            round(seg.start[1], 6),
            round(seg.end[0], 6),
            round(seg.end[1], 6),
            seg.width,
        )
        for seg in pcb.segments
    )
    vias = sorted(
        (
            via.net_number,
            round(via.position[0], 6),
            round(via.position[1], 6),
            via.size,
        )
        for via in pcb.vias
    )
    return (segments, vias)


def _graft_copper(base: Path, donor: Path) -> bool:
    """Copy *donor*'s copper (segments and vias) into *base* when it differs.

    ``kct optimize-traces`` re-mints unrelated uuids every run, so its saved
    file is not byte-stable even when the copper is.  The recipe therefore
    grafts only the donor's copper into its own file -- and skips the graft
    entirely once the copper is geometry-identical, so the fixed point keeps
    its own stable uuids (#9467).
    """
    from copy import deepcopy

    from kicad_tools.sexp import parse_file, serialize_sexp

    if _copper_signature(base) == _copper_signature(donor):
        return False
    base_doc = parse_file(base)
    donor_doc = parse_file(donor)
    for tag in ("segment", "via"):
        for node in [child for child in base_doc.children if child.name == tag]:
            base_doc.remove(node)
    for tag in ("segment", "via"):
        for node in [child for child in donor_doc.children if child.name == tag]:
            base_doc.append(deepcopy(node))
    base.write_text(serialize_sexp(base_doc))
    return True


def _canonicalize_copper(routed_path: Path) -> None:
    """Rewrite track uuids and ordering deterministically (Issue #9467).

    The cleanup chain's rewrites serialize segments in hash-seed-dependent
    iteration order, so two regenerations of the same route would differ
    byte-wise.  Sorting segments by geometry and deriving each uuid from that
    key makes the cleanup's fixed point byte-identical across runs.
    """
    import uuid as uuidlib

    from kicad_tools.schema.pcb import PCB

    def key(seg):
        return (
            seg.layer,
            seg.net_number,
            round(seg.start[0], 6),
            round(seg.start[1], 6),
            round(seg.end[0], 6),
            round(seg.end[1], 6),
            seg.width,
        )

    pcb = PCB.load(str(routed_path))
    ordered = sorted(pcb.segments, key=key)
    pcb.segments[:] = ordered
    for index, seg in enumerate(ordered):
        seg.uuid = str(
            uuidlib.uuid5(uuidlib.NAMESPACE_OID, f"board02-copper:{index}:{key(seg)}")
        )
    pcb.save(str(routed_path))


def _restamp_uuids(routed_path: Path) -> None:
    """Derive every uuid from its subtree's uuid-stripped serialization.

    ``kicad-cli pcb drc --refill-zones --save-board`` mints fresh random
    uuids for any node that lacks one (footprint properties, graphics
    points, ...).  Re-deriving each uuid from the serialized shape of the
    subtree it belongs to (uuid values stripped, so the stamp is idempotent)
    makes the refill's output byte-stable across runs; pcbnew preserves
    existing uuids, so the release's own refill rewrites nothing (#9467).
    """
    import uuid as uuidlib

    from kicad_tools.sexp import parse_file, serialize_sexp

    doc = parse_file(routed_path)

    def strip_uuids(node):
        return "".join(
            line for line in serialize_sexp(node).splitlines() if "(uuid " not in line
        )

    seen: dict[str, int] = {}

    def walk(node, ref):
        if node.name == "footprint":
            for child in node.children:
                if (
                    child.name == "property"
                    and child.get_first_atom() == "Reference"
                    and len(child.children) > 1
                ):
                    ref = f"{ref}/{child.children[1].get_first_atom()}"
        for child in node.children:
            walk(child, ref)
        uuid_node = node.find_child("uuid")
        if uuid_node is not None:
            key = f"board02:{ref}:{strip_uuids(node)}"
            seen[key] = seen.get(key, 0) + 1
            if seen[key] > 1:
                key = f"{key}#{seen[key]}"
            uuid_node.set_atom(0, str(uuidlib.uuid5(uuidlib.NAMESPACE_OID, key)))

    walk(doc, "board")
    routed_path.write_text(serialize_sexp(doc))


def stabilize_release_copper(routed_path: Path) -> None:
    """Step 5.5 (#9467): ship the release cleanup chain's fixed point.

    ``kct readiness --generate`` re-runs ``dedupe -> prune -> optimize-traces
    --drc-aware`` and ``kicad-cli pcb drc --refill-zones --save-board`` on
    the shipped board (the refill re-serializes the file and stamps uuids on
    uuid-less nodes, so the shipped board must already be in that form or
    the package's renders gate sees it as changed).  The recipe first
    iterates the python-side cleanup chain to a byte-stable fixed point,
    then normalizes through one refill, re-derives the uuids the refill
    minted, and refills once more -- packaging's own run then rewrites
    nothing and the committed renders stay valid.  The optimizer runs on a
    scratch copy and only its copper is grafted back, because its serializer
    re-mints unrelated uuids each run.
    """
    import hashlib
    import os
    import shutil
    import tempfile

    from kicad_tools.cli.runner import run_refill_zones
    from kicad_tools.pcb_prune import cleanup_copper

    env = {**os.environ, "PYTHONHASHSEED": "42"}
    for _ in range(4):
        before = hashlib.sha256(routed_path.read_bytes()).hexdigest()
        print(f"   Copper cleanup: {cleanup_copper(routed_path)}")
        with tempfile.TemporaryDirectory(prefix="board02-optimize-") as scratch:
            donor = Path(scratch) / routed_path.name
            shutil.copy2(routed_path, donor)
            for suffix in (".kicad_pro", ".kicad_dru"):
                sidecar = routed_path.with_suffix(suffix)
                if sidecar.exists():
                    shutil.copy2(sidecar, donor.with_suffix(suffix))
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "kicad_tools.cli",
                    "optimize-traces",
                    str(donor),
                    "--drc-aware",
                    "--mfr",
                    "jlcpcb",
                    "--project",
                    str(donor.with_suffix(".kicad_pro")),
                    "--format",
                    "json",
                ],
                capture_output=True,
                text=True,
                timeout=300,
                env=env,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"optimize-traces failed ({result.returncode}): "
                    f"{result.stdout}{result.stderr}"
                )
            _graft_copper(routed_path, donor)
        _canonicalize_copper(routed_path)
        after = hashlib.sha256(routed_path.read_bytes()).hexdigest()
        if after == before:
            break
    else:
        raise RuntimeError("Board02 release copper cleanup did not reach a fixed point in 4 passes")
    # Phase 2: adopt the optimizer's own serialization (footprint order and
    # uuid stamps the release's in-run pass will reproduce), then make its
    # uuids deterministic and normalize through one refill.
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "kicad_tools.cli",
            "optimize-traces",
            str(routed_path),
            "--drc-aware",
            "--mfr",
            "jlcpcb",
            "--project",
            str(routed_path.with_suffix(".kicad_pro")),
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        timeout=300,
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"optimize-traces failed ({result.returncode}): {result.stdout}{result.stderr}"
        )
    _restamp_uuids(routed_path)
    refill = run_refill_zones(routed_path)
    if not refill.success:
        raise RuntimeError(f"zone refill failed: {refill.stderr}")


def declare_ampacity_targets(output_dir: Path) -> None:
    """Declare the spec's supply currents as ``target_ampacity`` (Issue #7206).

    project.kct declares these currents; kct never infers which net carries
    them, so the net-class-map sidecar must map them or ``kct check`` reports
    ``ampacity: unknown``.
    """
    import json

    # Issue #7539: runs right after route_pcb, before anything else is
    # guaranteed to have created output_dir (a stubbed or partial route never
    # writes the routed PCB), so create it rather than masking the
    # route_success fast-fail gate's message with an ENOENT.
    output_dir.mkdir(parents=True, exist_ok=True)
    # ``kct route`` also writes the stem-keyed sidecar (#8156) and the
    # readiness/check auto-probe prefers it, so the declaration must land on
    # both names or ``kct check`` reports ``ampacity: unknown`` (#9467).
    sidecars = [
        output_dir / "net_class_map.json",
        output_dir / "charlieplex_3x3_routed.net_class_map.json",
    ]
    for sidecar in sidecars:
        if not sidecar.exists():
            continue
        data = json.loads(sidecar.read_text())
        for net, amps in AMPACITY_TARGETS_A.items():
            entry = data.setdefault(net, {"name": "Power", "priority": 1, "trace_width": 0.25})
            entry["target_ampacity"] = amps
        sidecar.write_text(json.dumps(data, indent=2))
    if not any(p.exists() for p in sidecars):
        sidecar = sidecars[0]
        data = {}
        for net, amps in AMPACITY_TARGETS_A.items():
            entry = data.setdefault(net, {"name": "Power", "priority": 1, "trace_width": 0.25})
            entry["target_ampacity"] = amps
        sidecar.write_text(json.dumps(data, indent=2))
    print(f"   Declared target_ampacity on {', '.join(AMPACITY_TARGETS_A)}: {sidecars[0]}")


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
        # machines.  --timeout below is retained only as a SAFETY backstop
        # (the normalizer warns if it would bind).
        #
        # Issue #5587: 240s was measured firing on a contended host even
        # with --deterministic-budget -- the outer stage deadline is still
        # WALL-CLOCK, so it can bind before the negotiated loop's own
        # best-stall-patience early-stop does, reintroducing host-load
        # dependence.  Raised to 900s (well beyond the ~30-100s a quiet
        # host needs) so the iteration budget, not the wall clock, is what
        # bounds the work in practice.
        "--deterministic-budget",
        "--timeout",
        "900",
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

    if not output_path.exists():
        print(f"\n   ERROR: ``kct route`` did not produce {output_path}", file=sys.stderr)
        return False

    # Issue #6427: exit 0 is the only success.  ``kct route`` exits 3 when
    # either DRC engine finds a violation in its own copper; finalize's
    # report-driven via repair used to quietly move that copper until DRC
    # passed, hiding a router defect behind a board-local edit.  A non-zero
    # exit now stops the recipe before finalize ever sees the board.
    if result.returncode != 0:
        print(
            f"\n   ERROR: ``kct route`` exited with code {result.returncode}; "
            "board 02 requires a clean route (exit 0) and does not repair "
            "router DRC violations after the fact",
            file=sys.stderr,
        )
        return False

    print("\n   SUCCESS: ``kct route`` reports all signal nets routed!")

    from finalize_routing import finalize_routing

    return finalize_routing(output_path, input_path.with_suffix(".kicad_sch"))


def clear_silk_over_holes(routed_path: Path) -> None:
    """Clip footprint silk off drilled holes and move reference text that sits on one.

    ``fix-silkscreen --clip-to-holes`` handles lines/polygons; text is left for
    ``place-silk-refs --over-holes`` (#9061).  Both are the generic commands --
    no board-local silk edit and no acknowledgement of ``silk_hole_overlap``.
    """
    for command in (
        ["fix-silkscreen", "--mfr", "jlcpcb", "--clip-to-holes", "--format", "summary"],
        ["place-silk-refs", "--over-holes"],
    ):
        subprocess.run(
            [sys.executable, "-m", "kicad_tools.cli", command[0], str(routed_path), *command[1:]],
            check=True,
            timeout=120,
        )


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
        "--spec",
        str(Path(__file__).resolve().parent / "project.kct"),
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

        # Step 4.1: Copy schematic pin names/electrical types onto the pads
        # (issues #5985, #6025), as KiCad's "Update PCB from Schematic" does.
        # The router appends its tracks to this text, so the routed PCB
        # inherits the ``(pintype ...)`` evidence ``kct detect-mistakes``
        # reads for rails.
        annotation = annotate_pcb_file_pintypes(pcb_path, sch_path)
        assert not annotation.missing_pads, annotation.missing_pads
        print(f"   Pad pin types annotated: {annotation.annotated}")

        # Step 5: Route PCB
        routed_path = output_dir / "charlieplex_3x3_routed.kicad_pcb"
        route_success = route_pcb(pcb_path, routed_path)
        declare_ampacity_targets(output_dir)

        # Finalization can reject physical DRC failures as well as incomplete
        # routing. Preserve the actual preceding diagnostic instead of assuming
        # the router's wall-clock safety backstop caused the failure.
        if not route_success:
            raise RuntimeError(
                "Board02 routing/finalization failed; see the route and native DRC "
                "diagnostics above. The PCB has not qualified for manufacturing export."
            )

        # Step 5.6 (#8532 / #8759): global fiducials and a board-ID silkscreen
        # (the default-on ``fiducials_missing`` / ``board_id_missing`` rules).
        # Board 02 has no pours, so nothing needs refilling afterwards; the
        # board-ID template reads ``rev`` from the PCB's title block.
        from kicad_tools.cli.pcb_add_board_info import run_add_board_info
        from kicad_tools.cli.pcb_add_fiducials import run_add_fiducials

        if run_add_fiducials(routed_path, sides="populated") != 0:
            raise RuntimeError("pcb add-fiducials failed")
        if run_add_board_info(routed_path, auto=True) != 0:
            raise RuntimeError("pcb add-board-info failed")

        # Step 5.7 (#9467 / #9061): clear ``silk_hole_overlap`` through the recipe,
        # after every copper edit (finalize, cleanup, fiducials) so the drilled
        # holes it clips against are the shipped ones.  Never acknowledged away.
        clear_silk_over_holes(routed_path)

        # Step 5.8 (#9467): ship the release cleanup chain's fixed point --
        # last board-writing step, so the serializers the packaging run will
        # replay (optimize-traces, the refill) find nothing to rewrite and
        # the committed renders keep matching the shipped board.
        stabilize_release_copper(routed_path)

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
            export_ok=mfg_success,
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
