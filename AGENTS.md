<!-- BEGIN KCT CONTRIBUTOR GUIDANCE -->
<!-- Generated from docs/contributing/agent-guidance.md. Do not edit inside these markers; edit the source, then run: uv run python scripts/render_agent_guidance.py --write -->

## Routing performance: build the C++ backend first

Before benchmarking routing latency or filing "router is slow" issues,
verify the C++ router extension (`src/kicad_tools/router/router_cpp.*.so`)
is built in the active worktree:

```bash
uv run kct build-native --check
# Expected: "C++ backend: available (version 1.0.0)"
```

If it reports "not installed", build it once:

```bash
uv run kct build-native
```

`uv sync` does **not** build the native extension — fresh checkouts and
new git worktrees (`.loom/worktrees/issue-N/`) need this step explicitly.
The C++ backend gives a 10-100x speedup for the A* loop, so a missing
extension is the most likely cause of multi-minute-per-net routing.

The test suite now **requires** the built backend (Issue #9705). A test case
that needs the extension fails with `C++ router backend not built: run uv
run kct build-native` — it no longer silently skips — via the
`requires_router_cpp` marker or a `require_router_cpp()` call from
`tests/conftest.py`. Do not add `skipif`/`importorskip` guards for this
backend; `tests/test_router_cpp_no_skip_guards.py` fails the PR if one
appears. (DRC and placement extensions keep their own skip guards — the
ruling covers only the router backend.)

### Check the acceleration backend before timing routes

The confirmed cause of slow routing under `--all-extras` (#8609 reported
~1,650 s vs ~140 s on one board) is **scipy, not MLX/CuPy** (Issue #8657).
`RoutingGrid._dilate_blocked`
called `scipy.ndimage.binary_dilation` whenever scipy was importable, and
scipy arrives transitively with the `bayesian`, `research` and `models`
extras. That branch is removed: the dilation is now always the NumPy loop,
so installed extras no longer change its speed or its result (the scipy
branch also disagreed with the loop within `radius` cells of the grid edge).

What was measured: on a synthetic 4 x 2001 x 2001 (16M-cell) grid, one
wide-net pure-Python `Router.route` took ~1.8 s without scipy and ~17.5 s
with it, almost all of it inside scipy's dilation. Adding only scipy made
it slow; blocking only the scipy import made it fast again. The original
full-board timing was **not** re-run, so whether this accounts for all of
that gap is not established.

Separately, when a `RoutingGrid` is built with a `PerformanceConfig` on a
GPU backend (MLX/CuPy), the pure-Python A* does per-cell scalar indexing
that is slower than on NumPy (unmeasured here). `Router` forces NumPy grid
arrays as a defensive guard. The default `kct route` path builds the grid
without a `config`, so it is NumPy regardless of installed extras. When
benchmarking, check what is active: `uv pip list | grep -i -E 'mlx|cupy'`,
and the `Grid using ... backend` log line from `RoutingGrid._select_backend`.
`uv sync --all-extras` now resolves on macOS (`cupy-cuda12x` is skipped there).

### Rebuilding after editing C++ sources

`kct build-native` now detects staleness automatically: it compares the
newest mtime of `src/kicad_tools/router/cpp/**` (`.cpp`/`.hpp` and
`CMakeLists.txt`) against the installed `.so` and **rebuilds when the
source is newer**, even without a `BUILD_VERSION` bump. When the `.so` is
already up to date it prints `SKIPPED rebuild` instead of the old
misleading `installed successfully!`.

`--force` always recompiles regardless of mtimes — reach for it if you
suspect the auto-detection missed something (e.g. a touched build flag).

See `README.md` "Fresh worktree checklist" for the full setup sequence.

## Native evidence: produce it on the pinned KiCad toolchain

Committed native DRC/ERC evidence (`readiness-verification/**`,
`readiness.json`, `native-drc*.json`) must not depend on the host's KiCad
config or release (issue #6962). Producers (`kct readiness`,
`scripts/boards/*.py`) run kicad-cli with a scratch `KICAD_CONFIG_HOME` holding
the pinned tables in `src/kicad_tools/data/kicad-config/10.0/`, and **refuse**
(exit 2, nothing written) a kicad-cli other than CI's image release, or a DRC
report with `lib_footprint_issues`. On a Mac with Homebrew KiCad, route
through the pinned image instead of fighting the refusal:

```bash
KCT_KICAD_DOCKER=1 uv run python -m kicad_tools.cli.kicad_toolchain --check
KCT_KICAD_DOCKER=1 uv run kct readiness boards/<board> --verify --checked-at <ISO>
```

Never "fix" a `lib_footprint_issues` refusal by ignoring the rule in a
`.kicad_pro`; it means the environment, not the board, was checked.

## Type checks: distrust a local mypy error outside your diff

`.mypy_cache/` is gitignored, so it survives `git reset --hard`,
`git clean -fd`, rebases, and worktree reuse — bare `mypy` /
`pnpm typecheck` can replay an error computed against an older tree, in a
file nobody touched. CI never reproduces it (no `actions/cache`; the Type
Check job pins `enable-cache: false`), which is exactly how two PR reviews
lost a cycle each to a phantom failure.

`scripts/ci/check_mypy_baseline.py` is immune by construction — it runs
`--no-incremental` (~20 s; `--incremental` opts back into the cache). For
anything else, `rm -rf .mypy_cache` and re-run **before** investigating. Never
use `--update` to make a phantom error go away; it bakes it into the baseline.

`--all-extras` environments type-check differently from the CI dev env:
installing the optional packages makes mypy resolve them, which both removes
the missing-import entries the dev baseline carries and surfaces typed
diagnostics (Ax/CuPy/MLX/PyMuPDF parameter and attribute checks) the dev env
never sees. As of issue #9241 the baseline gate passes **identically** under
`uv sync --frozen --extra dev` (what the CI Type Check job installs) and
`uv sync --frozen --all-extras` — the stub-less optional modules (`cupyx`,
`fitz`, `pynng`, `playwright`) are silenced by targeted per-module
`[[tool.mypy.overrides]]` in `pyproject.toml`, and the extras-typed call sites
are fixed rather than baselined. If the gate starts failing only under one of
the two environments, an environment-dependent diagnostic crept back in: fix
the call site (narrow the optional, annotate the duck-typed variable) or
extend the per-module override — do not split the baseline per environment.

## Slow single-test runs: check coverage overhead before suspecting a hang

`pyproject.toml`'s default `addopts` (`--cov=kicad_tools --cov-report=term-missing
--cov-report=html`) instruments the *entire* `kicad_tools` package on every
`pytest` invocation, even a single targeted test file. The collection, reporting
and interpreter-shutdown work that adds can dwarf a short test's own runtime: on
`tests/test_board02_real_hardware.py::test_generated_real_schematic_and_pcb_agree`
two independent runs measured 2.79s vs 138.15s and 0.987s vs 54.092s total wall
time (issue #5490). Most of that is CPU-bound and can continue long after pytest
has already printed the test's result, which is easy to mistake for an infinite
loop under a short timeout.

Before filing a "test hangs" issue, re-run with coverage disabled:

```bash
uv run pytest path/to/test_file.py::test_name --no-cov
```

A large timing drop points to coverage collection, reporting or shutdown
overhead. Confirm that both runs reach the same test result; a short timeout or
high CPU use alone does not distinguish slow progress from a bug. The original
300–550s observations in #5490 were not reproduced to completion, so this check
rules coverage overhead *in* — it does not rule every hang out.

## Daemon Judge steps: hand a pending-CI PR back, never wait out the step budget

A daemon-dispatched Judge step (`LOOM_SWEEP_ID=step-judge-<pr>-<epoch>`) gets
exactly one PR and roughly a 30-minute step budget, and this repository's CI
does not fit inside that budget: the Actions Test job on PR #9664 alone ran
about 66 minutes, and in one 8-hour sample about 40% of daemon Judge steps on
both runtimes were killed at the step timeout (exit 143) mid-wait (issue #9686).

The installed Judge instructions offer two paths when CI is pending: skip and
continue the batch, or — for a "single-PR / manual invocation" — wait in the
foreground for up to 1800 s. **In this repository a daemon dispatch is never
that second path**, even though it serves exactly one PR: a 1800 s foreground
wait is the whole step budget, so the step is killed before it can record why
it stopped. Treat a daemon step like the batch path:

1. Take the existing pre-check **before any scratch checkout, venv setup or
   pytest** — one bounded snapshot,
   `loom-daemon forge wait-checks <PR> --timeout 20 --settle-red` (the timeout
   is seconds; one bounded wait, never a loop of short polls).
2. Ordinary pending CI (`LOOM-CHECKS-TIMEOUT*`, `LOOM-CHECKS-HEAD-MOVED*`,
   checks queued behind the CI job cap): no review and no verdict on the
   pending head. Remove `loom:reviewing`, release the lease with
   `--skip-reason ci-pending`, leave `loom:review-requested` in place, and end
   the step with `LOOM_STEP_OUTCOME role=judge target=<PR> class=noop
   reason=ci_pending`. A later dispatch re-draws the PR once CI settles;
   `ci_pending` does not count toward the no-op repeat cap.
3. Settled results take the normal paths: `LOOM-CHECKS-RED*` → "When CI
   Fails"; `GREEN` / `NONE` → review as usual. Approval still requires the
   verdict gate on the reviewed head.
4. Ordinary pending CI is not the stale-head case: a head with **zero**
   check-runs at any age has its own recovery path in the Judge instructions
   (the `loom:ci-missing-retrigger` procedure), which stays in force. A head
   with queued or running checks is ordinary pending CI — step 2.

## "Have we already evaluated X?" -- ask the ecosystem registry

Before researching, benchmarking or reimplementing something a neighbouring
project already does, check what this repo already concluded about it:

```bash
kct ecosystem list                  # everything we track, with verdicts
kct ecosystem show kicadroutingtools
kct ecosystem where-we-sit          # our invariants and non-goals
```

Eight evaluation notes sat in `docs/research/` unlinked from anywhere before
issue #5839; grepping `docs/` is no longer the way to find them. The registry
is `src/kicad_tools/ecosystem/data/projects.toml` and each entry carries the
paths of our own notes, plus a `license_compat` field that says whether that
project's code may be reused here at all (**AGPL and unlicensed neighbours are
ideas-only, in both directions** -- never copy). Agents can reach the same data
through the `ecosystem_list` / `ecosystem_show` MCP tools.

Adding or editing an entry means re-rendering the README block:
`uv run python scripts/ecosystem_render.py --write`. A test fails if you
forget. Narrative: [`docs/ecosystem.md`](docs/ecosystem.md).

## Changelog: every user-visible PR adds a `changelog.d/` fragment

Don't edit `CHANGELOG.md`'s `[Unreleased]` in a PR — ~20 merges a day would
conflict on it. Instead add one new file per user-visible change (issue #5775):

```
changelog.d/<issue>.<kind>.md        # kind: added | fixed | changed | performance | upgrade
```

`<issue>` is the **issue** number (not the PR number). The body is the
finished bullet in the existing CHANGELOG voice, citing the issue:

```markdown
- **`kct check` flags vias under QFN bodies** (Issue #5736). New
  `via_under_body` category, on by default; findings are warnings.
```

Add an extra `<issue>.upgrade.md` when users must act (a check that can newly
fail, a changed default, a removed flag). The `Changelog Fragment Check` CI
job fails a PR whose title type is user-visible (`feat`, `fix`, `perf`, or a
non-conventional subject) with no fragment citing its issue. Internal titles
(`chore`, `ci`, `docs`, `test`, `refactor`, `build`, ...), Dependabot PRs and
Loom resyncs pass without one. `changelog:skip` is the escape hatch for
the rare user-visible-looking PR that needs no entry. Details:
`changelog.d/README.md`; check locally with
`uv run python scripts/changelog_gap_report.py --pr <N>`.

## Releasing

Releases go through a **PR-based flow** — see [`RELEASING.md`](RELEASING.md) for
the canonical process. Never push the version-bump commit directly to `main`;
the version is bumped on a branch, merged via PR, and the `vX.Y.Z` tag is
created **only after** the bump PR merges (the tag must point at a commit that
is actually on `main`, never a pre-merge `release/vX.Y.Z` branch commit —
`main` squash-merges, so the commit that lands has a different SHA than the
one on the release branch).

## Clean-room reimplementation of GPL KiCad behaviour

kicad-tools is MIT, KiCad is GPL: never port KiCad code, and never write or
review an implementation after reading GPL source. Behaviour may be
reimplemented through the **two-party clean-room protocol**
([`docs/clean-room.md`](docs/clean-room.md)): a **reader** studies the source
and writes a behaviour spec only (inputs, outputs, rules, units, edge/error
cases, test vectors — no code, no structure-following pseudo-code, no internal
identifiers) and is then **tainted for that topic**; a separate reviewer
checks the spec for leaked expression; a **writer** who has never seen the
source implements from the reviewed spec, public docs and black-box probes,
and acceptance stays black-box on the pinned kicad-cli image. Specs are
committed under `docs/clean-room/` with full provenance (from
`docs/clean-room/TEMPLATE.md`) and the implementing PR links the spec.
Review gate on such PRs: the spec's recorded reader and writer agent ids
differ, and the recorded writer is tied to the implementing PR — forge
author/committer identities are shared across agent sessions, so they
corroborate but cannot carry this check, and missing or ambiguous
attribution fails the PR before approval — plus the structural-port check
(same function breakdown, identifiers, step order — as
in #6661/#6779) runs against the committed spec, not KiCad source. Scope:
KiCad, GPL derivatives and AGPL registry neighbours; **unlicensed** ecosystem
neighbours stay ideas-only — the protocol does not apply to them
(`docs/ecosystem.md`).

<!-- END KCT CONTRIBUTOR GUIDANCE -->

<!-- BEGIN LOOM ORCHESTRATION (AGENTS) -->
This repository uses [Loom](https://github.com/rjwalters/loom) for AI-powered development orchestration (dual-runtime: Claude Code reads `CLAUDE.md`; OpenAI Codex CLI and other AGENTS.md-aware runtimes read this file). See the Loom repository for the full guide (roles, labels, worktrees, configuration). When installed, Loom also writes a locally-substituted copy of the runtime-neutral guide to `.loom/AGENTS.md`.
<!-- END LOOM ORCHESTRATION (AGENTS) -->
<!-- BEGIN SQUAD -->
## Squad — cross-agent collaboration

This repo has [Squad](https://github.com/rjwalters/squad) installed. Claude,
Codex and opencode share the same room and MCP tools. Join the Squad room for attended sessions. If you are running unattended and have neither the Squad CLI on PATH nor a Squad MCP server available, continue your assigned task and post your handoff on the issue or PR.

Before touching shared state in a room session, read and follow the installed Squad skill,
including its room/identity conventions:

- Claude: `.claude/skills/squad/SKILL.md`
- Codex: `.agents/skills/squad/SKILL.md` (invoke `$squad` or ask naturally)
- opencode: `.agents/skills/squad/SKILL.md` (reads this `AGENTS.md`; use the
  `/squad-<workflow>` commands, and loop with `squad wait`)

Both expose join, wait, goals, card, fanout, steward, and clear workflows. Claude aliases are
`/squad:<workflow>`; legacy Codex prompts are `/squad-<workflow>`.

**Other channels:** for agents participating in the room, in a squad-enabled repo, coordinate in the room, including
between agents of the same harness: repo work discussion, ownership questions,
hand-offs and collisions. A harness's built-in cross-session messaging (for
example Claude's) is only for pointing or waking a session that is not watching
the room ("see the squad room, message N"), or for repos without squad. If you
receive such a message about this repo's work, record the substance in the room
with a short summary naming the sender, and post any answer you give there too.
Rooms persist for later joiners; direct messages do not.

For research work, discover and reuse the durable Science Card node IDs surfaced
by join/node list; the shared card workflow connects dependencies, committed
artifacts and revision-bound bank provenance.
<!-- END SQUAD -->
