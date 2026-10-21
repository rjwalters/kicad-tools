# External benchmark re-run: four published cases (issue #7669)

First run of the dispatch workflow `.github/workflows/external-bench.yml`
(added in #7698). Files here are the unedited artifact `external-bench-39539832212`
of GitHub Actions run **39539832212** (`workflow_dispatch`, ref `main`).
Older collections are unchanged.

## Producer

| Field | Value |
|---|---|
| Workflow run | `39539832212` (artifact `external-bench-39539832212`) |
| Tool commit | `a802bab41ac5e43ea72e228eec83106a522347d3` (clean tracked tree: `tracked-source-status.txt` was empty in the artifact) |
| Image | `kicad/kicad@sha256:18693567392b80da435f9fa952ce3a3e534c66eb5a6033f5b9c80aa3b19dd3ec`, kicad-cli 10.0.6, Python 3.12.15, x86_64 Linux (`router_cpp.cpython-312-x86_64-linux-gnu.so`) |
| Native check | `C++ backend: available (version 1.0.0)`, build version 64 (required 64); see `native-check.txt` |
| Budgets | seed 42 (`PYTHONHASHSEED=42`), container `--memory 12g`, job `timeout-minutes: 120`; no per-case routing budget override |
| Runner CPU/RAM | Not recorded by the workflow (GitHub-hosted `ubuntu-latest`) |

Each case report (`*.json`) also records `tool_commit` (`a802bab`), board source
commit, protocol and `backend` (`cpp`, available). Board files are fetched at run
time from their pinned commits and are not vendored.

## Results (as measured)

Completion is physical connectivity after routing, out of the board's total connections.

| Case | Outcome | Completion | Pre-route | Vias | Wirelength | Duration |
|---|---|---|---|---|---|---|
| pocketbeagle, zero-touch | **failed** | 0/296 (0.0%) | 0/296 | 0 | 0 mm | 4.6 s |
| beagleconnect_freedom, zero-touch | partial (exit 1) | 131/460 (28.5%) | 134/460 | 0 | 0 mm | 42.3 s |
| strf, zero-touch | partial (exit 4) | 44/98 (44.9%) | 18/98 | 8 | 147.45 mm | 1811.6 s |
| strf, tuned | partial (exit 4) | 44/98 (44.9%) | 18/98 | 6 | 142.13 mm | 1881.9 s |

- **PocketBeagle fails with a new error.** The router raised
  `ValueError: Layer 'In1.Cu' is not available on this board. Declared copper layers: .`
  (an empty layer list) after 4.6 s and placed no copper. The earlier grid-size
  refusal (#4945) is no longer the blocker; this is a different failure and is
  not diagnosed here.
- **BeagleConnect Freedom lost connectivity.** It ended at 131/460, below its
  134/460 pre-route baseline (`newly_routed_connections` = -3), with no vias or
  copper wirelength. This is lower than the 197/460 of the 2026-09-15 partial run
  and is reported as measured. `SH1` remains placement-invalid; `GND` is excluded.
- **STRF improves on 2026-09-12.** Both protocols reach 44/98 from 18/98 (26
  newly routed). Both runs exit 4 (partial), the native checks fail (317E/154W
  zero-touch, 321E/154W tuned), and the tuned run completes 1 of 2 differential
  pairs where zero-touch completes 2 of 2.
- The reports do not record whether the router fell back to Python for any
  pass, so the durations are not claimed as pure-C++ speed. The native backend
  was present and the reports record `backend: cpp`.

No manufacturing acceptance, DRC-clean result or competitive claim follows from
these numbers. They bind to the tool commit above only.

## Reproduce

Dispatch the workflow (no inputs): `gh workflow run external-bench.yml --ref <branch>`,
then download the artifact (`gh run download <run id>`). See
`benchmarks/external/README.md`, "Re-running the published cases in CI".
