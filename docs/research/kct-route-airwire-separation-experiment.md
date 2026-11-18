# Does an airwire-separation cost term help `kct route`? (measured: no)

Issue #8999 (WeaveEngine evaluation #8993, epic #5509 ordering class).
Companion to [`kct-route-net-order-experiment.md`](kct-route-net-order-experiment.md)
(#5787 step 2, the prior null result for the *same signal* applied as a net
order) and
[`kct-route-phase-profile.md`](kct-route-phase-profile.md).

**Verdict: `--airwire-separation-weight` is NOT proposed as a default.** It
ships as an opt-in flag (default `0.0` = off, byte-identical cost model). At
weight `1.0` (and at a gentler `0.25` on the two boards that responded at
all) **no board gained completion or shed rip-up rounds, and two of three
regressed on vias and wirelength**. Per the issue's kill criterion — stated
up front precisely so a Builder would not be pushed to manufacture a win —
this is a **complete, successful outcome**: the term is measured, the null
result is recorded, the flag stays opt-in, and the issue closes as
measured-and-rejected.

Tool: `scripts/research/net_order_experiment.py` (`route` sub-command; the
`airwire` variant, the `mains` board and the `--nogood` hit-rate reporting
were added by this issue). Implementation under test:
`src/kicad_tools/router/airwire_cost.py` (plane construction + refresh),
the `_airwire_cost` plane and the read-time term in
`RoutingGrid.get_negotiated_cost`, and the C++ mirror
(`GridCell::airwire_cost`, `Grid3D::clear_airwire_costs` /
`set_airwire_cells`, summed in `Grid3D::get_negotiated_cost`; build version
66 → 67 — 66 was consumed on `main` by Issue #8994 while this branch was
in review).

## The mechanism under test

Every unrouted connection has a naive desired route: the straight line
between its endpoints. The negotiated cost model's two existing terms are
*retrospective* — `present_cost` and `history_cost` only rise once a cell has
been over-used by committed copper. The experiment priced the *prospective*
signal instead: a per-cell integer count of how many still-unrouted nets'
minimum-spanning flight-line skeletons cover the cell (the routing net's own
desire lines excluded), recomputed from the committed set before every
per-net search, added to the negotiated cost as `weight * count` on every
layer. The intent: a candidate path that slips around other nets' pending
desire lines should be preferred to one that cuts across them, attacking the
epic #5509 class where an early net's convenient route makes a later net
impossible.

This is mechanistically distinct from #5787's negative result, which applied
the same flight-line crossing signal as a one-shot *net order*. Here it is a
per-cell cost inside the search, continuously decaying as nets commit. The
prior null result is why the kill criterion was fixed in advance: "if no
board shows a completion gain or a reduction in rip-up rounds without a via
or wirelength regression, record the null result, the flag stays opt-in, and
close." That is what happened.

## What was measured

`uv run python scripts/research/net_order_experiment.py route --boards
02,03,mains --variants default,airwire --work-dir <dir> --seed 42 --nogood`,
plus a weight sweep point (`--airwire-separation-weight 0.25`) on the two
boards whose copper responded at 1.0. Boards: **02** (charlieplex, 10
routable nets — the #5787 board where crossing pressure displaces 2 nets),
**03** (USB joystick, 24 nets, displaces 20) and **mains**
(`boards/synthetic-mains-complete/`, the public #8605 fixture distilled from
the private #8610 dense mains board, which a Builder cannot access). The
`airwire` arm uses weight 1.0. Same `--seed 42` in every arm; the C++
backend was built (`kct build-native --check`, build version 67) and
confirmed active before timing anything.

Load-independent columns first; see "Timing" below for why wall-clock is not
a finding on this host.

| Board | Arm | Nets | Connections | Vias | Wirelength mm | Iters | A* calls | Overflow trajectory |
|---|---|---|---|---|---|---|---|---|
| 02 | default | 12/12 | 36/36 | **27** | **396.56** | **3** | 142 | [17, 19, 33] |
| 02 | airwire w=1.0 | 12/12 | 36/36 | 41 | 406.21 | 4 | 186 | [38, 22, 34, 23] |
| 02 | airwire w=0.25 | 12/12 | 36/36 | 35 | 403.29 | 6 | 322 | [27, 20, 23, 18, 21, 21] |
| 03 | default | 25/27 | 113/115 | 68 | 748.41 | 3 | 302 | [38, 38] |
| 03 | airwire w=1.0 | 25/27 | 113/115 | 68 | 748.41 | 3 | 302 | [38, 38] |
| mains | default | 8/8 | 10/10 | **5** | **364.68** | 2 | 29 | [1, 0] |
| mains | airwire w=1.0 | 8/8 | 10/10 | 6 | 367.63 | 2 | 30 | [1, 0] |
| mains | airwire w=0.25 | 8/8 | 10/10 | 6 | 367.63 | 2 | 30 | [1, 0] |

Completion is unchanged everywhere (02 and mains complete fully in both
arms; 03 leaves the same 2 nets / 2 connections open in both). No board
reduces `negotiated_iterations` or the overflow tail; 02 *increases*
iterations at both weights. Vias regress on 02 (+14 at w=1.0, +8 at w=0.25)
and mains (+1 at both weights); wirelength regresses on the same two boards
and is exactly unchanged on 03, whose aggregate metrics are identical in
both arms at every column — the term genuinely did not change 03's settled
copper, not "changed it invisibly" (per-net refreshes ran 57 times with up
to 1349 covered cells, verified by an instrumented in-process run).

### Nogood-cache interaction

`NogoodCache.fingerprint` hashes every non-`_saved` ndarray on the grid, so
the new `_airwire_cost` plane is folded in automatically (no key change was
needed for correctness). The A/B ran with `KCT_NOGOOD_CACHE=1` in both arms
(`--nogood`) to measure the hit-rate delta: **0.000 in every arm** — the
cache recorded misses only (02: 22 default / 28 airwire; 03: 6 / 6; mains:
11 / 11) and zero insertions, so the airwire refresh invalidated nothing
there was anything to lose. The feared hit-rate collapse did not materialise
because the opt-in cache never reached steady state on these boards in the
first place; the delta is nil.

### Flag-off byte-identity (regression, not a result)

Boards 02 and 03 routed at `--seed 42` with the flag absent, on
`origin/main` (`7bcc9b554`, native v65) and on the branch (native v66):
board 03 byte-identical outright; board 02 identical after normalising the
one kicad-cli-minted footprint-node UUID that differs run-to-run (the
documented #4536 convention — zero non-uuid diff lines, copper identical).
The Python-side inertness is pinned by
`tests/test_airwire_separation_cost_8999.py` (weight `0.0` ⇒ all-zero plane
and `get_negotiated_cost` exactly `present + history`, both backends; a
`0.0 * k` term is float-exact).

## Two dispatch facts worth recording

1. **Board 03 does not route through `route_all_negotiated` at all.** Its
   dispatch is escape pre-phase + two-phase (global corridors + detailed),
   whose per-net funnel is `Autorouter._route_net_with_corridor`, a *sibling*
   of `_route_net_negotiated`. The first 03 A/B row was a silent no-op
   ("Flag applied: False", copper identical to default) until the refresh
   hook was added to the corridor funnel too and the "Airwire separation
   cost:" guard line was printed from the refresh itself rather than
   `route_all_negotiated`. Both changes are in the landed code: any future
   per-net cost work must hook both funnels or it will silently no-op on
   every escape/two-phase board — the #5787 lesson recurring one level down.
2. **The CLI weight is an attribute-on-router** (`_apply_airwire_separation`,
   the `_ripup_strategy` / `report_detour_ratio` pattern) *and* an explicit
   `route_all_negotiated(airwire_weight=...)` kwarg, because board 03's
   adaptive-multi-resolution tail calls `route_all_negotiated` bare on a
   fresh router. A kwarg-only wire would have been dropped there.

## Why the term plausibly hurt

The 02 regression has a mechanism: pricing a cell by how many *unrouted*
desire lines cross it pushes the first nets routed *off* the direct
corridors that later nets' desire lines also want — but on a board where
those desire lines are all roughly parallel (a charlieplex matrix), the
cheapest legal route for net N is typically alongside, not across, the
others' lines, so the term pays detour cost to avoid crossings that were
never going to be forced, and pays again in vias when the detour leaves the
layer pair. #5787 measured the same shape from the ordering direction
(vias 37 → 38 on 02). Two null results from two mechanisms (a one-shot
permutation and a continuous per-cell cost) is now a reasonably strong
statement that this flight-line crossing signal, however intuitive, does
not pay on these boards at any plumbed weight; a third attempt should bring
a different signal, not a different weight.

## Determinism

The plane is a pure function of the committed set: recomputed from scratch
per refresh, integer counts accumulated with `+=` (order-independent and
bit-identical under any rasterisation order — pinned by unit test), the
routing net's own lines excluded, the weight applied at read time. Two
same-seed runs of the fixture board with the flag on produce identical
copper geometry (pinned by unit test). Tie-breaking is the existing
`IndexedAStarQueue` rule, unchanged; the term introduces no new rule.

## Timing is unusable here (host caveat)

All routing-loop/wall numbers above were taken on a shared 8-vCPU AWS
dispatch worker running up to 12 sweeps concurrently (load average varied
during the runs; the profile JSON records it per run). Wall-clock on this
host is contention, not signal — the #5787 note measured two runs with
identical overflow trajectories and byte-identical copper 154 s vs 63 s
apart. Read the load-independent columns (iterations, A* calls, overflow
trajectory, vias, wirelength); the seconds columns are reproduced only for
completeness. Notably the airwire arm's own plane refresh (skeleton
rasterise + sparse C++ push, ≤ tens of ms per net) never showed up as a
detectable stage cost at either weight.

## Reproduce

```bash
uv run kct build-native --check        # expects build version 67
uv run python scripts/research/net_order_experiment.py route \
    --boards 02,03,mains --variants default,airwire \
    --work-dir /tmp/airwire-ab --seed 42 --nogood --json /tmp/airwire-ab.json
```

The harness writes only under `--work-dir`; the repo's `boards/` tree is
never written. Weight sweeps: run `scripts/research/route_phase_profile.py`
directly with `--extra "--seed 42 --airwire-separation-weight W"` and grade
with the same referee functions (that is where the w=0.25 rows came from).
