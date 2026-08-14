# Board 07 -- Determinism Diagnostic Runs

This directory commits the AC1 / AC4 evidence for Issue #3146 (matchgroup-
routing non-determinism under CI load) as scoped by the issue's curator
analysis.

## Summary

After PR #3192 (#3144 CoupledPathfinder determinism) merged with the
canonical A* tie-break under field name `seq` (2-key comparator:
`f_score`, `seq`), this PR rebased to drop its now-duplicate
implementation under name `insertion_order` (3-key comparator:
`f_score`, `g_score`, `insertion_order`).

Five consecutive local re-routes of `boards/07-matchgroup-test` at
`--seed 42` with `PYTHONHASHSEED=42` against main's `seq` tie-break
produce **identical** results.

| Run | DRC errors (raw / blocking) | Main pass | Normalized PCB md5 |
|-----|------------------------------|-----------|--------------------|
| 1   | 24 / 16                      | 25/31     | 7ce24a45...        |

The `Normalized PCB md5` column hashes the PCB text after stripping
`(uuid ...)` lines (KiCad UUIDs are seeded from `os.urandom` and are
intentionally not deterministic) and sorting the remaining lines (so
emit-order differences in the writer don't mask geometric identity).

## Why 24 raw and not 26

The pre-rebase PR's evidence (before #3192 merged) captured 26 raw
errors against #3193's 3-key `insertion_order` tie-break. Main's
2-key `seq` tie-break (from #3192) produces a marginally different
A* path on a small number of equal-`f_score` ties, which in turn
produces 2 fewer DRC errors on this fixture (24 raw vs 26).

This is exactly the kind of small post-rebase drift the floor's
2-error slack (26 - 24 = 2) is designed to absorb. The strict floor
of 26 still passes (`24 <= 26`).

## Gate counting policy (Issue #3151)

This artifact is checked by TWO gates with DIFFERENT counting policies:

| Gate                                | Filter                       | Count on this artifact |
|-------------------------------------|------------------------------|------------------------|
| `check_routed_drc.py`               | `_count_blocking_errors`     | 16 blocking            |
| `check_matchgroup_coverage.py`      | raw `summary.errors`         | 24 raw                 |

Both gates share the floor of 26 in `.github/routed-drc-tolerance.yml`.
The binding gate is the matchgroup one (raw 24 <= 26).

Follow-up tracked in #3151: port `_count_blocking_errors` into
`check_matchgroup_coverage.py` so both gates use the same blocking-vs-
advisory policy, at which point the floor can drop back to 16.

## Reproducing locally

```bash
cd boards/07-matchgroup-test
PYTHONHASHSEED=42 uv run python generate_design.py --step route --seed 42
```

The `PYTHONHASHSEED=42` prefix is required -- `generate_design.py`
forwards it to the `kct route` subprocess env, but the outer routine
needs it too for any in-process dict/set iteration that affects pre-
route preparation. The CI workflow (`.github/workflows/ci.yml`) also
sets `PYTHONHASHSEED=42` on the matchgroup-routing-regression job.

## Deterministic relief-rescue bound: A/B measurement (#4770)

**Question this section answers** (issue #4770, the stated precondition for
any #4730 reattempt): board 07 measured `+1 net / +5 DRC` when
`DETERMINISTIC_RESCUE_DEFAULT` was flipped to `True`. *Which* rescues
change outcome, *whose* copper carries the extra DRC, and is that DRC
intrinsic to the extra net or an artifact?

**Answer, in one line: none of it is rescue output.** No rescue keeps any
of the nets that changed hands, and none of the extra DRC is on copper a
rescue committed. The deterministic bound *loses* two nets in the
negotiated pass, and the `+1` and the `+5` are both produced afterwards,
by the placement-delta feedback loop keeping a delta it refuses on `main`.

### What was compared

| | **B** -- 10 s wall clock (`main`) | **A** -- iteration-bounded (#4730 flip) |
|---|---|---|
| head / CI run | `6cfa8842` / run `31277512785` | `6071d1b2` / run `31281260812` (reproduced on `721fc052` / run `31283738762`) |
| `Relief-rescue sub-search cap:` | `10.0s wall clock -- deterministic rescue is off for this route` | `iteration-bounded (per-net node-expansion cap; issue #4536)` |

Both arms run the identical command line (the `--deterministic-budget`
`--placement-delta-feedback` invocation `route_pcb` builds in
`../generate_design.py`), so the *only* difference is the value
`Autorouter._relief_subsearch_budget` returns: `RELIEF_SUBSEARCH_BUDGET_S`
(10 s) in B, `RELIEF_SUBSEARCH_SAFETY_BACKSTOP_S` (120 s, non-binding) in A.

**Confound guard.** `Autorouter._post_negotiation_sweep_bounds` takes
`(timeout, per_net_timeout, elapsed, timed_out)` and never reads
`deterministic_rescue`, so the #4159 sweep bound is identical in both arms
by construction (pinned by `TestPlacementDeltaFeedbackCaveat` in
`tests/test_relief_subsearch_budget.py`). Confirmed empirically on a
same-`HEAD` local A/B (see "Host re-run" below): at the *same pass index*
the two arms print the byte-identical line, e.g. for the initial
CLI-driven pass in both:

```
  Post-negotiation sweep bound: 60.0s whole-sweep / 10.0s per-net wall clock -- the caller passed a stage timeout (issue #4159)
```

In *either* arm the later placement-feedback re-routes print
`0.0s per-net` instead of `10.0s` (A: `60.0s / 0.0s`; B: `52.3s / 0.0s`).
That is **#4776** -- the literal `0.0` from
`_normalize_deterministic_budget` reaching `route_all_negotiated` through
`_run_placement_delta_feedback` -- and a `0.0s` per-net budget makes the
sweep a **silent no-op**, in both arms. So the only pass where the sweep
can do anything is the initial CLI-driven one, and there the line is
byte-identical between arms; where the whole-sweep term does differ
(`60.0s` vs `52.3s`, just `min(60 s, remaining)` reacting to elapsed
time), the sweep is already inert. Either way #4776 cannot produce the
delta -- and it is invisible to `Autorouter._relief_subsearch_budget`
anyway, where `0.0` is falsy and selects the same branch `None` does.
This investigation was run against a tree where #4776 was still
**unfixed**; that is a property of these two recorded runs, not a live
confound. It stays a valid confound guard because the sentinel was present
**identically in both arms** -- but note that zeroing the sweep was only the
*narrowest* of its effects; see the next section for what the `0.0` actually
did to the negotiated loop itself. #4776 is now **FIXED**, and the fix was
measured to leave every *outcome* number in this file unchanged.

### #4776 is fixed -- what it actually changed, and why these numbers stand (2026-08-13)

#4776 normalized the `0.0` sentinel at both placement-feedback call sites
(`_run_placement_feedback`, `_run_placement_delta_feedback`) and hardened
`Autorouter._post_negotiation_sweep_bounds` to treat a falsy per-net budget
as absent. Board 07's placement-delta re-routes therefore now print
`60.0s whole-sweep / 10.0s per-net` where they used to print `0.0s per-net`,
i.e. **the sweep is live on those passes for the first time**.

**The sweep was not the only casualty -- do not repeat the earlier framing
of this fix as "sweep-only".** The `or None` at the call sites changes
`per_net_timeout` for the *entire* `route_all_negotiated` call, and `0.0` is
**not** falsy-safe on the way down:

* `derive_iter_per_net_cap(0.0, remaining)`
  (`router/algorithms/negotiated.py`) returns `min(0.0, remaining_cap)` =
  **`0.0`**, where `None` would have returned `remaining_cap` (60.0 for a
  600 s stage). So every rip-up-loop reroute site got a zero cap, not just
  the sweep.
* `NegotiatedRouter.route_net_negotiated` brackets a multi-pin net with
  `net_deadline = time.monotonic() + per_net_timeout if per_net_timeout is
  not None else None` -- an **`is not None`** test, so `0.0` yields an
  already-expired deadline and every RSMT edge is short-circuited to a
  timeout failure **before a single `router.route()` call**. That block sits
  inside `if len(pad_objs) > 2:`, so it is exactly the **≥3-pad** nets that
  were skipped; 2-pad nets took the direct path and survived only because
  the C++ backend is falsy-safe (`timeout_seconds = float(per_net_timeout)
  if per_net_timeout else 0.0`, where `0.0` means *no* deadline there).

So the pre-fix behaviour on a `--deterministic-budget` placement-feedback
re-route was **not** "the rescue sweep is inert". It was: *every net with
three or more pads was skipped outright with zero A\* calls, and only 2-pin
nets routed.* The fix restores multi-pin routing on those passes. It follows
that this fix **does** change the negotiated loop's trajectory on
placement-feedback re-routes -- any claim that it "cannot influence the
negotiated loop" is wrong and must not be reintroduced here.

Because activating a reach-deciding code path is exactly the shape that got
#4730 withdrawn (PR #4748, `6d8e9bd8`), the fix was gated on a back-to-back
host A/B, `origin/main` @ `fe662585` vs. the fix, C++ backend build 19,
`PYTHONHASHSEED=42`, `--seed 42`, same machine, sequential (load average
9-17 throughout -- this host was NOT quiet; CI remains authoritative):

| | base (`fe662585`) | fix |
|---|---|---|
| `Post-negotiation sweep bound:` (initial CLI pass) | `60.0s / 10.0s` | `60.0s / 10.0s` |
| `Post-negotiation sweep bound:` (both PDF re-routes) | **`60.0s / 0.0s`** | **`60.0s / 10.0s`** |
| nets recovered by the sweep, all passes | 0 | 0 |
| final reach | 26/31 | 26/31 |
| LVS open set (by name) | `DQ3, DQ4, MIPI_DAT0_N, TMDS_D0_N, TMDS_D1_N` | *identical* |
| blocking DRC (`--mfr jlcpcb`, advisory-filtered) | **8** (floor 8) | **8** (floor 8) |
| blocking DRC by rule id | `diffpair_length_skew` 4, `diffpair_routing_continuity` 4 | *identical* |
| deltas proposed / kept | 10 / 0 | 10 / 0 |
| routed segments / vias | 826 / 206 | 826 / 206 |
| final routed copper (segments+vias, UUIDs stripped) | — | **byte-identical to base** |

The activated sweep rescues **nothing** on this board: the 5 open nets are
the seed-invariant #3438/#4012 set, and one solo 10 s re-attempt on the live
grid does not close any of them. So the historical numbers above stand
as-is.

Two *probe-level* numbers did move:

* probe 0 `U2 mirror`: base `25 -> 23`, fix `25 -> 22` (reverted in both).
* probe 1 `U3 translate`: base `25 -> 26` (refused by the clearance-regression
  guard), fix `25 -> 25` (refused for no strict routed-net increase).

**Attribute these to the fix, not to host noise.** Both probe passes are
placement-delta re-routes, i.e. exactly the passes where the pre-fix `0.0`
skipped every ≥3-pad net; a changed multi-pin re-route trajectory is the
straightforward explanation for a changed probe reach. Host load plausibly
contributes as well -- `--timeout 600` is a *wall clock*, so iteration count
tracks machine load, and the probe passes exited at 502.4 s / 335.6 s (base)
vs 601.3 s / 600.4 s (fix) under load averages 9-17 -- but load is not the
sole cause, and the "structurally cannot touch the loop" argument that was
originally offered here is **false** (see the mechanism above). Treat these
two numbers as fix-plausible.

What *is* solid is the outcome: **both probes were refused in both arms**,
so the final board is the initial pass's either way, which is why the routed
copper is byte-identical. Note carefully that this is a **coincidence of the
accept gate**, not an invariant -- the fix changes what that gate is choosing
between, and `_normalize_deterministic_budget`'s docstring records the
adjacent failure mode where the placement-delta loop's *relative* accept gate
admits a delta `main` refuses (+1 net / +5 DRC). Board 07 sits at 8 blocking
DRC against a floor of 8, i.e. **zero headroom**, so a future run of this
board must re-measure the probe accept/refuse decisions rather than assume
they replay.

Wall-clock cost of the route step: 28m00s base vs 33m58s fix on this host
(+358 s). The sweep cap accounts for at most ~120 s of that (60 s per
placement-feedback pass, two passes); the residual is best explained by the
negotiated loop actually routing multi-pin nets again on those re-routes,
plus the load-driven pass-duration spread. The board-07 CI job carries a
90-minute allowance.

The two recorded CI runs predate `Autorouter._post_negotiation_sweep_bound_line`
(added by PR #4775), so that line does not appear in their logs at all --
which is itself evidence it cannot be the cause.

### The measured outcome

| | B (10 s wall clock) | A (iteration-bounded) |
|---|---|---|
| initial negotiated pass | **5** rip-up iterations in 600.0 s | **2** rip-up iterations in 602.4 s |
| reach after that pass | 25 -> **26/31** (iter 2 onward) | **24/31** (iter 1, then the stage timeout fires) |
| unrouted entering the delta loop | 5 | 7 |
| deltas proposed / kept | 10 / **0** | 14 / **1** |
| `U2 mirror` probe | routed 25 -> 21, reverted | routed 23 -> 22, reverted |
| `U3 translate` probe | routed 25 -> 25, reverted (*no strict routed-net increase*) | routed 23 -> **27**, **KEPT** |
| final reach | **26/31** | **27/31** |
| blocking DRC (`kct check --mfr jlcpcb`, advisory-filtered) | **8** (== allowlist) | **13** |
| copper-LVS opens | `DQ3, DQ4, MIPI_DAT0_N, TMDS_D0_N, TMDS_D1_N` | `DQS_N, MIPI_DAT0_P, TMDS_D0_N, TMDS_D1_N` |

Both arms' negotiated passes exit on the same `--timeout 600` stage
deadline ("Stopped due to timeout - returning best partial result" in
both logs). The bound does not buy the stage more time; it changes how
that fixed time is spent.

**The deterministic bound *does* deliver what it promises.** The second
A-side run (`721fc052` / run `31283738762`, a different head on a
different runner) reproduces the first line for line: initial pass
24/31 in 598.4 s, 7 unrouted, `Deltas proposed: 14`, `Deltas kept: 1`,
`kept: U3 translate (net MIPI_CLK_N)`, final 27/31, `DRC error count: 13`.
The outcome it makes machine-independent is simply a worse one on this
board. Reproducibility was never the objection; the routed result is.

### Which rescues differ (the per-net answer)

There is **no single net whose rescue flips commit-vs-rollback**, and --
the load-bearing part -- **not one of the nets whose routed state
differs between the arms is a rescue commit in either arm.**

Note first *which* pass produces each arm's artifact. B keeps no delta,
so its artifact is the initial negotiated pass; A keeps the `U3
translate`, so its artifact is the second probe pass. The rescues that
actually **commit** into each shipped board are:

| arm | artifact pass | rescues that committed |
|---|---|---|
| B | initial | `TMDS_D1_P` (x2), `MIPI_DAT1_P`, `MIPI_CLK_N` |
| A | probe 1 (`U3 translate` kept) | `TMDS_D2_N`, `MIPI_DAT1_P` |

Set that against the reach delta -- gained `DQ3`, `DQ4`,
`MIPI_DAT0_N`; lost `DQS_N`, `MIPI_DAT0_P` -- and the two lists are
**disjoint**. Every net that changed hands did so through ordinary
negotiated routing under a different placement, not through a rescue.

What the bound *does* change is the rescue trajectory, wholesale, from
the first pass onward -- a rescue that would have died at 10 s runs on
into the 120 s backstop and displaces every later attempt in the pass.
In the initial pass, for instance:

* `MIPI_CLK_N` commits in B and is never even attempted in A;
  `MIPI_DAT1_P` commits in B and rolls back on victims in A;
* `DQ2` and `DQS_N` are attempted in A and not in B; `MIPI_DAT0_P` is
  attempted in B and not in A;
* `DQ3`, `DQ4`, `TMDS_D0_N` and `MIPI_DAT0_N` are each retried **three
  times** in B where A gets one attempt -- the direct consequence of B
  completing five rip-up iterations to A's two.

The net effect of the rescue layer alone is **negative**: A ends the
negotiated pass at 24/31, two nets *behind* B's 26/31. Board 07 is
sub-search-budget-*starved*, not sub-search-budget-*straddled* the way
board 06 is (#4536); relaxing the bound spends the stage budget that
board 07's reach actually depends on.

#### The single decisive rescue: `DQ3`

Both arms' initial pass reaches `DQ3` at the same point (~253 s / ~265 s
into iteration 1) and both rescues **roll back**. What differs is the
price, straight from the logs' own elapsed markers:

| | B (10 s wall clock) | A (iteration-bounded) |
|---|---|---|
| `DQ4` rescue | `180.0s` -> `250.6s` = **70.6 s**, rolled back (8/9 victims) | `182.8s` -> `262.9s` = **80.1 s**, rolled back (8/9 victims) |
| `DQ3` rescue | `253.2s` -> `255.8s` = **2.6 s**, `has NO relief path (round 2)` | `265.1s` -> `544.2s` = **279.1 s**, ripped 9 blockers, `only 7/9 displaced victim(s) re-landed -- rolling back` |
| what happens next | iter 1 ends 25/31 at ~260 s; **iter 2 runs and reaches 26/31**; iters 3-4 hold | iter 1 ends 24/31; the 600 s stage deadline fires at 602.4 s; **iteration 2 never runs** |

Under the 10 s bound `DQ3`'s probe gives up in 2.6 s. Under the
deterministic bound the same probe *finds* a relief path, rips nine
blockers, and then spends **279 s -- 46 % of the entire 600 s stage
budget** failing to re-land two of them, before restoring the board
verbatim. Counting `DQ4`, arm A spends ~360 s (60 % of the stage) inside
two transactions that both leave the board exactly as they found it.

That is what costs the two nets: **the rip-up iteration that produces
board 07's 26th net is the one the flip cannot afford to run.**

Per-pass totals, same measure:

| pass | B: completed rescue transactions / total s / longest | A: completed / total s / longest |
|---|---|---|
| initial | 21 / 445.6 s / 70.6 s (`DQ4`) | 12 / 514.4 s / **279.1 s** (`DQ3`) |
| probe 0 | 19 / 295.0 s / 42.0 s | 15 / 418.2 s / 134.3 s |
| probe 1 | 21 / 375.8 s / 56.5 s | 18 / 658.4 s / 145.2 s |

Fewer transactions, more seconds, in every pass. (Both arms hit the
`C++ pathfinder gave up ...; falling back to the pure-Python A*` path
about equally often -- 37 times in B, 35 in A -- so the divergence is the
bound, not a different backend mix.)

### Where the +1 net actually comes from

`PlacementDeltaFeedbackLoop.run_delta` keeps a delta on
`new_count > pre_count and not regressed_drc` -- and **both** terms are
**relative**, measured against the state the loop currently holds rather
than against an absolute floor. So:

* on `main` the loop enters with 25 routed and the `U3 translate` probe
  measures 25 -> 25: no strict increase, **reverted**;
* under the flip the loop enters with 23 routed and the *same* target
  footprint's translate measures 23 -> 27: strict increase, **kept**.

The clearance term is relative in the same way, and the host re-run
caught it doing so. There, `main`'s probe *did* gain a net (25 -> 26)
and was still refused -- `revert_reason: "clearance violations 0 -> 2"`
in `output/matchgroup_test_routed_placement_delta.json`, the #4468 guard
board 07's `../README.md` documents for this host. Under the flip the
same move is measured from a worse starting state and **neither** term
fires. A depressed baseline does not just lower the reach bar; it also
lowers the clearance the move has to beat.

(The `25` / `23` are the loop's own internal `_routed_count()`, which
runs one below the pass's reported reach on this board -- the same
off-by-one `../README.md` already flags for its probe table. The
*relative* comparison is unaffected: what matters is that the flip's
baseline is two lower than `main`'s, which is exactly the pass-level
26 -> 24.)

The `+1 net` is therefore not a rescue outcome at all. It is a placement
change that `main` refuses and the flip admits, because the flip
depressed the baseline the accept-gate compares against. Both A-side CI
runs and the local re-run agree on the kept delta (`U3 translate`,
justified by `MIPI_CLK_N`), so this is not a single-run accident.

### Where the +5 DRC is (rule ids and nets)

Blocking (advisory-filtered) errors, from the `BY RULE` block of
`kct check --mfr jlcpcb` in each arm's Board 07 End-to-End job:

| rule id | B | A | delta |
|---|---|---|---|
| `diffpair_length_skew` | 4 | 3 | **-1** |
| `diffpair_routing_continuity` | 4 | 3 | **-1** |
| `clearance_pad_segment` | 0 | 4 | **+4** |
| `clearance_segment_via` | 0 | 2 | **+2** |
| `match_group_length_skew` | 0 | 1 | **+1** |
| **blocking total** | **8** | **13** | **+5** |

First, what actually changed hands. The two open sets differ by five
nets, not one:

* **newly routed under the flip**: `DQ3`, `DQ4`, `MIPI_DAT0_N`
* **newly open under the flip**: `DQS_N`, `MIPI_DAT0_P`
* net `+1`.

Per violation, with the classification the issue asked for
(newly-kept-net / displaced-victim / neighbour). Nets and geometry are
from `kct check --mfr jlcpcb --errors-only --format json` on the A-arm
artifact; the coordinates are the final (sheet-centred) frame:

1. **`clearance_segment_via` x2**, 0.049 mm against the 0.1016 mm jlcpcb
   floor, nets **`DQ1` / `DQ2`**, `F.Cu` at `(124.209, 68.779)` --
   `U1`'s DDR escape. **Neighbour**: both nets are routed in *both*
   arms; neither is newly kept, and no rescue that touched them
   committed.
2. **`clearance_pad_segment` x2**, 0.094 mm, nets **`DQ1` / `DQ2`**,
   `F.Cu` at `(118.5, 68.6)`. **Neighbour**, same two nets.
3. **`clearance_pad_segment` x2**, 0.076 mm, nets **`DQ4` / `DQS_N`**,
   `F.Cu` at `(118.5, 64.6)`. This is the only pair that touches the
   delta: `DQ4` is one of the three **newly-kept** nets and `DQS_N` is
   one of the two the flip **lost** (its pad is still there; its copper
   is not). So the flip's new copper is failing clearance against the
   pad of the net it dropped.
4. **`match_group_length_skew` x1**: `ADDR_BUS` skew **11.0299 mm**
   against a 0.500 mm tolerance. `ADDR_BUS` is `A0`-`A7` -- **disjoint
   from every net whose routed state changed**, so this is a pure
   **neighbour** regression. On `main` the tuner reports
   `ADDR_BUS: 8 members (7 tuned, 0 clean, 0 rolled back ...)` and the
   group lands at 0.000 mm; under the flip it reports `(6 tuned, 0 clean,
   1 rolled back ...)`.
5. The two **negative** entries are not improvements: `DQS_N`/`DQS_P`
   stops producing its `diffpair_length_skew` (11.034 mm) and
   `diffpair_routing_continuity` (4.5 %) findings only because **`DQS_N`
   is unrouted in A**, so the pair is no longer "engaged" and the rules
   stop firing. A lost net is scoring as -2 DRC.

So of the seven new errors, **five sit on copper with no connection to
the reach delta at all** (`DQ1`/`DQ2`, `ADDR_BUS`), and the remaining two
are a newly-kept net clashing with a newly-dropped net's pad. **None is
on copper a rescue committed**: the only rescues that commit into the A
artifact are `TMDS_D2_N` and `MIPI_DAT1_P`, and neither appears in any
of the seven.

Items 1-4 are precisely the cost the board's own `../README.md` already
records for this delta ("two `U1`-escape clearances drop to 0.076 /
0.094 mm against the 0.102 mm jlcpcb floor (blocking DRC 10 -> 13) and
the `ADDR_BUS` length-match tuner is left at 11.03 mm skew instead of
0.000 mm"). The flip did not discover a new failure mode; it re-admitted
a documented bad trade.

### Verdict: intrinsic, or artifact?

**Neither of the two hypotheses the issue offered.** It is not intrinsic
to keeping a net via a rescue -- no rescue keeps any of the nets that
changed hands. And it is not a re-land *ordering* effect inside
`Autorouter._relief_rescue` -- the extra errors are not on displaced
victims either. The rescue layer's only contribution to the delta is the
*time* it spends.

## Proportional relief-rescue transaction bound: A/B measurement (#4781)

**What changed.** Issue #4781 (carved from the #4770 measurement above)
bounds a single `_relief_rescue` transaction -- including its depth-1
nested rescues, which SHARE the parent's allowance -- to
`max(25% of the remaining stage budget, 90 s)`
(`RELIEF_RESCUE_TXN_FRACTION` / `RELIEF_RESCUE_TXN_FLOOR_S` in
`router/core.py`), evaluated only at the existing `_past_deadline()`
check sites. The 90 s floor was calibrated against this file's #4770
tables: the longest transaction the healthy `main` arm ever completes is
70.6 s (`DQ4`, initial pass, CI), so on a main-shaped trajectory the
bound never fires; the pathological `DQ3` transaction (279.1 s at ~337 s
remaining) would be cut at 90 s, returning ~189 s to the rip-up loop.
With no stage deadline (`timeout=None`) the bound is structurally inert.

**What was compared.** Same-host local A/B (Apple Silicon, 28 cores),
both arms `PYTHONHASHSEED=42 uv run python generate_design.py --step
route --seed 42`: **B** = `main` @ `3a37d414`, **A** = the #4781 bound on
top of the same head. The A arm's log prints the new evidence banner 3x
(once per negotiated pass):

```
  Relief-rescue transaction bound: proportional -- one rescue transaction (nested rescues share the parent's allowance) may spend at most max(25% of the remaining stage budget, 90s) before rolling back verbatim (issue #4781)
```

**The measured outcome: a clean null on the healthy trajectory,
identical to design intent.**

| | B (`main`) | A (#4781 bound) |
|---|---|---|
| final reach | 26/31 | 26/31 |
| copper-LVS opens | `DQ3, DQ4, MIPI_DAT0_N, TMDS_D0_N, TMDS_D1_N` | identical (`check_copper_lvs.py --expect-opens` passes both) |
| blocking DRC (`check_routed_drc.py`, jlcpcb) | **8** (== allowlist) | **8** (== allowlist) |
| deltas proposed / kept | 10 / 0 | 10 / 0 |
| transaction-allowance aborts | n/a | **0** |
| normalized PCB md5 (uuid-stripped, sorted) | `797984ea...` | `797984ea...` -- **identical artifact** |

Per-pass rescue-transaction tables (same elapsed-marker method as the
#4770 tables; durations include nested rescues):

| pass | B: transactions / total s / longest | A: transactions / total s / longest |
|---|---|---|
| initial | 31 / 419.4 s / 56.9 s (`DQ4`) | 31 / 376.6 s / 46.1 s (`DQ4`) |
| probe 0 | 48 / 388.2 s / 29.5 s (`MIPI_DAT1_P`) | 48 / 444.5 s / 30.7 s (`TMDS_D1_N`) |
| probe 1 | 33 / 389.2 s / 48.7 s (`DQS_N`) | 33 / 397.9 s / 48.7 s (`DQS_N`) |

Same transaction counts per pass (31/48/33), same commits per pass
(4/5/2), and every transaction sits well under the 90 s floor on this
host -- the differences are wall-clock jitter under load, and the
committed geometry is bit-identical after uuid normalization. The
pathological arm (a 279 s transaction) is exercised by the injected-clock
unit tests in `tests/router/test_relief_rescue.py`
(`TestReliefRescueProportionalBound`) rather than by this fixture, which
does not produce one on `main`'s sub-search bound (`DQ3` gives up in
seconds here -- see the #4770 section above for why the deterministic
bound was what made it pathological).

**Board 06 cross-check** (the board that needs rescues to *complete*):
`check_diffpair_coverage.py boards/06-diffpair-test --seed 42` on the A
arm passes -- signal reach 21/21 (required 21), pour connectivity OK, all
3 diff-pair rules exercised, 18 errors within the 18 allowlist. Board 06
routes with `timeout=None`, so `relief_deadline is None` and the bound is
structurally inert there (the byte-identical no-deadline arm).

The mechanism is **budget displacement**:

1. the deterministic bound lets relief sub-searches run far longer (the
   `DQ3` transaction: 2.6 s -> 279.1 s, rolling back either way);
2. the negotiated stage's `--timeout 600` is unchanged, so it completes
   2 rip-up iterations instead of 5 and ends 2 nets *behind*;
3. `PlacementDeltaFeedbackLoop.run_delta`'s accept-gate is relative, so
   the depressed baseline admits a placement delta `main` refuses;
4. that delta -- not the rescue -- brings both the `+1 net` and the
   `+5 DRC`.

The corollary matters for #4730: **the `+1 net` is not evidence that the
deterministic bound helps board 07.** Held at a fixed placement, the
bound is a straight 2-net regression on this board. The recommendation
recorded on #4730 is therefore *(c) accept the negative result as
permanent* -- not a per-board scoping (which `DETERMINISTIC_RESCUE_DEFAULT
= False` plus board 06's explicit opt-in already is) and not a rescue
change in service of a reattempt (there was never a reach gain to bank).

The investigation did expose an independent router defect, filed as
**#4781**: a single rescue transaction can consume ~46 % of a stage
budget and roll back, because `Autorouter._relief_rescue`'s only
whole-transaction bound is the *stage* deadline (#3413) and its
sub-search bound (#4536) bounds the parts, not their product. That is a
routing-throughput issue in its own right, not a route back to this flip.

### Host re-run (same `HEAD`, both arms)

The two CI runs above are the authoritative measurement (board 07's
`../README.md` "Host-vs-CI divergence" section documents that
probe-level routed counts differ between macOS arm64 and CI even at a
fixed seed). A same-`HEAD` local A/B was run on top of `c7313b39` for the
two things the historical CI logs cannot show -- that the
`Relief-rescue sub-search cap:` line differs between arms and the
`Post-negotiation sweep bound:` line does not -- with
`PYTHONHASHSEED=42 uv run python scripts/ci/check_matchgroup_coverage.py
boards/07-matchgroup-test --seed 42` and the C++ backend built. The host
was heavily loaded by concurrent agents (load average 40-66 on 28 cores),
which perturbs the wall-clock arm by construction; the host numbers are
therefore corroboration and the CI numbers stand.

It corroborates cleanly. **Both headline outcomes reproduced on the
host**, despite the load:

| | local B (10 s wall clock) | local A (iteration-bounded) |
|---|---|---|
| `Relief-rescue sub-search cap:` | `10.0s wall clock -- deterministic rescue is off ...` | `iteration-bounded (per-net node-expansion cap; issue #4536) ...` |
| initial pass | 25/31 at iter 1, **26/31** at iter 2, held to iter 10 | **24/31** at iter 1 (matching CI exactly), 25/31 at iter 2 |
| deltas kept | **0** (`U3 translate` reverted, `clearance violations 0 -> 2`) | **1** -- `kept: U3 translate (net MIPI_CLK_N)` |
| final reach / blocking DRC | **26/31** / **8** (gate exit 0) | **27/31** / **13** (gate exit 2) |

The one host/CI difference is how many rip-up iterations fit in the
600 s stage -- unsurprising, since the *stage deadline is still a wall
clock* even in the deterministic arm. Worth stating plainly: the #4536
bound makes the rescue's own commit/roll-back decisions
machine-independent, it does **not** make the pass machine-independent,
because the number of iterations that fit is still a function of machine
speed. On board 07 that iteration count is the term that decides reach.

### Reproducing this A/B

```bash
./.loom/scripts/worktree.sh <n> && cd .loom/worktrees/issue-<n>
uv run kct build-native                       # MUST report "available"
# B side: no source change.
# A side: DETERMINISTIC_RESCUE_DEFAULT = True in
#         src/kicad_tools/router/core.py -- nothing else.
PYTHONHASHSEED=42 uv run python scripts/ci/check_matchgroup_coverage.py \
  boards/07-matchgroup-test --seed 42 2>&1 | tee /tmp/board07-<arm>.log
```

Flip the **constant**, not the CLI. Board 07 re-routes through
`PlacementDeltaFeedbackLoop`, whose `negotiated_kwargs` carries only
`timeout` / `per_net_timeout` and forwards no `deterministic_rescue`, so
wiring the flag at the CLI's own `route_all_negotiated` call site would
not reach the passes that produced these numbers -- it would be a
different experiment.

## Per-attempt lattice deadline cap: A/B measurement (#4798)

**What changed.** Issue #4798 caps the lattice engine's absolute
`lattice_deadline` at the *current escalation attempt's*
`_per_attempt_budgeted_timeout` fair slice instead of the whole remaining
run budget, via a new `_lattice_attempt_deadline(args, attempt_timeout)`
helper in `route_cmd.py`. The `_attempt_timeout` computation is hoisted
ahead of `load_pcb_for_routing` at the three multi-attempt escalation
sites (`route_with_layer_escalation`, the rule-relaxation tier ladder,
the combined 2D layers x tiers matrix) so the value is available where
the deadline is stamped. The two single-attempt call sites keep
`_lattice_absolute_deadline` verbatim.

**Why board 07 is expected to be inert.** `generate_design.py` drives
`--strategy negotiated --no-auto-layers --layers 4`, so none of the three
escalation loops runs at all, and the engine is the grid negotiator, not
`--route-engine lattice` (only `Autorouter._negotiate_lattice_netset`
reads `_lattice_deadline`). Per the #4802 precedent, the gate is still
**measured and documented**, not waived.

**What was compared.** Same-host local A/B (Apple Silicon, 28 cores),
`PYTHONHASHSEED=42 uv run python boards/07-matchgroup-test/generate_design.py
<out> --step all --seed 42` with the C++ backend built (build 19) in both
arms. **B** = pristine clone of `main` @ `33666637` (a separate checkout, so
the running arm cannot see the working-tree edits -- board 07 invokes
`kct route` as a *subprocess*, which would otherwise pick them up
mid-run). **A** = the #4798 change on top of the same head. A **second B
run** (`B2`, same pristine clone, no source change) was added as the
run-to-run control.

| | B (`main`) | B2 (control, `main` again) | A (#4798) |
|---|---|---|---|
| final reach | 26/31 | 26/31 | 26/31 |
| copper-LVS opens (`--expect-opens`) | `DQ3, DQ4, MIPI_DAT0_N, TMDS_D0_N, TMDS_D1_N` | identical | identical |
| copper-LVS shorts / vacuous | 0 / 0 | 0 / 0 | 0 / 0 |
| bound pads | 244 | 244 | 244 |
| blocking DRC (`check_routed_drc.py`, jlcpcb) | **8** (== allowlist) | **8** | **8** |
| DRC rule split | `diffpair_length_skew` 4 + `diffpair_routing_continuity` 4 | identical | identical |
| raw `kct check` errors | 13 (8 blocking + 5 advisory `connectivity`) | 13 | 13 |
| deltas proposed / kept | 10 / 0 | 10 / 0 | 10 / 0 |
| final-pass rip-up iterations | 10 | 10 | 10 |
| normalized PCB md5 (uuid-stripped, sorted) | `026d5506...` | `1f36faa9...` | `1f36faa9...` |

**Every gated metric is identical across all three runs.** The md5 column
is the interesting one: `B` and `B2` are the *same code* and still differ,
while `B2` and `A` are **byte-identical after uuid normalization**. So the
copper drift on this fixture is run-to-run wall-clock variance, not the
change -- the same mechanism the #4770/#4781 sections above document
(board 07's stage deadline is still a 600 s wall clock even under
`--deterministic-budget`, so how much of a rip-up iteration fits is a
function of host load). A normalized log diff between `B` and `A` is 30
lines, all of them elapsed-time stamps, one zero-overflow-recovery
denominator (`0/7` vs `0/8`), the stranded-pad coordinates of the same 5
known-open nets, and a 1-segment orthogonal/45-deg split shift.

**Board 04 cross-check (the board this change can actually reach).**
`boards/04-stm32-devboard` is the fixture that runs `--auto-layers
--auto-mfr-tier --deterministic-budget --timeout 600`, i.e. it *does* execute
`route_with_layer_escalation` with a wall-clock budget, so it exercises
the hoisted `_attempt_timeout` (which the negotiated arm still receives
as its `timeout=` kwarg). Same-host A/B, same two checkouts:

| | B (`main`) | A (#4798) |
|---|---|---|
| nets routed | 9/9 | 9/9 |
| routed DRC (`check_routed_drc.py`, jlcpcb-tier1, strict 0) | **0** | **0** |
| copper-LVS (`check_copper_lvs.py`, plain clean gate) | clean | clean |
| recipe verdict | ERC PASS / Routing SUCCESS / DRC PASS / LVS PASS / Overall PASS | identical |
| normalized PCB md5 (uuid-stripped, sorted) | `4444f351...` | `4444f351...` -- **identical artifact** |

Board 04 is the strong result: byte-identical copper on the one fixture
whose route actually enters an escalation loop under `--timeout`.

### Reproducing this A/B

```bash
# B side: a PRISTINE checkout (clone, not your worktree) at the base commit.
git clone --shared --no-checkout <repo> /tmp/ab-before && cd /tmp/ab-before
git checkout <base-sha> && uv sync --frozen --extra dev --python 3.12
uv run kct build-native                       # MUST report "available"
PYTHONHASHSEED=42 uv run python boards/07-matchgroup-test/generate_design.py \
  /tmp/board07-before --step all --seed 42

# A side: the same command from the issue worktree.
```

The separate checkout is load-bearing, not hygiene: `route_pcb()` shells
out to `python -m kicad_tools.cli route`, so a "before" arm launched from
the worktree you are editing will silently pick up your changes on its
*next* subprocess. Run at least two B arms before attributing any copper
difference on this board to a diff.

## Files in this directory

- `README.md` -- this file.
- `per-run-net-order.txt` -- captured net iteration order from the
  original 5-run validation (pre-rebase, against #3193's
  `insertion_order` tie-break). Preserved for historical context.
