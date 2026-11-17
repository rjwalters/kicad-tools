#!/usr/bin/env python3
"""Fleet measurement for the bundle crossing report (Issue #6339). Read-only.

For each ``boards/0*/output/<name>.kicad_pcb`` (unrouted) run the report and,
where a committed ``<name>_routed.kicad_pcb`` exists, take its incomplete /
unrouted signal nets (``NetStatusAnalyzer``) as the "unfinished set":

* hit          flagged net that is unfinished in the committed routed board
* false alarm  flagged net that is complete in the committed routed board
* miss         unfinished net the report did not flag

A net is *flagged* when it is in an inverted pair of a bundle or in an
interleaved hub-column group.  Usage: ``uv run python scripts/measure_bundle_crossings.py``
"""

from __future__ import annotations

import sys
from pathlib import Path

from kicad_tools.analysis.net_status import NetStatusAnalyzer
from kicad_tools.router.bundle_crossings import _is_signal_net, compute_bundle_crossings
from kicad_tools.schema.pcb import PCB


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    chorus = root / "boards/external/chorus-test-revA"
    print(
        f"{'board':34} {'bundles':>7} {'forced':>6} {'ileave':>6} {'flagged':>7} "
        f"{'unfin':>5} {'hit':>4} {'false':>5} {'miss':>4}  note"
    )
    for pcb_path in sorted(root.glob("boards/0*/output/*.kicad_pcb")):
        if pcb_path.stem.endswith("_routed") or "_partial" in pcb_path.stem:
            continue
        rep = compute_bundle_crossings(PCB.load(str(pcb_path)))
        flagged = rep.flagged_nets()
        routed = pcb_path.with_name(pcb_path.stem + "_routed.kicad_pcb")
        label = f"{pcb_path.parent.parent.name}/{pcb_path.stem}"
        if routed.exists():
            res = NetStatusAnalyzer(routed).analyze()
            unfinished = {
                n.net_name for n in res.incomplete + res.unrouted if _is_signal_net(n.net_name)
            }
            hit, false, miss = (
                len(flagged & unfinished),
                len(flagged - unfinished),
                len(unfinished - flagged),
            )
            row = f"{len(unfinished):>5} {hit:>4} {false:>5} {miss:>4}"
            note = ""
        else:
            row = f"{'n/a':>5} {'n/a':>4} {'n/a':>5} {'n/a':>4}"
            note = "no committed routed board"
        print(
            f"{label:34} {len(rep.bundles):>7} {len(rep.flagged_bundles):>6} "
            f"{len(rep.flagged_hubs):>6} {len(flagged):>7} {row}  {note}"
        )
    if not any(chorus.glob("*")):
        print("boards/external/chorus-test-revA: absent in this checkout (skipped)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
