## Summary

Tighten the pad-exit relaxation in both router backends so the A* refuses to step a candidate trace centerline into the *inner* edge of an adjacent foreign pad's clearance halo. Adds `Pathfinder::is_foreign_pad_metal_within_radius` (C++) and `Router._is_foreign_pad_metal_within_radius` (Python) and gates the existing `is_clearance_only && is_pad_exit` branch with the new check. Adds 18 regression tests covering both backend helpers and a symmetry sweep.

## Pivot from the issue body

The curator pointed me at "Mechanism A + option 2" — bump `trace_radius_cells` by `(net_trace_width - rules.trace_width) / 2` so per-net wider traces compensate for the global-sized grid halo. Empirically that hypothesis does **not** hold on board 05:

- All 5 offender net classes (ISENSE_A+ → ANALOG, SWDIO/SWCLK → DEBUG, GATE_DRV_AH/PWM_AH → SIGNAL) map to `NET_CLASS_AUDIO` / `NET_CLASS_DEBUG` / `NET_CLASS_DIGITAL`, **all with `trace_width=0.2 mm`**.
- Global `rules.trace_width = 0.2 mm` (CLI default; the JLCPCB profile floors are checked by DRC, not by the router rules).
- Routed PCB segments for the offender nets are uniformly 0.2 mm wide.

So `extra = max(0, (net_trace_width - rules.trace_width) / 2) = 0` for every offender net. Option 2's radius bump would be a no-op. I had to find a different mechanism.

## Real mechanism

The grid halo at `_add_pad_unsafe` extends `clearance + trace_width/2 = 0.25 mm` from pad metal. On LQFP-32 0.8 mm pitch (U10's STM32G431), the halos of adjacent pads touch with zero gap (`0.8 - 2 * (0.15 + 0.15) = 0.2 mm` of usable channel, swallowed by halo overlap). The pad-exit relaxation at `pathfinder.cpp:680/1175` and `pathfinder.py:2674` then admits ANY foreign clearance-only halo cell while exiting same-net pad metal — including cells at the *inner* boundary of the adjacent halo, where the trace edge would land inside the foreign pad's required-clearance band.

This PR adds the missing geometric guard: when the relaxation considers a foreign clearance-only halo cell, it now also checks that no `pad_blocked && net != routing_net` cell sits within `trace_radius_cells` Chebyshev distance of the candidate. Halo overlap with own pad metal (`is_in_start_metal || is_in_end_metal`) continues to be admitted by the earlier branch unchanged, so the pre-existing `test_route_to_pad_adjacent_to_net0_pad` semantics are unaffected.

## Changes

- `src/kicad_tools/router/cpp/include/pathfinder.hpp` — declare `is_foreign_pad_metal_within_radius` on the public surface (also bound to Python for unit tests).
- `src/kicad_tools/router/cpp/src/pathfinder.cpp` — implement the helper; call it from the pad-exit relaxation in both the one-shot `route()` loop (line ~680) and the resumable `run_astar_loop()` (line ~1175). Emits `reason=pad_exit_clearance_too_tight` under `KICAD_ROUTER_TRACE_ASTAR=1`.
- `src/kicad_tools/router/cpp/src/bindings.cpp` — expose the helper to Python (`is_foreign_pad_metal_within_radius`).
- `src/kicad_tools/router/pathfinder.py` — add `Router._is_foreign_pad_metal_within_radius`; call it from the pad-exit relaxation in `route()` (~line 2674) and from the diff-pair `_coupled_route_internal` loop (~line 4163).
- `tests/test_router_pad_exit_clearance_guard.py` (NEW) — 18 regression tests covering the Python helper, the C++ binding, and a symmetry sweep on an LQFP-like 3-pad fixture.

## Acceptance criteria verification

| AC | Status | Notes |
|---|---|---|
| 1. Board 05 cpp `clearance_pad_segment ≤ 2` across 3 regens | ❌ NOT MET | Stuck at 9 across 3 regens — see "Why ACs 1-5 are not met" below |
| 2. Board 05 cpp `clearance_pad_via ≤ 1` across 3 regens | ✅ MET | 1 across all 3 regens |
| 3. Board 05 cpp blocking total ≤ 9 across 3 regens | ❌ NOT MET | Stuck at 12 (9 pad_segment + 1 pad_via + 1 segment_segment + 1 segment_via) |
| 4. U10-17 -0.265 mm violation closed | ❌ NOT MET | Same residual — this case is **not** Mechanism A; see analysis |
| 5. Remove `--backend python` pin from `boards/05-bldc-motor-controller/design.py` | ❌ NOT MET | Cannot remove without (1)-(4) — pin stays |
| 6. Softstart routing reach unaffected | ✅ MET | No `_build_pad_channel_budgets()` changes |
| 7. Board 07 PYTHONHASHSEED determinism unaffected | ✅ MET | `tests/test_board_07_matchgroup_test.py`: 48/48 passed |
| 8. C++ A* tests pass + new regression test for Mechanism A | ✅ PARTIAL | 42/42 cpp backend tests passed + 18 new tests passing; the new tests exercise the conceptual guard but the regression remains open on board 05 because the live mechanism differs from Mechanism A |

## Why ACs 1-5 are not met

Three sub-mechanisms in the residual on board 05, only one of which the fix in this PR is shaped to address:

1. **Pad-exit relaxation overshoot in the pad-pitch-touching gap (THIS PR FIXES THIS CASE GENERALLY).** When the A* relaxes the foreign-halo check during pad-exit on dense-pitch packages, it can step the centerline into the inner halo edge. The new guard rejects those steps. Unit tests confirm the helper does what it advertises. But on board 05's specific routes, the A* trace produced 0 `pad_exit_clearance_too_tight` C++ rejections (KICAD_ROUTER_TRACE_ASTAR=1 confirms zero). The C++ A* simply does not visit the cells near U10 — the trace approaches those pads via the B.Cu layer where SMT pad metal is not marked, then drops a via.

2. **B.Cu trace under SMT pads (live mechanism on board 05, NOT FIXED).** SMT pads are marked `pad_blocked=True` only on their copper layer (F.Cu). On B.Cu the same (x, y) cells are unblocked. The A* searches freely on B.Cu and then emits a via at the end of a B.Cu run. The F.Cu side of the via overlaps the F.Cu pad metal of a foreign pin — producing the `clearance_pad_via` (-0.300 mm at U10-17) and the coincident `clearance_pad_segment` (-0.265 mm) the issue calls out. The fix here doesn't apply because the A* never traverses an F.Cu pad-exit clearance cell on this path. A proper fix needs the cpp `is_via_blocked_diag` (which already scans all layers) to actually fire on the via candidate placement — which it should, since the F.Cu cell at the via location IS pad-metal-blocked. There is likely a separate bug where the via emit path bypasses `is_via_blocked_diag` (in-pad-via rescue at `escape.py:5104-5117`, which the existing code explicitly comments will "trigger DRC errors at the manufacturer's clearance rule"). Filing a follow-up issue.

3. **Sub-127um positive `clearance_pad_segment` at U3-26 / U3-18 / U3-46 / U10-24 (live mechanism on board 05, NOT FIXED).** These violations (0.029 - 0.110 mm) appear even with `--no-optimize`, so the segment optimizer isn't responsible. They're consistent with grid-quantization in the post-route validator: the trace center is at a grid cell whose Chebyshev distance to a foreign pad metal cell rounds to `trace_radius_cells` but whose Euclidean distance is slightly less than `(trace_width/2 + clearance)`. The fix here would need to also catch these cases, but they require an Euclidean (not Chebyshev) post-step check, which is a deeper change to the A* expansion model than this PR can take on. Filing a follow-up issue.

The **conceptual** Mechanism A fix lands. The **empirical** board-05 mechanism is mostly (2) and (3), not (1). The curator's hypothesis that the U10-17 case is "same class as Mechanism A" is incorrect: it's a via-placement bypass of `is_via_blocked_diag`, not a pad-exit relaxation overshoot.

## What I would recommend next

- **Follow-up issue for Mechanism (2)**: in-pad-via rescue at `escape.py:5104-5117` emits vias with full awareness that they violate DRC; the empirical residual on U10-17 comes from there. Either rip out the in-pad-via rescue when `manufacturer.via_in_pad_supported=False`, or make `is_via_blocked_diag` mandatory at emit time.
- **Follow-up issue for Mechanism (3)**: the sub-127um positive violations need an Euclidean post-step check in the A* expansion (Chebyshev metric is too loose). This is a wider change in scope than #3226.
- **Keep `--backend python` pin in board 05's `design.py`** until (2) and (3) are resolved. The empirical baseline (1 `clearance_pad_segment`) on the Python backend matches the routed-drc floor; switching to C++ adds 8 surface-pad-pitch violations that this PR cannot close.

## Test plan

- [x] New regression tests: `tests/test_router_pad_exit_clearance_guard.py` (18/18 passing)
- [x] Existing C++ foreign-pad-metal rejection tests: `tests/test_router_cpp_foreign_pad_metal_rejection.py` (9/9 passing)
- [x] Existing C++ backend tests: 42 passed
- [x] Existing Python pathfinder/grid tests: 153 passed
- [x] Wider router subset (`pytest -k router`): 1632 passed; the 5 failing tests are all pre-existing on `origin/main` (verified by `git stash` + re-run)
- [x] Board 07 PYTHONHASHSEED determinism: 48 passed (AC #7)
- [x] Board 05 fresh regens with `--backend cpp` x 3: residual unchanged at 12 blocking errors (was the baseline post-#3225 floor, not a regression)

## Notes on AC compliance

This PR ships a useful general fix (pad-exit relaxation overshoot is a real bug class that **could** be the root cause on a different dense-pitch board) but does not close the board 05 floor that AC #1-5 demand. The route from "Mechanism A" to "remove --backend python pin" runs through a board whose live mechanism is different. I recommend treating this as `Refs #3226` (not `Closes`) so the issue stays open for follow-ups (2) and (3) above.

Refs #3226
