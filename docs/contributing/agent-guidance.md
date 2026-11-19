# Contributor guidance (single source)

This file is the **single source of truth** for kicad-tools' project-specific
contributor guidance (issue #5964, Epic #5952). Everything below the
`kct:guidance-body` marker is rendered verbatim into a managed block in both
agent guide files at the repo root:

- `CLAUDE.md`, read by Claude Code;
- `AGENTS.md`, read by Codex CLI, opencode and other AGENTS.md-aware runtimes.

The block sits between `<!-- BEGIN KCT CONTRIBUTOR GUIDANCE -->` and
`<!-- END KCT CONTRIBUTOR GUIDANCE -->`. Never hand-edit it there; edit this
file, then re-render:

```bash
uv run python scripts/render_agent_guidance.py --write   # rewrite both blocks
uv run python scripts/render_agent_guidance.py           # check: exit 1 on drift
uv run python scripts/render_agent_guidance.py --stdout  # print the rendered block
```

`tests/test_agent_guidance_render.py` runs the check, so a hand-edit to either
rendered block, or an edit here without re-rendering, fails CI with the fix
command in the message. The other managed blocks in those files (REPO-SKILLS,
LOOM ORCHESTRATION, SQUAD) belong to their installers; the renderer never
touches them.

Writing rules for the body:

- It is rendered into files at the repo root, so relative links and paths are
  **repo-root relative** (`RELEASING.md`, not `../../RELEASING.md`).
  Those links therefore do not resolve when this source file is viewed on
  GitHub; follow them from `CLAUDE.md` or `AGENTS.md` instead.
- It must not contain a `<!-- BEGIN ` or `<!-- END ` marker line; the
  renderer refuses such a body rather than duplicate a managed-block marker.
- Keep it harness-neutral: the #5954 lint in
  `tests/test_agent_surface_neutrality.py` (no harness tool names, no
  harness-specific guide or directory pointers) runs over it. A genuinely
  harness-specific paragraph must be a marked note (`> **Claude Code only** ...`)
  that names a fallback for other harnesses.
- Use `##` headings; the body is one section among others in each file.

<!-- kct:guidance-body -->

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
