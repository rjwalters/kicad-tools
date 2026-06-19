"""Board 03 (usb-joystick) routing baseline regression guard.

This test pins the **measured routing reach** of
``boards/03-usb-joystick/`` against the ``kct route`` CLI as of
June 2026.

June 9 2026 RE-BASELINE (issue #3410):
    Board 03 was overhauled to close the reach gap to 13/13 + 0 DRC:
    the stale 60x40mm committed board (written by generate_design.py's
    since-removed internal PCB copy) was replaced by the canonical
    ``generate_pcb.py`` 80x60mm layout; J1's USB-C footprint was
    re-spun so same-signal SMT tails exit in the same column (killing
    the structural D+/D- X-crossover); the MST now starts intra-IC
    tie groups from the externally-facing pad; column-aligned USB-C
    connectors skip the escape via-fanout; and auto-pour no longer
    deletes hand-tuned zones (double origin-subtraction bug).  The
    production recipe is ``kct route --seed 42 --manufacturer
    jlcpcb-tier1 --backend cpp --timeout 600`` and measures
    **13/13 nets, 0 DRC errors** at jlcpcb-tier1.  The historical
    notes below document the pre-#3410 state for archaeology.

June 11 2026 (issues #3507/#3454): ``--raw`` dropped from the recipe.
    The optimizer's grid-staleness defect (collision checking against
    pre-optimization copper) was fixed by the grid-transactional
    optimize pass (``optimize_routes_grid_synced``), which retires the
    deterministic XTAL1/XTAL2 ``clearance_segment_via`` merge
    violation that made ``--raw`` load-bearing.  13/13 + 0 DRC holds
    with the TraceOptimizer ON.

Baseline measurement at pre-#3410 HEAD (with ``kct route --backend cpp
--seed 42 --auto-fix --auto-fix-passes 2 --manufacturer jlcpcb-tier1``):

- **Routed: 11/13 nets (85%)** post-#3278 (PR #3300) and post-#3304
  (this ratchet).  USB_D+ and USB_D- remain partial.
- **Layer count: 4** (the 4-layer attempt produces the best result)
- USB_D+ / USB_D- partial due to escape-geometry interactions with J1's
  USB-C connector pad layout.  USB_CC2 recovered from 1/2 -> 2/2 pads
  via the #3304 C++ backend layer-index fix (B.Cu was being mismapped
  to In1.Cu on 4L stacks because of a ``layer.value % num_layers``
  shortcut in ``_route_impl``).
- The committed unrouted PCB has 16 nets total: 13 are routed, 3 are
  power/pour nets (VCC, VBUS, GND) that are skipped by the router and
  served by auto-pour zones instead.

Context (the "1/16" myth):
    The pre-test ``kct fleet status`` output for board 03 reads
    ``incomplete routing (1/16 nets)`` because it queries the stale
    committed ``output/usb_joystick_routed.kicad_pcb`` snapshot.  Live
    routing produces 10/13.  The stale-fleet-status reporting gap is
    tracked separately in #3280.

June 7 2026 re-measurement (issue #3293):
    Re-measured after #3278 landed via PR #3300.  The fix was the
    primary blocker we expected to recover the 11/13 (or better)
    floor, but it initially produced 10/13: the structural
    per-pad escape width fix corrected USB_D+/D- escape geometry,
    but a long-latent C++ backend layer-index mismapping caused
    USB_CC2 to regress from 2/2 -> 1/2 pads.  Issue #3304
    diagnosed the mismapping (B.Cu mapped to In1.Cu on 4L stacks
    via ``layer.value % num_layers``) and the fix recovered the
    11/13 floor.  The committed routed PCB
    (``output/usb_joystick_routed.kicad_pcb``, last updated by PR
    #3195 on June 4) is still STRICTLY BETTER at 12/13 with 4 DRC
    errors on jlcpcb-tier1.  The route_demo.py vs ``kct route``
    reach divergence is tracked in issue #3308.

Manufacturability verdict (June 7 2026, post-#3304):
    Board 03 is NOT JLCPCB-tier1 ship-ready.  After #3304's
    C++ backend layer-index fix, a fresh route at HEAD produces
    11/13 nets (USB_D+, USB_D- partial).  The committed routed
    PCB (the strictly better artifact we ship today) carries
    4 DRC errors on jlcpcb-tier1:
      - 1 USB_D+ stranded pad
      - 1 diffpair_clearance_intra (USB_D+/USB_D-)
      - 1 clearance_segment_via
      - 1 clearance_pad_via
    Recovering ship-ready will require closing out the remaining
    USB_D+/USB_D- partial routes (escape-geometry interactions
    with J1's USB-C row -- separate work; #3308 for the
    route_demo divergence in the meantime).

June 8 2026 refresh attempt (issue #3335):
    Attempted to refresh the committed artifacts using the canonical
    ``generate_design.py:route_pcb()`` recipe (per PR #3327's
    consolidation).  The fresh route landed at 11/13 with 3 DRC
    errors (USB_CC1 + USB_D- partial; USB_D+ now routes fully).
    This is a DIFFERENT failure mode from the committed PCB --
    the committed PCB has USB_D+ partial (DRC=4); the fresh PCB
    has USB_CC1 and USB_D- partial (DRC=3).

    Per-net comparison:
      | Net      | Committed (12/13)     | Fresh (11/13)        |
      | -------- | --------------------- | -------------------- |
      | USB_D+   | partial (2/3 pads)    | COMPLETE             |
      | USB_D-   | COMPLETE              | partial (2/3 pads)   |
      | USB_CC1  | COMPLETE              | partial (1/2 pads)   |
      | USB_CC2  | COMPLETE              | COMPLETE             |
      | VBUS     | COMPLETE              | COMPLETE             |

    The fresh route trades USB_D+ partial (committed) for USB_CC1 +
    USB_D- partial (refresh).  Net count regresses 12 -> 11, so the
    committed PCB remains strictly better in routing reach.  The
    refresh was NOT shipped (per PR #3273 lesson: do not ship
    artifacts that regress committed reach).

    Schematic drift is structural and unaffected by refresh:
    the schematic represents the USB connector with a simplified
    4-pin USB_PIN_MAP (VCC/USB_D-/USB_D+/GND) while the PCB
    places a full 16-pin USB Type-C connector with VBUS/USB_CC1/
    USB_CC2 net assignments.  ``kct fleet status`` reports the
    drift as "13 nets in schematic, 16 in PCB" (3 added: VBUS,
    USB_CC1, USB_CC2).  Clearing the drift requires either:
      (a) adding VBUS/USB_CC1/USB_CC2 global_label calls to the
          schematic generator in ``create_usb_joystick_schematic``
          (out of scope for the refresh-only ticket; tracked
          separately under "schematic gap" follow-ups), or
      (b) the unlabeled-local-net headroom being raised from 2
          to >= 3 in ``_DRIFT_ADDED_ONLY_TOLERANCE`` (a fleet-side
          fix; would mask drift more broadly).

Known follow-on issues that prevent a higher baseline:
    - **#3278** (closed by PR #3300): Escape generator used
      ``pads[0].net_name``'s net-class trace width for the whole
      row, pulling Power-class 0.5mm width into USB_D+/USB_D-
      HighSpeed escapes.  Fixed by per-pad ``escape_width``.
    - **#3304** (THIS RATCHET): The C++ backend's ``_route_impl``
      computed search-time goal layer as
      ``end.layer.value % num_layers``.  For ``B.Cu`` (value=5)
      on a 4L stack (num_layers=4), this gave index 1 (In1.Cu)
      instead of 3 (B.Cu).  The narrower post-#3278 escape stub
      stopped blocking the main router's path so the A* found
      the wrong-layer goal cell.  Replaced with the canonical
      ``self._grid.layer_to_index`` lookup that
      ``Router.route`` in ``pathfinder.py`` already used.
    - **#3279**: 2-layer boards with GND pour on B.Cu have no
      pipeline step to stitch F.Cu SMD GND pads to the pour, so
      ``kct check`` reports ``Net 'GND' is partially routed: 26 of 29
      pads stranded``.  Routing itself is fine; it's a pipeline gap.
      Partially closed by PR #3290 (cross-layer stitch detection); 5
      stranded GND pads remain due to stitch-clearance refusals.
    - **#3308**: ``kct route`` (and ``route_demo.py``) on board 03
      regressed between June 4 and June 7 — USB_D+ went from 2/3
      connected to 1/3, USB_D- went from fully routed to 1/3, and
      USB_CC1/CC2 are now partial.  Bisection between PRs #3197 and
      #3290 is needed.

Acceptance criteria pinned by this test:

1. **Reach floor**: ``kct route`` produces >= 11 routed signal nets
   (out of 13).  Drops to 10 or fewer indicate a routing-quality
   regression on USB-C-class pad-density boards.  The floor was
   temporarily relaxed from 11 to 10 by PR #3300 then ratcheted
   back to 11 by the PR that closes #3304 (C++ backend layer-
   index mismapping fix).
2. **Deterministic across seeds**: seeds 1 / 42 / 99 all produce the
   same routed-net count, so a single-seed run is a reliable
   indicator of overall quality.
3. **The 1/16 myth stays dead**: if the test ever measures <= 1 net
   routed, somebody re-broke board 03 in a non-trivial way.
4. **Committed-PCB ship quality**: the committed
   ``usb_joystick_routed.kicad_pcb`` must not silently degrade past
   the 4-error / 12-net-connected ceiling without the change being
   acknowledged in this docstring.  See ``test_committed_pcb_drc_state``.

References:
    - Parent tracking issue: #3259
    - June 7 refresh tracking issue: #3293
    - June 8 refresh attempt tracking issue: #3335
    - Stale fleet-status reporting: #3280
    - Escape clearance bug (fixed): #3278 (PR #3300)
    - Main-router USB_CC2 regression follow-up: #3304
    - 2-layer pour stitching gap: #3279 (PR #3290 partial fix)
    - route_demo regression: #3308 (closed by PR #3327 recipe consolidation)
    - Existing board-03 demo-path test: tests/test_board_03_regression.py
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BOARD_DIR = REPO_ROOT / "boards" / "03-usb-joystick"
UNROUTED_PCB = BOARD_DIR / "output" / "usb_joystick.kicad_pcb"
COMMITTED_ROUTED_PCB = BOARD_DIR / "output" / "usb_joystick_routed.kicad_pcb"

# Acceptance criteria for the June 2026 baseline.
#
# 13 signal nets total (USB_CC1, USB_CC2, USB_D+, USB_D-, XTAL1, XTAL2,
# JOY_X, JOY_Y, JOY_BTN, BTN1, BTN2, BTN3, BTN4).  The 3 pour/power
# nets (VCC, VBUS, GND) are auto-skipped and served by zones; they do
# NOT appear in the ``Nets routed: N/M`` line.
#
# Post-#3278 escape contract correction (landed via PR #3300): the
# per-pad ``escape_width`` fix corrected fine-pitch HighSpeed escapes
# (USB_D+ improved 1/3 -> 2/3 pads), but the main router's A* path-finder
# initially saw a different B.Cu channel topology around J1 and regressed
# USB_CC2 from 2/2 -> 1/2 pads (10/13 fresh-route reach).
#
# Issue #3304 (THIS RATCHET): the regression was traced to a layer-
# index mismapping in the C++ backend's ``_route_impl`` -- when a
# destination virtual-pad sat on ``B.Cu`` (the inner-escape-layer
# fallback the 4L SIG-GND-PWR-SIG stack picks when no inner SIGNAL
# layer exists), the C++ A* used ``layer.value % num_layers`` to
# compute the goal layer.  For B.Cu (enum value=5) on a 4L stack
# (num_layers=4), the modulo gave index 1 (In1.Cu) instead of the
# correct index 3 (B.Cu).  The A* then terminated on In1.Cu, the
# escape stub laid down on B.Cu, and the union-find connectivity
# check saw the pad as disconnected.  Replacing the modulo with
# the canonical ``self._grid.layer_to_index`` lookup restores
# USB_CC2 to 2/2 pads and lifts board 03 to 11/13.  USB_D+ /
# USB_D- still defer to the main router due to escape-geometry
# interactions with J1's USB-C row (separate work).
# Issue #3410 (June 9 2026): the re-spun J1 footprint + MST tie-group
# representative fix + escape-defer for column-aligned USB-C lifted the
# fresh-route reach to 13/13.  The floor is the FULL net population --
# any regression below 100% on this board is a reach regression.
REQUIRED_NETS_ROUTED = 13
EXPECTED_TOTAL_NETS = 13

# Committed-PCB ceiling pinned by the June 7 2026 measurement.  The
# committed ``usb_joystick_routed.kicad_pcb`` (last updated by PR
# #3195 on June 4) is strictly better than a fresh route at HEAD
# (12/13 with 4 DRC errors vs a fresh canonical-recipe route of
# 11/13 with 3 DRC errors — see #3304 for USB_CC2 main-router
# regression and #3308 for the now-resolved route_demo divergence).
# Re-confirmed June 8 2026 (#3335 refresh attempt): the fresh route
# routes USB_D+ but strands USB_CC1 and USB_D-, a different failure
# mode but same net-count regression.  We assert that the committed
# file cannot silently degrade past this ceiling without somebody
# re-running the recipe AND updating these numbers in the same commit.
# Issue #3410 (June 9 2026): the committed routed PCB is regenerated by
# the production recipe and is DRC-CLEAN at jlcpcb-tier1 (0 errors).
# The only BY-RULE entries are silkscreen_text_height WARNINGS (0402
# value text on the demo passives); the breakdown parser below sees
# warnings too, so the rule allowlist names it explicitly.
# Issue #3527 (June 11 2026): the new ``clearance_segment_zone`` rule
# (segments vs foreign-net zone *fill* copper) surfaced 4 pre-existing
# stale-fill shorts in the committed artifact (BTN3 vs the GND B.Cu
# fill).  These were always in the copper -- the gate simply could not
# see them before the rule existed.
# Issue #3551 (June 11 2026): ``kct zones fill`` regenerated the pours
# against the final trace set (refill-only -- segment/via copper is
# byte-identical to the pre-fix artifact), clearing all 4 shorts.  The
# ceiling is back at 0 and the rule is dropped from the allowlist set.
# Issue #3556 (June 13 2026): the new ``clearance_pad_zone`` rule (pad
# copper vs foreign-net zone *fill* copper -- the pad sibling of #3527's
# segment-vs-fill rule) surfaces 14 pre-existing findings on the
# committed artifact: foreign-net pads (GND/JOY_*/XTAL1/XTAL2/VCC vs the
# VCC F.Cu and GND B.Cu pours) overlapping the opposite-net fill by
# 0.003-0.008mm at the dense MCU/connector cluster.  These are GENUINE
# stale-pour-carve shorts (the fill was not re-carved after the final
# pad geometry settled), the same legitimate class grandfathered for
# boards 04-07 in ``.github/routed-drc-tolerance.yml`` -- NOT false
# positives (the rule correctly skips same-net fills).  They were
# invisible before #3556 because no gate compared pad copper to zone
# fill.  Ceiling raised 0 -> 14 and the rule added to the allowlist set;
# burn-down (re-fill the VCC/GND pours against the final pads, sibling of
# #3549-#3553) drops it back to 0.
# Issue #3730 (June 15 2026): the burn-down landed.  ``kct zones fill``
# regenerated the VCC/GND pours against the final pad set (applying the
# merged #3728 solid-thermal + #3725 island-removal logic, refill-only --
# no copper re-route), clearing all 14 ``clearance_pad_zone`` findings and
# the 11 ``starved_thermal`` findings.  Connectivity preserved (kicad-cli
# ``unconnected_items`` 15 -> 14, 0 ``isolated_copper``).  Fresh
# measurement: 0 errors under both ``kicad-cli pcb drc`` and ``kct check
# --mfr jlcpcb-tier1``.  Ceiling tightened 14 -> 0 and ``clearance_pad_zone``
# dropped from the allowlist set; the board-03 entry is removed from
# ``.github/routed-drc-tolerance.yml`` (absence = strict 0 gate).  The only
# BY-RULE entries remaining are ``silkscreen_text_height`` WARNINGS.
MAX_COMMITTED_DRC_ERRORS = 0
EXPECTED_COMMITTED_DRC_RULES = {
    "silkscreen_text_height",
}


def _parse_routed_net_count(stdout: str) -> tuple[int, int] | None:
    """Extract the final ``Nets routed: N/M`` count from kct route output.

    The expected summary block contains a line of the form::

        Nets routed:     11/13

    (Multiple matches may exist in escalation mode; we return the LAST
    one since that reflects the final state the router landed on.)

    Returns ``(routed, total)`` or ``None`` if no such line was found.
    """
    pattern = re.compile(r"Nets routed:\s+(\d+)/(\d+)")
    matches = pattern.findall(stdout)
    if not matches:
        return None
    routed, total = matches[-1]
    return int(routed), int(total)


def _parse_per_net_status(stdout: str) -> dict[str, str]:
    """Extract per-net routing status from ``kct route`` ROUTING PREVIEW output.

    Issue #3308 AC #4: the existing baseline test only asserts on the
    aggregate ``Nets routed: N/M`` headline.  Per-net assertions for
    USB_D+/USB_D-/USB_CC1/USB_CC2 guard against a future regression
    where the headline holds but the specific high-value nets quietly
    flip from routed to unrouted.

    ``kct route`` emits a per-net block under the ``ROUTING PREVIEW``
    header of the form::

        Net: USB_D+
          Layers:   F.Cu -> B.Cu
          Length:   12.34mm
          Segments: 7, 1 via(s)
          Status:   <check>  Routed

    or (for unrouted nets)::

        Net: USB_CC1
          Status:   <x>  No path found

    We parse the ``Net:`` lines and the corresponding ``Status:`` lines
    into a ``{net_name: status}`` dict where status is one of
    ``"routed"`` or ``"unrouted"``.  Nets with neither a Routed nor a
    No-path status block are omitted (e.g. skipped power-pour nets).

    The escalation mode can emit multiple ROUTING PREVIEW blocks; we
    take the LAST occurrence of each net (the final state the router
    landed on, matching the convention in ``_parse_routed_net_count``).
    """
    result: dict[str, str] = {}
    # Iterate through ``Net: NAME`` lines and look at the next few lines
    # for a Status: line.  Use re.finditer to get the position of each
    # Net: header so we can scan forward a bounded distance.
    net_header_pattern = re.compile(r"^Net:\s+(\S+)\s*$", re.MULTILINE)
    status_routed = re.compile(r"Status:\s+\S+\s+Routed", re.MULTILINE)
    status_unrouted = re.compile(r"Status:\s+\S+\s+No path found", re.MULTILINE)

    headers = list(net_header_pattern.finditer(stdout))
    for idx, header in enumerate(headers):
        net_name = header.group(1)
        # Scan from this header to the next (or end of stdout) for a
        # Status: line.  Bound the window so we don't accidentally pull
        # in a status line from a later net.
        start = header.end()
        end = headers[idx + 1].start() if idx + 1 < len(headers) else len(stdout)
        window = stdout[start:end]
        if status_routed.search(window):
            result[net_name] = "routed"
        elif status_unrouted.search(window):
            result[net_name] = "unrouted"
        # else: skipped or absent -- don't pollute the result dict
    return result


@pytest.fixture(scope="module")
def unrouted_pcb_path() -> Path:
    """Verify the committed unrouted board 03 PCB exists.

    The PCB is committed under ``boards/03-usb-joystick/output/``.  If
    the file is missing the test cannot run -- skip with a clear message.
    """
    if not UNROUTED_PCB.exists():
        pytest.skip(
            f"Board 03 unrouted PCB not found at {UNROUTED_PCB!s}; "
            "regenerate via `uv run python boards/03-usb-joystick/generate_pcb.py`."
        )
    return UNROUTED_PCB


def _run_kct_route(unrouted: Path, seed: int) -> str:
    """Run ``kct route --backend cpp --seed N --auto-fix`` and capture stdout.

    Mirrors the recipe in the parent issue (#3259) and the standard
    fleet/build invocation.  Routes to a tmpdir so it never overwrites
    the committed artifact.
    """
    with tempfile.TemporaryDirectory() as td:
        pcb_copy = Path(td) / "usb_joystick.kicad_pcb"
        shutil.copy2(unrouted, pcb_copy)
        output_path = Path(td) / "usb_joystick_routed.kicad_pcb"
        cmd = [
            sys.executable,
            "-m",
            "kicad_tools.cli",
            "route",
            str(pcb_copy),
            "--output",
            str(output_path),
            "--seed",
            str(seed),
            "--manufacturer",
            "jlcpcb-tier1",
            "--backend",
            "cpp",
            # Issue #3799: kept in lock-step with the production recipe in
            # ``generate_design.py:route_pcb()`` -- --deterministic-budget
            # (#3538) routes under a fixed iteration backstop instead of the
            # per-net wall-clock cutoff, so the seed-42 re-route is
            # byte-identical (UUID-normalized) across machines.
            "--deterministic-budget",
            "--timeout",
            "600",
            # Issues #3507/#3454: ``--raw`` removed in lock-step with the
            # production recipe in ``generate_design.py:route_pcb()`` --
            # the grid-transactional optimize pass retired the
            # deterministic clearance_segment_via merge violation that
            # made it load-bearing.  Keep these flag lists in sync.
        ]
        # Issue #3799: pin PYTHONHASHSEED to match the production recipe so
        # the baseline measured here is the SAME copper the recipe emits.
        _route_env = os.environ.copy()
        _route_env["PYTHONHASHSEED"] = "42"
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
            env=_route_env,
        )
        # Exit codes from cli/route_cmd.py:
        #   0 = full route + DRC clean
        #   2 = partial routing below --min-completion
        #   3 = >= min-completion but DRC violations remain
        # Board 03 lands at 2 or 3 (partial + DRC).  Codes 1 and 5 are
        # fatal (crash / SIGINT).
        if proc.returncode in (1, 5):
            pytest.fail(
                f"kct route returned fatal exit code {proc.returncode}\n"
                f"stderr (last 2000 chars):\n{proc.stderr[-2000:]}\n"
                f"stdout (last 2000 chars):\n{proc.stdout[-2000:]}"
            )
        return proc.stdout


@pytest.fixture(scope="module")
def route_stdout(unrouted_pcb_path: Path) -> str:
    """Run the canonical ``kct route`` invocation once per module."""
    return _run_kct_route(unrouted_pcb_path, seed=42)


@pytest.mark.slow
class TestBoard03RoutingBaseline:
    """Pin the June 2026 routing reach baseline for board 03.

    The full CLI subprocess takes ~2.5 minutes wall-clock (timeout 600 s
    on the route step + setup/teardown), so the class is marked
    ``@pytest.mark.slow``.  The nightly slow-tests workflow picks it up;
    PR-time CI skips it by default.
    """

    def test_reach_meets_floor(self, route_stdout: str) -> None:
        """``kct route --backend cpp`` produces 13/13 nets routed (issue #3410).

        This is the post-#3278 / post-#3304 baseline (PR #3300 corrected
        the per-pad escape width; #3304 fixed the C++ backend layer-
        index mismapping that prevented USB_CC2 from re-connecting).
        USB_D+/USB_D- still defer to the main router due to escape
        geometry on J1.  A regression below 11 means the router lost
        ground on a USB-C-class board -- bisect against escape /
        diff-pair / negotiated-loop changes and the layer_to_index
        invariant in ``tests/router/test_cpp_backend_layer_mapping.py``.
        """
        parsed = _parse_routed_net_count(route_stdout)
        assert parsed is not None, (
            "Could not find 'Nets routed: N/M' line in kct route stdout.  "
            "This typically means the router crashed before producing a "
            "summary.\n"
            f"stdout (last 4000 chars):\n{route_stdout[-4000:]}"
        )
        routed, total = parsed
        assert total == EXPECTED_TOTAL_NETS, (
            f"Board 03 routable-net count changed from {EXPECTED_TOTAL_NETS} "
            f"to {total}.  The schematic/PCB generator may have added or "
            "removed nets; update EXPECTED_TOTAL_NETS and REQUIRED_NETS_"
            "ROUTED to match the new topology after re-baselining."
        )
        assert routed >= REQUIRED_NETS_ROUTED, (
            f"Board 03 routing reach regressed: routed {routed}/{total}, "
            f"expected >= {REQUIRED_NETS_ROUTED}/{EXPECTED_TOTAL_NETS} "
            "(post-#3278 / post-#3304 baseline).  Common regression "
            "sources to bisect:\n"
            "  - escape clearance / lateral_offset changes for USB-C "
            "(escape contract is post-#3278)\n"
            "  - C++ backend layer-index mapping in ``_route_impl`` "
            "(post-#3304; should use ``layer_to_index`` not modulo)\n"
            "  - negotiated-loop rip-up policy on BLOCKED_BY_COMPONENT\n"
            "  - per-pad channel budget for J1's 12 SMT signal pads\n"
            "  - any change to ``_create_intra_ic_routes`` that affects "
            "diff-pair partner consolidation on the same package."
        )

    def test_per_net_reach_usb_signals(self, route_stdout: str) -> None:
        """Zero partially-connected nets -- the per-net USB contract.

        Issue #3308 AC #4 wanted per-net reach pins for the four USB-C
        signals.  The original implementation parsed ``Net:``/``Status:``
        blocks from a "ROUTING PREVIEW" section -- but ``kct route``
        only emits that section under the INTERACTIVE ``--preview``
        flag, so the parser never saw any per-net lines and the test
        failed on every nightly run (silently: the slow-tests workflow
        does not gate on the pytest exit code).

        Issue #3410 replaces the broken stdout contract with the
        summary the CLI actually prints::

            Nets routed:     13/13
            Partial routes:  0/13 -- have segments, not all pads connected
            Unrouted:        0/13 -- no segments at all

        At the post-#3410 baseline the reach floor is the FULL net
        population (13/13, ``test_reach_meets_floor``), so the per-net
        USB contract collapses to "zero partial + zero unrouted":
        any USB net regression (the historical failure modes were
        USB_D+/USB_D-/USB_CC1/USB_CC2 stranding in every combination)
        necessarily surfaces as a non-zero partial/unrouted count.  When a
        regression fires, the ``Partially connected nets:`` block in
        the same output names the exact nets.
        """
        partial = re.search(r"Partial routes:\s+(\d+)/(\d+)", route_stdout)
        assert partial is not None, (
            "Could not find 'Partial routes: N/M' summary line in kct "
            "route stdout -- the CLI summary format changed; update "
            "this parser alongside _parse_routed_net_count.\n"
            f"stdout (last 4000 chars):\n{route_stdout[-4000:]}"
        )
        partial_count = int(partial.group(1))

        unrouted = re.search(r"Unrouted:\s+(\d+)/(\d+)", route_stdout)
        unrouted_count = int(unrouted.group(1)) if unrouted else 0

        # Name the offenders when the contract breaks.
        offenders = re.findall(r"^\s{4}(\S+): \d+/\d+ pads connected", route_stdout, re.M)

        assert partial_count == 0 and unrouted_count == 0, (
            f"Board 03 has {partial_count} partial + {unrouted_count} "
            f"unrouted net(s) at HEAD (post-#3410 contract is 0/0; the "
            f"historical offenders were the USB-C signals).  Offending "
            f"net(s): {offenders or 'see stdout'}.  Bisect against:\n"
            "  - reduce_pads_after_intra_ic (tie-group representative, "
            "#3410)\n"
            "  - _escape_usb_c_connector column-aligned defer (#3410)\n"
            "  - auto_pour _detect_uninset_zones (zone preservation, "
            "#3410)\n"
            "  - the C++ layer_to_index invariant (#3304)."
        )

    def test_the_1_of_16_myth_stays_dead(self, route_stdout: str) -> None:
        """If routing reach ever drops to 1 or fewer, somebody broke it badly.

        The pre-test ``kct fleet status`` reported board 03 as
        ``1/16 nets routed`` based on a stale committed routed PCB
        snapshot.  Live routing produces 10/13.  This test guards against
        accidentally re-introducing a catastrophic regression: anything
        that drops the live count to 1 or below is a hard fail.

        See #3280 for the fleet-status staleness gap that gave rise to
        the original "1/16" report.
        """
        parsed = _parse_routed_net_count(route_stdout)
        assert parsed is not None, "Could not parse routed net count; see test_reach_meets_floor"
        routed, _total = parsed
        assert routed > 1, (
            f"Board 03 live routing produced {routed} routed nets — "
            "the catastrophic baseline that #3259 was opened to prevent.  "
            "Either a router fix landed that regressed J1's escape "
            "geometry catastrophically, or the unrouted PCB is missing "
            "the destination MCU U1 (in which case generate_pcb.py "
            "needs to be regenerated)."
        )


@pytest.mark.slow
def test_routing_reach_deterministic_across_seeds(unrouted_pcb_path: Path) -> None:
    """The same reach (13/13 post-#3410) is produced for seeds 1, 42, and 99.

    The negotiated A* router uses the global seed for tie-breaks during
    A* and for the rip-up selection in BLOCKED_BY_COMPONENT.  For a
    USB-C-class board where the bottleneck is escape geometry (not A*
    tie-breaks), the reach should be stable across seeds.  If a future
    change introduces seed sensitivity, that's a determinism regression
    worth investigating.

    Marked slow because it runs ``kct route`` THREE times (~7.5 minutes
    wall-clock).
    """
    counts = {}
    for seed in (1, 42, 99):
        stdout = _run_kct_route(unrouted_pcb_path, seed=seed)
        parsed = _parse_routed_net_count(stdout)
        assert parsed is not None, (
            f"Could not parse routed net count for seed {seed}; "
            f"stdout (last 4000 chars):\n{stdout[-4000:]}"
        )
        counts[seed] = parsed[0]
    unique_counts = set(counts.values())
    assert len(unique_counts) == 1, (
        f"Board 03 routing reach is NOT seed-stable: got {counts!r}.  "
        "Variance across seeds indicates the A* tie-break or the "
        "BLOCKED_BY_COMPONENT rip-up order is now load-bearing for the "
        "USB_D+/USB_D- failure mode.  Either the escape geometry is "
        "fragile in a new way, or the negotiated loop's stall detection "
        "introduced seed-dependence.  Investigate before relaxing this "
        "assertion."
    )
    # And the seed-stable count must be at the floor.
    routed = next(iter(unique_counts))
    assert routed >= REQUIRED_NETS_ROUTED, (
        f"Seed-stable count {routed} is below the floor of "
        f"{REQUIRED_NETS_ROUTED}; see test_reach_meets_floor for "
        "bisection guidance."
    )


def test_committed_pcb_drc_state() -> None:
    """The shipped routed PCB does not silently degrade past its June 7 ceiling.

    Background: the committed ``output/usb_joystick_routed.kicad_pcb``
    was last updated by PR #3195 on June 4 2026.  A fresh ``kct
    route`` invocation against the same unrouted PCB on June 7
    produces a STRICTLY WORSE result (10/13 vs 12/13; USB_D+/USB_D-
    both partial vs only USB_D+; USB_CC2 partial vs OK — the
    USB_CC2 regression is tracked under #3304 as a follow-on to
    #3278/PR #3300, and the route_demo divergence under #3308).
    Until those are bisected and fixed, the committed file is the
    canonical "best known" routed PCB for board 03 and we MUST guard
    against its quality silently degrading.

    This test runs the pure-Python DRC pass with the jlcpcb-tier1
    profile and asserts:

    - <= 4 errors (matches the June 7 measurement: 1 connectivity,
      1 diffpair_clearance_intra, 1 clearance_segment_via,
      1 clearance_pad_via).
    - The rule mix is bounded: any NEW rule appearing in the error
      breakdown that isn't in the expected set is a regression
      (e.g. a fresh ``via_in_pad`` error would indicate the PCB
      slipped past tier-1 capabilities).

    This is a FAST test (no routing) — it just parses an existing
    artifact, so it runs in PR CI, not slow.

    See issues #3293 (refresh tracking), #3304 (USB_CC2 main-router
    regression), and #3308 (route_demo regression) for the full
    context.
    """
    if not COMMITTED_ROUTED_PCB.exists():
        pytest.skip(
            f"Committed routed PCB not found at {COMMITTED_ROUTED_PCB!s}; "
            "this test guards the shipped artifact's DRC state."
        )

    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "check",
        str(COMMITTED_ROUTED_PCB),
        "--mfr",
        "jlcpcb-tier1",
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    stdout = proc.stdout

    # Parse the error count from the "Errors:     N" line.
    err_match = re.search(r"Errors:\s+(\d+)", stdout)
    assert err_match is not None, (
        "Could not parse error count from 'kct check' output.  Did the "
        "CLI output format change?\n"
        f"stdout (last 2000 chars):\n{stdout[-2000:]}"
    )
    err_count = int(err_match.group(1))
    assert err_count <= MAX_COMMITTED_DRC_ERRORS, (
        f"Committed routed PCB has {err_count} DRC errors on "
        f"jlcpcb-tier1; ceiling is {MAX_COMMITTED_DRC_ERRORS} "
        "(June 7 2026 baseline, issue #3293).  Either:\n"
        "  - a router fix that landed regressed the committed PCB "
        "without regenerating it (regenerate the routed PCB), OR\n"
        "  - the route_demo regression in #3308 leaked into the "
        "committed file (revert the committed PCB), OR\n"
        "  - the manufacturer profile tightened (legitimate ceiling "
        "bump — update MAX_COMMITTED_DRC_ERRORS in the same commit).\n"
        f"Full stdout (last 4000 chars):\n{stdout[-4000:]}"
    )

    # Parse the "BY RULE:" breakdown and assert the rule set is bounded.
    by_rule_match = re.search(
        r"BY RULE:\s*\n((?:\s+\w[\w_]*: \d+ \w+\s*\n)+)",
        stdout,
    )
    if by_rule_match:
        rules_seen = set(re.findall(r"\b([a-z][a-z_]+):\s+\d+\b", by_rule_match.group(1)))
        # All rules seen must be in our expected set; new rules indicate
        # a new failure mode worth investigating.
        unexpected = rules_seen - EXPECTED_COMMITTED_DRC_RULES
        assert not unexpected, (
            f"Committed routed PCB has NEW DRC rules failing on "
            f"jlcpcb-tier1: {sorted(unexpected)}.  The June 7 2026 "
            f"baseline only had {sorted(EXPECTED_COMMITTED_DRC_RULES)}.  "
            "A new failure mode means the PCB drifted off the known "
            "tier-1 ceiling — investigate the new rule(s) before "
            "expanding EXPECTED_COMMITTED_DRC_RULES.\n"
            f"stdout (last 4000 chars):\n{stdout[-4000:]}"
        )
