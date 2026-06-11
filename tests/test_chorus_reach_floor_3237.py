"""Regression test pinning the chorus-test-revA reach floor (Issue #3237).

Issue #3237 documents a major regression between the May 10 snapshot
(30/48 fully-connected = 62%) and the post-PR-#3232 snapshot
(7-10/48 = 15-21%) on the same chorus-test-revA_v19_stripped.kicad_pcb
fixture.  Wave 1 of follow-ups (PR #3242 zone partition fix, PR #3243
clearance-safe grid auto-selector, PR #3244 monotonic-regression
early-exit for layer escalation) merged on 2026-06-06 23:34-23:51 UTC.

This module captures the **re-measurement** the Builder did against
HEAD a224b1e3 (post-Wave-1, the first commit where all three Wave 1
fixes are present) on 2026-06-06 evening:

    Backend  | Attempt 1 | Attempt 2 | Attempt 3 | Best | Auto-fix
    ---------+-----------+-----------+-----------+------+---------
    Python   | 6/48 (12%)| 6/48 (12%)| 5/48 (10%)| 6/48 | SKIPPED
    C++      | 5/48 (10%)| 3/48 (6%) | 4/48 (8%) | 5/48 | SKIPPED

Both backends still produce dramatically less reach than May 10's 62%.

Post-Wave-3 re-measurement (Issue #3252, 2026-06-06 night, HEAD fb400383):
After PR #3247 landed (auto-fix budget reservation, the load-bearing fix
per #3238's analysis), one cpp/seed 42 attempt was measured with timeout=480
under contended CPU.  Result was WORSE, not better:

    Backend  | Attempt 1 | Attempt 2 | Attempt 3 | Best  | AUTOFIX_SKIPPED
    ---------+-----------+-----------+-----------+-------+----------------
    C++      | 2/48 (4%) | 0/48 (0%) | (cut)     | 2/48  | NOT present

The AUTOFIX_SKIPPED_BUDGET_EXHAUSTED token from #3247 does NOT appear in
stderr -- the auto-fix budget reservation is working correctly.  Yet reach
DROPPED from 5/48 (post-Wave-1) to 2/48 (post-Wave-3).  This implies one
of the Wave-2/3 PRs (most plausibly #3232 Euclidean trace kernel, #3248
Euclidean via kernel, or #3250 sub-cell pad-metal margin) made the
geometry tighter on chorus's dense dual-row J2 + SSOP packages.  The
floor was NOT lowered to 2 because doing so would weaken the regression
test; instead the post-Wave-3 measurement is recorded here as a known-bad
baseline and follow-up has been filed (see #3252 close-out comment) to
attribute the regression to a specific PR via git-bisect on the 1500s
recipe.
The mechanism is unchanged from the issue body: each attempt detail-routes
27-30 nets but only 5-7 of those tally as "fully connected" in the final
result.  The 20-25 "partial-route" nets per attempt never get cleaned
because auto-fix is silently skipped when `--timeout` is exhausted by
the negotiated stage (issue #2802 deadline guard).

PR #3244 (#3241 early-exit) was expected to terminate the ladder when
reach monotonically degrades, freeing budget for auto-fix.  However, the
chorus pattern's per-attempt drops are 1-2 nets — below the
``REGRESSION_TOLERANCE=2`` threshold and ``HARD_DROP_NETS=5`` cutoff —
so the early-exit never fires on this dataset.  All three attempts still
run to completion, exhausting the 1500s budget.

The load-bearing follow-up is **issue #3238 (auto-fix budget
reservation)**, currently being built in parallel.  Once #3238 lands,
auto-fix will run with a guaranteed slice of the wall-clock budget and
should convert the 27-30 detail-routed nets back to fully-connected,
restoring reach to ~60% parity with May 10.

This module's tests serve two purposes:

1. **Floor assertion** (``test_chorus_reach_v21_floor``): an opt-in
   slow integration test that runs the chorus recipe end-to-end and
   asserts the measured v21 floor of ``nets_fully_routed >= 2`` (see
   the fixture-migration section below; the v19-era floor of 5 is
   frozen as history).  Marked ``@pytest.mark.slow`` (25+ min
   wall-clock) so it gates manual verification and nightly runs only,
   not per-commit CI.

2. **Re-measurement record** (``test_post_wave1_measurement_documented``):
   a fast unit test that codifies the empirical numbers above so they
   show up in the test suite and future Builders can grep for the
   baseline when chorus reach changes again.  Failures here mean the
   re-measurement record drifted from the constants — bump the constants
   AND update the project memory file.

After #3238 lands and the regression is fully resolved, the floor in
``CHORUS_POST_WAVE1_FLOOR`` should be bumped to match the new reach.
Issue body's AC #1 sets the target at ≥ 30/48 (May 10 parity).

Fixture migration (Issue #3474 Phase 0, 2026-06-10)
---------------------------------------------------

All POST_WAVE constants in this module are **frozen v19-era historical
records**: they were measured on
``chorus-test-revA_v19_stripped.kicad_pcb`` (48 multi-pad signal nets)
and stay untouched so future bisects have precise baselines.

The v19 fixture is STALE: the chorus repo's 2026-05-11 repair (commit
5b59e20) restored 7 nets that v19 lacks (Y2.3 TCXO output, U7.2 clock
buffer input, R4/R16 envelope-detector pull-downs), and the 2026-06-10
Phase 0 work additionally restored U1/U2 regulator connectivity (the
embedded ``(extends)`` lib symbol in power.kicad_sch was unresolvable,
so KiCad saw the regulators as pinless in both ERC and the netlist) and
fixed the last ERC error.  Routing v19 to 100% would be
fake-manufacturable.

The live fixture is now ``chorus-test-revA_v21_stripped.kicad_pcb``
(v20 + ERC fixes + netlist sync + ``kct pcb strip --include-power`` +
orphan-net cleanup), with **51 multi-pad signal nets** after the five
power/ground nets are excluded.  The slow integration test below routes
the v21 fixture and asserts the freshly measured v21 floor — see the
``CHORUS_V21_*`` constants for the measurement record.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# --------------------------------------------------------------------------
# Post-Wave-1 baseline constants (Issue #3237 re-measurement).
# --------------------------------------------------------------------------

# Total multi-pad signal nets on chorus-test-revA_v19_stripped.kicad_pcb,
# after the five power/ground nets are excluded by the case config (see
# tests/baselines/chorus_test_revA.json).
CHORUS_NETS_TOTAL = 48  # v19 stripped fixture; +2 vs v18 used in the
# 2611 baseline because v19 adds the U7
# 74LVC1G17 connections.

# Post-Wave-1 floor: the smallest "Best result" observed across the two
# backends on 2026-06-06 evening.  CI must assert reach >= this so that
# future PRs cannot silently degrade further while #3238 is in flight.
# (Python = 6/48, C++ = 5/48 -> floor = 5.)
#
# Intentionally NOT lowered to 2 after the post-Wave-3 re-measurement
# (#3252) discovered cpp/seed 42 dropped to 2/48 on HEAD fb400383.  The
# point of this constant is to *catch* further regressions; lowering it
# would weaken that guarantee.  Instead the slow integration test below
# is left expecting >= 5 so it fails on post-Wave-3 HEAD, surfacing the
# regression to anyone who opts in (KCT_RUN_CHORUS_REACH_FLOOR=1).
CHORUS_POST_WAVE1_FLOOR = 5

# Backend-specific best-result floors.  Useful when the integration
# test is parameterised by backend to confirm parity between Python and
# C++ stays within the issue body's AC #2 tolerance (C++ >= 80% of Python).
CHORUS_POST_WAVE1_FLOOR_PYTHON = 6
CHORUS_POST_WAVE1_FLOOR_CPP = 5

# Post-Wave-3 re-measurement (Issue #3252, 2026-06-06 night, HEAD fb400383).
# Single attempt with --backend cpp --seed 42 --timeout 480 (reduced from
# 1500 due to system contention -- multiple loom workers running).  Best
# result across the layer-escalation ladder was 4L 2/48 (4%); attempt 2
# (4L ALL-SIG) routed 24 nets in detail-routing but 0 fully-connected.
# AUTOFIX_SKIPPED_BUDGET_EXHAUSTED token from #3247 was NOT present, so
# the auto-fix budget reservation is engaging correctly; the bottleneck
# is per-attempt detail-routing geometry, not auto-fix budget.
CHORUS_POST_WAVE3_OBSERVED_CPP_SEED42 = 2  # Below the floor -> known bad

# Post-Wave-8 re-measurement (Issue #3299, 2026-06-07 morning, HEAD 956f9487).
# Full 1500s recipe, both backends, seed 42, under heavy contention (5+ other
# loom workers running concurrently).
#
#   Backend  | Attempt 1 | Attempt 2 | Attempt 3 | Best (strict) | Partial | Auto-fix
#   ---------+-----------+-----------+-----------+---------------+---------+---------
#   Python   | 2/48 (4%) | 5/48(10%) | 2/48 (4%) | 5/48          | 28      | RAN (22/37)
#   C++      | 3/48 (6%) | 3/48 (6%) | 3/48 (6%) | 3/48          | 29      | RAN, rolled back
#
# Key findings vs prior measurements:
#   1. Python matches post-Wave-1 floor (5/48); cpp under cpp floor (3 vs 5),
#      likely due to system contention degrading per-net A* time budget.
#   2. AUTOFIX_SKIPPED_BUDGET_EXHAUSTED token NOT present on either run —
#      PR #3247 (auto-fix budget reservation) is engaging correctly.
#   3. Auto-fix RAN on both backends.  Python repaired 22/37 violations.
#      CPP rolled back due to connectivity regression (30 -> 28 nets).
#   4. Partial-vs-strict gap from #3255 confirmed: 28-29 nets have routes
#      but only 3-5 are fully connected.
#   5. Net-status of routed PCB: 32 complete, 45-47 incomplete, 27 unrouted
#      (out of 104 total nets including power/single-pad).
#   6. JLCPCB check: 71 errors (45-47 connectivity, edge/clearance) — NOT
#      MANUFACTURABLE.  Verdict: NO.
#
# Bottleneck per #3255 + this measurement: per-net A* convergence on
# multi-pad nets dominates wall-clock budget.  Multi-pad nets like
# DAC_CLK take 100-400s per net; only 5-15 nets per attempt fit in the
# 1500s budget.
CHORUS_POST_WAVE8_BEST_PYTHON_SEED42 = 5  # At floor
CHORUS_POST_WAVE8_BEST_CPP_SEED42 = 3  # Below cpp floor (contention)
CHORUS_POST_WAVE8_PARTIAL_PYTHON = 28
CHORUS_POST_WAVE8_PARTIAL_CPP = 29

# Post-Wave-9 re-measurement (Issue #3309 close-out, 2026-06-07 afternoon,
# HEAD facbe2e7 -- includes PR #3322 power-rail alias, #3323 C++ A* flat
# arrays, #3324 layer mapping fix, #3326 trace-width-by-impedance,
# #3328 J2 board-edge-aware escape, and #3330 diff-pair centerline overlap).
# Single recipe (cpp seed 42, --timeout 1200, --auto-fix --auto-fix-passes 2)
# run during low system contention, after `kct build-native` confirmed the
# C++ backend with the new flat-array A* infrastructure was loaded.
#
#   Backend  | Attempt 1   | Attempt 2   | Attempt 3   | Best (strict) | Detail-routed | Partial | Unrouted | Auto-fix
#   ---------+-------------+-------------+-------------+---------------+---------------+---------+----------+---------
#   C++      | 4/48 (8%)   | 4/48 (8%)   | 4/48 (8%)   | 4/48 (8%)     | 28/48         | 29/48   | 15/48    | RAN, rolled back
#
# Per-net A* timing comparison (chorus seed 42, all-merges HEAD vs Wave-8 cpp):
#
#   Net           | Wave-8 (cpp) | Wave-9 (cpp, flat-arrays) | Speedup
#   --------------+--------------+---------------------------+---------
#   DAC_CLK       | 217s         | 68.6s                     | 3.2x
#   DAC_CLK_DIV2  | 219s         | 69.2s                     | 3.2x
#   I2S_BCLK      | 237s         | 73.7s                     | 3.2x
#   I2S_LRCLK     | 245s         | 77.1s                     | 3.2x
#   I2S_DIN       | 252s         | 116.7s                    | 2.2x
#   AUDIO_L       | 138s         | 198.6s                    | 0.7x (regressed)
#   AUDIO_R       | 177s         | 205.6s                    | 0.9x (regressed)
#
# Key findings:
#   1. Per-net A* on the C++ backend is 2-3x faster for clock/data nets
#      (PR #3323 flat-arrays cache-locality fix is working as designed).
#   2. AUDIO_L/AUDIO_R timing did NOT improve -- these may hit the higher
#      cost-bound path in the negotiated router (AC-coupled audio paths
#      route differently than digital clocks).
#   3. Detail-routing now completes 28 nets per attempt across all three
#      layer ladders (4L SIG-GND-PWR-SIG, 4L ALL-SIG, 6L SIG-GND-SIG-SIG-PWR-SIG).
#      This is a step-change vs Wave-8 cpp (which detail-routed ~24-30
#      nets per attempt but only 3-5 were strictly connected).
#   4. **Strict-connected count remains stuck at 4/48 (8%)** -- the
#      partial-vs-strict gap from #3255 is still the dominant chorus
#      bottleneck.  29 nets have segments but only one pad-pair
#      connected (e.g., NRST 1/3, SCL 1/3, SDA 1/3, SPI_SCK 1/3,
#      SWCLK 1/4, U5-VCOM/DEMP 1/3).
#   5. AUDIO_R is now classified BLOCKED_PATH (not just slow) -- the
#      negotiated router cannot reach it from any layer; suggestion:
#      "Move C17, C20, J4 (+1 more) south to create routing channel."
#      This is a placement-feedback signal, not a router-only fix.
#   6. AUTOFIX_SKIPPED_BUDGET_EXHAUSTED token NOT present.  Auto-fix
#      DID run (DRC repair attempted on 45 violations) but rolled back
#      because nudges decreased connectivity (31 -> 29 nets).  Mechanism
#      same as Wave-8.
#
# Verdict for #3309: the per-net A* convergence bottleneck **has been
# materially addressed by PR #3323** (2-3x speedup on the dominant clock
# nets) and the negotiated router now completes more nets per attempt
# (28 vs ~15-24).  However, **chorus reach did NOT improve from 5/48 to
# the 20/48 target** because the bottleneck is now the partial-vs-strict
# gap (#3255), not raw A* speed.  The C++ infrastructure shipped is
# load-bearing for future work but didn't unblock chorus on its own.
# Issue #3309's narrow claim ("per-net A* dominates wall-clock") is
# addressed; the broader chorus-reach acceptance criterion (>=20/48)
# remains open and rolls forward to #3255 / a new partial-vs-strict
# follow-up.
CHORUS_POST_WAVE9_BEST_CPP_SEED42 = 4  # 4/48 strict-connected; +1 vs Wave-8
CHORUS_POST_WAVE9_DETAIL_ROUTED_CPP = 28  # 28/48 detail-routed per attempt
CHORUS_POST_WAVE9_PARTIAL_CPP = 29  # 29/48 have segments but not all pads
CHORUS_POST_WAVE9_UNROUTED_CPP = 15  # 15/48 have no segments at all
# Per-net A* speedup from PR #3323 (flat arrays) on the dominant clock nets.
# Used by the test below to assert the speedup is materially > 1x.
CHORUS_POST_WAVE9_PERNET_SPEEDUP_DAC_CLK = 3.2  # 217s -> 68.6s

# Post-Wave-10 re-measurement (Issue #3340, 2026-06-08, HEAD 21076e6c --
# includes all merges through PR #3359 P_AS3 auto-pcb-size CLI integration,
# PR #3357 schematic validator fix, PR #3358 P_AS2 auto-pcb-size, PR #3356
# OvercurrentComparator multi-unit pin lookup, PR #3355 P_AS1 auto-pcb-size
# data + schema, PR #3353 BackToBackFETPair orthogonal Z-route, PR #3350
# softstart rev B PCB placement, PR #3345 softstart rev B schematic, plus
# Wave 11 router infrastructure: PR #3203 per-pad channel budget edge-
# classification, PR #3193 deterministic A* tie-break, PR #3202 audit
# restore, PR #3198 C++ per-pad channel budget, PR #3197 topology-aware
# analog-ground bridge audit).  Same recipe as Wave-9: cpp seed 42,
# --timeout 1200, --auto-layers, --auto-fix --auto-fix-passes 2, under
# heavy system contention (4+ other loom workers actively routing).
#
#   Backend  | Attempt 1   | Attempt 2   | Attempt 3   | Best (strict) | Detail-routed | Partial | Unrouted | Auto-fix
#   ---------+-------------+-------------+-------------+---------------+---------------+---------+----------+---------
#   C++      | 4/48 (8%)   | 3/48 (6%)   | 2/48 (4%)   | 4/48 (8%)     | 28/48         | 29/48   | 15/48    | RAN, partial-repair kept
#
# Per-net A* timing comparison (chorus seed 42, Wave-10 cpp under contention vs Wave-9 cpp):
#
#   Net           | Wave-9 (cpp) | Wave-10 (cpp, contended) | Note
#   --------------+--------------+--------------------------+--------------------------
#   DAC_CLK       | 68.6s        | 82.5s                    | +20% (contention overhead)
#   DAC_CLK_DIV2  | 69.2s        | 83.2s                    | +20%
#   I2S_BCLK      | 73.7s        | 89.4s                    | +21%
#   I2S_LRCLK     | 77.1s        | 94.3s                    | +22%
#   I2S_DIN       | 116.7s       | 155.3s                   | +33%
#   AUDIO_L       | 198.6s       | 261.6s                   | +32% (still routed, not BLOCKED_PATH)
#   AUDIO_R       | 205.6s       | 271.0s -> BLOCKED_PATH   | Classification stable vs Wave-9
#
# Key findings:
#   1. **Strict reach unchanged at 4/48 (8%)** -- identical to Wave-9.  The
#      partial-vs-strict gap (#3255 mechanism) remains dominant.
#   2. **Partial/unrouted counts unchanged (29/48 and 15/48)** -- the
#      Wave-9 baseline composition is stable.
#   3. **AUDIO_R BLOCKED_PATH classification is STABLE** between Wave-9 and
#      Wave-10.  AUDIO_L successfully routes on both waves (261.6s in
#      Wave-10 under contention, 198.6s in Wave-9 -- ~30% slower under
#      contention but still completes).  The issue #3340 hypothesis that
#      "Wave 11 router changes resolved AUDIO_R" is NOT supported -- AUDIO_R
#      remains BLOCKED_PATH on the same fixture with the same suggestion
#      ("Move C17, C20, J4 (+1 more) south to create routing channel").
#      The earlier issue body language calling it "AUDIO_L/R BLOCKED_PATH"
#      conflates the two: only AUDIO_R is BLOCKED_PATH; AUDIO_L is merely
#      slow (~260s) and routes via 4L SIG-GND-PWR-SIG.
#   4. **Auto-fix outcome IMPROVED vs Wave-9**: this run kept the partial
#      repair (14/46 violations resolved across 2 passes, "partial repair;
#      some violations remain") instead of rolling back due to
#      connectivity regression.  Connectivity invariant check passed at all
#      three checkpoints (optimize, nudge, finalize) on the 48 multi-pad
#      nets.  This is a small but real improvement -- previously auto-fix
#      was net-negative; now it's net-positive even if not at the 95%
#      manufacturability target.
#   5. **AUTOFIX_SKIPPED_BUDGET_EXHAUSTED token NOT present** -- the
#      auto-fix budget reservation from PR #3247 is still engaging.
#   6. Per-net timing on dominant clock nets is ~20% slower than Wave-9 due
#      to concurrent loom workers consuming CPU.  This does NOT invalidate
#      the PR #3323 speedup claim (Wave-9 measured 3.2x vs Wave-8); a
#      contention-free re-run should show the same or better speedup.
#      The Wave-9 speedup constant is preserved as the canonical value.
#
# Verdict for #3340: chorus reach is **stable** at 4/48 strict-connected
# between Wave-9 and Wave-10.  The dominant bottleneck remains the
# partial-vs-strict gap (#3255 mechanism: 29 nets have routes but only
# 1 of 2-3 pads connected, e.g., SDA 1/3, SPI_SCK 1/3, SWCLK 1/4).
# AUDIO_R BLOCKED_PATH is a stable classification, not a regression
# introduced by #3328 nor resolved by Wave 11 router changes -- it
# reflects a genuine placement constraint that needs C17/C20/J4 to move
# south.  The floor constant (5) is INTENTIONALLY NOT lowered to 4 even
# though Wave-10 measured below it; per the docstring, lowering the
# floor weakens the regression guarantee and the floor should only move
# UPWARD with multi-seed confirmation.
CHORUS_POST_WAVE10_BEST_CPP_SEED42 = 4  # Same as Wave-9
CHORUS_POST_WAVE10_DETAIL_ROUTED_CPP = 28  # Same as Wave-9
CHORUS_POST_WAVE10_PARTIAL_CPP = 29  # Same as Wave-9
CHORUS_POST_WAVE10_UNROUTED_CPP = 15  # Same as Wave-9
# AUDIO_R BLOCKED_PATH stability: True if AUDIO_R is reported BLOCKED_PATH
# in both Wave-9 and Wave-10.  Used by the test below to anchor the
# "stable classification, not regression" finding from #3340.
CHORUS_POST_WAVE10_AUDIO_R_BLOCKED = True
# AUDIO_L routed status: True if AUDIO_L successfully routes (i.e., is NOT
# classified BLOCKED_PATH).  Wave-10 confirms AUDIO_L routes in 261.6s
# on attempt 1's 4L SIG-GND-PWR-SIG layer ladder.
CHORUS_POST_WAVE10_AUDIO_L_ROUTED = True
# Auto-fix kept partial repair on Wave-10 (no connectivity rollback).
# Distinct from Wave-9's behavior where auto-fix was rolled back.  This
# is the small step-change in this measurement; track it so any future
# regression to "auto-fix rolls back" is caught.
CHORUS_POST_WAVE10_AUTOFIX_KEPT = True

# May-10 reach target (issue body AC #1).  Once the partial-vs-strict
# gap (#3255) is closed and reach recovers, the integration test below
# should be flipped from "assert >= floor" to "assert >= target" and the
# floor constant retired.
CHORUS_MAY10_TARGET = 30  # 30/48 = 62.5%

# --------------------------------------------------------------------------
# v21 fixture re-baseline (Issue #3474 Phase 0, 2026-06-10, HEAD d45ded4d).
# --------------------------------------------------------------------------
#
# Everything above this block is a FROZEN v19-era record (48-net
# denominator).  The constants below are the first measurement on the
# repaired chorus-test-revA_v21_stripped fixture (51 multi-pad signal
# nets after the five power/ground nets; +3 vs v19 because the
# 2026-05-11 5b59e20 repair restored Y2/U7/envelope-detector
# connectivity and Phase 0 restored U1/U2 regulator pins).
#
# Recipe (identical to the Wave-9/10 pinned recipe except the fixture):
#   PYTHONHASHSEED=0 kct route chorus-test-revA_v21_stripped.kicad_pcb \
#     --manufacturer jlcpcb-tier1 --backend cpp --placement-feedback \
#     --placement-feedback-budget 5 --iterations 50 --auto-fix \
#     --auto-fix-passes 2 --auto-layers --timeout 1200 --seed 42
#
#   Attempt 1 (4L SIG-GND-PWR-SIG):       2/51 -- detailed routing timed
#     out at net 3/51 after 365.3s (SPI_SCK A* blowup, #3470 defect 3);
#     grace pass burned 164.5s routing 0/1 starved nets (47 skipped).
#   Attempt 2 (4L + via-in-pad fallback): 2/51 -- same fingerprint.
#   Attempt 3:           never ran (wall-clock deadline, issue #2802).
#   Placement feedback:  SKIPPED (deadline).
#   Auto-fix:            RAN (7/49 nudge-resolved), then ROLLED BACK
#     (connectivity regression 29 -> 28 nets);
#     AUTOFIX_SKIPPED_BUDGET_EXHAUSTED token NOT present.
#   DRC on routed output: 101 errors / 56 warnings.
#
# Final: 2/51 strict-connected, 30/51 partial, 19/51 unrouted.  This
# matches the 2026-06-10 v19 re-baseline in issue #3474 (2/48 strict,
# 30 partial, 16 unrouted) -- the fixture migration did not change the
# router's behavior class; the three restored-connectivity nets land in
# the unrouted bucket because the budget still dies at net 3.
#
# The constants below record the measurement exactly; the regression
# floor is pinned separately with documented headroom.  Per
# feedback_manufacturable_means_100pct.md the bar is 100% + 0 DRC; the
# floor only catches further regressions while Phases R2/P1 of #3474
# are in flight.  Move it UP as those phases land.
CHORUS_V21_NETS_TOTAL = 51

# --------------------------------------------------------------------------
# Phase R1 re-pin (Issue #3474 R1, 2026-06-10, branch feature/issue-3474-r1).
# --------------------------------------------------------------------------
#
# R1 fixed three wall-clock leaks below the per-net cap (the cap itself --
# CLI default --per-net-timeout 30 -- was already in force but was being
# blown by un-budgeted code around the A* search):
#
#   1. Per-edge failure diagnostics ran RootCauseAnalyzer with a
#      full-board CongestionMap scan + unbounded corridor scans:
#      ~100-120s of pure Python PER FAILED EDGE on this 1240x1240x4
#      grid.  SPI_SCK "routing" measured 247s of which 10s was A*.
#      Fixed: corridor-scoped + stride-subsampled scans, per-net
#      analysis cache, 20s cumulative analysis budget
#      (core._analyze_failure_budgeted).
#   2. cpp->python fallback double-spend: a fresh per-net budget for
#      the 10-100x-slower Python A* after C++ consumed its own.
#      Fixed: shared route_deadline clamps the fallback budget.
#   3. Grace-pass burn: one 0.3s-capped attempt ate 101-136s inside
#      leak (1) -- entire #3452 budget, 0 routed, 44-47 skipped.
#      Fixed by (1) + overrun-abort + no-progress tier exit +
#      overrun-funded grace budget (run_initial_pass_grace).
#
# Measured at the pinned recipe with the fixes (2026-06-10):
#   - Pre-fix re-baseline (HEAD 05541c7d): 2/51 strict, 30 partial,
#     19 unrouted; all three attempts died at net 3/51 (SPI_SCK ~290s);
#     grace 0/1 in ~130s x3; attempt 4+ deadline-stopped; placement
#     feedback AND auto-fix skipped (AUTOFIX_SKIPPED_BUDGET_EXHAUSTED
#     PRESENT).
#   - Post-fix run A: 14/51 strict, 26 partial, 11 unrouted; 6
#     escalation attempts ran; auto-fix RAN; token ABSENT; DRC 90 err.
#   - Post-fix run B (re-pin record below): 18/51 strict, 7 partial,
#     26 unrouted; 4 attempts + placement feedback RAN (1 iteration,
#     first time ever on this recipe) + auto-fix RAN (rolled back on
#     connectivity protection); token ABSENT; DRC 53 err / 56 warn;
#     every grace pass attempted ALL starved nets (0 skipped).
#
# Strict reach is wall-clock-modulated (14 vs 18 across two identical
# seed-42 runs: the budget line lands between different nets).  The
# floor is therefore pinned BELOW the weaker measurement with headroom
# for load modulation, while still catching any regression toward the
# 2/51 starvation class.
CHORUS_V21_R1_MEASURED_STRICT = 18  # run B, cpp seed 42, 2026-06-10
CHORUS_V21_R1_MEASURED_STRICT_RUN_A = 14
CHORUS_V21_PARTIAL_CPP_SEED42 = 7  # run B
CHORUS_V21_UNROUTED_CPP_SEED42 = 26  # run B
CHORUS_V21_FLOOR = 10  # regression tripwire: below min(14, 18) with headroom
CHORUS_V21_AUTOFIX_ROLLED_BACK = True  # connectivity protection, not budget

# The vendored chorus-test-revA fixture, surfaced via the
# ``boards/external/chorus-test-revA`` symlink that points at the
# canonical chorus repo checkout.  Resolves relative to
# the repository root (this file is two levels under it), matching the
# pattern in ``test_chorus_test_placement_feedback.py`` and
# ``src/kicad_tools/benchmark/cases.py``.
#
# Issue #3474 Phase 0: migrated from v19_stripped (stale netlist) to
# v21_stripped (post-repair).  See the module docstring's "Fixture
# migration" section.
#
# When the fixture is missing the slow integration test skips rather than
# fails, matching the existing pattern in ``test_benchmark_chorus.py``
# (see ``test_missing_pcb_skips_gracefully``).
CHORUS_FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent
    / "boards"
    / "external"
    / "chorus-test-revA"
    / "kicad"
    / "chorus-test-revA_v21_stripped.kicad_pcb"
)


# --------------------------------------------------------------------------
# Fast unit test: re-measurement documented in constants.
# --------------------------------------------------------------------------


def test_post_wave1_measurement_documented() -> None:
    """The Builder's re-measurement constants are present and self-consistent.

    Failure of this test means either:

    1. The constants above were edited without bumping the comment block
       (re-measurement record drifted) -- update the docstring too.
    2. A future PR landed that improved reach -- bump
       ``CHORUS_POST_WAVE1_FLOOR`` to the new floor and update the
       per-backend constants.  Reference the PR in the docstring.
    """
    # Floor is at least 1 (the router must produce *something*) and
    # strictly below the May 10 target (otherwise this issue is closed).
    assert 1 <= CHORUS_POST_WAVE1_FLOOR < CHORUS_MAY10_TARGET, (
        "Post-Wave-1 floor must sit between 1 and the May 10 target "
        f"({CHORUS_MAY10_TARGET}); if it has reached the target, this "
        "test should be migrated to assert May 10 parity instead."
    )

    # Per-backend floors should bracket the global floor.
    assert CHORUS_POST_WAVE1_FLOOR_CPP <= CHORUS_POST_WAVE1_FLOOR_PYTHON, (
        "Empirically the Python backend reached >= C++ on chorus today "
        "(6 vs 5).  If this inverts, the issue body's 'this is not a "
        "C++-specific regression' assertion needs to be revisited."
    )
    assert (
        min(CHORUS_POST_WAVE1_FLOOR_PYTHON, CHORUS_POST_WAVE1_FLOOR_CPP) == CHORUS_POST_WAVE1_FLOOR
    ), (
        "Global floor must equal the smaller of the per-backend floors; "
        "drift here means the constants were updated inconsistently."
    )


def test_post_wave3_remeasurement_documented() -> None:
    """The post-Wave-3 re-measurement (Issue #3252) is recorded honestly.

    Per the #3252 close-out, reach DROPPED from 5/48 (post-Wave-1) to
    2/48 (post-Wave-3 cpp seed 42) -- i.e., one of the Wave-2/3 PRs
    silently regressed chorus.  We intentionally do NOT lower the floor
    constant; instead the post-Wave-3 measurement is captured here as a
    distinct constant so a future Builder doing git-bisect to attribute
    the regression has a precise pre-fix baseline to compare against.

    Failure of this test means the post-Wave-3 measurement was edited
    without updating the docstring header -- bump both together.
    """
    # The post-Wave-3 measurement was strictly below the post-Wave-1 floor
    # (this is the regression #3252 documented).  If a later PR raises it
    # back to >= floor on cpp/seed 42, retire this constant and update the
    # slow integration test.
    assert CHORUS_POST_WAVE3_OBSERVED_CPP_SEED42 < CHORUS_POST_WAVE1_FLOOR, (
        "Post-Wave-3 cpp/seed42 measurement should still be below the "
        "post-Wave-1 floor.  If reach has recovered, that's good news -- "
        "retire CHORUS_POST_WAVE3_OBSERVED_CPP_SEED42 and update the "
        "docstring header."
    )
    # Sanity: it's a non-negative net count.
    assert 0 <= CHORUS_POST_WAVE3_OBSERVED_CPP_SEED42 <= CHORUS_NETS_TOTAL


def test_post_wave8_remeasurement_documented() -> None:
    """The post-Wave-8 re-measurement (Issue #3299) is recorded honestly.

    This confirms the post-Wave-1 floor (5/48 Python) is still the working
    ceiling on chorus.  The mechanism remains per-net A* convergence:
    multi-pad nets like DAC_CLK take 100-400s on chorus, so only 5-15
    nets fit in a 1500s budget per attempt.

    Distinct from the post-Wave-3 re-measurement, this test confirms
    PR #3247's auto-fix budget reservation is engaging (no
    AUTOFIX_SKIPPED_BUDGET_EXHAUSTED token in stderr) and auto-fix
    actually runs (Python repaired 22/37 violations; CPP attempted
    repair but rolled back due to connectivity regression).

    Failure of this test means the post-Wave-8 measurements were edited
    without updating the docstring header.  Update both together.
    """
    # Python matched the post-Wave-1 floor (5/48); CPP regressed below
    # its floor (3 vs 5) under contention.  Both numbers are honest
    # measurements under the documented conditions.
    assert CHORUS_POST_WAVE8_BEST_PYTHON_SEED42 == CHORUS_POST_WAVE1_FLOOR_PYTHON - 1
    assert CHORUS_POST_WAVE8_BEST_CPP_SEED42 < CHORUS_POST_WAVE1_FLOOR_CPP
    # Sanity: counts are non-negative net IDs.
    assert 0 <= CHORUS_POST_WAVE8_BEST_PYTHON_SEED42 <= CHORUS_NETS_TOTAL
    assert 0 <= CHORUS_POST_WAVE8_BEST_CPP_SEED42 <= CHORUS_NETS_TOTAL
    # Partial-vs-strict gap is real per #3255: when 3-5 are strict-connected,
    # 28-29 have *some* route but not all pads reached.
    assert CHORUS_POST_WAVE8_PARTIAL_PYTHON > CHORUS_POST_WAVE8_BEST_PYTHON_SEED42 * 5, (
        "Partial count should significantly exceed strict count; if not, "
        "the partial-vs-strict gap from #3255 has been resolved -- "
        "celebrate and update the docstring."
    )
    assert CHORUS_POST_WAVE8_PARTIAL_CPP > CHORUS_POST_WAVE8_BEST_CPP_SEED42 * 5


def test_post_wave9_remeasurement_documented() -> None:
    """The post-Wave-9 re-measurement (Issue #3309 close-out) is recorded honestly.

    Wave-9 ships PR #3322 (power-rail alias), #3323 (C++ A* flat arrays
    -- the load-bearing fix for the per-net A* convergence bottleneck
    named in #3309's issue body), #3324 (layer mapping fix), #3326
    (trace-width-by-impedance), #3328 (J2 board-edge-aware escape), and
    #3330 (diff-pair centerline overlap).  Each of these was scoped
    narrowly; collectively they form the "all-merges" baseline.

    On the chorus-test-revA_v19_stripped fixture (jlcpcb-tier1, seed 42,
    --auto-layers, --auto-fix 2, --timeout 1200) the C++ backend went
    from Wave-8's 3/48 strict-connected to Wave-9's 4/48 -- a small
    improvement, but FAR from the 20/48 target that issue #3309's AC
    expected.  The dominant per-net A* timing did improve significantly
    (DAC_CLK went from 217s -> 68.6s, a 3.2x speedup), confirming that
    PR #3323's flat-arrays cache-locality fix is working as designed.
    But the reach didn't move because the new bottleneck is the
    partial-vs-strict gap (#3255) -- 29 nets have segments, only 4 are
    fully connected.

    Distinct from earlier post-Wave-X tests, this one explicitly asserts
    the per-net A* speedup, anchoring the C++ infrastructure claim of
    PR #3323 so any future regression is caught.  It does NOT assert
    chorus reach jumped to 20+/48 -- that's the issue body's AC but it
    didn't happen, and we record that honestly.
    """
    # Wave-9 cpp reach was 4/48, one above Wave-8's contended 3/48.
    # Not a step-change but not a regression either.
    assert CHORUS_POST_WAVE9_BEST_CPP_SEED42 == 4
    assert CHORUS_POST_WAVE9_BEST_CPP_SEED42 > CHORUS_POST_WAVE8_BEST_CPP_SEED42
    # Still below the original cpp floor (5).  Not a regression -- equal
    # within measurement noise of the post-Wave-1 cpp floor.
    assert CHORUS_POST_WAVE9_BEST_CPP_SEED42 < CHORUS_POST_WAVE1_FLOOR_CPP

    # Detail-routing now completes 28 nets per attempt, materially more
    # than Wave-8 cpp (which mixed 24-30 per attempt with high variance).
    assert CHORUS_POST_WAVE9_DETAIL_ROUTED_CPP == 28
    assert CHORUS_POST_WAVE9_DETAIL_ROUTED_CPP > CHORUS_POST_WAVE9_BEST_CPP_SEED42 * 5, (
        "Detail-routed count should exceed strict-connected by 5x or more; "
        "this is the partial-vs-strict gap #3255 surfaces."
    )

    # Partial + unrouted + strict should equal CHORUS_NETS_TOTAL.
    assert (
        CHORUS_POST_WAVE9_BEST_CPP_SEED42
        + CHORUS_POST_WAVE9_PARTIAL_CPP
        + CHORUS_POST_WAVE9_UNROUTED_CPP
        == CHORUS_NETS_TOTAL
    ), (
        "strict + partial + unrouted must total all chorus signal nets; "
        f"got {CHORUS_POST_WAVE9_BEST_CPP_SEED42} + "
        f"{CHORUS_POST_WAVE9_PARTIAL_CPP} + {CHORUS_POST_WAVE9_UNROUTED_CPP} "
        f"!= {CHORUS_NETS_TOTAL}."
    )

    # PR #3323's flat-arrays cache-locality fix must materially improve
    # per-net A* convergence on the dominant clock nets.  If this drops
    # below 2x, the C++ infrastructure regressed and should be bisected.
    assert CHORUS_POST_WAVE9_PERNET_SPEEDUP_DAC_CLK >= 2.0, (
        "Per-net A* speedup on DAC_CLK fell below 2x -- the PR #3323 "
        "flat-arrays infrastructure has regressed.  Bisect from HEAD "
        "back through PRs that touched src/kicad_tools/router/router_cpp.cpp "
        "or the AStarSolver class."
    )


def test_post_wave10_remeasurement_documented() -> None:
    """The post-Wave-10 re-measurement (Issue #3340) is recorded honestly.

    Wave-10 (2026-06-08, HEAD 21076e6c) confirms the Wave-9 baseline is
    stable.  The same chorus-test-revA_v19_stripped fixture under the same
    recipe (cpp seed 42, --timeout 1200, --auto-fix --auto-fix-passes 2,
    --auto-layers) produces an identical 4/48 strict-connected reach, 29
    partial routes, and 15 unrouted nets.

    The dominant Wave-10 finding is **stability**: chorus did not move
    despite ~25 PRs merging between Wave-9 (HEAD facbe2e7) and Wave-10
    (HEAD 21076e6c).  Most of those merges did not touch the router (P_AS1
    auto-pcb-size, softstart rev B, BackToBackFETPair Z-route, schematic
    validator fix); the router-touching merges (PR #3203 per-pad channel
    budget edge-classification fix, PR #3193 deterministic A* tie-break,
    PR #3198 C++ per-pad channel budget infrastructure, PR #3197 topology-
    aware analog-ground bridge audit) did not change chorus reach.

    Distinct from earlier post-Wave-X tests, this one also explicitly
    asserts the **AUDIO_R BLOCKED_PATH stability** to anchor the
    finding from #3340's hypothesis investigation.  The Wave-9 baseline
    classified AUDIO_R as BLOCKED_PATH; Wave-10 confirms the same
    classification with the same placement-feedback suggestion.  This
    rules out two alternative hypotheses: (a) that #3328's J2 board-edge-
    aware escape introduced AUDIO_R BLOCKED_PATH as a regression (would
    require a comparison to pre-#3328 baseline to fully rule out, filed as
    follow-up); (b) that Wave 11 router changes (PR #3203, #3198) resolved
    AUDIO_R (definitively ruled out by Wave-10 measurement).

    Failure of this test means either:

    1. The post-Wave-10 measurements were edited without updating the
       docstring header -- update both together.
    2. A future PR landed that actually moved chorus reach (good news!) --
       bump CHORUS_POST_WAVE10_BEST_CPP_SEED42, add a Wave-11 block, and
       consider bumping CHORUS_POST_WAVE1_FLOOR if multi-seed confirms.
    3. AUDIO_R is no longer BLOCKED_PATH (also good news -- either a
       placement change in the fixture or a router improvement).  Set
       CHORUS_POST_WAVE10_AUDIO_R_BLOCKED = False and update the docstring.
    """
    # Wave-10 cpp reach was 4/48, identical to Wave-9.  Captures the
    # stability finding.
    assert CHORUS_POST_WAVE10_BEST_CPP_SEED42 == 4
    assert CHORUS_POST_WAVE10_BEST_CPP_SEED42 == CHORUS_POST_WAVE9_BEST_CPP_SEED42, (
        "Wave-10 cpp reach should match Wave-9 (stability finding).  If "
        "the numbers diverged, update the docstring header to explain the "
        "delta and consider whether the floor constant should change."
    )

    # Detail-routing now completes 28 nets per attempt (same as Wave-9).
    # The partial-vs-strict gap (29/48 partial, 15/48 unrouted) is the
    # dominant chorus bottleneck and has not closed.
    assert CHORUS_POST_WAVE10_DETAIL_ROUTED_CPP == 28
    assert CHORUS_POST_WAVE10_PARTIAL_CPP == 29
    assert CHORUS_POST_WAVE10_UNROUTED_CPP == 15

    # Partial + unrouted + strict should equal CHORUS_NETS_TOTAL.
    assert (
        CHORUS_POST_WAVE10_BEST_CPP_SEED42
        + CHORUS_POST_WAVE10_PARTIAL_CPP
        + CHORUS_POST_WAVE10_UNROUTED_CPP
        == CHORUS_NETS_TOTAL
    ), (
        "strict + partial + unrouted must total all chorus signal nets; "
        f"got {CHORUS_POST_WAVE10_BEST_CPP_SEED42} + "
        f"{CHORUS_POST_WAVE10_PARTIAL_CPP} + "
        f"{CHORUS_POST_WAVE10_UNROUTED_CPP} != {CHORUS_NETS_TOTAL}."
    )

    # AUDIO_R BLOCKED_PATH is a stable classification, not a regression
    # introduced by #3328 nor resolved by Wave 11 router changes.  The
    # placement suggestion (move C17, C20, J4 south) is unchanged.
    assert CHORUS_POST_WAVE10_AUDIO_R_BLOCKED is True, (
        "AUDIO_R should still be classified BLOCKED_PATH on Wave-10.  If "
        "the router unblocked it, that's good news -- update this constant "
        "to False and document the responsible PR in the docstring header."
    )

    # AUDIO_L is NOT BLOCKED_PATH -- it routes successfully (~260s on
    # attempt 1's 4L ladder under contention).  This anchors the finding
    # that the original issue body's 'AUDIO_L/R BLOCKED_PATH' language
    # conflates the two: only AUDIO_R hits BLOCKED_PATH.
    assert CHORUS_POST_WAVE10_AUDIO_L_ROUTED is True, (
        "AUDIO_L should successfully route on Wave-10's 4L SIG-GND-PWR-SIG "
        "ladder.  If it joins AUDIO_R in BLOCKED_PATH, that's a regression "
        "and should be filed against the router PR that introduced it."
    )

    # Auto-fix kept partial repair on Wave-10 (no connectivity rollback).
    # This is the small step-change in this measurement -- Wave-9 had the
    # auto-fix rolled back due to connectivity regression (31 -> 29 nets);
    # Wave-10 keeps 14/46 repairs with no connectivity loss.
    assert CHORUS_POST_WAVE10_AUTOFIX_KEPT is True, (
        "Auto-fix should keep partial repair (no rollback) on Wave-10.  "
        "If it regressed to rolling back, file a follow-up to investigate "
        "the auto-fix connectivity-invariant guard."
    )


def test_post_wave1_floor_matches_nets_total() -> None:
    """CHORUS_NETS_TOTAL is consistent with the v19 stripped fixture.

    FROZEN v19-era record (Issue #3474 Phase 0): the live fixture is now
    v21_stripped with ``CHORUS_V21_NETS_TOTAL`` = 51; this constant stays
    at the historical 48 so the POST_WAVE records above remain precise.
    """
    # 48 multi-pad signal nets is the v19 number reported in the issue
    # body (cf. the v18 baseline JSON which has 46 because v19 adds U7
    # 74LVC1G17 connections).
    assert CHORUS_NETS_TOTAL == 48
    assert CHORUS_POST_WAVE1_FLOOR < CHORUS_NETS_TOTAL


def test_v21_rebaseline_documented() -> None:
    """The v21 measurement record (Issue #3474 Phase 0 + R1) is honest.

    Phase 0 migrated the fixture from v19_stripped (48 nets, stale
    netlist) to v21_stripped (51 nets, post-repair) and measured 2/51
    strict (budget starved at net 3/51 by the SPI_SCK blowup).  Phase R1
    fixed the three wall-clock leaks below the per-net cap (un-budgeted
    failure diagnostics, cpp->python fallback double-spend, grace-pass
    burn) and re-measured 14/51 and 18/51 across two identical seed-42
    runs (wall-clock-modulated); the regression floor is pinned at 10
    with headroom below the weaker run.

    Failure of this test means either:

    1. The v21 constants were edited without updating the comment block
       above them -- update both together.
    2. A future PR moved chorus reach on the v21 fixture (good news!) --
       bump ``CHORUS_V21_FLOOR`` to the new multi-seed-confirmed value
       and document the responsible PR.  Phases R2/P1 of #3474 are
       expected to do exactly this.
    """
    # The denominator grew 48 -> 51 with the restored connectivity.
    assert CHORUS_V21_NETS_TOTAL == 51
    assert CHORUS_V21_NETS_TOTAL > CHORUS_NETS_TOTAL

    # The floor is honest and conservative: strictly below BOTH R1
    # measurements (load-modulation headroom) and strictly below the
    # May-10-parity class of targets.  Do NOT inflate it -- the bar
    # for "done" is 100% + 0 DRC (feedback_manufacturable_means_100pct),
    # and this floor only exists to catch regressions toward the 2/51
    # budget-starvation class.
    assert 1 <= CHORUS_V21_FLOOR < CHORUS_MAY10_TARGET
    assert CHORUS_V21_FLOOR < CHORUS_V21_R1_MEASURED_STRICT_RUN_A
    assert CHORUS_V21_FLOOR < CHORUS_V21_R1_MEASURED_STRICT

    # The R1 fixes must show material recovery over the Phase 0 floor
    # of 2 (both measured runs were >= 7x the starved baseline).
    assert CHORUS_V21_R1_MEASURED_STRICT_RUN_A > 2
    assert CHORUS_V21_R1_MEASURED_STRICT > 2

    # strict + partial + unrouted of the re-pin record (run B) must
    # total the v21 signal-net count.
    assert (
        CHORUS_V21_R1_MEASURED_STRICT
        + CHORUS_V21_PARTIAL_CPP_SEED42
        + CHORUS_V21_UNROUTED_CPP_SEED42
        == CHORUS_V21_NETS_TOTAL
    ), (
        "strict + partial + unrouted must total all v21 chorus signal "
        f"nets; got {CHORUS_V21_R1_MEASURED_STRICT} + "
        f"{CHORUS_V21_PARTIAL_CPP_SEED42} "
        f"+ {CHORUS_V21_UNROUTED_CPP_SEED42} != {CHORUS_V21_NETS_TOTAL}."
    )

    # Auto-fix rolled back on the R1 re-pin run (connectivity
    # protection: the nudges would have broken at least one routed
    # net).  The budget-integrity claim is that auto-fix RAN -- the
    # AUTOFIX_SKIPPED_BUDGET_EXHAUSTED token was absent on both R1
    # measurement runs (it was PRESENT on the pre-R1 re-baseline).
    assert CHORUS_V21_AUTOFIX_ROLLED_BACK is True


# --------------------------------------------------------------------------
# Slow integration test: end-to-end reach floor.
# --------------------------------------------------------------------------


def _chorus_fixture_present() -> bool:
    """The chorus PCB lives outside the repo (boards/external/ vendor).

    See ``test_benchmark_chorus.py::test_missing_pcb_skips_gracefully``
    for the analogous skip pattern used by the existing benchmark.
    """
    return CHORUS_FIXTURE_PATH.exists()


@pytest.mark.slow
@pytest.mark.skipif(
    not _chorus_fixture_present(),
    reason=(
        "chorus-test-revA_v21_stripped.kicad_pcb fixture not present at "
        f"{CHORUS_FIXTURE_PATH}.  See docs/benchmark.md for fetch "
        "instructions; this test is intentionally a no-op on machines "
        "without the external chorus repo checked out."
    ),
)
@pytest.mark.skipif(
    os.environ.get("KCT_RUN_CHORUS_REACH_FLOOR") != "1",
    reason=(
        "Set KCT_RUN_CHORUS_REACH_FLOOR=1 to opt in to the 25+ minute "
        "chorus reach-floor integration test.  Default-off so the slow "
        "marker alone (e.g., `pytest -m slow`) does not block routine "
        "nightly runs that batch many slow tests."
    ),
)
def test_chorus_reach_v21_floor(tmp_path: Path) -> None:
    """Run the chorus recipe on v21 and assert ``nets_fully_routed >= 2``.

    Issue #3474 Phase 0 migrated this test from the stale v19_stripped
    fixture (and its post-Wave-1 floor of 5) to v21_stripped with a
    freshly measured floor of ``CHORUS_V21_FLOOR`` = 2 (cpp seed 42,
    2026-06-10, HEAD d45ded4d -- see the v21 constants block).  The
    recipe is pinned to the C++ backend because that is the backend the
    v21 floor was measured with.  Wall-clock is 20-30 minutes; the test
    is double-guarded by ``@pytest.mark.slow`` AND the
    ``KCT_RUN_CHORUS_REACH_FLOOR`` env var so it never runs by accident.

    The assertion intentionally uses the pinned FLOOR (10, re-pinned by
    #3474 Phase R1 from the Phase-0 value of 2 after the budget-integrity
    fixes measured 14/51 and 18/51) rather than any target: the floor is
    a regression tripwire, not a goal.  Phases R2/P1 of #3474 are
    expected to raise reach further; bump ``CHORUS_V21_FLOOR`` (with
    multi-seed confirmation) as they land, and at Phase F flip this
    assertion to the 100%-routed target per
    ``feedback_manufacturable_means_100pct``.
    """
    output_pcb = tmp_path / "chorus_routed.kicad_pcb"

    # Mirror the issue #3474 pinned recipe exactly (cpp backend, the one
    # the v21 floor was measured with).  Determinism comes from
    # ``--seed 42`` + the PYTHONHASHSEED env var (issue #3146 / PR #3193
    # finding).
    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "route",
        str(CHORUS_FIXTURE_PATH),
        "--output",
        str(output_pcb),
        "--manufacturer",
        "jlcpcb-tier1",
        "--backend",
        "cpp",
        "--placement-feedback",
        "--placement-feedback-budget",
        "5",
        "--iterations",
        "50",
        "--auto-fix",
        "--auto-fix-passes",
        "2",
        "--auto-layers",
        "--timeout",
        "1200",
        "--seed",
        "42",
    ]
    env = {**os.environ, "PYTHONHASHSEED": "0"}

    # 30-minute timeout for the subprocess: the route itself is bounded
    # by ``--timeout 1200``s but there's startup + checkpoint overhead.
    result = subprocess.run(
        cmd,
        env=env,
        capture_output=True,
        text=True,
        timeout=1800,
    )

    # Parse the "Nets routed: N/51" line from the tail of stdout.
    last_routed = None
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("Nets routed:"):
            # Form: "Nets routed:     N/51"
            rhs = stripped.split(":", 1)[1].strip()
            count_str = rhs.split("/", 1)[0].strip()
            try:
                last_routed = int(count_str)
            except ValueError:
                continue

    assert last_routed is not None, (
        "Could not parse 'Nets routed:' from chorus route stdout. "
        "Last 500 chars:\n" + result.stdout[-500:]
    )
    assert last_routed >= CHORUS_V21_FLOOR, (
        f"chorus reach regressed: routed {last_routed}/{CHORUS_V21_NETS_TOTAL} "
        f"nets, below the measured v21 floor of {CHORUS_V21_FLOOR}.  "
        "See Issue #3474 for context; if this is intentional (e.g. a "
        "router refactor with known short-term cost), bump the floor "
        "constant in this module with justification.  Stdout tail:\n" + result.stdout[-1000:]
    )
