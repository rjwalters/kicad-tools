# Notebooks

Exploratory analysis frontends for the two FOM research studies. Both notebooks
are **thin** by design — as `fom_phase0.ipynb` puts it, "all heavy lifting lives
in `scripts/research/`". The notebooks visualize; the scripts are the
reproducible, headless source of truth; `docs/research/` holds the conclusions.

| Notebook | Issue | Frontend for | Reads |
|---|---|---|---|
| `fom_calibration.ipynb` | #3188 | `scripts/research/calibrate_fom.py` | `data/research/fom_weights/calibration_report.json`, `data/research/fom_weights/term_cache.npz` |
| `fom_phase0.ipynb` | #3187 | `scripts/research/train_phase0_classifier.py` | `data/research/fom_phase0/labels.jsonl` |

Write-ups: [`docs/research/fom_calibration.md`](../docs/research/fom_calibration.md)
and [`docs/research/learned_fom_phase0.md`](../docs/research/learned_fom_phase0.md).
Data provenance and regeneration commands: [`data/research/README.md`](../data/research/README.md).

## Install: you need two extras, not one

```bash
uv sync --extra research --extra visualization
# or, equivalently for these notebooks:
uv sync --extra all
```

**Why both.** The `research` extra (`pyproject.toml`) pulls `pymoo`, `pandas`,
`pyarrow`, `scikit-learn`, and `joblib` — but **not matplotlib**. Every plotting
cell in both notebooks does `import matplotlib.pyplot as plt`, and matplotlib
lives in the `visualization` extra (and in `all` and `dev`). `uv sync --extra
research` alone gets you a `ModuleNotFoundError` on the first plot cell.

You also need a Jupyter front-end; neither extra ships one, so run them through
whatever you already use (`jupyter lab`, VS Code, `nbclient`, …).

## Working directory matters

- **`fom_calibration.ipynb` must be launched with the working directory set to
  `notebooks/`.** Its first code cell hard-codes a relative path:

  ```python
  OUTPUT_DIR = Path("../data/research/fom_weights")
  ```

  Run it from the repo root and the load fails.

- **`fom_phase0.ipynb` tolerates either working directory.** It resolves the
  repo root defensively —

  ```python
  REPO_ROOT = Path.cwd().parent if Path.cwd().name == "notebooks" else Path.cwd()
  ```

  — then `sys.path`-injects `REPO_ROOT/src` and `REPO_ROOT/scripts/research` so
  it can `import train_phase0_classifier as t0` and call the same functions the
  CLI driver calls.

## Committed notebooks have no stored outputs

Both notebooks are committed with **all outputs stripped** (0 stored output
cells). Opening one shows the code and the narrative markdown but no figures,
no tables, and no numbers — you must execute it to see anything. That is
deliberate: the numbers of record live in the committed artifacts under
`data/research/` (and are quoted in `docs/research/`), not in notebook output
blobs that would churn the diff on every re-run.

**Neither notebook is executed by CI or by pytest.** `pyproject.toml` sets
`testpaths = ["tests"]`, and no workflow references `notebooks/`. A notebook
that breaks will not turn a build red — if you need a check that runs
unattended, put it in `scripts/research/` (or in `tests/`, the way
`tests/test_calibrate_fom.py` gates the shipped weight YAMLs).

## What each notebook actually shows

**`fom_calibration.ipynb`** (8 cells) — per-board term distributions (committed
value marked against the perturbation histogram), calibrated vs. uniform
rank-consistency per board with the train/holdout split called out, and the
selected global weight vector on a log axis. Everything it renders comes from
the two committed artifacts; it re-runs no calibration.

**`fom_phase0.ipynb`** (22 cells) — loads the labelled corpus, runs
leave-one-seed-out `GroupKFold` cross validation via `t0.cross_validate_across_seeds`,
prints the metrics + go/iterate/abandon decision, then shows the calibration
curve, predicted-probability distribution, permutation feature importances, a
qualitative false-positive / false-negative inspection, and (only when global
OOF AUC > 0.70) a demonstration that the trained classifier plugs into
`compute_fom(..., predictor=..., beta=0.1)` unmodified.

## Headless equivalents

If you want the artifacts rather than the exploration, skip the notebooks:

```bash
uv run python scripts/research/calibrate_fom.py          # fom_weights/
uv run python scripts/research/train_phase0_classifier.py  # fom_phase0/
```

`train_phase0_classifier.py` writes the same metrics and figures
`fom_phase0.ipynb` renders (`metrics.json`, the three `*_plot.png` /
`score_distribution.png` files, the CSVs); `calibrate_fom.py` writes the weight
YAMLs and `calibration_report.json` that `fom_calibration.ipynb` reads — it
emits no figures of its own, so the plots are the notebook's only contribution
there. Both land under `data/research/`; see
[`data/research/README.md`](../data/research/README.md) for the full
regeneration sequence and its cost.
