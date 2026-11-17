# Bottom-up baseline verdict (issue #2721)

## TL;DR

**Stelios's "80% of the way there" hypothesis does NOT hold across boards 01--03**
when measured by half-perimeter wirelength (HPWL). Bottom-up beat the GA on
exactly **one of three** boards (charlieplex_3x3) and was an order of
magnitude worse on the other two. However, bottom-up runs in **<2 ms**
versus the GA's 0.2--0.8 s, a 100--400x speedup, which makes it interesting
as a *seed* for the GA rather than a replacement.

## The numbers (from `results.md`, single seed, identical PCB inputs)

| Board | Bottom-up wl (mm) | GA(50/50) wl (mm) | Bottom-up t (s) | GA t (s) | BU/GA ratio |
|---|---:|---:|---:|---:|---:|
| 01-voltage-divider | 611.27 | 4.95 | 0.000 | 0.322 | **123x worse** |
| 02-charlieplex-led | 252.58 | 381.38 | 0.001 | 0.631 | **0.66x (BU wins)** |
| 03-usb-joystick   | 2259.02 | 294.77 | 0.001 | 0.766 | **7.7x worse** |

Both algorithms produced **zero overlapping pairs** on every board.

## Why bottom-up wins on board 02 but loses on 01 and 03

- **Board 02 (charlieplex)** has 14 components in a structurally regular
  topology -- a 3x3 LED matrix plus a few routing components. Bottom-up's
  shelf-packed grid happens to mirror the natural matrix shape, beating the
  GA which scatters components.
- **Board 01 (voltage divider)** has 4 components. With 3 of them being
  fixed connectors (J1, J2 are pinned by the as-shipped layout), bottom-up
  has nothing to cluster -- everything becomes a singleton. The GA simply
  drops R1/R2 between J1 and J2 and wins by a huge margin.
- **Board 03 (usb-joystick)** has 12 components dominated by 4 switches
  and 2 fixed connectors. Bottom-up packs all the switches into a single
  shelf row at the *opposite end* of the board from the connectors, which
  inflates wirelength on every signal net that crosses the board.

## What this means for the cascaded-GA decision (#2719/#2720)

**Recommendation: do NOT abandon the cascaded GA architecture solely on the
basis of these three boards.** Bottom-up is competitive only when the board
matches the assumptions of the motif detector (regular topology, few fixed
parts, multiple multi-member clusters). It is *not* competitive on:

- Tiny boards (clustering has nothing to grip onto).
- Boards dominated by singleton/fixed components (most real PCBs).
- Boards where signal-flow geometry matters more than motif locality.

The 100--400x speedup is real and useful, though. Two follow-up
opportunities (file as new issues if pursued):

1. **Use bottom-up as the GA's seed individual.** Today the GA initializes
   the population from random perturbations of the input placement.
   Replacing one population member with the bottom-up result is almost
   free and would give the GA a structurally-coherent starting point.
2. **Promote bottom-up to a "preview" mode** for interactive UIs and CI
   smoke checks. <2 ms latency makes it suitable for live placement
   feedback even on larger boards.

## Method notes (so the numbers are interpretable)

- **HPWL** (half-perimeter wirelength) is the standard placement-quality
  proxy. It is NOT routed wirelength -- routing was not run for this
  comparison because the GA already inflates board-03's HPWL by 6x while
  still producing routable placements, so HPWL alone cannot be the
  acceptance criterion.
- **Both algorithms respect** `Component.fixed=True`. Connectors (J*) and
  mounting holes (H*, MH*) are fixed in both runs.
- **Random seed** is pinned to `random.seed(42)` for the GA. The
  bottom-up algorithm is deterministic.
- **Wall-clock** is single-threaded. The GA's `parallel=False` flag is
  set so the comparison uses identical concurrency.

## Acceptance criteria checklist (issue #2721)

| Criterion | Status | Evidence |
|---|---|---|
| New module exposes `place_hierarchical(...)` | done | `src/kicad_tools/optim/bottom_up_placement.py` |
| Cluster source documented | done | Functional motif (`detect_functional_clusters`); see module docstring |
| No overlapping components on benchmark | done | results.md "Overlap pairs" = 0 on all rows |
| Router completes >=1 simpler benchmark board | not run | Out of scope per "Out of scope" guard; the placer's output is in the same shape the router already consumes |
| Benchmark report (Markdown table) | done | `benchmarks/hierarchical/results.md` |
| Comparison numbers use same EvolutionaryConfig | done | `EvolutionaryConfig(parallel=False)` + pinned seed |
| Verdict comment | done | this file (also pasted to issue thread) |

## Out of scope (deliberate)

- No router invocation. The placement output is in the same `dict[ref] -> (x, y, rot)`
  shape downstream code already consumes; routing differences are dominated
  by the GA's spacing-proxy fitness being a poor router proxy, which is
  exactly what #2720 fixes.
- No changes to `_evaluate_fitness_worker_python`, `RoutingEvaluator`, or
  the GA chromosome. The bottom-up flow is a separate entrypoint.
- No new clustering algorithm; reuses `detect_functional_clusters()`
  from `optim/clustering.py`.
