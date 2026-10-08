# Calibrating pre-route placement signals against routing outcomes

**Issues**: [#5948](https://github.com/rjwalters/kicad-tools/issues/5948)
(study), [#6233](https://github.com/rjwalters/kicad-tools/issues/6233)
(both corpora folded into this one note)
**Status**: complete. **Decision: do not adopt** a calibrated pre-route score
as the `optimize-placement` acceptance criterion. Use a bounded real route
instead (`--route-check`, [#6234](https://github.com/rjwalters/kicad-tools/issues/6234)).
See [Decision](#decision).
**Script**: [`scripts/research/placement_score_calibration.py`](../../scripts/research/placement_score_calibration.py)
**Data**: [`data/research/placement_score/`](../../data/research/placement_score/)
**Related**: [learned_fom_phase0.md](learned_fom_phase0.md) (corpus A's labels
and the learned classifier), [fom_calibration.md](fom_calibration.md) (FOM
weight calibration), [circuit-skills-evaluation.md](circuit-skills-evaluation.md)
(prior art)

## Question

Do the placement-only signals we already compute predict which of two
placements of the **same board** routes better? If one of them, or a fitted
combination, did that reliably, it could be the acceptance test for
`kct optimize-placement`: accept a candidate placement only if its score says
it routes at least as well as the incumbent.

The reference point is the circuit-skills score study, which reports that a
combined score fitted on 319 placements of 64 boards picks the more routable
placement of a pair **0.89** of the time. We have not reproduced that number.
circuit-skills is unlicensed (`kct ecosystem show circuit-skills`), so we used
the idea of pairwise ranking accuracy and nothing else. No code, features or
weights came from it. TraceMaker's `--route-check` mode (GPL-3.0, ideas only)
is the other prior art. It is the recommendation in
[Decision](#decision).

This study reuses the learned figure-of-merit (FOM) machinery but does not
fork it. Both corpora build placements with the Phase 0 perturbation function
`perturb_pcb` from
[`generate_perturbations.py`](../../scripts/research/generate_perturbations.py)
([`learned_fom_phase0.md`](learned_fom_phase0.md), #3187). Phase 0 predicts a
binary "manufacturable" label. Here the evaluation is pairwise within a board,
which is the comparison an acceptance gate actually makes.
[`fom_calibration.md`](fom_calibration.md) (#3188) asks a third question:
whether the soft FOM ranks a committed placement above its perturbations.

## Two corpora

The study ran twice, independently, with different labels and signal code.
Both are kept because they answer the question from different directions.

| | Corpus A: recorded Phase 0 labels | Corpus B: fresh routed fleet corpus |
|---|---|---|
| Subcommand | `phase0` | `generate`, then `analyze` |
| Routing run for this study | none | yes: 169 min summed route time for the kept rows |
| Boards with pairs | 2 (01 voltage_divider, 02 charlieplex_3x3) | 6 (00, 01, 02, 03, 04, 06) |
| Placements / pairs | 155 rows, 915 pairs | 85 placements, 370 pairs |
| Label ("more routable") | lower severity: route failed = 1000, else `kct check` error count | higher signal-net completion (`kct net-status`), then fewer non-connectivity `kct check` errors |
| Seed boards | as of `83b0bc82` (when the Phase 0 labels were committed) | `boards/0N-*/output/*.kicad_pcb` as of the run, tracks stripped |
| Signal code | the script's own scorer (`score_placement`) | `compute_signals`, plus `kct optimize-placement --dry-run` for the cost terms |
| Fit | z-scored gradient-descent logistic on 4 signals | `log1p` Newton logistic on 10 signals |
| Data | `results.json` | `labels_budget{45,120}.jsonl`, `analysis*.{json,md}` |

Same-named signals are **not** the same numbers across the corpora. For
example, corpus A's `placement_cost` calls `evaluate_placement` with default
weights and an outline taken from the pad extent. Corpus B's
`placement_cost_total` is what `kct optimize-placement --dry-run` reports for
the real board outline. Compare the two corpora's conclusions, not their
individual cells.

## Corpus A: recorded Phase 0 labels

### Method

* **Labels are reused, not regenerated.** No routing was run for this corpus.
  `data/research/fom_phase0/labels.jsonl` (155 rows, from the Phase 0 study)
  already stores per-sample RNG seed, sigma, rotate-prob and the routing
  outcome (`route_ok`, `drc_errors` from `kct route --backend cpp` +
  `kct check`, 45 s budget).
* **Placements are rebuilt deterministically** with `perturb_pcb` from the
  stored RNG seed. The committed seed boards have changed since that corpus
  was routed (charlieplex_3x3: 14 -> 19 footprints), so the seed boards are
  read from git revision `83b0bc82` (the commit that added the labels). The
  script checks that the footprint count and the moved/rotated counts match
  each record, and aborts otherwise.
* **Outcome severity** (lower = better): route failed or timed out = 1000,
  otherwise the `kct check` error count.
* **Pairs**: two samples of the same board with different severity. A signal
  is right when it is lower for the better one. Exact ties count 0.5. Pairs
  never cross boards.
* **Signals** (all pre-route): `hpwl` (pad-anchored), `rudy_peak` (max tile
  demand), `rudy_overflow` (sum of demand above 1.5x the seed's mean tile
  demand, an arbitrary, uncalibrated capacity), `overlap` (bbox overlap
  area), `placement_cost` (`evaluate_placement` default weights),
  `steiner_signal_length` (stored Phase 0 feature). RUDY uses a fixed
  per-board grid (seed pad extent + 10 mm) so perturbations are comparable.
* **Fitted combination**: linear weights on within-board z-scored
  `hpwl, rudy_peak, rudy_overflow, overlap`, logistic loss on pairwise feature
  deltas, evaluated **leave-one-board-out** (`fitted_lobo`). An in-sample fit
  is reported only as an upper bound on how optimistic the fit can be.

### Corpus actually available

| board | samples | clean | routed w/ DRC errors | route failed | usable pairs |
|---|---|---|---|---|---|
| voltage_divider | 40 | 28 | 9 | 3 | 383 |
| charlieplex_3x3 | 40 | 4 | 15 | 21 | 532 |
| usb_joystick | 25 | 0 | 0 | 25 | 0 |
| stm32_devboard | 25 | 0 | 0 | 25 | 0 |
| matchgroup_test | 25 | 0 | 0 | 25 | 0 |

Only **two boards, both tiny** (4 and 14 footprints), have outcome variation.
The three larger boards failed to route in every perturbed sample and
contribute no pairs. Pairs within a board share samples, so the 915 pairs are
far fewer than 915 independent observations.

### Results

Pair-weighted pooled accuracy over both usable boards (915 pairs) with a
sample-level bootstrap 95% CI (300 resamples), then per board. 0.5 = chance.

| signal | pooled | 95% CI | voltage_divider | charlieplex_3x3 |
|---|---|---|---|---|
| `hpwl` | 0.584 | 0.46-0.71 | 0.533 | 0.620 |
| `rudy_peak` | 0.462 | 0.37-0.58 | 0.530 | 0.414 |
| `rudy_overflow` | 0.570 | 0.47-0.68 | 0.593 | 0.555 |
| `overlap` | 0.550 | 0.46-0.62 | 0.628 | 0.494 |
| `placement_cost` | **0.672** | 0.56-0.77 | 0.768 | 0.603 |
| `steiner_signal_length` | 0.551 | 0.44-0.67 | 0.530 | 0.566 |
| fitted combo, leave-one-board-out | 0.586 | -- | 0.551 | 0.611 |
| fitted combo, in-sample (optimistic) | 0.626 | -- | 0.655 | 0.605 |

* Every signal is at most modestly better than chance. The best,
  `placement_cost` (0.67), has a CI that only just excludes 0.5.
* `rudy_peak` is at or below chance (0.46). With RUDY's uniform-HPWL spreading
  on boards this small, peak demand mostly tracks net span, not local
  blockage.
* The fitted combination does **not** beat the best single existing signal,
  even in-sample (0.63 < 0.67). Out of sample it is 0.59. With two usable
  boards, leave-one-board-out trains on a single board, so this is a weak
  test of the fit.

## Corpus B: fresh routed fleet corpus

### Method

The corpus covers every fleet board (`kct fleet status`) that has an unrouted
`output/<name>.kicad_pcb`: boards `00` to `07`. Board `09` is not listed by
`kct fleet status` and was not used, and `08` has no PCB. Each placement is
built like this:

1. Load the board. Strip any existing tracks and vias with
   `PCB.strip_traces()`, because boards 06, 07 and 09 ship copper in their
   "unrouted" file.
2. **Original placement**: route it unchanged with router seeds 0, 1 and 2.
   Seed 0 joins the analysis set. Seeds 1 and 2 measure label noise.
3. **Perturbed placements**: apply `perturb_pcb`, which jitters every
   non-locked, non-`J`/`MH`/`MK`/`TP`/`X` footprint by Gaussian (x, y) noise
   and rotates it +-90 degrees with probability 0.1. Sigma cycles through
   0.5, 1.0, 2.0 and 4.0 mm. Each perturbation has its own RNG seed, drawn
   from master seed 5948 and recorded per row.

| Boards | Perturbations / board | Route budget |
|---|---|---|
| 00 simple-led, 01 voltage-divider | 16 | 45 s |
| 02 charlieplex | 16 | 120 s |
| 03 usb-joystick, 04 stm32, 05 bldc, 06 diffpair, 07 matchgroup | 12 | 120 s |

The two budgets were not planned. The first run used 45 s on every board. On
board 02 and larger, that left too many routes without any checkpoint, so the
run was stopped and boards 02 to 07 were re-run from scratch at 120 s. Only
rows from boards that ran under one budget were kept. Pairs are formed within
a board, so no pair ever mixes the two budgets.

#### Routing and labels

Every placement is routed with:

`kct route --backend cpp --no-optimize --no-auto-layers --oracle-rounds 0
--skip-drc --no-sync-check --no-placement-feedback --no-routing-plan
--timeout <budget> --seed <s>`

Three workers ran in parallel on an 8-core machine that other agents were
also using. The load average ranged from about 3 to 27 during the run.

The negotiated router uses nearly all of its budget. When the deadline hits,
it writes its best-so-far board as a checkpoint, named in
`<out>.timeout.json`. The label is taken from the final board if one exists,
otherwise from that checkpoint, and each row records which one was used.
Completion is measured on that file with `kct net-status`, not taken from the
router's own summary, so final and checkpoint boards are scored the same way:

- **completion**: the % of multi-pad *signal* nets (excluding plane and pour
  nets) that are fully connected. Plane nets are left out because their
  connectivity depends on a zone fill that may not have finished inside the
  budget.
- **DRC**: the count of unwaived `kct check --mfr jlcpcb` errors, excluding
  the `connectivity` rule. In practice most of these errors are
  placement-legality findings, not routing defects. Counting how many rows
  contain each rule: `silk_pad_clearance` 36, `clearance_pad_pad` 31,
  `courtyards_overlap` 29, `clearance_net0_bridge` 13, `hole_to_hole_clearance`
  7, `clearance_pad_segment` 6, and others below 6.
- **"More routable"** means higher completion. When completion is equal, fewer
  DRC errors wins. Pairs that are equal on both are dropped.

A route that hits the budget **before writing any checkpoint**
(`timeout_no_checkpoint`) has no measurable completion. The primary analysis
excludes those rows. A sensitivity run keeps them as the worst outcome
(`--timeouts-as-worst`).

#### Signals (all computed on the unrouted placement)

| Signal | Source |
|---|---|
| `hpwl_all`, `hpwl_signal` | Bounding-box half-perimeter wirelength summed over all nets, or over signal nets only (power nets filtered by the Phase 0 `_looks_like_power_net` heuristic). |
| `steiner_signal_length` | Phase 0 feature (`fom_features._steiner_total_signal_length`). |
| `rudy_peak`, `rudy_overflow`, `rudy_top10_mean` | `router/congestion_estimator.CongestionEstimator` demand on a ~100-tile grid over signal nets. The estimator has no capacity model, so the script adds the simplest one: capacity = tile area / 0.4 mm track pitch x copper layers. The values are peak utilisation, the sum of utilisation above 1.0, and the mean of the top 10% of tiles. |
| `crossing_count` | Star-topology ratsnest crossings (`fom_geometry.crossing_count`). |
| `placement_cost_total`, `placement_wirelength`, `placement_overlap`, `placement_drc`, `placement_boundary` | The `optimize-placement` objective and its breakdown, read from `kct optimize-placement --dry-run --format json`. |
| `sigma` | **Reference only, not a signal.** This is the perturbation size. It shows how much of the outcome "how hard was the board shaken" explains on its own. A real gate cannot see it. |

Every signal is treated as cost-like (lower = more routable). That direction
is fixed in advance and never fitted, so the single-signal accuracies carry
no selection bias.

#### Metrics

- **Pairwise accuracy**: over same-board pairs with a strict outcome order,
  the fraction where the more routable placement has the lower signal. A tie
  in the signal counts as 0.5.
  - *All pairs* uses the full completion-then-DRC order.
  - *Completion-decided* keeps only pairs whose completion differs.
  - *Same-sigma* keeps only pairs of two perturbations drawn at the same sigma.
    This removes the shake-size confound and comes closest to a gate comparing
    two optimizer candidates.
- **Spearman correlation**: computed within each board, then averaged. `n` is
  the number of boards where both sides vary.
- **Fitted combination**: a pairwise (RankNet-style) logistic regression on
  differences of `log1p(signal)`, with no intercept and an L2 penalty, over 10
  signals. `placement_cost_total` and `hpwl_all` are left out because they are
  linear mixes of included terms. The fit is evaluated **leave-one-board-out**:
  standardise and fit on every other board's pairs, then score the held-out
  board. The honest baseline is the single signal with the best *training*
  accuracy, re-chosen in each fold.

### Sample sizes

- 132 placement rows in total. **35 timed out without a checkpoint and are
  excluded**: all 15 on board 05, 13 of 15 on board 07, 6 of 15 on board 03
  and 1 of 15 on board 06.
- Primary analysis: **7 boards with usable rows, 6 contributing pairs, 85
  placements, 370 pairs.** 229 of those pairs are decided by completion, and
  63 are same-sigma pairs.
- Summed route wall time for the kept corpus is 169 min, or about 57 min
  across 3 workers. The aborted first run added about 11 min more.
- **Label noise**: the original placement routed with seeds 0, 1 and 2 gave
  identical completion and DRC on all 6 boards that produced a label. On
  boards 05 and 07, all three seeds timed out without a checkpoint. With this
  configuration the router is effectively deterministic, so disagreement in
  the table below comes from the signals, not from the labels.

### Ranking accuracy and correlation (primary analysis)

| Signal | Pairwise acc (all 370) | Completion-decided (229) | Same-sigma (63) | Spearman vs completion | Spearman vs DRC |
|---|---|---|---|---|---|
| `hpwl_all` | **0.749** | **0.795** | **0.698** | -0.50 (n=6) | 0.51 (n=6) |
| `placement_wirelength` | 0.716 | 0.782 | **0.698** | -0.48 (n=6) | 0.48 (n=6) |
| `hpwl_signal` | 0.708 | 0.734 | 0.683 | -0.43 (n=6) | 0.44 (n=6) |
| `steiner_signal_length` | 0.678 | 0.703 | 0.667 | -0.41 (n=6) | 0.41 (n=6) |
| `placement_cost_total` | 0.659 | 0.672 | 0.643 | -0.36 (n=6) | 0.46 (n=6) |
| `placement_overlap` | 0.614 | 0.570 | 0.627 | -0.29 (n=4) | 0.56 (n=4) |
| `placement_boundary` | 0.600 | 0.659 | 0.579 | -0.73 (n=3) | 0.47 (n=3) |
| `placement_drc` | 0.591 | 0.535 | 0.516 | -0.21 (n=4) | 0.66 (n=4) |
| `rudy_peak` | 0.551 | 0.559 | 0.524 | -0.01 (n=6) | -0.03 (n=6) |
| `crossing_count` | 0.545 | 0.574 | 0.587 | -0.20 (n=6) | 0.19 (n=6) |
| `rudy_top10_mean` | 0.541 | 0.515 | 0.460 | -0.07 (n=6) | 0.24 (n=6) |
| `rudy_overflow` | 0.500 | 0.500 | 0.500 | n/a (constant 0) | n/a |
| *`sigma` (reference)* | *0.739* | *0.806* | *0.500 by construction* | *-0.55 (n=6)* | *0.54 (n=6)* |
| **Fitted combination (LOBO)** | **0.743** (macro 0.743) | | **0.603** | | |
| Best single signal, chosen per fold (LOBO) | 0.646 (macro 0.702) | | | | |

Per held-out board, fitted vs. best single signal chosen in that fold:

| Held-out board | Pairs | Fitted | Best single (chosen) | `hpwl_all` | `placement_cost_total` |
|---|---|---|---|---|---|
| charlieplex_3x3 | 124 | 0.774 | 0.605 (`steiner_signal_length`) | 0.758 | 0.565 |
| diffpair_test | 66 | 0.667 | 0.712 (`placement_wirelength`) | 0.742 | 0.727 |
| simple_led | 30 | 0.933 | 1.000 (`placement_wirelength`) | 1.000 | 1.000 |
| stm32_devboard | 74 | 0.797 | 0.541 (`hpwl_signal`) | 0.716 | 0.730 |
| usb_joystick | 21 | 0.667 | 0.810 (`placement_wirelength`) | 0.810 | 0.810 |
| voltage_divider | 55 | 0.618 | 0.545 (`placement_wirelength`) | 0.618 | 0.455 |

Acceptance-gate view of the LOBO-fitted model. Each held-out pair is
considered in both directions, and the candidate is accepted when
P(candidate routes better) >= t:

| t | Moves accepted | Precision | Recall |
|---|---|---|---|
| 0.5 | 370 | 0.743 | 0.743 |
| 0.6 | 310 | 0.777 | 0.651 |
| 0.7 | 242 | 0.831 | 0.543 |
| 0.8 | 182 | 0.868 | 0.427 |
| 0.9 | 112 | 0.911 | 0.276 |

Full tables, including per-board accuracy for `rudy_peak` and the per-board
corpus summary: [`analysis.md`](../../data/research/placement_score/analysis.md).

### Sensitivity: timeouts counted as the worst outcome

When the 35 no-checkpoint timeouts are kept as the worst outcome (8 boards,
116 placements, 446 pairs), every number gets weaker. The fitted LOBO
accuracy drops to **0.666** (macro 0.607, same-sigma 0.548). The best single
signal chosen per fold reaches 0.713 and is `placement_wirelength` in every
fold. `hpwl_all` is 0.709. On board 07 (22 pairs) the fitted model scores
0.000. The only two placements on that board that produced a routed file
were blocked outright: 0% completion, 52 placement-legality errors and a 5 s
route. Their signals mark them as *worse* than the placements that timed out,
while the label ranks them above, so every pair comes out inverted. Full
tables:
[`analysis_timeouts_as_worst.md`](../../data/research/placement_score/analysis_timeouts_as_worst.md).

### Corpus B findings

1. **Wirelength is the only pre-route signal with consistent predictive
   value.** `hpwl_all` and the optimizer's own `placement_wirelength` term
   score 0.72-0.75 on all pairs and about 0.70 on same-sigma pairs. Steiner
   length and signal-only HPWL are slightly worse.
2. **Neither RUDY nor crossing count is better than a coin flip** (0.52-0.59,
   Spearman about 0). With the capacity model described above, peak
   utilisation never goes above 0.16 on any fleet placement, so `rudy_overflow`
   is 0 everywhere. The tiles hold far more track than the HPWL demand on
   them, and the hotspots RUDY reports do not line up with where these boards
   actually fail, which is pin-escape and legality around dense parts.
3. **The `optimize-placement` total cost ranks worse (0.66) than its own
   wirelength term (0.72).** Its overlap, DRC and area terms are tuned to make
   the optimizer converge, not to predict routability, and in a ranking they
   add noise. They do carry real information about DRC: `placement_drc` has
   Spearman +0.66 vs DRC count, the highest of any signal.
4. **A fitted combination does not beat plain HPWL.** Leave-one-board-out it
   scores 0.743, against 0.749 for `hpwl_all`, which used no fitting at all.
   On same-sigma pairs it falls to 0.603, below HPWL's 0.698. Its weights are
   unstable: the two RUDY terms that vary (`rudy_peak`, `rudy_top10_mean`)
   receive large weights with opposite signs. That points to overfitting on
   collinear features in only 6 training boards.
5. **Much of the apparent signal is shake size.** The perturbation sigma
   alone ranks 0.739 of all pairs, about as well as the best signal. A gate
   never sees sigma. On the sigma-controlled subset the best signal falls
   from 0.749 to 0.698 and the fitted combination from 0.743 to 0.603.
6. **The hardest boards could not be labelled.** Boards 05 (BLDC) and 07
   (matchgroup) never produced even a checkpoint in 120 s, original placement
   included. Those are exactly the boards where a placement gate would matter
   most, so this corpus cannot say how any signal behaves on them.

## Combined reading

Headline pairwise ranking accuracy from both corpora. Rows pair signals that
measure the same idea; as noted above, the implementations differ.

| Signal (corpus A / corpus B name) | Corpus A pooled (915 pairs, 2 boards) | Corpus B all pairs (370 pairs, 6 boards) | Corpus B same-sigma (63 pairs) |
|---|---|---|---|
| HPWL (`hpwl` / `hpwl_all`) | 0.584 | **0.749** | **0.698** |
| Optimizer cost (`placement_cost` / `placement_cost_total`) | **0.672** | 0.659 | 0.643 |
| Steiner length (`steiner_signal_length`) | 0.551 | 0.678 | 0.667 |
| Overlap (`overlap` / `placement_overlap`) | 0.550 | 0.614 | 0.627 |
| RUDY peak (`rudy_peak`) | 0.462 | 0.551 | 0.524 |
| RUDY overflow (`rudy_overflow`) | 0.570 | 0.500 (constant 0) | 0.500 |
| Fitted combination, leave-one-board-out | 0.586 | 0.743 | 0.603 |
| Fitted combination, in-sample (optimistic) | 0.626 | -- | -- |
| *circuit-skills combined score (reported, different corpus, unverified)* | *0.89* | *0.89* | |

Where the corpora agree:

* **No signal and no fitted combination comes near 0.89.** The best number in
  either corpus is 0.749, 0.14 short, and it falls to 0.698 once shake size is
  controlled.
* **A fitted combination never beats the best fixed signal** of its own
  corpus, out of sample: 0.586 vs 0.672 (A), 0.743 vs 0.749 (B), and 0.603
  vs 0.698 on corpus B's same-sigma pairs.
* **RUDY is at or near chance** in both (0.46-0.57), under two different
  capacity models.

Where they disagree, and why we do not resolve it:

* **Which single signal is best.** Corpus A ranks the optimizer cost first
  (0.672) and HPWL at 0.584. Corpus B ranks HPWL first (0.749) and the
  optimizer cost at 0.659. Even on the two boards both corpora share, the
  per-board numbers point different ways: voltage_divider HPWL 0.533 (A) vs
  0.618 (B), cost 0.768 (A) vs 0.455 (B); charlieplex_3x3 HPWL 0.620 (A) vs
  0.758 (B), cost 0.603 (A) vs 0.565 (B). The labels differ (A: route failed
  or DRC count; B: completion first), the seed board revisions differ, and
  the cost is computed differently. One plausible reading, not tested here:
  corpus A's "better" is mostly "routed at all, with fewer legality errors",
  which a legality-heavy cost term should track, while corpus B's is mostly
  "routed more nets", which wirelength should track. A gate would need to be
  right under both definitions, and no single signal is.
* **How good the fit looks.** Corpus B's leave-one-board-out fit (0.743)
  looks far better than corpus A's (0.586). Corpus A trains each fold on one
  board, corpus B on five. But corpus B's fit loses most of that on
  same-sigma pairs (0.603), so the larger corpus does not show the fit
  learning routability rather than shake size.

### Comparability with circuit-skills' 0.89

The 0.89 figure is not directly comparable with either corpus:

- It is self-reported and we have not reproduced it.
- Their corpus is much larger (64 boards) and their placements come from
  distinct placers. Ours are Gaussian perturbations of one placement per
  board, and same-board perturbations are harder to tell apart than different
  placers' outputs.
- Their router stack, labels and fitted features are unknown to us. Reading
  their code to find out is permitted (ideas only), but this study
  deliberately did not.
- We do not know whether their figure is evaluated across held-out boards.

## Decision

**Do not adopt a calibrated pre-route score as the `optimize-placement`
acceptance criterion.** Neither corpus supports it:

- In both corpora a fitted score does not beat the best single signal, and
  the two corpora do not even agree on which single signal that is.
- Corpus B's best signal is raw HPWL, which `optimize-placement` already
  minimises through its wirelength term. A "calibrated" gate would mostly
  re-check what the objective already optimises.
- At a threshold precise enough to be trusted (corpus B, t = 0.8, precision
  0.87), the gate rejects 57% of moves that really did route better. At
  t = 0.5 it accepts a worse placement about 1 time in 4.
- The evidence comes from small boards. The boards where a gate would earn
  its keep (corpus A: 03, 04, 07; corpus B: 05, 07) produced no pairs.

**No threshold is proposed**, because none of the measured operating points
justifies one. No code in `optimize-placement` was changed.

### What to do instead

- **For acceptance, measure routability directly: `--route-check`
  ([#6234](https://github.com/rjwalters/kicad-tools/issues/6234)).** This is
  the idea behind TraceMaker's `--route-check` (GPL-3.0, idea only): accept a
  candidate only if a short, bounded real `kct route` reaches at least the
  incumbent's completion with no more DRC errors. It is the only
  discriminator that matches the label by construction and needs no
  calibration. Its cost is route time per accepted move: in corpus B even
  small boards took about 15-120 s per check, and on boards 05 and 07 a 120 s
  check produced nothing at all. #6234 has to measure that cost.
- **Do not add RUDY to the placement objective in its current form.** It is
  uninformative under both capacity models tried here. A capacity model
  calibrated on real pin-escape demand would have to come first, which is a
  separate research question.
- **The learned-FOM line (#3187) remains the place for richer features**
  (escape congestion at dense packages rather than global RUDY). Its Phase 0
  classifier needs scikit-learn, which was not in this environment, so it was
  not scored on either corpus. That is a gap, not a negative result.
- **Grow corpus A only through the existing corpus scripts** if the question
  is reopened: it needs boards where some perturbations route (smaller sigma
  on 03/04/07) and more fleet boards.

## Not measured

* Corpus A: no new routing; outcomes are the Phase 0 labels (single run per
  sample, 45 s budget, C++ backend; router non-determinism not quantified).
  Overflow capacity and `placement_cost` weights are untuned defaults.
* Corpus B: one router configuration; labels from a loaded shared machine
  (fewer timeouts are expected on an idle one); boards 05 and 07 unlabelled.
* Neither corpus: cross-board generalisation beyond leave-one-board-out, the
  Phase 0 classifier on the same pairs, or a 0.89-style comparison on an
  equal corpus.

### Note: the optimizer objective gained a decoupling term (issue #6020)

Corpus B's `placement_cost_total` came from `kct optimize-placement --dry-run`
before #6020. Since #6020 that objective adds `2.0 x` the summed
cap-to-assigned-supply-pin distance (`breakdown.decoupling`) on any board with
decoupling caps, and it scores rotated parts at their true orientation. A
re-run would therefore shift `placement_cost_total` on boards 02-07 and 09,
and corpus A's `placement_cost` would also shift wherever `evaluate_placement`
is given decoupling groups. The decision above does not depend on that term.
The decoupling term is a placement-quality preference (the
`decoupling_proximity` FOM term measures the same thing), not a routability
predictor. #6020 validated it with a real route instead: board 04's floorplan
with the caps snapped beside their pins routes 9/9 signal nets under the
board's own route flags, the same as the hand floorplan.

## Reproduce

Corpus A (no routing, seconds):

```bash
uv run python scripts/research/placement_score_calibration.py phase0
# -> data/research/placement_score/results.json
```

Re-running reproduces every accuracy and CI in `results.json` exactly. The
fitted weights (`weights_lobo`, `weights_in_sample`) can differ in the last
floating-point digit (about 1e-16) across BLAS builds.

Corpus B analysis from the committed labels (seconds, reproduces
`analysis*.{json,md}` byte for byte):

```bash
D=data/research/placement_score
uv run python scripts/research/placement_score_calibration.py analyze \
    $D/labels_budget45.jsonl $D/labels_budget120.jsonl \
    --json $D/analysis.json > $D/analysis.md
uv run python scripts/research/placement_score_calibration.py analyze \
    $D/labels_budget45.jsonl $D/labels_budget120.jsonl --timeouts-as-worst \
    --json $D/analysis_timeouts_as_worst.json > $D/analysis_timeouts_as_worst.md
```

Corpus B labels from scratch (about 1 h on 3 workers; routing is expensive on
shared machines):

```bash
SCR=$(mktemp -d)
uv run python scripts/research/placement_score_calibration.py generate \
    --boards boards/00-simple-led/output/simple_led.kicad_pcb \
             boards/01-voltage-divider/output/voltage_divider.kicad_pcb \
    --samples-per-board 16 --route-timeout 45 --workers 3 --cleanup \
    --out "$SCR/labels_budget45.jsonl" --work-dir "$SCR/work45"
uv run python scripts/research/placement_score_calibration.py generate \
    --boards boards/02-charlieplex-led/output/charlieplex_3x3.kicad_pcb \
    --samples-per-board 16 --route-timeout 120 --workers 3 --cleanup \
    --out "$SCR/labels_budget120.jsonl" --work-dir "$SCR/work120"
uv run python scripts/research/placement_score_calibration.py generate \
    --boards boards/03-usb-joystick/output/usb_joystick.kicad_pcb \
             boards/04-stm32-devboard/output/stm32_devboard.kicad_pcb \
             boards/05-bldc-motor-controller/output/bldc_controller.kicad_pcb \
             boards/06-diffpair-test/output/diffpair_test.kicad_pcb \
             boards/07-matchgroup-test/output/matchgroup_test.kicad_pcb \
    --samples-per-board 12 --route-timeout 120 --workers 3 --cleanup \
    --out "$SCR/labels_budget120.jsonl" --work-dir "$SCR/work120"
```

Expect label differences on a less loaded machine: fewer no-checkpoint
timeouts and more final, rather than checkpoint, labels. The committed
`labels_budget45.jsonl` comes from the aborted first run, filtered to boards
00 and 01, with its `route_timeout` field (45, that run's `--route-timeout`)
back-filled because the field was added after the run.

The math helpers of both corpora are pinned by
`tests/test_placement_score_calibration.py`, which also checks that the
committed data still matches the tables in this note.
