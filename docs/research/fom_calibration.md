# FOM Weight Calibration Report (issue #3188)

## Summary

This report documents the Pareto-sweep calibration of the hybrid FOM soft-term weights introduced in #3186. The procedure runs in three phases:

1. **Per-board random search**: each board's committed routed placement is compared against 40 Gaussian-perturbed alternatives (sigma=2.5 mm). Random search over log-uniform weights finds the best per-board weights.
2. **Pareto sweep (NSGA-II)**: a multi-objective search across the train boards selects the conservative middle of the Pareto frontier as the global default.
3. **Cross-board holdout**: train weights are evaluated on held-out boards to test generalisation.

## Boards used

Calibration uses the 7 routed in-repo boards (01-07). The issue originally listed 8 (including "softstart"), but that board has no `.kicad_pcb` in the repo (only project + design-rules files at `boards/external/softstart/`); it is excluded.

## Per-board weights

| Board | rank_consistency | discrimination | Top-3 terms by weight |
|---|---|---|---|
| voltage_divider | 0.725 | 10728.31x | diff_pair_clearance_margin(15.46), trace_length_excess(11.53), weighted_via_count(7.69) |
| charlieplex_3x3 | 0.975 | 538251921.93x | diff_pair_clearance_margin(9.20), trace_length_excess(5.10), net_congestion_variance(3.21) |
| usb_joystick | 1.000 | 1078.40x | diff_pair_clearance_margin(88.17), match_group_skew(0.17), compactness(0.16) |
| stm32_devboard | 1.000 | 238148.31x | net_congestion_variance(9.54), turning_penalty(3.51), diff_pair_clearance_margin(1.19) |
| bldc_controller | 0.925 | 96702408703.17x | compactness(83.46), diff_pair_clearance_margin(19.87), net_congestion_variance(4.14) |
| diffpair_test | 0.975 | 9479377.20x | turning_penalty(49.28), decoupling_proximity(0.62), match_group_skew(0.33) |
| matchgroup_test | 0.725 | 114200738981568423454048256.00x | trace_length_excess(41.44), match_group_skew(2.14), decoupling_proximity(1.73) |

## Global default (Pareto-derived)

Selected weight vector (geometric mean of train-board rank consistencies):

| Term | Weight |
|---|---|
| trace_length_excess | 1.2209 |
| weighted_via_count | 0.0126 |
| turning_penalty | 33.3018 |
| net_congestion_variance | 2.4173 |
| match_group_skew | 0.0216 |
| diff_pair_clearance_margin | 0.4750 |
| decoupling_proximity | 0.0485 |
| crossing_count | 0.0245 |
| thermal_spread | 0.0289 |
| compactness | 0.0157 |

### Pareto-sweep summary

- Pareto frontier size: **80**
- Selected candidate's geometric-mean rank_consistency across train boards: **0.922**

## Cross-board generalisation

Weights fit on the train set (`01-05`) are evaluated on the holdout set (`06-07`). AC #3 requires holdout rank_consistency >= 0.7.

| Board | Set | rank_consistency (tuned) | rank_consistency (uniform=1.0) | discrimination (tuned) |
|---|---|---|---|---|
| voltage_divider | train | 0.725 | 0.400 | 2.97x |
| charlieplex_3x3 | train | 1.000 | 0.800 | 88.16x |
| usb_joystick | train | 1.000 | 0.925 | 26736.47x |
| stm32_devboard | train | 0.975 | 0.600 | 157723062766.40x |
| bldc_controller | train | 0.950 | 0.900 | 20453799031192316.00x |
| diffpair_test | holdout | 0.975 | 0.925 | 12.24x |
| matchgroup_test | holdout | 0.775 | 0.650 | 2437584656497.57x |

## Acceptance criteria status

- **AC #3 (holdout rank_consistency >= 0.7)**: mean holdout rank_consistency = **0.875** (PASS).
- **AC #4 (discrimination >= 5x on 6 of 8 boards)**: **6/7** boards meet the threshold.
- **Improvement over uniform=1.0 baseline**: **7/7** boards have higher rank_consistency under the tuned weights.

## Per-board signal availability (oracle ceiling)

Before judging the calibration, ask: how much signal does the FOM *structurally* have on each board? The 'oracle' column is the rank_consistency that an ideal weight selector could achieve, computed by zeroing every term where committed is empirically *worse* than the median perturbation. Boards where the oracle is itself below 0.7 cannot meet AC #3 by any weight tuning -- the FOM term set doesn't see what makes the committed placement preferable.

| Board | oracle rc (informative terms only) | n_informative |
|---|---|---|
| voltage_divider | 0.775 | 2 |
| charlieplex_3x3 | 0.800 | 5 |
| usb_joystick | 0.925 | 4 |
| stm32_devboard | 0.800 | 4 |
| bldc_controller | 0.900 | 4 |
| diffpair_test | 0.925 | 5 |
| matchgroup_test | 1.000 | 4 |

## Term-by-term discussion

Examining the per-board random-search results across boards reveals which soft terms are *useful signal* vs *noise* for each topology:

| Term | mean per-board weight | std | CV (board-to-board variability) |
|---|---|---|---|
| diff_pair_clearance_margin | 19.194 | 29.069 | 1.51 |
| compactness | 12.027 | 29.162 | 2.42 |
| trace_length_excess | 8.527 | 13.980 | 1.64 |
| turning_penalty | 8.316 | 16.781 | 2.02 |
| net_congestion_variance | 3.027 | 3.190 | 1.05 |
| weighted_via_count | 1.146 | 2.672 | 2.33 |
| match_group_skew | 0.669 | 0.729 | 1.09 |
| decoupling_proximity | 0.421 | 0.568 | 1.35 |
| thermal_spread | 0.244 | 0.432 | 1.77 |
| crossing_count | 0.032 | 0.009 | 0.28 |

Terms with **high CV** are board-specific (e.g. `diff_pair_clearance_margin` matters for boards with diff pairs; doesn't on the others). Terms with **low CV** are *transferable* — the global default is a good fit for them. Terms with both high mean and high CV are candidates for topology-specific weight families (a Phase 4 follow-up, not this issue).

## Honest scope caveats

- The perturbations modify footprint positions but keep the committed routing intact. This means terms that depend on routing (vias, turning penalty) are insensitive to perturbation and hence not informative for weight calibration — they carry whatever weight the search assigns by chance. A proper re-routed-perturbation pipeline would expose them but costs ~3 hours of compute we elected not to spend (see issue's compute estimate of 600 hours).
- 7 boards is a small training corpus; the cross-board generalisation result should be interpreted with care. The weights are *better than uniform 1.0* on the train set with high confidence; their holdout performance is the headline number.
- The Pareto sweep optimises a proxy (rank_consistency on perturbation distributions), not actual manufacturability — see issue #3187's classifier for that signal. The choice not to use the classifier as the inner objective is deliberate: doing so would overfit to the classifier's biases (it has 131 training samples and 0.92 OOF AUC, leaving room for systematic error).

## Reproducibility

Regenerate the weight files with:

```bash
uv sync --extra research      # one-time: install pymoo
uv run python scripts/research/calibrate_fom.py
```

The script is deterministic given `--seed` (default: 42).

## Addendum: `decoupling_proximity` re-check after the #5939 classifier change (issue #5984)

PR #5971 (issue #5939) replaced the term's substring power-rail match with the shared whole-token `router.net_class.is_power_rail_name`. Only two calibration boards change: `stm32_devboard` (`+3.3V` was never in the old hint list) and `bldc_controller` (`VIN`, `VM`, `V3P3`). `scripts/research/fom_decoupling_ab.py` scores the same seed-42 perturbations once and computes the term under both classifiers, so the two arms differ only in that column. Boards are the current committed `*_routed.kicad_pcb` files, which have moved since the May calibration above.

| Board | committed / perturbed median (old) | committed / perturbed median (new) | rank consistency at 0.0181 (old -> new) |
|---|---|---|---|
| stm32_devboard | 5.99 / 7.36 | 70.24 / 62.86 | 0.975 -> 0.950 |
| bldc_controller | 31.51 / 34.59 | 67.87 / 75.97 | 0.900 -> 0.875 |
| other 5 boards | unchanged | unchanged | unchanged |

On `stm32_devboard` the term is now *anti-informative*: the committed placement scores worse than 70% of random jitters, because U2's VDD pins sit 6-14 mm from the nearest `+3.3V` cap. Fitting therefore pulls the weight **down**:

| Fit (train boards 01-05) | old classifier | new classifier |
|---|---|---|
| 1-D re-fit, other weights fixed, argmax | 0.178 | 0.036 |
| 1-D re-fit, band within 0.01 of best score | [0.106, 0.237] | [0.0045, 0.0708] |
| NSGA-II, 8 seeds, median / geo-mean | 0.054 / 0.055 | 0.012 / 0.018 |
| NSGA-II, 8 seeds, range | [0.032, 0.124] | [0.010, 0.049] |

**Decision: keep 0.0181.** It is inside both post-#5939 near-optimal bands, and it equals the 8-seed Pareto geo-mean (0.0183). Its selection score (0.784) is within 0.008 of the 1-D best (0.792), which is less than two perturbations on one board. Moving only this weight would also be a partial re-fit, because the full Pareto vectors on today's boards differ from `default.yaml` in other terms too (for example, `net_congestion_variance` is about 10x higher). A full ten-weight re-calibration is a separate decision.

## Addendum: full multi-seed re-calibration on the October 2026 boards (issue #6021)

The generated sections above now come from a default `calibrate_fom.py` run (seed 42) on today's committed `*_routed.kicad_pcb` files. The May 2026 numbers they replace, and the "moved since the May calibration" remark in the #5984 addendum, refer to the previous version of this file. The 1-Steiner speedup in #6019 makes a default run cheap: Phase 1a took about 21 minutes wall time on a heavily loaded 8-core host (load average 20-60).

**The "Global default" table above is a single draw.** It is the seed-42 Pareto pick that the script writes to `data/research/fom_weights/default.yaml`, and it is *not* the shipped `src/kicad_tools/optim/weights/default.yaml`. One draw puts `turning_penalty` at 33.3, which is meaningless, because that term never moves under perturbation (see below). The shipped file changes only the three weights that the multi-seed evidence below supports.

### Protocol

- **Perturbation samples.** Four independent caches of 40 perturbations per board: seed 42 (the official run) and seeds 1, 2 and 3. Each cache uses the same sigma (2.5 mm) and rotation probability (0.2). A fifth, pooled cache concatenates the four, giving 160 perturbations per board. The committed-placement terms are identical across the four caches (asserted).
- **Fits.** `calibrate_fom.pareto_sweep` (80 generations x 80 population) was run for 8 NSGA-II seeds (42, 1-7) on each of the five caches, giving 40 fitted vectors.
- **Selection score.** `pareto_sweep`'s own selection score: geo-mean of the saturation-weighted rank consistency over train boards 01-05, minus 0.5 x std.

### Which terms the data can fit at all

Rank consistency only compares committed against perturbed. A term that takes the same value on every perturbation contributes nothing to the ranking, so its weight is *unidentifiable*: only the saturation penalty bounds it, and only from above. On the five train boards, these terms never move under footprint jitter: `weighted_via_count`, `turning_penalty`, `match_group_skew` and `diff_pair_clearance_margin` (the last two are 0 everywhere), plus `thermal_spread` on every board except `bldc_controller`. Routing is kept fixed, so via and bend counts cannot change. Their fitted weights are noise, and the spreads below show it: `turning_penalty` ranges over 0.013 - 98.5. The informative terms are `trace_length_excess`, `net_congestion_variance`, `decoupling_proximity`, `crossing_count` and `compactness`.

### Per-term evidence (40 fits)

| term | default.yaml (before) | 40-fit geo-mean | p10 - p90 | min - max | geo-mean per cache (s42 / s1 / s2 / s3 / pooled) | decision |
|---|---|---|---|---|---|---|
| `trace_length_excess` | 1.6126 | 0.8591 | 0.596 - 1.23 | 0.461 - 1.59 | 0.858 / 0.921 / 0.775 / 0.706 / 1.08 | **-> 0.8591** (above every fit) |
| `weighted_via_count` | 0.0107 | 0.0161 | 0.0104 - 0.0335 | 0.01 - 0.0556 | 0.0118 / 0.0183 / 0.0152 / 0.0221 / 0.015 | keep (unidentifiable; inside range) |
| `turning_penalty` | 0.0116 | 1.5504 | 0.0246 - 39.2 | 0.0127 - 98.5 | 6.18 / 7.97 / 0.126 / 2.62 / 0.549 | keep (unidentifiable) |
| `net_congestion_variance` | 0.2226 | 3.0319 | 2.09 - 5.17 | 1.26 - 7.56 | 2.84 / 3.96 / 2.04 / 2.61 / 4.27 | **-> 3.0319** (5.6x below the lowest fit) |
| `match_group_skew` | 1.5237 | 0.5070 | 0.0355 - 23.7 | 0.0199 - 46.4 | 0.544 / 0.505 / 0.643 / 0.674 / 0.282 | keep (unidentifiable) |
| `diff_pair_clearance_margin` | 0.9995 | 1.3749 | 0.0337 - 37.7 | 0.0163 - 95.2 | 0.944 / 8.49 / 0.767 / 1.22 / 0.657 | keep (unidentifiable) |
| `decoupling_proximity` | 0.0181 | 0.0226 | 0.0109 - 0.0493 | 0.01 - 0.0629 | 0.0183 / 0.0232 / 0.0409 / 0.0136 / 0.0248 | keep (inside; bimodal, see below) |
| `crossing_count` | 0.0145 | 0.0191 | 0.0139 - 0.0249 | 0.0106 - 0.0291 | 0.0223 / 0.0155 / 0.021 / 0.02 / 0.0177 | keep (inside) |
| `thermal_spread` | 0.0221 | 0.0295 | 0.0124 - 0.113 | 0.0105 - 0.172 | 0.0396 / 0.0541 / 0.0223 / 0.0247 / 0.019 | keep (inside) |
| `compactness` | 0.0968 | 0.0114 | 0.0101 - 0.0143 | 0.01 - 0.0157 | 0.0122 / 0.0112 / 0.0118 / 0.0109 / 0.0109 | **-> 0.0114** (6x above the highest fit) |

Three weights meet both conditions, and every one of the five caches agrees. The committed value lies outside the range of *all 40 fits*, and the per-cache geo-means sit on the same side. `trace_length_excess` is the marginal one: 1.6126 is just above the highest fit (1.59), but it lies above the 90th percentile in every cache. `compactness` fits pile up at the search box floor (10^-2): the data wants the term small *relative to the others*, so 0.0114 is effectively an upper bound and not a point estimate.

### The gain survives held-out perturbation samples

Rank consistency is scale-invariant, so only the *ratios* between the informative weights matter. Moving one weight without the others can land off the fitted ridge. All three moves were therefore validated together and against a single-term alternative. In the leave-one-sample-out (LOSO) columns, the candidate is built from the 24 fits of the *other three* caches and scored on the held-out cache.

| selection score (train 01-05) | s42 | s1 | s2 | s3 | pooled (160) |
|---|---|---|---|---|---|
| old `default.yaml` | 0.784 | 0.837 | 0.771 | 0.822 | 0.807 |
| `net_congestion_variance` only -> 3.0 | 0.822 | 0.882 | 0.847 | 0.892 | 0.863 |
| LOSO: `net_congestion_variance` only | 0.819 | 0.892 | 0.857 | 0.892 | - |
| LOSO: three-term move | 0.855 | 0.925 | 0.894 | 0.921 | - |
| **new `default.yaml`** (three-term move, 40-fit geo-means) | **0.855** | **0.931** | **0.894** | **0.921** | **0.904** |
| typical full NSGA fit (mean over 40) | - | - | - | - | 0.901 |

The three-term move beats the single-term move on every held-out cache by 0.03-0.04, and it gets within noise of a full unconstrained fit while leaving the seven other weights alone. As a consistency check, with the new vector each of the ten weights lies inside its own 1-D near-optimal band on the pooled cache (score within 0.01 of the best, others held fixed). For example, `trace_length_excess` [0.56, 0.94], `net_congestion_variance` [2.37, 3.98], `compactness` [0.001, 0.028], `decoupling_proximity` [0.0019, 0.047] and `crossing_count` [0.0075, 0.034].

Per-board rank consistency on the pooled cache (160 perturbations; one perturbation = 0.006):

| board | set | old | new |
|---|---|---|---|
| voltage_divider | train | 0.713 | 0.819 |
| charlieplex_3x3 | train | 0.781 | 0.938 |
| usb_joystick | train | 0.956 | 1.000 |
| stm32_devboard | train | 0.956 | 0.994 |
| bldc_controller | train | 0.906 | 0.944 |
| diffpair_test | holdout | 0.963 | 0.956 |
| matchgroup_test | holdout | 0.650 | 0.706 |
| usbc_pd_power (board 09) | extra | 0.781 | 0.812 |

`diffpair_test` drops by one perturbation, which is within noise. Every other board improves.

### Multimodality

The #6022 judge saw the decoupling fits split into two clusters, and so does this run. Sorting the 40 `decoupling_proximity` fits gives one tight group at the search floor, 0.0100 - 0.0150 (14 fits), and a broad spread from 0.0183 to 0.0629 (26 fits), split by the largest gap in the sorted values (0.09 dex). The committed 0.0181 sits at the junction and inside both the 40-fit range and the conditional band, so it stays. The other informative terms are unimodal: `trace_length_excess`, `net_congestion_variance`, `crossing_count` and `compactness` show no gap larger than 0.15 dex. Most of the spread *between* caches is perturbation-sampling noise. The in-sample selection score of a fit ranges from 0.86-0.87 on cache s42 to 0.95 on cache s1 for the same procedure. Out of sample, those fits average 0.892, against 0.919 in sample. That gap is why one draw (or one cache) is not evidence.

### Board 09 is not added to the calibration set

`boards/09-usbc-pd-power` is routed (47 footprints, 32/32 nets), but it is at *Development* status. Manufacturing is blocked by 51 ampacity errors, and 61 off-angle segments are pending a source-emitter repair (#5084). Calibration treats each committed placement as the reference that random jitter should score worse than. Training on a placement that has not passed its own release gates would fit the weights toward unfinished work. Adding a sixth train board would also break comparability with the #3188 and #5984 train/holdout split that this re-fit is checked against. The board was scored as an **extra out-of-sample board** with all four perturbation seeds (each about 35 s). Rank consistency rises from 0.781 to 0.812 under the new weights. On this board `net_congestion_variance` is *anti-informative*: the committed layout beats only 16% of jitters on it, so it is a useful counterweight check, and the raised weight still does not hurt it. Revisit when board 09 reaches *Assembly ready*. At that point add it to `BOARDS` with its non-`_routed` filename, `output/usbc_pd_power.kicad_pcb`.

### Downstream effect

The only consumer of `default.yaml` is `kicad_tools.optim.fom.default_weights()`, which only `kct optim fom-debug` (when `--weights` is not given) and direct Python callers of `compute_fom` use. No placement command uses these weights: `kct placement optimize`, `optimize-placement`, `kct route` and `kct route-auto` all score layouts with their own cost functions. **No board's placement changes.** What changes is the soft score that `fom-debug` prints:

| board | soft sum (old -> new) | composite score (old -> new) | dominant weighted term (old -> new) |
|---|---|---|---|
| 00 simple_led | 1.58 -> 14.45 | 0.205 -> 5.3e-07 | net_congestion_variance -> net_congestion_variance |
| 01 voltage_divider | 1.77 -> 7.43 | 0.17 -> 5.9e-04 | compactness -> net_congestion_variance |
| 02 charlieplex_3x3 | 6.68 -> 6.14 | 1.3e-03 -> 2.2e-03 | trace_length_excess -> net_congestion_variance |
| 03 usb_joystick | 15.56 -> 16.50 | 1.7e-07 -> 6.8e-08 | crossing_count -> crossing_count |
| 04 stm32_devboard | 4.94 -> 8.74 | 7.1e-03 -> 1.6e-04 | crossing_count -> net_congestion_variance |
| 05 bldc_controller | 23.39 -> 20.53 | 7.0e-11 -> 1.2e-09 | trace_length_excess -> crossing_count |
| 06 diffpair_test | 8.23 -> 10.82 | 2.7e-04 -> 2.0e-05 | crossing_count -> crossing_count |
| 07 matchgroup_test | 77.12 -> 60.77 | exp(-60) cap -> exp(-60) cap | trace_length_excess -> crossing_count |
| 09 usbc_pd_power | 17.01 -> 15.93 | 4.1e-08 -> 1.2e-07 | trace_length_excess -> crossing_count |

The hard gate is unchanged on every board. Absolute composite scores move by up to 5 orders of magnitude, most on the smallest boards. On those boards `net_congestion_variance` is large in raw units (2.3 on board 01, 4.7 on board 00), and it now carries a 3.03 weight. The composite is calibrated for *ranking alternative placements of the same board*. Comparing absolute `fom-debug` scores across boards, or across this change, was never meaningful, and now gives visibly different numbers.

### Reproduce

```bash
uv sync --extra research
uv run python scripts/research/calibrate_fom.py          # official seed-42 cache + per-board YAMLs
# The extra perturbation caches use calibrate_fom.collect_all_terms(path, 40, 2.5, 0.2, seed)
# for seed in 1, 2, 3, and the same call with BOARDS = [board 09] for seeds 42, 1, 2, 3.
# The 40 fits are calibrate_fom.pareto_sweep(cache, train, 80, 80, seed) for seed in 42, 1..7
# on each cache and on the pooled cache.
```

`calibrate_fom.py` now carries every `## Addendum` section of this file forward when it regenerates the report, so a re-run no longer drops these decision records.
