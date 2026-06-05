## Summary

Geometry-tuning pass on `Autorouter._build_pad_channel_budgets()` for Issue #3201. Extends the per-pad lateral-channel strip to cover the contested post-escape turn column (previously missed by the fixed 4-cell strip when F_CU escape endpoints sit ~8 cells outside the package edge), with a new env-var diagnostic switch following the PR #3222 pattern. Includes 3 new test cases that validate the endpoint-aware extension on a U1 TSSOP-20-mirror fixture.

**Headline AC1 (≥8/10 softstart reach) NOT met.** Worst-of-3 fresh regens hold at 6/10, matching the documented ceiling from PR #3198. An expanded empirical sweep (4 new parameter combinations, recorded in the source comment) confirms the 8/10 target is unreachable through budget-geometry tuning alone — the routing bottleneck is the negotiated rip-up loop, not the search cost function.

## Changes

- `src/kicad_tools/router/core.py`:
  - `_build_pad_channel_budgets()`: extend the lateral-channel strip outward in the escape direction to whichever is farther of `escape_strip_thickness_cells` (the floor, 4 cells) or `escape_endpoint_position + escape_endpoint_margin_cells` (2-cell margin past the virtual-pad escape endpoint). On softstart's U1 east-side F_CU pads (endpoint at `center_x + 3.45`, ~8 cells outside the package edge), this widens the strip from 4 cells to ~10 cells, covering the post-endpoint turn column where N/S detour traffic settles. B_CU corner-routed pads (endpoint INSIDE bbox) fall back to the floor so the budget still appears outside the package.
  - `route_with_escape()`: add `KCT_DISABLE_PAD_BUDGETS` env-var diagnostic that suppresses budget application without recompilation. Mirrors the diagnostic pattern PR #3222 used to confirm board 05 does not invoke this code path.
  - Update the inline empirical-sweep documentation: record the new endpoint-extended geometry's behavior across penalty values 0.25× / 0.5× / 1.0× / 2.0× plus a `perp_extension_cells=8` variation. All non-default penalties either stagnate at 6/10 (parity) or regress to ≤5/10 (excessive cumulative penalty pushes NRST detour around U1 to fail).
  - Update the `geometry derivation` docstring section to describe the new endpoint-aware extension.
- `tests/test_router_cpp_per_pad_channel_budget.py`: add 3 new `TestSoftstartRealisticCluster` test cases:
  - `test_softstart_endpoint_anchored_strip_diverts_post_endpoint_turn` — proves the endpoint-extended strip produces measurably different A* search behavior than a pre-#3201 fixed-thickness strip on a route through the post-endpoint column.
  - `test_b_cu_corner_routed_endpoint_falls_back_to_thickness_floor` — proves B_CU east-side endpoints inside the bbox still get a 4-cell strip anchored at the package edge (no incorrect inward extension).
  - `test_f_cu_outside_endpoint_extends_strip_to_endpoint_margin` — proves F_CU east-side endpoints outside the bbox get a strip extending past the endpoint + 2-cell margin (thickness > 4 cells).

## Empirical findings

| Parameter set | Softstart reach |
|---|---|
| **Baseline (post-#3203):** penalty=0.5×, fixed 4-cell strip | 6/10 |
| New geometry: endpoint-extended, penalty=0.5× (default this PR) | 6/10 (3/3 runs) |
| New geometry: endpoint-extended, penalty=0.25× | 6/10 |
| New geometry: endpoint-extended, penalty=1.0× | 4/10 (regression) |
| New geometry: endpoint-extended, penalty=2.0× | 5/10 (regression) |
| New geometry: endpoint-extended, `perp_extension_cells=8` | 6/10 |
| **`KCT_DISABLE_PAD_BUDGETS=1`** (A/B control) | 6/10 |

The A/B with budgets disabled shows **the budget at default calibration is essentially inert on softstart at HEAD** — disabling it produces the same 6/10 reach with the same per-net timing profile. This is consistent with the curator's note ("8/10 AC1 target requires additional work upstream of the budget").

## Acceptance Criteria Verification

| Criterion | Status | Verification |
|-----------|--------|--------------|
| AC1: Softstart reach ≥ 8/10 (worst-of-3) | **NOT MET** | 6/10 across 3/3 fresh regens. Empirical sweep proves ceiling not movable through budget tuning alone. See "Empirical findings" table above. |
| AC2: Board 05 ≤ 9 blocking DRC | MET | Board 05 routes via `--backend python` and does not invoke `_build_pad_channel_budgets()` (PR #3222 diagnostic). Invariant by construction; regen confirms. |
| AC3: Board 07 deterministic routing preserved | MET | No changes to `PYTHONHASHSEED` plumbing or A* tie-break comparator. |
| AC4: DRC ≤ 3 on softstart (or allowlisted) | N/A | Unchanged from baseline since reach unchanged. |
| AC5: `kct fleet status` ship_ready on softstart | NOT MET | Gated on AC1. |
| AC6: Wall-clock ≤ 8 min softstart | MET | Unchanged (~96s end-to-end). |
| AC7: New test fixture mirrors U1 east-side contention better | MET | Added 3 test cases covering the endpoint-anchored strip extension on U1-shaped geometry, distinguishing F_CU outside-bbox from B_CU inside-bbox endpoint paths. |
| AC8: Geometry-derivation comment explains chosen strip parameters | MET | Updated docstring section and inline tuning comment block in `_build_pad_channel_budgets`. |

## Why AC1 is not met

This is the second consecutive PR against #3201 to land at 6/10 reach (the first was PR #3203, which merged with `Refs #3201` rather than `Closes`). The empirical sweep has now exercised 9 distinct parameter combinations across two geometry generations and consistently demonstrates that the negotiated rip-up loop — not the per-cell A* cost function — is the binding constraint on softstart reach. The 4 stranded nets (GATE_NEG, I_SENSE_OUT, SWCLK, ZC_DETECT) survive the "Initial pass stall" path's BLOCKED_BY_COMPONENT rip-up with a per-net budget of 3; the budget penalty either helps too little (no detour selected) or too much (NRST detour around U1 fails).

The issue body's "no struct field changes" guardrail is correct — but the strip-geometry parameters available within the existing struct have been exhausted. Per the issue body: "If a tunable becomes required that's not exposed through the existing `PadChannelBudget` struct, that's a sign the strategy is wrong — re-scope before adding fields." That is the diagnosis here.

## Recommended follow-up (proposed as separate issue)

A new issue tracking the **negotiated rip-up loop's selection heuristic** is the natural next step. Candidate directions documented in the updated source comment:

1. Improve the rip-up loop's tie-break to better exploit budget hints when picking which net to rip up under congestion.
2. Diversify escape-stub layer assignment in `generate_escape_routes` so adjacent east-side pads don't all converge on the same post-escape column (upstream of the budget code path entirely).

Neither requires C++/nanobind binding changes, so both fit the original scope philosophy of #3201.

## Test Plan

- [x] All 18 pre-existing `test_router_cpp_per_pad_channel_budget.py` tests pass
- [x] All 3 new test cases pass (`test_softstart_endpoint_anchored_strip_diverts_post_endpoint_turn`, `test_b_cu_corner_routed_endpoint_falls_back_to_thickness_floor`, `test_f_cu_outside_endpoint_extends_strip_to_endpoint_margin`)
- [x] Worst-of-3 softstart regen confirms 6/10 reach with new geometry + default 0.5× penalty
- [x] Board 05 regen confirms invariance (≤ 9 blocking DRC)
- [x] `KCT_DISABLE_PAD_BUDGETS=1` A/B control confirms budgets are inert at default calibration (6/10 reach unchanged)

Refs #3201
