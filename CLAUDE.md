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

<!-- BEGIN REPO-SKILLS -->
This repository has [Repo Skills](https://github.com/rjwalters/repo) v0.11.1 installed —
general repository hygiene and environment commands invoked as `/repo:<command>`. Run
`/repo:help` for the command list, or see `.claude/skills/repo/SKILL.md` for the full
guide. Hygiene commands apply safe, reversible fixes by default and report each
change; run with `--ask` to review first, and `--prune` to allow irreversible
removals. Managed by `install.sh` — edit outside the markers only.
<!-- END REPO-SKILLS -->

## Releasing

Releases go through a **PR-based flow** — see [`RELEASING.md`](RELEASING.md) for
the canonical process. Never push the version-bump commit directly to `main`;
the version is bumped on a branch, merged via PR, and the `vX.Y.Z` tag is
created **only after** the bump PR merges (the tag must point at a commit that
is actually on `main`, never a pre-merge `release/vX.Y.Z` branch commit —
`main` squash-merges, so the commit that lands has a different SHA than the
one on the release branch).

<!-- BEGIN LOOM ORCHESTRATION -->
This repository uses [Loom](https://github.com/rjwalters/loom) for AI-powered development orchestration — see the Loom repository for the full guide (roles, labels, worktrees, configuration). When installed, Loom also writes a locally-substituted copy of that guide to `.loom/CLAUDE.md`.
<!-- END LOOM ORCHESTRATION -->
<!-- BEGIN SQUAD -->
## Squad — cross-agent collaboration

This repo has [Squad](https://github.com/rjwalters/squad) installed. Claude and
Codex share the same room and MCP tools. Before touching shared state, read
and follow the installed Squad skill, including its room/identity conventions:

- Claude: `.claude/skills/squad/SKILL.md`
- Codex: `.agents/skills/squad/SKILL.md` (invoke `$squad` or ask naturally)

Both expose join, goals, card, fanout, steward, and clear workflows. Claude aliases are
`/squad:<workflow>`; legacy Codex prompts are `/squad-<workflow>`.

For research work, discover and reuse the durable Science Card node IDs surfaced
by join/node list; the shared card workflow connects dependencies, committed
artifacts and revision-bound bank provenance.
<!-- END SQUAD -->
