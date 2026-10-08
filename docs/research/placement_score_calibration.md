# Calibrating pre-route placement signals against routing outcomes

**Issue**: [#5948](https://github.com/rjwalters/kicad-tools/issues/5948)
**Status**: bounded first increment (small corpus, no new routing runs)
**Related**: [learned_fom_phase0.md](learned_fom_phase0.md) (the corpus and the
learned classifier), [fom_calibration.md](fom_calibration.md) (FOM weight
calibration), [circuit-skills-evaluation.md](circuit-skills-evaluation.md)
(prior art)

## Question

Do the placement signals we already compute -- RUDY peak / overflow
(`router/congestion_estimator.py`), HPWL, bbox overlap and the optimizer's
`evaluate_placement` cost -- pick the **more routable of two placements of the
same board**?  circuit-skills reports 0.89 for a fitted combined score on 319
placements of 64 boards (idea only; unlicensed, nothing copied).  TraceMaker's
`--route-check` (accept a move only if it routes at least as well; GPL, idea
only) is the other reference point.

## Method

* **Labels are reused, not regenerated.**  No routing was run for this study.
  `data/research/fom_phase0/labels.jsonl` (155 rows, from the Phase 0 study)
  already stores per-sample RNG seed, sigma, rotate-prob and the routing
  outcome (`route_ok`, `drc_errors` from `kct route --backend cpp` +
  `kct check`).
* **Placements are rebuilt deterministically** with the Phase 0
  `perturb_pcb` from the stored RNG seed.  The committed seed boards have
  drifted since that corpus was routed (charlieplex_3x3: 14 -> 19
  footprints), so the seed boards are read from git revision `83b0bc82` (the
  commit that added the labels).  The script asserts that footprint count and
  moved/rotated counts match each record and aborts otherwise.
* **Outcome severity** (lower = better): route failed/timed out = 1000,
  otherwise the `kct check` error count.
* **Pairs**: two samples of the same board with different severity.  A signal
  is right when it is lower for the better one; exact ties count 0.5.  Pairs
  never cross boards.
* **Signals** (all pre-route): `hpwl` (pad-anchored), `rudy_peak` (max tile
  demand), `rudy_overflow` (sum of demand above 1.5x the seed's mean tile
  demand -- an arbitrary, uncalibrated capacity), `overlap` (bbox overlap
  area), `placement_cost` (`evaluate_placement` default weights),
  `steiner_signal_length` (stored Phase 0 feature).  RUDY uses a fixed
  per-board grid (seed pad extent + 10 mm) so perturbations are comparable.
* **Fitted combination**: linear weights on within-board z-scored
  `hpwl, rudy_peak, rudy_overflow, overlap`, logistic loss on pairwise feature
  deltas, evaluated **leave-one-board-out** (`fitted_lobo`); an in-sample fit
  is reported only as an optimism upper bound.

Reproduce:

```bash
uv run python scripts/research/placement_score_calibration.py
# -> data/research/placement_score/results.json
```

## Corpus actually available (the honest limitation)

| board | samples | clean | routed w/ DRC errors | route failed | usable pairs |
|---|---|---|---|---|---|
| voltage_divider | 40 | 28 | 9 | 3 | 383 |
| charlieplex_3x3 | 40 | 4 | 15 | 21 | 532 |
| usb_joystick | 25 | 0 | 0 | 25 | 0 |
| stm32_devboard | 25 | 0 | 0 | 25 | 0 |
| matchgroup_test | 25 | 0 | 0 | 25 | 0 |

Only **two boards, both tiny** (4 and 14 footprints), have outcome variation;
the three larger boards failed to route in every perturbed sample and
contribute no pairs.  Pairs within a board share samples, so the 915 pairs are
far fewer than 915 independent observations.  This is a small-n study versus
circuit-skills' 319 placements / 64 boards.

## Results: pairwise ranking accuracy

Pair-weighted pooled accuracy over both usable boards (915 pairs) with a
sample-level bootstrap 95% CI (300 resamples), then per board.  0.5 = chance.

| signal | pooled | 95% CI | voltage_divider | charlieplex_3x3 |
|---|---|---|---|---|
| `hpwl` | 0.584 | 0.46-0.71 | 0.533 | 0.620 |
| `rudy_peak` | 0.462 | 0.37-0.58 | 0.530 | 0.414 |
| `rudy_overflow` | 0.570 | 0.47-0.68 | 0.593 | 0.555 |
| `overlap` | 0.550 | 0.46-0.62 | 0.628 | 0.494 |
| `placement_cost` | **0.672** | 0.56-0.77 | 0.768 | 0.603 |
| `steiner_signal_length` | 0.551 | 0.44-0.68 | 0.530 | 0.566 |
| fitted combo, leave-one-board-out | 0.586 | -- | 0.551 | 0.611 |
| fitted combo, in-sample (optimistic) | 0.626 | -- | 0.655 | 0.605 |
| circuit-skills (reported, different corpus) | 0.89 | -- | -- | -- |

Reading it:

* Every signal is at most modestly better than chance; the best,
  `placement_cost` (0.67), has a CI that only just excludes 0.5 and is far
  from 0.89.
* `rudy_peak` is at or below chance (0.46): with RUDY's uniform-HPWL spreading
  on boards this small, peak demand mostly tracks net span, not local
  blockage.
* The fitted combination does **not** beat the best single existing signal,
  even in-sample (0.63 < 0.67); out of sample it is 0.59.  With two usable
  boards, leave-one-board-out trains on a single board, so this is a weak
  test of the fit, not evidence the idea cannot work.

## Decision

**Do not adopt a fitted placement score as the `optimize-placement` acceptance
criterion on this evidence.**  The existing signals rank routability only
slightly above chance on the corpus we have, and a fitted combination adds
nothing.  Adopting it would let the optimizer accept or reject moves on a
signal that is wrong about as often as a coin flip.  No code in
`optimize-placement` was changed.

Follow-ups, in order of expected value:

1. **`--route-check` mode** (TraceMaker idea): confirm a candidate with a short
   real `kct route` before accepting.  This sidesteps the weak proxy and needs
   no calibration; cost is routing time per accepted move.
2. **Grow the labelled corpus** before re-fitting: needs boards where some
   perturbations route (the three larger boards currently give zero pairs,
   so smaller sigma / more moderate perturbations on 03/04/07), plus the
   other fleet boards.  Routing is expensive on the shared workers and should
   go through the existing corpus scripts, not ad-hoc loops.
3. Revisit richer features (see Phase 0's "iterate" branch) -- escape
   congestion at dense packages, not global RUDY -- and a calibrated RUDY
   capacity (the 1.5x-mean constant here is a placeholder).
4. Compare against the Phase 0 classifier on the same pairs; not done here.

## Not measured

* No new routing; outcomes are the Phase 0 labels (single run per sample,
  45 s budget, C++ backend; router non-determinism not quantified).
* No cross-board generalisation, no boards beyond 01/02 with outcome
  variance, no 0.89-style comparison on an equal corpus.
* Overflow capacity and the `placement_cost` weights are untuned defaults.
