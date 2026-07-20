---
name: "Repo Skills"
description: "General repository hygiene and environment tools — audits, cleanup, branch/worktree pruning, link checking, and cloud dev sessions"
domain: repo
type: skill
user-invocable: false
---

# Repo Skills

General-purpose tools for keeping a git repository healthy and productive. The
hygiene commands **apply their safe, reversible fixes by default** and report
each change; add `--ask` to review findings and confirm first. Anything
irreversible — deleting a branch, worktree, stash, or untracked file — is never
automatic: it takes an explicit opt-in and passes a permanent-loss check.
Commands whose only action is consequential (`orphans`, `update-tools`,
`followups`, `release`, `remote`) always confirm first by nature. The environment commands (`remote`) stand up
infrastructure only after showing exactly what they will create and what it
costs.

## Commands

| Command | What it does |
|---------|--------------|
| [[help]] | Explain the installed `/repo:*` commands — what each does, where to start |
| [[all]] | The whole hygiene pass in order — audit, docs, tidy, update-tools, reset — safe fixes by default, destructive steps gated |
| [[audit]] | Full sweep — runs all hygiene checks, produces a summary report |
| [[reset]] | Back to baseline — review stale worktrees/branches/stashes, sync with remote, return to the default branch |
| [[tidy]] | Tidy up — build artifacts, caches, temp files, empty dirs |
| [[release]] | Cut a release — pre-flight, semver decision, CHANGELOG, version bump, tag, GitHub Release |
| [[remote]] | Launch a cloud dev session (GCP or AWS) with this repo ready to go, then open SSH |
| [[update-tools]] | Check installed tool packages (Loom, Anvil, …) against their sources and offer updates |
| [[followups]] | Capture follow-on work from this session and file it as issues — here or in upstream tool repos, always confirmed first |
| [[branches]] | Branch & worktree hygiene — merged PRs, orphaned branches, stale worktrees |
| [[gitignore]] | Gitignore hygiene — over-ignored files, under-ignored build artifacts |
| [[docs]] | Documentation health — content accuracy, README structure, cross-references (canonical docs command) |
| [[links]] | Internal cross-references — markdown links, CLAUDE.md paths, skill graph |
| [[orphans]] | Files with no references — dead scripts, stale data, outputs without sources |
| [[readme]] | README accuracy vs actual directory contents |

## When to Use

- After finishing a task, to get back to a known-good state (`reset`)
- After a large refactor, consolidation, or import (`audit`, `docs`)
- When the working tree feels messy (`tidy`, `orphans`)
- When `git branch` output has grown unmanageable (`branches`)
- When local hardware isn't enough or you need a clean Linux box (`remote`)
- Periodically, to keep installed tool packages current (`update-tools`)
- Periodically (monthly) as general hygiene (`audit`)
- Before a demo, handoff, or onboarding (clean up before they arrive)

## Principles

1. **Apply safe fixes, gate destructive ones.** Reversible fixes (doc/link/
   gitignore edits, regenerable clutter) apply by default and are reported as
   they're made; `--ask` restores review-and-confirm. Irreversible actions
   — deleting branches, worktrees, stashes, untracked files, or creating
   infrastructure — are never automatic: show the plan (and cost, for cloud
   resources), run the permanent-loss check, and act only on explicit opt-in.
2. **Scope matters.** Most hygiene commands accept an optional path argument to
   limit scope (e.g., `/repo:readme docs/`). Without it, they scan the full repo.
3. **General by design.** These commands make no assumptions about org,
   project structure, or infrastructure. Anything repo-specific is read from
   the consumer repo's own files (CLAUDE.md conventions, `.env`),
   never hardcoded.
4. **Don't be noisy.** Only flag things that are actually wrong or confusing.
   A missing README in a tiny utility directory isn't worth flagging.

## Destructive-command guard (PreToolUse hook)

Installing Repo Skills also wires a **PreToolUse safety hook** —
`.claude/skills/repo/hooks/guard-destructive.sh` — into the consumer repo's
`.claude/settings.json`. It runs before every agent `Bash` command and:

- **Blocks** catastrophic operations outright: `rm -rf` of root / `$HOME` / a
  top-level system dir, force-push to `main`/`master`, fork bombs,
  `curl … | sh`, `gh repo delete`/`archive`, `docker system prune`, cloud
  destruction (`aws iam delete`, `aws s3 rb`, `aws cloudformation delete-stack`,
  `az … delete`, `gcloud … delete`, `aws ec2 terminate`), system-lifecycle
  commands (`halt`/`reboot`/`poweroff`/`shutdown`/`init 0|6`), and SQL DDL/DML
  (`DROP TABLE`, `TRUNCATE TABLE`, `DELETE FROM …` without a `WHERE`).
- **Asks** for confirmation on reversible-but-risky ones: `git reset --hard`,
  `git clean -fd`, `git push --force` (non-main), `kubectl delete`, `docker rm`,
  `gh pr/issue close`, credential reads (`cat ~/.ssh/…`), etc.
- **Allows** everything else — including scoped deletes like `rm -rf /tmp/foo`
  and `rm -rf node_modules`.

The hook only fires when Claude Code runs with `--dangerously-skip-permissions`
(it is skipped entirely under `--permission-mode bypassPermissions`).

### Opting out per repo

Two guard categories can be turned off for repos where they are a category
error (a database engine, or a repo whose job is managing cloud/containers).
Resolution order, highest precedence first:

| Category | Env var (wins) | Legacy env var | Config key |
|----------|----------------|----------------|------------|
| SQL DDL/DML | `REPO_GUARD_SQL` | `LOOM_GUARD_SQL` | `guards.sqlDdl` |
| Cloud CLI | `REPO_GUARD_CLOUD` | `LOOM_GUARD_CLOUD` | `guards.cloudCli` |

Env values `0`/`false`/`no` disable; `1`/`true`/`yes` force on. The config key
is read from `.claude/skills/repo/config.json` (Repo Skills' own location),
falling back to the legacy `.loom/config.json` for repos migrating off Loom's
guard. Only an explicit `false` disables — a missing key keeps the guard on.

```json
// .claude/skills/repo/config.json
{ "guards": { "sqlDdl": false, "cloudCli": true } }
```
