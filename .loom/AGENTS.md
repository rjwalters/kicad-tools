<!-- GENERATED FILE — DO NOT EDIT DIRECTLY.
     Produced by defaults/scripts/generate-agents-md.sh from the
     `agents-md:include` marker ranges in defaults/.loom/CLAUDE.md. To change
     this file, edit those ranges in the source and re-run the generator;
     CI (scripts/check-agents-md-sync.sh) fails if this file is stale.
     Attribution: seeded by the gpeyton/loom fork's PR #8 (AGENTS.md codegen),
     ported to single-source generation for issue #4479 (epic #4167). -->

# Loom Orchestration - Repository Guide (AGENTS.md)

This repository uses **Loom** for AI-powered development orchestration.

**Installation Date**: 2026-08-10

> **Dual-runtime status**: this file is the runtime-neutral instruction anchor
> read by AGENTS.md-aware runtimes (OpenAI Codex CLI and others — see
> https://agents.md). Claude Code additionally reads the richer `.loom/CLAUDE.md`
> sibling as its primary format; the content below is a strict subset extracted
> from that same source, so the two files cannot diverge. Anything marked
> **Claude Code only today** (see the end of this file) is not yet portable to
> other runtimes — perform the equivalent `gh` / `git` / `./.loom/scripts/*`
> steps directly instead.

## Credential storage

Secrets must stay outside every repository and worktree, including ignored
`.env`, `.loom-local`, logs and artifacts. Use owner-only user credential files
or an OS credential store; reference them without copying values. Never print
secrets. `.gitignore` is insufficient. See [credential policy](docs/credential-storage.md).
Task credentials (cloud tokens, SSH keys) follow a reference-by-name convention:
look up `./.loom/credentials.md` (names only) before asking the operator for
anything but a name — see [`.loom/docs/credentials.md`](docs/credentials.md).

## What is Loom?

Loom is a CLI + daemon for AI-powered development orchestration. It coordinates
AI development workers using git worktrees and a forge (GitHub or Gitea) as the
coordination layer, via manual roles, continuous autonomous orchestration (the
Rust `loom-daemon` binary), and a local tmux agent pool.

**Loom Repository**: https://github.com/rjwalters/loom

> **Forge note**: The `gh` commands shown below are for GitHub. For Gitea
> repositories, Loom's scripts handle API calls internally — the label-based
> workflow is identical regardless of forge.

## Orchestration Architecture

Loom decomposes development into three coordination tiers, with the forge
(GitHub / Gitea) as the shared state.

| Tier | Entry point | Purpose | Mode |
|------|-------------|---------|------|
| Tier 3 | Human | Oversight — approve proposals, handle edge cases | Observer |
| Tier 2 | `loom-daemon` (MCP) + tmux agent pool | Multi-issue dispatch + scheduled support roles | Continuous |
| Tier 1 | `/loom:sweep <issue>` | Single-issue lifecycle (Curator → Merge) | Per-issue |
| Tier 0 | `/loom:builder`, `/loom:judge`, etc. | Task execution — single focused work units | Per-task |

## Label-Based Workflow

Agents coordinate through labels. See `.github/labels.yml` for full definitions.

**Issue Lifecycle**:
```
(created) → loom:triage → loom:curating → loom:curated → loom:issue → loom:building → (closed)
           ↑ filer        ↑ Curator        ↑ Curator      ↑ human     ↑ Builder
```

**PR Lifecycle**:
```
(created) → loom:review-requested → loom:pr → (merged)
           ↑ Builder                ↑ Judge    ↑ Champion or human
```

**Proposal Lifecycle**:
```
(created) → loom:architect/loom:hermit/loom:auditor → (evaluated) → loom:issue
           ↑ Architect/Hermit/Auditor                 ↑ Champion    ↑ Ready for Builder
```

**Epic Lifecycle**: `loom:epic` → phased `loom:architect` + `loom:epic-phase`
child issues.

**Escape-hatch / status labels**: `loom:blocked` (needs help),
`loom:operator-only` (human action outside automation: credentials, infra,
hardware; dispatch skips it), `loom:abort` (abort in-flight work, returns to
`loom:issue`). `loom:operator-priority`: the operator's star, land it first;
human-only, not a hold. Priority axis: `tier:goal-advancing` /
`tier:goal-supporting` / `tier:maintenance`.

### REST vs GraphQL for forge queries

When GitHub GraphQL is rate-limited, use the separate REST quota: read issues
with `gh api repos/:owner/:repo/issues/:number`; mutate via `--method PATCH`
or `POST`. Prefer this fallback over `gh issue list` / `gh issue view`.

### Issues Are Suggestions (Role Autonomy)

Filed issues are the *input queue*, not mandates. Curator, Builder, and Judge
may **close** or **rescope** an issue — with a stated rationale — when building
it is not the best outcome. Comment the rationale before closing; rescope
instead of closing when the core is worth keeping (drop back to
`loom:triage`/`loom:curated`, removing `loom:issue`, so it isn't re-dispatched
with a stale scope). Never close an issue that encodes a pending human
decision — route it to `loom:blocked` or `loom:operator-only` instead.

## Git Worktree Workflow

Loom uses git worktrees to isolate agent work. **Issue Worktrees**
(`.loom/worktrees/issue-N`) hold issue-specific work for Builder agents. The
guard-to-PR recipe lives in exactly one place — "Builder Workflow" below — so no
second copy can go missing its pre-claim guard.

- Always use `./.loom/scripts/worktree.sh <issue-number>` (writes a
  `.loom-managed` sentinel that authorizes cleanup). **Never run `git worktree`
  directly** — the helper prevents nested worktrees; use `./.loom/scripts/worktree.sh
  remove <issue-number>` to remove one worktree on demand, or `loom-clean` for
  the bulk stale-cleanup path.
- Loom-managed worktrees are auto-removed when their PR merges; user-provisioned
  worktrees are never touched — set `LOOM_PRESERVE_WORKTREE=1` to disable
  cleanup for a session.
- A live `.loom/locks/issue-<N>/owner.json` means a sweep owns issue N — do
  not claim it (`worktree.sh` enforces this).

### Merging PRs

**Never use `gh pr merge`** — always use `./.loom/scripts/merge-pr.sh <PR_NUMBER>`
instead (`--auto` to wait then merge, `--dry-run` to preview). `gh pr
merge` attempts a local checkout that fails when the PR branch is linked to a
worktree; the script merges via the forge API directly and handles worktree
cleanup automatically. A `PreToolUse` hook redirects `gh pr merge` calls to
this script.

### CI is dumb and reliable, on purpose

Prefer a slow correct job to a clever fast one. **Never cancel a distinct commit's
run once started**; the default branch supersedes only pending runs (no verdict).
Path-filtering is an optimisation, not a correctness tool. One
mechanism per behaviour: two that both cancel, skip or retry will surprise
someone. A check that *cannot run* must never look like one that passed. Rules +
the incidents behind them: [`.loom/docs/ci-principles.md`](docs/ci-principles.md).

## Claude Code only today

The coordination mechanics above (labels, worktrees, the sweep lifecycle,
`merge-pr.sh`) are runtime-neutral — any worker that can run `gh` and `git`
participates. These dispatch surfaces are Claude-Code-specific for now:

- **Slash commands** (`/loom:sweep`, `/loom:builder`, `/loom:judge`, …) are
  defined under `.claude/commands/loom/`. A non-Claude runtime cannot invoke
  them by name; it performs the equivalent steps directly (claim the issue,
  create the worktree, implement, open the PR).
- **MCP tools** (`mcp__loom__dispatch_sweep`, `mcp__loom__list_sweeps`, …) are
  reachable only through Claude Code's MCP integration.
- **`PreToolUse` guard hooks** and the **`.loom/tokens/` multi-account token
  pool** guard Claude tool calls and rotate Claude Code credentials — they do
  not apply to other runtimes.

For the full Claude-Code-oriented guide (daemon MCP surface, configuration,
token pool, troubleshooting) see `.loom/CLAUDE.md`.

---

**Generated by Loom Installation Process**
Last updated: 2026-08-10
