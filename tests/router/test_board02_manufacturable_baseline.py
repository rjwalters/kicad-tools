"""Board 02 (charlieplex-led) manufacturability baseline regression guard.

This test pins the **measured manufacturable** state of the
boards/02-charlieplex-led example board against the routing + DRC
pipeline as of June 2026 (post-Wave-3 router fixes: PRs #3247 / #3248
/ #3250 / and the #3214 route_demo subprocess port from #3207).

Baseline measurement at HEAD (worst-of-3 across seeds 42/43/44 with
``kct route --backend cpp``):

- **Routed: 8/8 signal nets (100%)** -- LINE_A-D + NODE_A-D
- **Connected pads: 34/34 (100%)** including GND/VCC via auto-pour
- **DRC: 0 errors, 0 warnings** at ``jlcpcb-tier1`` profile
- **Deterministic output**: 22 routes / 24 vias / 328.16mm total
  length identical across seeds 42/43/44 -- this small 2-layer board
  has fully converged.  Segment count is 397 on macOS-arm64 and 399
  on Linux-x86_64 (platform-dependent collinear resplit at identical
  geometry; see the #3545 PLATFORM NOTE in
  ``test_routing_output_deterministic_across_seeds``).  (387 segments
  before the #3545 static-halo rip-up survival; 193/325.08mm before
  the #3532 45-degree pad-tail doglegs; 299/325.55 before the #3436
  burn-down straightening (#3203/#3510); 206/324.92 before the #3438
  rip-up parity fix; 155/324.66 before the #3433 collision-checker
  scoping; see the re-baseline notes below.)

**Re-verified 2026-06-07** on current main (issue #3292):
- Includes Wave 6/7 PRs #3286 (board-04 NRST refresh), #3288 (plane-net
  classifier narrowing), #3290 (2L stitch when GND pour is cross-layer).
- All 3 cpp seeds (42/43/44) still produce bit-perfect
  22/155/24/324.66mm output, 8/8 nets, DRC PASS at jlcpcb-tier1.
- ``kct fleet status`` reports ``ship_ready=YES`` for board 02.

**Re-verified 2026-06-08** on current main (issue #3334) at HEAD 21076e6c:
- Covers all Wave 9 router PRs landed since the #3292 re-verification:
  #3300 (escape width), #3322 (power-rail alias + per-segment retry),
  #3323 (A* flat arrays), #3324 (layer_to_index goal layer fix),
  #3326 (trace-width-by-impedance), #3328 (board-edge-aware multi-row
  escape), #3329 (deterministic Segment/Via/Zone UUIDs under seed=N),
  #3330 (diff-pair centerline overlap rejection), #3332 (BGA-49 /
  budget-exit diff-pair priority).
- Board 02 has no impedance net classes (the #3326 trace-width-by-
  impedance path does not engage), no diff pairs (#3330/#3332 inactive),
  no multi-row connectors (#3328 inactive), and no escape-width-affected
  topology -- so most Wave 9 changes are no-ops on this board.
- All 3 cpp seeds (42/43/44) still produce bit-perfect
  22/155/24/324.66mm output, 8/8 nets, DRC PASS at jlcpcb-tier1.
- ``kct fleet status`` reports ``ship_ready=YES`` for board 02, manifest
  ``fresh`` (no artifact refresh needed -- the routed PCB and manifest
  from the #3292 verification round remain byte-identical).

**Re-baselined 2026-06-09** for Issue #3433 (collision-checker
overflow-tolerance scoping):
- #3433 scoped ``GridCollisionChecker``'s ``ignore_overflow`` tolerance
  to GENUINELY overused cells (``usage_count > 1``).  Board 02 finishes
  with residual overflow, so the post-route trace optimizer previously
  ran in blanket-tolerant mode and compressed staircases ACROSS clean
  foreign-net traces (the same mechanism that committed board-04's
  -0.200 mm SWCLK/SWO overlaps).  With the tolerance scoped, those
  compressions are correctly declined and more staircase segments are
  retained: 155 -> 206 segments.  Routes (22), vias (24) and reach
  (8/8) are unchanged; total length moves +0.26 mm (324.66 -> 324.92).
  DRC still passes at jlcpcb-tier1 across all 3 seeds.
- This also CONVERGES local (no rtree -> grid checker) behavior with
  rtree-equipped environments (vector checker), whose exact narrow
  phase never honored the blanket tolerance for foreign segments.
- All 3 cpp seeds (42/43/44) produce bit-perfect 22/206/24/324.92mm
  output, 8/8 nets, DRC PASS at jlcpcb-tier1.

Board 02 is the smallest non-trivial routing target in the repo
(~37mm x ~22mm, 10 nets, 34 pads).  The 4 NODE_x charlieplex matrix
nets connect to 4-6 LEDs each in an interleaved pattern -- per
Issue #2432 this is uniquely hostile to the python backend (which
gets 6/8 = 75%), but the cpp backend's negotiated congestion router
handles it cleanly.

The acceptance criteria pinned by this test:

1. CPP routing achieves >= 8/8 signal nets routed (100% reach).
   Regression to 7 or below indicates a foundational A* / negotiated-
   loop regression on small dense topologies -- bisect against
   PRs #3248 (Euclidean via-clearance), #3250 (sub-cell pad-margin),
   and #3247 (auto-fix budget) first.
2. DRC reports 0 errors at jlcpcb-tier1 profile (the consumer
   manufacturer target).  Any non-zero clearance violation count here
   signals a regression in the negotiated loop's clearance handling
   OR a regression in the post-route auto-fix sweep.
3. Output determinism: routes/segments/vias counts stable across
   seeds 42/43/44 (the convergence claim).  This is the bit-perfect
   property recorded above; a non-trivial drift indicates a
   determinism regression (seed plumbing, hash-set iteration order,
   etc.).

History:

- **PR #3036**: closed the original NODE_x routing gap by aligning
  U1 pads to the 0.1mm router grid (Issue #2917 / #2933).
- **PR #3102 + PR #3121**: same-net via-in-pad nudge sweep
  (Issue #3112) -- moves escape vias off LED pads so the jlcpcb
  ``via_in_pad`` rule passes.
- **PR #3163** (Issue #3147): auto-export mfg bundle from the
  ``generate_design.py`` recipes so fleet status reads ``ship_ready``.
- **PR #3214** (Issue #3207): replace bare ``router.route_all()`` in
  ``route_demo.py`` with subprocess ``kct route`` matching the
  ``generate_design.py:route_pcb()`` recipe; established the 8/8 floor
  for the demo path.
- **PR #3247** (Issue #3238): reserve auto-fix budget + structured
  skip signal.
- **PR #3248** (Issue #3232 sibling): Euclidean via-clearance kernel.
- **PR #3250** (Issue #3233): ``_add_pad_unsafe`` sub-cell pad-metal
  margin closure.

Related ongoing work:

- Issue #2432: charlieplex NODE_x nets stall on python backend.  This
  test pins cpp-backend reach only; python backend regression is
  tracked separately.

Runs unconditionally (no slow gate) -- board 02 routing completes in
~6 s wall-clock, which is well within ``pnpm check:ci`` budget.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

# Issue #3436: CI runs the suite with `-n auto --timeout=60`.  These
# tests route real boards (often via subprocess) and comfortably beat
# 60s alone, but under full-suite xdist CPU contention the wall-clock
# reaper killed them spuriously.  The marker overrides the CLI default
# with a contention-tolerant budget; it does NOT slow the happy path.
pytestmark = pytest.mark.timeout(900)


REPO_ROOT = Path(__file__).resolve().parents[2]
BOARD_DIR = REPO_ROOT / "boards" / "02-charlieplex-led"
UNROUTED_PCB = BOARD_DIR / "output" / "charlieplex_3x3.kicad_pcb"

# Acceptance criteria for the post-Wave-3 baseline.
REQUIRED_SIGNAL_NETS_ROUTED = 8  # all 4 LINE_x + 4 NODE_x signal nets
REQUIRED_SIGNAL_NETS_TOTAL = 8

# Issue #3724 (June 15 2026): tightened 4 -> 0.  The 4 grandfathered
# ``clearance_pad_zone`` findings added by #3556 (3x GND pads overlapping
# the VCC F.Cu fill + 1x VCC pad overlapping the GND B.Cu fill, all
# sub-0.01mm at the U1 DIP-8 cluster) were stale-pour-carve copper from
# before the final pad geometry settled.  ``kct zones fill`` regenerated
# the VCC/GND pours against the final trace + pad set (refill-only, no
# copper re-route, the #3712 antipad clearance re-carves the pad antipads),
# clearing all 4.  The copper-union pour audit confirms GND and VCC each
# remain ONE connected copper component after the refill (no stranded
# pads / split fills) and kicad-cli ``unconnected_items`` stays 0.  Both
# the committed-PCB tolerance entry (.github/routed-drc-tolerance.yml) and
# this ceiling are now 0 -- the board is honestly manufacturable.  The
# board's reach + determinism baseline is unchanged (8/8, bit-perfect
# across seeds).
MAX_DRC_ERRORS = 0  # strict 0 gate after #3724 zone refill
GRANDFATHERED_DRC_RULES = {"clearance_pad_zone"}


def _parse_routed_signal_nets(stdout: str) -> tuple[int, int] | None:
    """Extract the canonical ``Nets routed: N/M`` line from kct route output.

    Board 02 has 10 named nets (LINE_A-D + NODE_A-D + GND + VCC), but
    GND is in the skip list (auto-poured into a zone) and VCC is a
    single-pad net handled implicitly, so the router-summary ``Nets
    routed: N/M`` line shows 8/8 for the signal-net topology when fully
    routed.

    Returns ``(routed, total)`` or ``None`` if no match found.
    """
    # The canonical line emitted by the layer-escalation summary is the
    # final one and the most reliable.
    pattern = re.compile(r"Nets routed:\s+(\d+)/(\d+)")
    matches = pattern.findall(stdout)
    if matches:
        # Take the last occurrence (final summary, after any iteration logs).
        last = matches[-1]
        return int(last[0]), int(last[1])
    return None


def _parse_drc_status(stdout: str) -> bool | None:
    """Return True if the kct route output reports ``DRC PASSED``.

    Returns False on explicit ``DRC FAILED``, None if neither is found.
    """
    if "DRC PASSED" in stdout:
        return True
    if "DRC FAILED" in stdout:
        return False
    return None


def _parse_drc_error_rules(stdout: str) -> dict[str, int] | None:
    """Extract the post-route DRC **error** count + per-rule breakdown.

    ``kct route`` emits a ``--- DRC Validation ---`` block of the form::

        --- DRC Validation ---
          ...
          Errors:   4
            - clearance_pad_zone: Short: pad on net 'GND' overlaps ...
            - clearance_pad_zone: Short: pad on net 'GND' overlaps ...
            ...

    When the board has **zero** errors but some non-error (advisory)
    findings, ``kct route`` omits the ``Errors:`` headline entirely and
    instead prints only a ``Warnings: N`` block (issue #3843/#3830 added
    the WARNING-severity ``copper_sliver`` rule, which surfaces a handful
    of ~0.04mm residual copper ribbons on board 02's committed fill --
    these are the committed-fill-vs-kicad-cli-refill divergence, tracked
    behavior, not errors).  We treat that warnings-only block as a clean
    **0-error** result so the strict 0-ERROR ceiling still passes.

    We parse the ``Errors:`` headline count and tally the per-rule
    breakdown ONLY from the ``- <rule_id>:`` lines in the ERROR section
    (warning lines must NOT inflate the error tally).  Returns a dict
    mapping ``rule_id -> count`` (with the synthetic key ``"__total__"``
    set to the headline error count) or ``None`` if no DRC Validation
    block was found at all.

    Used by ``test_drc_clean_at_jlcpcb_tier1`` to allow ONLY the
    grandfathered #3556 ``clearance_pad_zone`` findings through while
    still failing on any other (or excess) ERROR.
    """
    err_match = re.search(r"Errors:\s+(\d+)", stdout)
    if err_match is None:
        # No ``Errors:`` headline.  If the DRC block ran at all (it emits
        # a ``Warnings:`` headline when error-free but advisory-bearing),
        # treat it as a strict 0-error result.  copper_sliver and any
        # other WARNING-severity findings live here and must NOT count as
        # errors -- so we return an empty error breakdown, not None.
        if re.search(r"Warnings:\s+(\d+)", stdout) is not None:
            return {"__total__": 0}
        return None
    total = int(err_match.group(1))
    # Per-rule lines look like ``    - clearance_pad_zone: <message>``.
    # Scope the tally to the ERROR section only: start at the ``Errors:``
    # headline and stop at the next ``Warnings:`` headline (or end of
    # text), so WARNING-severity rules (e.g. copper_sliver) listed under a
    # trailing ``Warnings:`` block never inflate the error breakdown.
    error_section = stdout[err_match.end() :]
    warn_match = re.search(r"^\s*Warnings:\s+\d+", error_section, re.MULTILINE)
    if warn_match is not None:
        error_section = error_section[: warn_match.start()]
    rule_counts: dict[str, int] = {}
    for rule_id in re.findall(r"^\s+-\s+([a-z][a-z_]+):", error_section, re.MULTILINE):
        rule_counts[rule_id] = rule_counts.get(rule_id, 0) + 1
    rule_counts["__total__"] = total
    return rule_counts


@pytest.fixture(scope="module")
def unrouted_pcb_path() -> Path:
    """Verify the committed unrouted board 02 PCB exists for the route run."""
    if not UNROUTED_PCB.exists():
        pytest.skip(
            f"Board 02 unrouted PCB not found at {UNROUTED_PCB!s}; "
            "regenerate via `uv run python boards/02-charlieplex-led/generate_pcb.py`."
        )
    return UNROUTED_PCB


@pytest.fixture(scope="module")
def route_stdout(unrouted_pcb_path: Path) -> str:
    """Run the canonical ``kct route`` invocation for board 02 once per module.

    Mirrors the recipe in ``boards/02-charlieplex-led/generate_design.py``
    (``route_pcb()`` at line ~528) and ``route_demo.py`` (subprocess
    invocation post-PR #3214) but routes to a tmpdir so it never
    overwrites the committed artifact.

    Uses ``--backend cpp`` (the production default) with seed 42 for
    deterministic reproduction.  The bit-perfect-across-seeds property
    is checked separately by ``test_routing_output_deterministic``.
    """
    with tempfile.TemporaryDirectory() as td:
        pcb_copy = Path(td) / "charlieplex_3x3.kicad_pcb"
        shutil.copy2(unrouted_pcb_path, pcb_copy)
        output_path = Path(td) / "charlieplex_3x3_routed.kicad_pcb"
        cmd = [
            sys.executable,
            "-m",
            "kicad_tools.cli",
            "route",
            str(pcb_copy),
            "--output",
            str(output_path),
            "--seed",
            "42",
            "--manufacturer",
            "jlcpcb-tier1",
            "--backend",
            "cpp",
            "--timeout",
            "300",
            "--auto-fix",
            "--auto-fix-passes",
            "2",
        ]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=480,
            check=False,
        )
        # Exit codes from cli/route_cmd.py:
        #   0 = full route + DRC clean
        #   2 = partial routing below --min-completion
        #   3 = >= min-completion but DRC violations remain
        # The current baseline reaches 0; any other code is a regression.
        # Codes 1 and 5 are fatal (crash / unhandled exception).
        if proc.returncode in (1, 5):
            pytest.fail(
                f"kct route returned fatal exit code {proc.returncode}\n"
                f"stderr (last 2000 chars):\n{proc.stderr[-2000:]}\n"
                f"stdout (last 2000 chars):\n{proc.stdout[-2000:]}"
            )
        return proc.stdout


class TestBoard02ManufacturableBaseline:
    """Pin the post-Wave-3 manufacturable baseline for board 02.

    Runs ``kct route --backend cpp`` as a subprocess (the production
    invocation path) and asserts the measured reach + DRC profile
    match the documented baseline.
    """

    def test_all_signal_nets_routed(self, route_stdout: str) -> None:
        """All 8 multi-pad signal nets must reach 100% connectivity.

        The cpp backend's negotiated congestion router achieves 8/8
        on this board's interleaved charlieplex topology.  A
        regression below 8 would indicate one of:
        - A regression in the negotiated rip-up loop in
          ``router/core.py`` (the iteration-1-best-restoration path
          in particular -- board 02 hits the
          ``best-metric early-stop`` branch after 3 iterations).
        - A regression in PR #3248's Euclidean via-clearance kernel
          (more lateral via-escape conflicts on a dense board).
        - A regression in PR #3250's ``_add_pad_unsafe`` sub-cell
          pad-metal margin fix (the U1 DIP-8 cluster is the tightest
          on this board).
        - A regression in PR #3247's auto-fix budget reservation
          (this board exercises auto-fix on the dense NODE_x
          interleaving).

        The python backend currently reaches only 6/8 on this board
        (Issue #2432); this test pins the cpp path only.
        """
        parsed = _parse_routed_signal_nets(route_stdout)
        assert parsed is not None, (
            "Could not find 'Nets routed: N/M' line in kct route output. "
            "Last 2000 chars:\n"
            f"{route_stdout[-2000:]}"
        )
        routed, total = parsed
        assert total == REQUIRED_SIGNAL_NETS_TOTAL, (
            f"Board 02 signal-net total changed from "
            f"{REQUIRED_SIGNAL_NETS_TOTAL} to {total}.  If this is "
            "intentional (e.g. a new signal net added to the design), "
            "update REQUIRED_SIGNAL_NETS_TOTAL in this test and the "
            "baseline-measurement notes in the module docstring."
        )
        assert routed >= REQUIRED_SIGNAL_NETS_ROUTED, (
            f"Board 02 cpp routing regressed to {routed}/"
            f"{REQUIRED_SIGNAL_NETS_TOTAL} (expected >= "
            f"{REQUIRED_SIGNAL_NETS_ROUTED}/{REQUIRED_SIGNAL_NETS_TOTAL}). "
            "See the docstring history for bisect targets."
        )

    def test_drc_clean_at_jlcpcb_tier1(self, route_stdout: str) -> None:
        """The post-route DRC sweep carries only the grandfathered #3556 findings.

        ``kct route`` runs ``drc_verify_and_nudge`` after routing
        (see ``router/drc_nudge.py:1513``) and reports the per-rule
        DRC error breakdown in its ``--- DRC Validation ---`` block.

        Through Wave 3 (PRs #3247-#3250) this board was perfectly clean
        (0 errors) at jlcpcb-tier1 across all 3 seeds.  Issue #3556 then
        added the ``clearance_pad_zone`` rule (pad copper vs foreign-net
        zone fill), which surfaces 4 pre-existing stale-pour-carve shorts
        at the U1 DIP-8 cluster (3x GND-pad-in-VCC-fill + 1x
        VCC-pad-in-GND-fill).  These are GENUINE foreign-net defects, not
        false positives -- the same class grandfathered for boards 04-07
        in ``.github/routed-drc-tolerance.yml`` (#3556).  This test
        therefore allows ONLY the grandfathered ``clearance_pad_zone``
        findings (<= ``MAX_DRC_ERRORS``) through and FAILS on:
        - any NEW rule appearing in the error breakdown (a real
          clearance/auto-fix regression -- bisect PRs #3247-#3250), OR
        - MORE than ``MAX_DRC_ERRORS`` total (the pour-carve cluster grew).

        Burn-down: once the VCC/GND pours are re-carved against the final
        pad geometry (sibling of #3549-#3553), drop ``MAX_DRC_ERRORS``
        back to 0 and remove the #3556 entries here + in the tolerance yml.

        Issue #3843/#3830 (June 20 2026): the new WARNING-severity
        ``copper_sliver`` rule surfaces a handful of ~0.04mm residual
        copper ribbons on board 02's committed fill (the
        committed-fill-vs-kicad-cli-refill divergence -- tracked behavior,
        not errors).  Because the board now has 0 errors but some
        warnings, ``kct route`` prints a ``Warnings: N`` block with NO
        ``Errors:`` headline and no ``DRC PASSED`` line.
        ``_parse_drc_error_rules`` treats that warnings-only block as a
        strict 0-ERROR result, and the error-section scoping keeps
        copper_sliver (and any other WARNING) out of the error breakdown,
        so the strict ``MAX_DRC_ERRORS`` (0) ceiling is preserved.
        """
        # If the route happened to land perfectly clean (e.g. after the
        # pour-carve burn-down), the CLI prints ``DRC PASSED`` and no
        # error block -- accept that as the strict-clean outcome.
        if _parse_drc_status(route_stdout) is True:
            return

        rule_counts = _parse_drc_error_rules(route_stdout)
        assert rule_counts is not None, (
            "Could not find a 'DRC PASSED' status or an 'Errors: N' count "
            "in kct route output -- the DRC step may have crashed before "
            "emitting a status.  Last 2000 chars:\n"
            f"{route_stdout[-2000:]}"
        )

        total = rule_counts.pop("__total__")
        unexpected = {
            rule: count
            for rule, count in rule_counts.items()
            if rule not in GRANDFATHERED_DRC_RULES
        }
        assert not unexpected, (
            "Board 02 post-route DRC at jlcpcb-tier1 has NEW (non-"
            f"grandfathered) error rule(s): {sorted(unexpected)}.  Only "
            f"{sorted(GRANDFATHERED_DRC_RULES)} (#3556 stale-pour-carve) is "
            "tolerated.  A new rule means a real regression -- check the "
            "negotiated loop's clearance handling or the auto-fix sweep "
            "(Issue #3238 / PR #3247).\n"
            f"Last 2000 chars:\n{route_stdout[-2000:]}"
        )
        assert total <= MAX_DRC_ERRORS, (
            f"Board 02 post-route DRC at jlcpcb-tier1 has {total} errors; "
            f"the #3556 grandfathered ceiling is {MAX_DRC_ERRORS} "
            "clearance_pad_zone stale-pour-carve shorts.  The cluster grew "
            "-- either a clearance regression added foreign-net pad-in-pour "
            "overlaps, or the pour geometry drifted.  Inspect the U1 DIP-8 "
            "cluster.\n"
            f"Last 2000 chars:\n{route_stdout[-2000:]}"
        )


def test_routing_output_deterministic_across_seeds(unrouted_pcb_path: Path) -> None:
    """The cpp-backend routing output is bit-perfect across seeds 42/43/44.

    This pins the convergence claim made in the module docstring:
    board 02 is small and the negotiated congestion router converges
    fully, so different seeds produce identical route geometries
    (same segment count, same via count, same total length).

    A failure here means one of:
    - Determinism regressed (e.g. a non-deterministic data structure
      iteration order introduced by a future PR).
    - The convergence assumption is no longer valid (e.g. router logic
      now picks different but equally-good solutions per seed).

    The latter is not necessarily a bug, but the test will catch it
    so we can decide intentionally whether to relax the assertion.
    """
    seeds = [42, 43, 44]
    results: dict[int, tuple[int, int, int, float]] = {}

    for seed in seeds:
        with tempfile.TemporaryDirectory() as td:
            pcb_copy = Path(td) / "charlieplex_3x3.kicad_pcb"
            shutil.copy2(unrouted_pcb_path, pcb_copy)
            output_path = Path(td) / "charlieplex_3x3_routed.kicad_pcb"
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
                "--timeout",
                "300",
                "--auto-fix",
                "--auto-fix-passes",
                "2",
            ]
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=480,
                check=False,
            )
            if proc.returncode in (1, 5):
                pytest.fail(
                    f"kct route returned fatal exit code "
                    f"{proc.returncode} on seed {seed}\n"
                    f"stderr (last 2000 chars):\n{proc.stderr[-2000:]}\n"
                    f"stdout (last 2000 chars):\n{proc.stdout[-2000:]}"
                )
            stdout = proc.stdout

            routes_m = re.search(r"Routes created:\s+(\d+)", stdout)
            segments_m = re.search(r"Segments:\s+(\d+)", stdout)
            vias_m = re.search(r"Vias:\s+(\d+)", stdout)
            length_m = re.search(r"Total length:\s+([\d.]+)mm", stdout)
            assert all([routes_m, segments_m, vias_m, length_m]), (
                f"Could not parse route summary metrics for seed {seed}. "
                f"Last 2000 chars:\n{stdout[-2000:]}"
            )
            assert routes_m is not None
            assert segments_m is not None
            assert vias_m is not None
            assert length_m is not None
            results[seed] = (
                int(routes_m.group(1)),
                int(segments_m.group(1)),
                int(vias_m.group(1)),
                float(length_m.group(1)),
            )

    # Reference values from the documented baseline (seed 42).
    ref = results[seeds[0]]
    for seed in seeds[1:]:
        assert results[seed] == ref, (
            f"Board 02 routing output diverged across seeds: "
            f"seed {seeds[0]}={ref} vs seed {seed}={results[seed]}.  "
            "This means either (a) determinism regressed (a future PR "
            "introduced a non-deterministic data-structure iteration "
            "order) or (b) the convergence assumption is no longer "
            "valid (the router now picks different but equally-good "
            "solutions per seed).  Inspect recent router changes "
            "before relaxing the assertion."
        )

    # Exact baseline numbers (re-baselined 2026-06-11 for Issue #3545:
    # static foreign-pad halo cells now SURVIVE rip-up -- pre-fix the
    # unmark erased them, letting later iterations route through pad
    # clearance halos -- and the negotiated A* treats them as
    # non-negotiable.  Routes, vias, total length and reach are
    # UNCHANGED (22 routes, 24 vias, 328.16mm, full reach, 0 DRC at
    # jlcpcb tier-1); the +10 segment delta (387 -> 397) is collinear
    # re-splitting from the restored halo cells shifting merge
    # boundaries -- the same harmless mode the stale-venv NOTE below
    # documents.  All 3 cpp seeds (42/43/44) bit-perfect at
    # (22, 397, 24, 328.16), measured in a uv.lock-synced venv.
    #
    # PLATFORM NOTE (#3545): the SEGMENT count -- and only the segment
    # count -- diverges between macOS-arm64 (397) and CI Linux-x86_64
    # (399) at identical routes/vias/length/reach and exact cross-seed
    # equality on both platforms.  The collinear-merge pass gates each
    # merge on a float collision probe of the merged candidate
    # (optimizer/algorithms.py merge_collinear -> path_is_clear), and
    # with #3545 those probes now graze static pad-halo boundaries where
    # libm/FMA rounding differs across platforms, flipping two
    # borderline merges.  Same harmless splitting mode as the stale-venv
    # 507-segment NOTE below: identical geometry, different collinear
    # segmentation.  We therefore pin routes/vias/length EXACTLY (these
    # are the regression-sensitive metrics) and allow the segment count
    # a narrow documented band.  The cross-seed equality assertion above
    # remains exact, so within-platform determinism regressions are
    # still caught; only the platform-dependent resplit is tolerated.
    #
    # Prior pin (22, 387, 24, 328.16), re-baselined 2026-06-11 for
    # Issue #3532: 45-degree quantization of the pad-tail emitters.
    # Off-grid pad
    # tails are now emitted as exact two-leg doglegs instead of a single
    # skewed segment (+1 segment per off-grid tail), and the pull-tight
    # pass declines moves that would skew chain neighbours off the
    # 45-degree set (fewer post-merge eliminations).  Routes, vias and
    # reach are UNCHANGED (22 routes, 24 vias, full reach);
    # deterministic across seeds and 0-DRC (the manufacturable-baseline
    # tests in this file still pass, and the new
    # tests/test_fleet_45_census.py enforces 0 off-angle segments on
    # committed artifacts).  Re-measured 2026-06-11 after rebasing onto
    # the #3436 burn-down pin of (22, 193, 24, 325.08) -- which carried
    # the #3203 per-pad channel-budget fix and #3510 grid-re-marking
    # straightening -- all 3 cpp seeds (42/43/44) produce bit-perfect
    # (22, 387, 24, 328.16): the dogleg splits plus the
    # quantization-gated pull-tight account for the segment/length
    # delta vs the straightened pin.  NOTE: measure in a venv synced to
    # uv.lock (`uv sync --extra dev`, what CI uses) -- a stale local
    # venv reproducibly reported 507 segments at the SAME 328.16mm
    # total length (identical geometry, different collinear-segment
    # splitting).  Prior pins: (22, 193, 24, 325.08)
    # re-baselined 2026-06-11 for Issue #3436; (22, 299, 24, 325.55)
    # re-baselined 2026-06-10 for Issue #3438; (22, 206, 24, 324.92)
    # for Issue #3433; (22, 155, 24, 324.66) re-verified 2026-06-07.
    # Pinning these catches "all-seeds drift identically" regressions
    # that the cross-seed equality check above would silently allow
    # (e.g. a router cost-function tweak that improves all seeds in
    # lockstep -- still a measurable regression vs the PR #3265 baseline).
    EXPECTED_ROUTES = 22
    EXPECTED_VIAS = 24
    EXPECTED_LENGTH = 328.16
    # Measured: 397 on macOS-arm64, 399 on CI Linux-x86_64 (see
    # PLATFORM NOTE above).  Band is deliberately tight (+/-3 around
    # the two measured values) so a real collinear-handling regression
    # still trips it.
    EXPECTED_SEGMENTS_RANGE = (394, 402)
    got_routes, got_segments, got_vias, got_length = ref
    exact = (got_routes, got_vias, got_length)
    expected_exact = (EXPECTED_ROUTES, EXPECTED_VIAS, EXPECTED_LENGTH)
    assert exact == expected_exact, (
        f"Board 02 routing baseline drifted: got "
        f"(routes, vias, length)={exact}, expected {expected_exact} "
        f"(full tuple {ref}). This is consistent across seeds (so no "
        "determinism regression), but a real router-output change.  "
        "Investigate the router PRs landed since 2026-06-07 (see PR "
        "#3265 baseline log) and decide whether the change is desired "
        "-- if yes, update the EXPECTED_* pins here AND the docstring "
        "baseline."
    )
    lo, hi = EXPECTED_SEGMENTS_RANGE
    assert lo <= got_segments <= hi, (
        f"Board 02 segment count {got_segments} outside the documented "
        f"platform band [{lo}, {hi}] (macOS-arm64: 397, Linux-x86_64: "
        "399; see PLATFORM NOTE above).  Routes/vias/length matched the "
        "exact pin, so this is a change in collinear segmentation -- "
        "investigate merge_collinear/path_is_clear behaviour before "
        "widening the band."
    )
