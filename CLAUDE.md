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

<!-- BEGIN REPO-SKILLS -->
This repository has [Repo Skills](https://github.com/rjwalters/repo) v0.10.0 installed —
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

<!-- BEGIN LOOM ORCHESTRATION -->
This repository uses [Loom](https://github.com/rjwalters/loom) for AI-powered development orchestration — see the Loom repository for the full guide (roles, labels, worktrees, configuration). When installed, Loom also writes a locally-substituted copy of that guide to `.loom/CLAUDE.md`.
<!-- END LOOM ORCHESTRATION -->