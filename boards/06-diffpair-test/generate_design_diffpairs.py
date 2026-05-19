#!/usr/bin/env python3
"""Board 06 driver that uses route_all_with_diffpairs (Issue #3078 reproduction).

This is a verification-only script that lives alongside the original
``generate_design.py``.  It re-uses every piece of the original
pipeline -- net classes, schematic, unrouted PCB, optimization, save
-- and only swaps the routing call:

    OLD: router.route_all(per_net_timeout=30.0, timeout=240.0)
    NEW: random.seed(42); router.route_all_with_diffpairs(
             DifferentialPairConfig(enabled=True)
         )

Before the fix in this branch, this produced 36k+ DRC errors with
catastrophic ``diffpair_clearance_intra`` violations (-0.2mm = full
trace overlap).  After the fix the count should be at most ~32 (the
``route_all`` baseline on board 06 with the existing jlcpcb tier).

DO NOT migrate ``generate_design.py`` itself in this PR -- that's
issue #3071's job.  This script is a one-off harness so the CI run
can stay on the existing ``route_all`` path while we validate the
fix empirically.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# We monkey-patch the router.route_all call inside generate_design's
# main() by importing and replacing it.  Easier than copying 200 lines.
import generate_design  # noqa: E402

from kicad_tools.router import DifferentialPairConfig  # noqa: E402

_original_route_all = None


def _patched_route_all(self, *args, **kwargs):
    """Replacement for Router.route_all in this run only.

    Forces the route_all_with_diffpairs path so we exercise the
    CoupledPathfinder end-to-end with all 9 pairs.
    """
    random.seed(42)
    print("    [issue-3078 patch] using route_all_with_diffpairs(enabled=True)")
    return self.route_all_with_diffpairs(
        diffpair_config=DifferentialPairConfig(enabled=True),
    )


if __name__ == "__main__":
    from kicad_tools.router.core import Autorouter

    _original_route_all = Autorouter.route_all
    Autorouter.route_all = _patched_route_all
    try:
        sys.exit(generate_design.main())
    finally:
        Autorouter.route_all = _original_route_all
