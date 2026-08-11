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
Commands whose only action is consequential (`orphans`, `update-tools`, `deps`,
`followups`, `release`, `remote`, `sudo`) always confirm first by nature. The environment commands (`remote`) stand up
infrastructure only after showing exactly what they will create and what it
costs.

## Commands

| Command | What it does |
|---------|--------------|
| [[help]] | Explain the installed `/repo:*` commands — what each does, where to start |
| [[all]] | The whole hygiene pass in order — audit, scrub, docs, tidy, update-tools, reset — safe fixes by default, destructive steps gated |
| [[audit]] | Full sweep — runs all hygiene checks, produces a summary report |
| [[scrub]] | Public-surface scrub — scan code, history, issues, PRs (and forks) for sensitive identifiers, and report which findings can actually be removed. Report-only; severity gates verbosity so it stays quiet inside [[all]]. Its fork-network sweep is implemented in `scripts/repo/repo-scrub-forks.sh` (installed to `.claude/skills/repo/scripts/`) |
| [[reset]] | Back to baseline — review stale worktrees/branches/stashes, sync with remote, return to the default branch |
| [[handoff]] | Roll the session safely — file follow-ups, reset, check for a CLI update, write a handoff note the next session reads first |
| [[tidy]] | Tidy up — build artifacts, caches, temp files, empty dirs |
| [[release]] | Cut a release — pre-flight, semver decision, CHANGELOG, version bump, tag, GitHub Release. Supports per-project release policy via named phase-boundary seams in `.repo/release-policy.md` |
| [[host-optimize]] | Prepare a Mac (or Linux box) for heavy Loom/agent build use — audit Gatekeeper churn, backup-agent interference, build-tree bloat; apply safe fixes, gate consequential ones |
| [[remote]] | Launch a cloud dev session (GCP or AWS) with this repo ready to go, then open SSH. Its provisioning contract is implemented once in `scripts/repo/repo-remote.sh` (installed to `.claude/skills/repo/scripts/`); the interactive flow delegates to that script, which also serves as a headless `repo-remote up --yes --json` entry point for non-interactive callers (e.g. loom's `fleet add-worker`) |
| [[sudo]] | Opt-in passwordless-sudo setup for a dev machine — install a `visudo`-validated `/etc/sudoers.d` drop-in (blanket `ALL` or a scoped command list) so an agent over SSH isn't blocked on password prompts; always confirmed first, validated with rollback on failure |
| [[update-tools]] | Check installed tool packages (Loom, Anvil, …) against their sources and offer updates |
| [[deps]] | Third-party dependency currency — verify/scaffold Dependabot (config *and* the repo-level security flag as distinct items) and triage open Dependabot PRs, always confirmed first |
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
- Before turning a Mac into a heavy Loom/agent build host (`host-optimize`)
- To unblock an agent driving a dev box over SSH from `sudo` password prompts (`sudo`)
- Periodically, to keep installed tool packages current (`update-tools`) and
  third-party dependencies current — Dependabot setup and bot-PR triage (`deps`)
- Periodically (monthly) as general hygiene (`audit`)
- Before making a repo public, and periodically after — to check what the
  public surface actually exposes (`scrub`)
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
`.claude/settings.json`. This is the **canonical generic destructive-command
guard** (rjwalters/repo#30). It runs before every agent `Bash` command and:

- **Blocks** catastrophic operations outright: `rm -rf` of root / `$HOME` / a
  top-level system dir (with lexical `..`/`//` normalization so traversal
  can't smuggle one past), `rm` targets outside the repo/temp scope (see
  `rmScope` below), force-push to `main`/`master`, fork bombs, piping a
  download to a shell (`curl … | sh` — only when the pipe target *is* a
  shell), `gh repo delete`/`archive`, `docker system prune`, cloud destruction
  (`aws iam delete`, `aws s3 rb`, `aws cloudformation delete-stack`,
  `az … delete`, `gcloud … delete`), system-lifecycle commands
  (`halt`/`reboot`/`poweroff`/`shutdown`/`init 0|6`, command-word matched so
  prose never trips), and SQL DDL/DML (`DROP TABLE`, `TRUNCATE TABLE`,
  `DELETE FROM …` without a `WHERE`).
- **Asks** for confirmation on risky-but-legitimate ones: force ops
  (`git push --force` / `git reset --hard`, branch-aware via `forceScope`),
  `git clean -fd`, un-isolated `git read-tree`, mutating cloud verbs
  (`aws ec2 run|terminate|stop-…`, `aws s3 cp|rm|sync`, `docker rm|stop|…` —
  read-only `describe*`/`ls`/`get*` never prompt), `kubectl delete`,
  `gh release delete`, credential reads (`cat ~/.ssh/…`), etc.
- **Allows** everything else — scoped deletes like `rm -rf node_modules` or
  `/tmp` subpaths, and obviously read-only commands (`git status`, `ls`,
  `grep`, …) via a structural fast path that skips the pattern gauntlet.

**Deferral by other tooling is conditional, and as of 0.9.0 it holds**
(rjwalters/repo#168, #188). Loom wires its own
`.loom/hooks/guard-destructive.sh` as the registered hook; that script is a
dispatcher which `exec`s this canonical guard only when the copy it finds
passes both a version probe **and** a capability probe for Bash-tool write
confinement (`grep -q 'worktree-write-confinement'`). This guard implements
that category and emits the marker, so the dispatcher now prefers it.

The probe is a **single marker that switches which guard runs entirely**, not a
per-feature advertisement — so emitting it while behind on any other category
would silently downgrade protection fleet-wide. It was therefore gated on
behavioral parity with the vendored copy, established by diffing both guards'
deny/allow decisions across the pattern surface rather than by counting
functions. Two classes of gap were closed:

- **`git stash` had no coverage here at all.** `pop`/`drop`/`clear` were
  allowed silently while the vendored guard asked. `refs/stash` is a single
  stack shared across every linked worktree, so this is how one agent destroys
  another's WIP. Now gated by `guards.stashScope` / `REPO_GUARD_STASH_SCOPE`.
- **Four checks scanned the raw command** (`COMMAND_NO_COMMENT`, or `COMMAND`)
  instead of the literal-redacted `COMMAND_ASK_SCAN`: SQL DDL, force ops, cloud
  CLI, and `rm -rf` scope. That made this guard deny ordinary prose — filing an
  issue that quoted a destructive statement, or committing a note mentioning
  one. The vendored copy had always scanned the redacted copy.

One deliberate divergence remains: `aws iam delete-role` is a hard **deny**
here and an ask in the vendored copy, so the switch makes a Loom-managed repo
stricter on IAM deletion rather than weaker.

A second, narrower divergence (#195): the vendored guard's ASK-tier positional-
argument masking (`mask_ask_positional_args()`) carries one hardcoded allowlist
entry, `./.loom/scripts/check-duplicate.sh` — always masked, with no way to
extend or disable it per repo. This guard ports the same masking mechanism but
makes the allowlist a `guards.positionalMaskAllowlist` config array instead
(empty/absent by default, so it is a no-op on every repo that hasn't opted
in — see "Configuring per repo" below). It does **not** carry the vendored
copy's hardcoded entry, since `check-duplicate.sh` is a Loom-specific script
this repo doesn't ship: a repo that relies on the vendored copy's built-in
masking for that script and switches to this guard must add it explicitly via
config, or the switch is (by default) stricter for that one command — more
asks, never fewer. `grep`/`egrep`/`fgrep`/`rg` **and** `cp`/`mv`/`tee`/`sed`
can never be added to the allowlist here regardless of config: the working
copy this masking narrows (`COMMAND_ASK_SCAN`) also feeds two **deny**-tier
scans — the SQL DDL/DML check (which reads a `grep '<pattern>' file`
invocation's own quoted pattern, the vendored copy's reason for excluding the
grep family) and the #4178 Bash-tool write-confinement check (which extracts
`cp`/`mv`/`tee`/`sed` write targets from that same text). Masking either
scan's subject would silently downgrade a hard deny to an allow, so the
exclusion set is hardcoded and not operator-overridable. **Only allowlist a
command whose positional arguments are inert text it merely reads** — never
one that acts on them (writes them as paths, executes them as statements).
See `positional_mask_cmdre()`'s consumer audit table in
`guard-destructive.sh`. Loom's vendored copy can adopt the same configurable
mechanism to drop its hardcoded path (rjwalters/loom#5660).

Precision features ported from Loom's guard: quote-aware command segmentation
(a `|` inside quotes is not a pipe), literal-text redaction (a dangerous phrase
quoted in `--body`/`-m`/`--title`/`--notes`/`--comment` values doesn't trip the
scan — command substitution inside such a value still does), comment stripping,
and an opt-in JSONL decision-telemetry log for measuring guard friction.

The hook only fires when Claude Code runs with `--dangerously-skip-permissions`
(it is skipped entirely under `--permission-mode bypassPermissions`).

### Configuring per repo

All toggles resolve: `REPO_*` env var (wins) → legacy `LOOM_*` env var →
`guards.<key>` in `.claude/skills/repo/config.json` (wins) → legacy
`.loom/config.json` → default. On/off values: `0`/`false`/`no` and
`1`/`true`/`yes`.

| Toggle | Env var (wins) | Legacy env var | Config key | Default |
|--------|----------------|----------------|------------|---------|
| Read-only fast path | `REPO_GUARD_READONLY_FASTPATH` | `LOOM_GUARD_READONLY_FASTPATH` | `guards.readOnlyFastPath` (+ extend-only `guards.readOnlyFastPathExtra`) | on |
| ASK-tier positional-arg masking allowlist | — (config-only) | — | `guards.positionalMaskAllowlist` (array of command names; grep/egrep/fgrep/rg and cp/mv/tee/sed can never be added — they are the subjects of deny-tier scans) | `[]` (no-op) |
| SQL DDL/DML | `REPO_GUARD_SQL` | `LOOM_GUARD_SQL` | `guards.sqlDdl` | on |
| Cloud CLI (mutating-verb asks + `az`/`gcloud` delete denies) | `REPO_GUARD_CLOUD` | `LOOM_GUARD_CLOUD` | `guards.cloudCli` | on |
| Reversible-GitHub asks (`gh pr/issue close`, `gh label delete`) | `REPO_GUARD_REVERSIBLE_GH` | `LOOM_GUARD_REVERSIBLE_GH` | `guards.reversibleGh` | **off** (opt-in) |
| rm scope (`repo` denies outside-repo/temp targets; `off`/`permissive` restores legacy) | `REPO_RM_SCOPE` | `LOOM_RM_SCOPE` | `guards.rmScope` | `repo` |
| Force-op branch scope (`all` / `protected` / `off`) | `REPO_FORCE_SCOPE` | `LOOM_FORCE_SCOPE` | `guards.forceScope` | `all` |
| Decision telemetry log | `REPO_GUARD_DECISION_LOG` (path: `REPO_GUARD_DECISION_LOG_FILE`) | `LOOM_GUARD_DECISION_LOG` (`…_FILE`) | `guards.decisionLog` | **off** (opt-in) |

For the on-by-default guards only an explicit `false` disables — a missing key
or malformed config keeps the guard on; the opt-in toggles are the inverse.

```json
// .claude/skills/repo/config.json
{ "guards": { "sqlDdl": false, "cloudCli": true, "forceScope": "protected" } }
```

The full stable interface (input/output contract, exit semantics, every env
name) is documented in the hook's own header — downstream tools (e.g. Loom's
installer) gate on it via this repo's release version.

## Handoff-note hook (SessionStart)

The installer wires a second hook, `session-start-handoff.sh`, as two
`SessionStart` entries — one matching `startup`, one matching `resume`. When
[[handoff]] has left a note at `.claude/handoff.md`, the hook emits it as
session context via `hookSpecificOutput.additionalContext`: the note's path,
its age, a staleness warning once the note passes seven days, and the note
itself. When the note is at or under a size cap (`MAX_BODY_BYTES`, 10 KB) the
**full body is inlined**; above the cap it falls back to an outline built from
the note's `#`/`##` headers plus an explicit oversize warning. Inlining the body
is deliberate (issue #33): the load-bearing content lives in the body, and a
headers-only summary conveys shape but nothing actionable.

Behavioral contract:

- **Read-only.** It never writes, deletes, or modifies the note. Absorbing the
  note and deleting it is [[handoff]]'s own one-shot contract.
- **Silent when there is nothing to say.** No note, or an unreadable one, means
  no output and exit 0.
- **Fails open.** Malformed stdin, a missing `cwd`, or any internal error exits
  0 with no output. A hook fault must never block session start.
- **Skips `/clear`.** `clear` is not a process relaunch, so re-emitting the
  banner there would be noise.

There are no configuration toggles — the hook's behavior is fixed. To disable
it, remove its `SessionStart` entries from `.claude/settings.json` (or run
`uninstall.sh`, which removes only the entries it owns).

## Refreshing this install (`resync-installed.sh`)

Everything above is a **copy** made at install time, so a fix merged upstream
does not reach this repo on its own. `.claude/skills/repo/scripts/resync-installed.sh`
refreshes the copied surfaces from the source clone recorded in the
machine-local sidecar:

```bash
.claude/skills/repo/scripts/resync-installed.sh --dry-run   # report drift, write nothing
.claude/skills/repo/scripts/resync-installed.sh             # apply
```

It is idempotent, reports per-file created/updated/unchanged/skipped, skips
symlinked (`--dev`) destinations, and **never removes a file** — anything with
no source counterpart is named and left alone. Exit status: `0` in sync, `2`
from `--dry-run` when drift was found, `1` on error. `--quiet` reduces it to a
one-line summary; `--source` / `--target` override the resolved paths.

Hook wiring in `.claude/settings.json`, the `CLAUDE.md` block, and `.gitignore`
belong to `install.sh`, not to a refresh — re-run the installer if those need
updating. This split is requirement **C7** of the normative
[tool-package installer contract](https://github.com/rjwalters/repo/blob/main/INSTALLER-CONTRACT.md),
which [[update-tools]] follows for every tool in the family.
