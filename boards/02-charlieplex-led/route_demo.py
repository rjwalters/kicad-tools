#!/usr/bin/env python3
"""
Demonstrate autorouting on the charlieplexed LED grid PCB.

This script invokes ``kct route`` with the proven flag recipe used by
``generate_design.py:route_pcb()``.  Using the orchestrator path
(rather than a bare in-process ``router.route_all()`` call) is what
unlocks ≥ 8/10 nets DRC-clean on this geometry, because ``kct route``:

  1. uses the negotiated congestion router with adaptive rip-up,
  2. emits auto-pour zones for GND/VCC after routing (so power pads
     reach ``status=complete`` via plane connectivity),
  3. runs auto-layer-escalation when 1-layer routing is blocked, and
  4. runs ``drc_verify_and_nudge`` post-route (Issue #3112) to slide
     same-net via-in-pad escape vias off offending pads.

Issue #3207: this script previously called the bare ``router.route_all()``
in-process path, which regressed to 4/8 nets routed with 6 DRC errors
on board 02 even though ``kct route`` direct on the same PCB reaches
8/10 + DRC-clean.  Replacing the in-process path with a subprocess
call to ``kct route`` (matching ``generate_design.py:route_pcb()``)
guarantees the two recipes can't drift again.

Usage:
    python route_demo.py [input_pcb] [output_pcb]

Example:
    python route_demo.py output/charlieplex_3x3.kicad_pcb output/charlieplex_3x3_routed.kicad_pcb
"""

import contextlib
import os
import subprocess
import sys
from pathlib import Path

# Add src to path for development (ensures source version is used)
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from kicad_tools.dev import warn_if_stale

# Warn if running source scripts with stale pipx install
warn_if_stale()


def run_drc(pcb_path: Path) -> tuple[bool, int, int]:
    """Run DRC on the PCB using kct check for consistent results.

    Uses kct check as a subprocess to ensure the same DRC rules
    are applied as when running kct check manually.

    Returns:
        Tuple of (success, error_count, warning_count)
    """
    try:
        result = subprocess.run(
            [sys.executable, "-m", "kicad_tools.cli", "check", str(pcb_path)],
            capture_output=True,
            text=True,
        )

        # Parse the output to extract error/warning counts
        error_count = 0
        warning_count = 0
        for line in result.stdout.split("\n"):
            if "Errors:" in line:
                with contextlib.suppress(ValueError):
                    error_count = int(line.split(":")[-1].strip())
            elif "Warnings:" in line:
                with contextlib.suppress(ValueError):
                    warning_count = int(line.split(":")[-1].strip())

        return result.returncode == 0, error_count, warning_count

    except Exception as e:
        print(f"  Warning: DRC check failed: {e}")
        return False, -1, -1


def export_manufacturing_bundle(routed_path: Path, output_dir: Path) -> bool:
    """Regenerate the manufacturing bundle so ``kct fleet status`` stays fresh.

    Issue #3264: ``kct fleet status`` flags a board ``ship_ready=false``
    with the ``"artifacts stale"`` blocker whenever the routed PCB is
    newer than ``output/manufacturing/manifest.json``.  Re-running
    ``route_demo.py`` always rewrites the routed PCB, so the demo must
    also regenerate the manufacturing bundle to keep the manifest current
    (mirrors what ``generate_design.py:export_manufacturing_bundle()``
    already does at the end of the full design flow).

    ``kct export`` runs the standard JLCPCB recipe (gerbers + drill + BOM
    + CPL + report.{md,pdf} + manifest.json).  ``--skip-preflight`` skips
    the strict pre-flight DRC/ERC gate so the bundle is produced even for
    boards that ship with allowlisted tolerances; for clean boards (like
    board 02 post-#3207) it is harmless.
    """
    print("\n" + "=" * 60)
    print("Exporting manufacturing bundle (Issue #3264)...")
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


def _parse_routed_net_count(stdout: str) -> tuple[int, int] | None:
    """Extract ``Nets routed: N/M`` (or equivalent) from ``kct route`` output.

    ``kct route`` emits a summary block of the form::

        Nets routed: 8/10
        ...

    Returns ``(routed, total)`` or ``None`` if no match.
    """
    import re

    for pattern in (
        r"Nets routed:\s+(\d+)/(\d+)",
        r"Routed\s+(\d+)/(\d+)\s+nets",
        r"(\d+)/(\d+)\s+nets\s+complete",
    ):
        m = re.search(pattern, stdout)
        if m:
            return int(m.group(1)), int(m.group(2))
    return None


def main():
    """Run the routing demo via ``kct route`` (matching generate_design.py)."""
    # Parse arguments
    demo_dir = Path(__file__).parent
    input_pcb = sys.argv[1] if len(sys.argv) > 1 else "output/charlieplex_3x3.kicad_pcb"
    output_pcb = sys.argv[2] if len(sys.argv) > 2 else "output/charlieplex_3x3_routed.kicad_pcb"

    input_path = demo_dir / input_pcb
    output_path = demo_dir / output_pcb

    if not input_path.exists():
        print(f"Error: Input PCB not found: {input_path}")
        print("Run generate_pcb.py first to create the PCB file.")
        sys.exit(1)

    print("=" * 60)
    print("Charlieplex LED Grid Autorouting Demo")
    print("=" * 60)
    print(f"\nInput:  {input_path}")
    print(f"Output: {output_path}")

    # GND is a pour net (auto-poured into a copper zone by ``kct route``).
    # Excluded from the per-net pathfinder to avoid wasted iterations.
    # This matches ``generate_design.py:route_pcb()`` exactly.
    skip_nets = ["GND"]

    # Same recipe as ``boards/02-charlieplex-led/generate_design.py:route_pcb()``.
    # Keeping these two recipes byte-identical is the whole point of Issue #3207:
    # the in-process ``router.route_all()`` path drifted from the orchestrator
    # path and silently regressed board 02 to 4/8 nets + 6 DRC errors.  This
    # subprocess invocation re-uses the same code path that ``kct build``,
    # ``kct route``, and the canonical board-05 / board-07 recipes use.
    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "route",
        str(input_path),
        "--output",
        str(output_path),
        "--strategy",
        "negotiated",
        "--iterations",
        "30",
        # Issue #3799: route under an ITERATION budget instead of the
        # per-net WALL-CLOCK cutoff so the seed-42 route is byte-identical
        # (UUID-normalized) across machines.  --seed only seeds Python's
        # global random; it does NOT control the per-net A* deadline checked
        # in the C++ loop.  On a loaded machine that wall-clock budget fires
        # mid-search and the net lands less copper -- same seed, different
        # copper.  --deterministic-budget (#3538) disables the per-net
        # wall-clock cutoff and pins a fixed node-expansion backstop.
        # --timeout 240 below is retained only as a SAFETY backstop.
        # This MUST stay in sync with generate_design.py:route_pcb()
        # (Issue #3207 no-drift guard,
        # tests/test_board02_route_demo_recipe.py).
        "--deterministic-budget",
        "--timeout",
        "240",
        "--seed",
        "42",
        "--skip-nets",
        ",".join(skip_nets),
        # Issue #3112: pass the manufacturer through so the post-route
        # ``drc_verify_and_nudge`` sweep can consult ``via_in_pad_supported``
        # and slide any same-net via-in-pad escape vias off the offending pad.
        # The default jlcpcb profile does NOT support via-in-pad, so this is
        # the case that exercises the sweep.
        "--manufacturer",
        "jlcpcb",
    ]

    print("\n--- Routing via `kct route` (orchestrator path) ---")
    print(f"  Skipping nets: {skip_nets}")
    print(f"  Command: {' '.join(cmd)}")

    # ``kct route`` streams its own progress output; do NOT capture it.
    # It returns 0 on full success and a non-zero code on partial routing
    # (in which case it still writes the partially-routed PCB).  We capture
    # stdout for net-count parsing by tee-ing through subprocess.PIPE.
    # Issue #3799: pin PYTHONHASHSEED for the route subprocess so any
    # string-keyed dict/set iteration in the negotiated router is
    # reproducible across runner environments (CPython randomizes string
    # hashing per-process otherwise).  Combined with --seed 42 +
    # --deterministic-budget this makes the full pipeline deterministic,
    # mirroring generate_design.py:route_pcb().
    env = os.environ.copy()
    env["PYTHONHASHSEED"] = "42"
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    # Echo subprocess output so users can see routing progress, errors, etc.
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)

    if not output_path.exists():
        print(
            f"\nERROR: `kct route` did not produce {output_path} (exit code {result.returncode})",
            file=sys.stderr,
        )
        return 1

    # Parse the routed-net summary so the demo can print a final tally.
    parsed = _parse_routed_net_count(result.stdout)
    routed, total = parsed if parsed is not None else (None, None)

    # Issue #3264: regenerate the manufacturing bundle so its
    # ``manifest.json`` mtime is newer than the freshly-routed PCB.
    # Otherwise ``kct fleet status`` reports ``ship_ready=false`` with
    # blocker ``"artifacts stale"`` even though the route succeeded.
    # This mirrors the post-route step in
    # ``generate_design.py:export_manufacturing_bundle()``.
    mfg_success = export_manufacturing_bundle(output_path, output_path.parent)

    # Run DRC validation on the routed output.
    print("\n--- DRC Validation ---")
    drc_passed, drc_errors, drc_warnings = run_drc(output_path)
    if drc_passed:
        print("  DRC PASSED")
    else:
        if drc_errors > 0:
            print(f"  Errors:   {drc_errors}")
        if drc_warnings > 0:
            print(f"  Warnings: {drc_warnings}")
        print(f"\n  Run 'kct check {output_path}' for full details")

    # Summary
    print("\n" + "=" * 60)
    if parsed is None:
        print("WARNING: Could not parse routed-net count from `kct route` output")
        all_nets_routed = result.returncode == 0
    else:
        all_nets_routed = routed == total
        print(f"Nets routed: {routed}/{total}")

    if all_nets_routed and drc_passed:
        print("SUCCESS: All nets routed, DRC passed!")
    elif all_nets_routed and not drc_passed:
        print(f"WARNING: All nets routed, but {drc_errors} DRC violation(s) detected!")
        print("  Review DRC errors before manufacturing.")
    else:
        if parsed is not None:
            print(f"PARTIAL: Routed {routed}/{total} nets")
        else:
            print(f"PARTIAL: `kct route` exited with code {result.returncode}")
        if not drc_passed:
            print(f"  Additionally, {drc_errors} DRC violation(s) detected.")
    # Issue #3264: surface mfg bundle freshness so users can see whether
    # fleet status will report ship_ready=true.
    print(f"MFG bundle: {'FRESH' if mfg_success else 'STALE (fleet status will block)'}")
    print("=" * 60)

    # Return success only if all nets routed AND DRC passed.
    return 0 if all_nets_routed and drc_passed else 1


if __name__ == "__main__":
    sys.exit(main())
