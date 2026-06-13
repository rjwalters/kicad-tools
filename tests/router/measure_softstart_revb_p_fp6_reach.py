"""Alternate measurement harness for P_FP6 reach on softstart rev B (Issue #3390).

This script bypasses the heavyweight consumer test's CLI subprocess
path (which previously timed out at 660 s; see Issue #3390) and
instead drives the autorouter directly in-process.  Use this for ad-hoc
local measurement of the P_FP6 (PR #3389) SOP in-pad rescue and the
PR #3386 U1 LQFP-32 subgrid rescue contributions to softstart rev B
reach.

This is NOT a pytest test -- it is a CLI tool committed to the
``tests/router/`` directory so the measurement is reproducible and
version-controlled alongside the consumer test it complements.

Usage::

    uv run python tests/router/measure_softstart_revb_p_fp6_reach.py

Output captures:

    - Wall-clock for load, route, total.
    - ``nets_routed`` (fully-connected pad-to-pad nets).
    - Per-net status (connected / unconnected) with net name.
    - Number of P_FP6 SOP in-pad rescue log lines.
    - Number of PR #3386 U1 LQFP rescue log lines (best-effort
      pattern match -- absence does not imply the rescue did not fire,
      only that it did not log at INFO level on this path).
    - Identification of dense packages detected by
      ``Autorouter.detect_dense_packages`` (the dispatch gate that
      determines whether P_FP6 SOP rescue can fire).

The architect's pre-PR estimate (#3381 comment) was +3 UCC27211 nets
unlocked at L=2 single-attempt (24/30 -> 27/30).  The empirical
measurement on the PR #3389 + PR #3386 codebase found that the SOP
rescue path is NOT reached during the end-to-end ``route_with_escape``
pipeline because UCC27211 SOIC-8 at 1.27 mm pitch + 0.30 mm trace +
0.20 mm clearance does not pass ``is_dense_package`` (dynamic
threshold = 1.0 mm < 1.27 mm pitch).  See
``test_softstart_revb_p_fp6_dispatcher.py`` for the unit-level
verification that the rescue *would* fire if dispatched.

Issue: https://github.com/rjwalters/kicad-tools/issues/3390
"""

from __future__ import annotations

import io
import logging
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BOARD_DIR = REPO_ROOT / "boards" / "external" / "softstart"


def _regenerate(output_dir: Path) -> Path:
    sys.path.insert(0, str(BOARD_DIR))
    try:
        import generate_design  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)
    output_dir.mkdir(parents=True, exist_ok=True)
    generate_design.create_project(output_dir, "softstart")
    generate_design.create_softstart_schematic(output_dir)
    return generate_design.create_softstart_pcb(output_dir)


def _setup_log_capture() -> io.StringIO:
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(name)s:%(levelname)s:%(message)s"))
    root = logging.getLogger("kicad_tools")
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    return buf


def main(per_net_timeout: float = 30.0, total_timeout: float = 300.0) -> int:
    """Run a softstart rev B reach measurement.

    Args:
        per_net_timeout: Per-A* timeout in seconds.  30 s matches
            the updated consumer test default.
        total_timeout: Overall budget for the main routing phase.

    Returns:
        Process exit code (0 on success).
    """
    os.environ.setdefault("PYTHONHASHSEED", "0")

    # Lazy import so the script can be loaded outside of a uv shell.
    from kicad_tools.router import DesignRules, load_pcb_for_routing

    output_dir = Path("/tmp/softstart_p_fp6_measure")
    t0 = time.time()
    pcb_path = _regenerate(output_dir)
    print(f"PCB regenerated: {pcb_path} ({time.time() - t0:.1f}s)", flush=True)

    rules = DesignRules(
        trace_width=0.30,
        trace_clearance=0.20,
        via_drill=0.3,
        via_diameter=0.6,
        min_trace_width=0.127,
        manufacturer="jlcpcb-tier1",
    )
    skip_nets = [
        "AC_LINE",
        "AC_NEUTRAL",
        "FUSED_LINE",
        "GND",
        "+3.3V",
        "VRECT",
        "SCAP_POS+",
        "SCAP_POS_GND",
        "SCAP_NEG+",
        "SCAP_NEG_GND",
        "ISENSE_POS",
    ]

    log_buf = _setup_log_capture()

    t1 = time.time()
    router, _ = load_pcb_for_routing(
        str(pcb_path),
        skip_nets=skip_nets,
        rules=rules,
    )
    router.rules.manufacturer = "jlcpcb-tier1"
    print(f"Load: {time.time() - t1:.1f}s", flush=True)

    # Identify which packages will reach the escape pre-pass.
    dense_packages = router.detect_dense_packages()
    print(f"\nDense packages detected: {len(dense_packages)}")
    for pkg in dense_packages:
        print(
            f"  {pkg.ref}: {pkg.package_type.name} ({pkg.pin_count} pins, "
            f"pitch={pkg.pin_pitch:.3f} mm)"
        )

    t2 = time.time()
    router.route_with_escape(
        use_negotiated=True,
        per_net_timeout=per_net_timeout,
        timeout=total_timeout,
    )
    route_elapsed = time.time() - t2
    total_elapsed = time.time() - t0
    print(f"\nRoute: {route_elapsed:.1f}s | Total: {total_elapsed:.1f}s", flush=True)

    stats = router.get_statistics()
    print("\n=== REACH SUMMARY ===", flush=True)
    print(f"nets_routed (fully connected): {stats['nets_routed']}", flush=True)
    print(f"total routes emitted: {len(router.routes)}", flush=True)

    # Inspect log buffer for rescue lines.
    log_text = log_buf.getvalue()
    sop_lines = [l for l in log_text.splitlines() if "SOP in-pad rescue" in l]
    print(f"\nP_FP6 SOP in-pad rescues fired: {len(sop_lines)}", flush=True)
    for l in sop_lines:
        print(f"  {l}", flush=True)

    u1_rescue_lines = [l for l in log_text.splitlines() if "in-pad" in l and "U1" in l]
    print(f"\nPR #3386 U1 LQFP-32 rescue mentions: {len(u1_rescue_lines)}", flush=True)
    for l in u1_rescue_lines[:5]:
        print(f"  {l}", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
