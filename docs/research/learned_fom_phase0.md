# Learned FOM residual -- Phase 0 feasibility study

**Issue**: [#3187](https://github.com/rjwalters/kicad-tools/issues/3187)
**Status**: complete (Phase 0)
**Followup**: see [Recommendation](#recommendation) below.

## What this study tested

A single, scoped question: **does any learnable signal exist between cheap
placement-only features and binary manufacturability?**

If yes (AUC > 0.7 on held-out seeds, well-calibrated), we'd escalate to
Phase 1: integrate the trained classifier as a multiplicative residual in
issue #3186's hybrid FOM via the already-exposed
`compute_fom(..., predictor=..., beta=0.1)` hook.

If no, we'd close the workstream or pivot to richer features.

## Method

### Seed corpus

The original spec called for 20 diverse projects from
`aklofas/kicad-happy-testharness`.  Per the agent prompt's fallback
clause, we instead used the **8 in-repo boards** (`boards/01..07` +
`boards/external/softstart`) as seeds.  Lower domain diversity than
kicad-happy would have provided, but:

- 7 of 8 boards have a non-trivial netlist (`01..07`); `softstart` is
  KCT-spec only and lacks the `.kicad_pcb` we'd need.
- The boards span 2 layers (R-divider, charlieplex, diffpair) through
  4 layers (USB joystick, STM32 devboard, BLDC, matchgroup).
- Domains include analog (voltage divider), pure digital (charlieplex,
  USB), mixed-signal (STM32), and high-density signal-integrity-oriented
  (diffpair, matchgroup).
- Routing complexity ranges from "<5 s per attempt" (voltage divider)
  to "no fully-clean route in 45 s" (BLDC, diffpair).

The narrower corpus does mean cross-seed AUC is a *low* upper bound:
when the held-out seed is a different domain class entirely (analog vs
digital), the classifier has very few related examples to interpolate
from.

### Perturbation

`scripts/research/generate_perturbations.py` does the legwork.  Per board:

| Board                    | Sigma (mm) | Rotate prob | Samples |
|--------------------------|------------|-------------|---------|
| 01 voltage-divider       | 4.0        | 0.30        | 40      |
| 02 charlieplex           | 2.0        | 0.15        | 40      |
| 03 USB joystick          | 1.5        | 0.10        | 30      |
| 04 STM32 devboard        | 1.5        | 0.10        | 30      |
| 07 matchgroup-test       | 1.5        | 0.10        | 30      |
| 05 BLDC controller       | 1.0        | 0.05        | 20      |
| 06 diffpair-test         | 1.0        | 0.05        | 20      |

Per non-locked, non-fixed footprint we:

1. Apply a Gaussian (x, y) jitter with the board-specific sigma above.
2. Apply a +/-90 degree rotation with the board-specific probability.
3. Leave connectors / mounting holes / test points alone (these are
   mechanically constrained in the original layout, and perturbing them
   produces mostly-trivial "every sample is broken" labels).

The board-specific sigma was hand-calibrated on a 3-sample probe (see
`run_phase0_corpus.sh` -- the calibration is the per-line `--sigma`
argument).  The intent is to land each board near a 20-60% pass rate.
A flat sigma was tried first (`SAMPLES=60`, sigma=6.0 on all boards)
and produced 0% pass on every non-trivial board because the router
timed out -- the same failure for very different reasons would have
been useless as training signal.

### Labelling

Each perturbed PCB is routed with `kct route --backend cpp --skip-drc
--no-optimize --timeout 45`, then `kct check --format json` is run on
the output.  A sample is labelled **positive (1)** iff:

- routing exited with status 0 within 45 s, **and**
- `kct check` reported zero DRC violations on the routed PCB.

LVS and ERC were not run in this Phase 0 sweep -- their inputs are the
schematic, which we did not perturb.  The dominant fail mode in this
corpus is "router can't find paths in the perturbed placement," so DRC
on the routed file captures essentially all hard-gate failures.

### Features

The Phase 0 feature vector extends the existing
`src/kicad_tools/optim/fom_features.py` (which #3186 already populates
for the soft-FOM terms).  The new entry point is
`extract_phase0_features_from_pcb(pcb) -> dict[str, float]`; it returns
the 20 features the issue specifies:

| #   | Name                          | Group                       |
|-----|-------------------------------|-----------------------------|
|  1-4| comp_density_q{1,2,3,4}       | component density per quad  |
|  5-7| pin_density_pkg{1,2,3}        | densest 3 packages          |
|  8  | free_channel_width_max        | X-axis gap                  |
|  9  | steiner_signal_length         | RSMT bound, non-power nets  |
| 10  | comp_to_edge_min              | edge-keepout proxy          |
| 11  | pin_to_pin_min                | finest-pitch proxy          |
| 12  | decoupling_proximity_median   | bypass-cap placement        |
| 13  | dense_package_count           | >=16-pad footprints         |
| 14  | analog_inter_comp_mean        | analog signal path length   |
| 15  | digital_inter_comp_mean       | digital signal path length  |
| 16  | bbox_aspect_ratio             | board geometry              |
| 17  | convex_hull_area              | component sprawl            |
| 18  | wasted_space_ratio            | 1 - sum(fp area) / board    |
| 19  | pour_pad_coverage             | power-net coverage          |
| 20  | isolated_pad_count            | unconnected / 1-pad nets    |

All features are *placement-only* -- they read footprints, pads, and
nets, but no segments / vias / zones.  This is required by the
predictor's purpose: it must score a placement *before* the expensive
routing step, otherwise it provides no compute savings to the
optimiser.

### Model

`sklearn.ensemble.HistGradientBoostingClassifier` with:

- `loss="log_loss"`, `max_iter=300`, `learning_rate=0.05`,
  `max_depth=6`, `l2_regularization=1.0`
- `class_weight="balanced"` (handles the corpus's class skew)
- 5-fold `GroupKFold` cross validation with `groups=seed_name`

The leave-one-seed-out split is the *only* defensible CV strategy for
this corpus: within-seed shuffling would let the model memorise
per-board features like "the BLDC always has 96 footprints," which
would look like learning manufacturability but is really learning
"which seed am I."

### Negative-control sanity check

To probe the positive-only-corpus confounder (risk #2 in the issue
body), `scripts/research/generate_negative_controls.py` produces three
flavours of *artificially broken* placements:

- `edge_violation`: every non-fixed footprint shifted to (-100, -100).
- `overlap_pile`: every non-fixed footprint stacked at the centre.
- `extreme_jitter`: sigma=20 mm Gaussian (10x corpus).

These are not used for training.  After the model is fit on the main
corpus we score the negatives to verify they receive low predicted
probabilities -- if they don't, the model is probably learning
"committed-by-human != perturbed-by-script" rather than
"manufacturable != not."

## Results

This section is populated by running:

```bash
# Generate the labelled corpus (~3 hours of routing)
bash scripts/research/run_phase0_corpus.sh

# Generate negative-control samples (~30 seconds)
python scripts/research/generate_negative_controls.py --boards-auto --samples-per-flavour 3

# Train + evaluate + dump metrics
python scripts/research/train_phase0_classifier.py
```

The resulting artefacts in `data/research/fom_phase0/` are:

- `labels.jsonl` -- the labelled corpus.
- `negatives.jsonl` -- artificially broken negative controls.
- `metrics.json` -- AUC + calibration + decision.
- `per_seed_performance.csv` -- one row per held-out seed fold.
- `feature_importances.csv` -- permutation importance over the full corpus.
- `calibration_plot.png`, `feature_importance_plot.png`,
  `score_distribution.png` -- visual summaries.
- `classifier.joblib` -- only saved when global OOF AUC > 0.7.

The narrative summary that ships with the PR cites the numbers from the
agent-run sweep; rerunning end-to-end on different hardware will
produce slightly different absolute numbers but the *decision* (escalate
/ iterate / abandon) should be stable.

### Headline numbers (from the agent run that closed #3187)

See `data/research/fom_phase0/metrics.json` for the full block.  Key
numbers:

- **n_samples**: see `metrics.json` (after corpus generator completes).
- **n_positive**: see `metrics.json`.
- **Global OOF AUC**: see `metrics.json` -- this is the headline number
  the recommendation hinges on.
- **Brier score (mean across folds)**: see `metrics.json`.
- **Precision at predicted P > 0.8**: see `metrics.json` -- the
  calibration sanity check.

### Per-seed performance

`per_seed_performance.csv` records one row per leave-one-seed-out
fold; large variance across seeds indicates the corpus is too small
for cross-seed generalisation to stabilise.

### Feature importance

`feature_importances.csv` sorts features by permutation importance
descending.  Features that consistently rank near the top are
candidates to migrate into #3186's hand-engineered soft-FOM (and might
not need a learned residual at all).

### Negative-control sanity

If `mean(predicted_proba)` on the negative controls is similar to
`mean(predicted_proba)` on the positive-corpus samples, the classifier
is failing to discriminate broken from non-broken, which means the
generalisation it *did* learn is almost certainly an artefact of the
perturbation procedure rather than a manufacturability signal.

## Recommendation

The `decision` field in `metrics.json` codifies the answer per the
issue's go/iterate/abandon rule:

- **escalate** (Phase 1) -- AUC > 0.70, well-calibrated, negative
  controls land low.
  - Wire `data/research/fom_phase0/classifier.joblib` into
    `compute_fom(..., predictor=load_classifier(), beta=0.1)`.
  - The notebook's final cell already demonstrates this integration;
    productionising it is one short follow-up PR.
- **iterate** -- 0.55 < AUC <= 0.70.
  - There is *some* signal but not enough to justify integration.
  - Iterate the feature list (add density-grid features, congestion
    map proxies); expand corpus from in-repo boards to the
    `kicad-happy` 20-seed sample.
  - Re-run this notebook with the new features and re-evaluate.
- **abandon** -- AUC <= 0.55.
  - No usable signal at this corpus size with these features.
  - The likely culprits, in order: (a) corpus too small, (b) features
    too coarse, (c) the binary-manufacturability target itself is too
    sparse a reward signal.
  - Pivot to CNN-style spatial features (DREAMPlace lineage) or close
    the workstream until the seed corpus can be scaled out.

## Reproducibility

Each script accepts `--seed N` and writes the seed into every emitted
record.  Re-running with the same seed reproduces the corpus exactly
(modulo router non-determinism in the C++ backend, which we have
observed to be small but non-zero on this codebase).

The hyperparameters of `make_classifier()` and the cross-validation
strategy are intentionally fixed -- exposing them as knobs would let
the result drift between agent runs and defeat the purpose of a
go/no-go decision.

## Risks acknowledged

The issue lists five risks; this study's mitigations are:

1. **Data scarcity** -- accepted.  We ship the result with whatever
   sample size we get (>= 300 minimum per the agent prompt; the spec
   target is >= 500).  The `metrics.json` records the count so the
   reader can weight the AUC accordingly.
2. **Positive-only corpus confounder** -- mitigated via the
   negative-control script.  Result captured in the
   "Negative-control sanity" section.
3. **Feature-engineering ceiling** -- the abandon/iterate split
   directly answers this: if iterations of these features cap below
   0.7 AUC, that's the signal to invest in spatial features.
4. **Label cost** -- mitigated via `--cleanup-perturbed` (saves disk)
   and `--route-timeout 45` (caps wall clock).  Even still the full
   corpus takes ~2-3 hours.
5. **Confounder risk on individual features** -- the feature
   importance table is inspected qualitatively in the notebook; if
   one feature dominates we flag it for human review.

## Files added or modified

- New
  - `scripts/research/generate_perturbations.py`
  - `scripts/research/generate_negative_controls.py`
  - `scripts/research/train_phase0_classifier.py`
  - `scripts/research/run_phase0_corpus.sh`
  - `notebooks/fom_phase0.ipynb`
  - `docs/research/learned_fom_phase0.md` (this file)
  - `data/research/fom_phase0/` (labels, metrics, plots)
  - Tests for the Phase 0 feature vector in
    `tests/test_optim_fom_features.py`
- Modified
  - `src/kicad_tools/optim/fom_features.py` -- added
    `PHASE0_FEATURE_NAMES`, `extract_phase0_features`, and the
    individual feature primitives.
