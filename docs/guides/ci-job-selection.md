# CI job selection

CI starts on every pull request to main and every main push. The `Select CI
jobs` job reads the event file and a complete Git diff; it does not filter out
the workflow itself. `scripts/ci/select_jobs.py` emits one boolean per heavy
job. Lint, type checking and the existing changed-routed-PCB check remain
present for every run.

All selected heavy jobs require the planner, lint and type checking to succeed.
A failed or cancelled prerequisite prevents dependent work from starting. An
unselected heavy job is intentionally skipped, not evidence that its tests
passed. Reviewers must still reject failed prerequisite checks.

| Changed inputs | Selected heavy jobs |
| --- | --- |
| `docs/**` | Python Test |
| `boards/00-simple-led/**` through `boards/04-stm32-devboard/**` | Python Test and the corresponding board E2E |
| `boards/05-bldc-motor-controller/**` | Python Test and Board05 routing regression |
| `boards/06-diffpair-test/**` | Python Test, Board06 E2E and diff-pair routing regression |
| `boards/07-matchgroup-test/**` | Python Test and both Board07 jobs, subject to the existing operator switch |
| `boards/08-precision-acquisition/**`, `boards/09-usbc-pd-power/**` | Python Test (no dedicated native E2E job currently exists) |
| Shared code, tests, scripts, CI, dependency/build inputs, unknown paths or new board directories | Full matrix |
| Multiple known board/documentation paths | Union of their consumers |
| Missing history, invalid event or discovery failure | Full matrix |

Changes under a known board's `output/` also select the standalone KiCad
round-trip smoke job, which reads committed schematic/PCB outputs.

The full Python suite remains selected for docs and board inputs: tests read
source citations, documentation contracts, recipes and saved PCB fixtures. This
change does not pretend those tests are independent of their inputs. It avoids
unrelated native build/smoke and board-routing jobs. Narrowing Python test
collection further requires its own verified dependency map.

PRs use the merge base of the event's base/head commits. Main pushes use the
entire before/after range, including batched commits. Rename detection is off,
so both old and new paths select their consumers. Paths are NUL-delimited to
preserve whitespace and newlines. Event values are read as data and commit IDs
must be full SHA values; no event text is interpolated into a shell command.
An unavailable base must never be treated as an empty change set.

The Board07 operator switch, job commands, seeds, allowances, native versions,
container limits and timeouts remain unchanged. Main-only self-hosted routing
and per-ref concurrency from #5239 remain in force. Main verification is not
reused or omitted based on a previous PR result, and tests are not sharded.

## Validation and measurement

Run the selector and workflow contract tests with:

```sh
uv run pytest --no-cov tests/test_ci_job_selection.py tests/test_ci_routed_drc_workflow.py
actionlint -shellcheck= .github/workflows/ci.yml
```

Behavioral tests use real Git repositories to cover divergent PR histories,
multi-commit pushes, cross-board renames, deletions, unusual filenames and
missing revisions. The workflow tests check every heavy job's output wiring and
prerequisites. The selection change itself selects full CI coverage.

Issue #5240 remains open until live evidence demonstrates the original outcome:
lower daily job-minutes and PR queue waits below five minutes at comparable
traffic rates. Record docs-only, single-board and shared-source examples; also
verify that a failed cheap check prevents heavy execution. Do not merge an
intentionally failing probe into main.

For before/after windows, retain run IDs/attempts, event/head, created/start/end
timestamps, job names, conclusions and runner identities from the Actions API.
Separate hosted and self-hosted jobs, cancellations and superseded runs. Sum
job execution durations, not run wall time, and do not call that billing data.
Report sample size and PR arrival rate. With `needs` in place, job creation to
start can include prerequisite time: report that separately from runner queue
delay, using prerequisite completion to identify the earliest eligible start.
Do not claim a queue improvement from classifier tests or predicted savings.
