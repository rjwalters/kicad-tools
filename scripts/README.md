# scripts/

Developer and CI helper scripts. These are **not** part of the installed `kct`
package — they support building, deploying, diagnosing, and gating the project.

Run shell scripts with `bash scripts/<name>.sh` (or `./scripts/<name>.sh`) and
Python helpers with `uv run python scripts/<name>.py`, from the repository root.

## Top-level scripts

| Script | Purpose |
|--------|---------|
| `build-cpp.sh` | Build/install the nanobind C++ router extension (`clean`, `check` subcommands). Wrapped by `kct build-native`. |
| `deploy-site.sh` | Manual one-command deploy of the kicad-tools.org demo gallery to Cloudflare Pages (via a locally authenticated `wrangler`). |
| `install-kct.sh` | Install kicad-tools into a consumer PCB-design repo (uv dependency + vendored `.claude/commands/kct/` skills and `ci/` gate scripts). |
| `calibrate_area_estimate.py` | Calibration helper for the sum-of-clearances board-area estimator (#3403). |
| `check_trace_vs_zone_fills.py` | Verify track segments against foreign-net zone fill copper (clearance/short check DRC cannot yet do; #3527). |
| `diagnose_b03_diffpair.py` | Diagnostic for why the differential-pair pre-pass fails on board 03 (#2490). |
| `route_chorus.py` | Canonical chorus-test-revA routing recipe runner with partial-net rescue (#3474). |

## Subdirectories

### `ci/`

Gate scripts invoked by GitHub Actions (and vendored into consumer repos by
`install-kct.sh`). They take a board path as a CLI argument and gate the build
on copper-LVS, routed-DRC, diff-pair / match-group coverage, board-specific
end-to-end checks, the mypy baseline, and route determinism:

`board06_determinism_smoke.sh`, `board_route_determinism_smoke.sh`,
`check_board_00_e2e.py`, `check_board_05_blocking.py`, `check_copper_lvs.py`,
`check_diffpair_coverage.py`, `check_matchgroup_coverage.py`,
`check_mypy_baseline.py`, `check_routed_drc.py`, `net_class_map_resolver.py`.

### `research/`

FOM-calibration and corpus-generation scripts (pair with `data/research/`):

`calibrate_fom.py`, `check_negatives.py`, `demo_integration.py`,
`generate_negative_controls.py`, `generate_perturbations.py`,
`run_phase0_corpus.sh`, `run_phase0_fast_corpus.sh`,
`train_phase0_classifier.py`.
