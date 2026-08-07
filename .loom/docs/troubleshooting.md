# Loom Troubleshooting Guide

## Common Issues

### Hooks not firing (`guard-destructive.sh` not blocking commands)

**Symptom**: Commands that should be blocked or confirmed by `guard-destructive.sh` (e.g., `git reset --hard`, `gh issue close`) are executing without any prompt or denial.

**Root cause**: Claude Code's `--permission-mode bypassPermissions` flag skips ALL PreToolUse hooks entirely. If Claude Code is invoked with this flag, hooks never run — not even safety hooks like `guard-destructive.sh`.

**How to diagnose**:
```bash
# Check if you have a shell alias that sets bypassPermissions
alias claude 2>/dev/null || echo "no alias"

# Check if Loom scripts are using the correct flag
grep -r 'permission-mode' .loom/scripts/ .loom/roles/ 2>/dev/null
```

**The two flags behave differently**:

| Flag | Hooks fire? | Use case |
|------|-------------|----------|
| `--dangerously-skip-permissions` | ✅ YES | Loom automation (agents use this) |
| `--permission-mode bypassPermissions` | ❌ NO | Fully bypasses all permission checks AND hooks |

**Fix**: If you have a shell alias using `--permission-mode bypassPermissions`, change it to use `--dangerously-skip-permissions` instead:

```bash
# WRONG - hooks silently disabled:
alias claude="claude --permission-mode bypassPermissions"

# CORRECT - hooks still fire:
alias claude="claude --dangerously-skip-permissions"
```

Note: `--dangerously-skip-permissions` still skips interactive permission prompts (so agents can run non-interactively), but hooks are executed. This is the intended mode for Loom agents.

**Verify the fix**: After updating your alias, restart your shell and confirm hooks fire by checking the hook error log:
```bash
# Hook invocations log errors here:
cat .loom/logs/hook-errors.log
```

If the log is absent or empty and hooks aren't blocking, confirm Claude Code is invoked with `--dangerously-skip-permissions` (not `bypassPermissions`).

### Cleaning Up Stale Worktrees and Branches

Use the `loom-clean` command to restore your repository to a clean state:

```bash
# Interactive mode - prompts for confirmation (default)
loom-clean

# Preview mode - shows what would be cleaned without making changes
loom-clean --dry-run

# Non-interactive mode - auto-confirms all prompts (for CI/automation)
loom-clean --force

# Deep clean - also removes build artifacts (target/, node_modules/)
loom-clean --deep

# Combine flags
loom-clean --deep --force  # Non-interactive deep clean
loom-clean --deep --dry-run  # Preview deep clean
```

`loom-clean` is a thin shim for `loom-daemon clean` and needs a `loom-daemon`
binary built at or after commit `dba33666` (PR #4301) — see [fail on a stale
binary](#loom-clean--loom-cleanup--loom-recover-orphans-fail-on-a-stale-binary-4384)
if it errors out instead of running.

**What loom-clean does**:
- Removes worktrees for closed GitHub issues (prompts per worktree in interactive mode)
- Deletes local feature branches for closed issues
- Cleans up Loom tmux sessions
- (Optional with `--deep`) Removes `target/` and `node_modules/` directories

**IMPORTANT**: For **CI pipelines and automation**, always use `--force` flag to prevent hanging on prompts:
```bash
loom-clean --force  # Non-interactive, safe for automation
```

**What `--safe` actually narrows (#4890)**: `--safe` is documented as
"merged-PR-only mode", but that promise only has meaning for artifacts that
*are* an artifact of a merged PR — worktrees (gated on issue-closed +
PR-merged + grace period) and branches. A tmux session has no PR association
at all, so `loom-clean --safe` skips tmux cleanup **entirely** rather than
silently killing a live session (a Manual-Orchestration-Mode terminal on
Loom's isolated `-L loom` tmux socket does not show up in a plain `tmux ls`,
so an operator has no other way to notice). Use `loom-clean --tmux-only`
(optionally with `--force`) outside `--safe` to clean tmux sessions
explicitly. Even then, a session with an attached client (someone is actively
looking at it) is preserved unless `--force` is passed.

**Backlog of pre-existing `[gone]` local branches (#4100)**: `merge-pr.sh` deletes
the local feature branch for every PR it merges as of #4100, but repos that ran
Loom before that fix accumulated one orphaned local branch per merged issue —
each pointing at a remote ref the merge already deleted server-side, so `git
branch -vv` shows them as `[gone]`. Reap the existing backlog with:

```bash
git fetch --prune   # drop stale remote-tracking refs first
loom-clean --force  # clean_branches() deletes local branches whose remote is gone
```

`loom-clean`'s `clean_branches()` already handles this in two passes: a
pattern-agnostic pass that deletes any local branch (other than the
default/current/other-worktree-checked-out ones) whose `origin/<branch>` no
longer exists, plus an issue-state pass that probes `feature/issue-*` branches
still tracking a remote against the forge and deletes them once the issue is
closed. No separate one-off backlog tool is needed.

**Manual cleanup** (if needed, but use with caution):

**WARNING**: Running `git worktree remove` while your shell is in the worktree directory will corrupt your shell state. Always ensure you've navigated out of the worktree first, or use `loom-clean` which handles this safely.

```bash
# First, ensure you're NOT in the worktree you're removing
cd /path/to/main/repo

# List worktrees
git worktree list

# Remove specific stale worktree (only after navigating out!)
git worktree remove .loom/worktrees/issue-42 --force

# Prune orphaned worktrees
git worktree prune
```

### `loom-clean` / `loom-cleanup` / `loom-recover-orphans` fail on a stale binary (#4384)

**Symptom**: one of the three commands below fails outright instead of doing
anything — either with a `No module named loom_tools.clean` traceback (an
old pip-installed console script), or with an explicit `ERROR clean.sh: … does
not support the 'clean' subcommand (stale build)` from the wrapper.

**Root cause**: all three are now thin front-ends for native `loom-daemon`
subcommands (#4272 / PR #4301, commit `dba33666`):

| Command / wrapper | Native subcommand |
|---|---|
| `loom-clean`, `./.loom/scripts/clean.sh` | `loom-daemon clean` |
| `./.loom/scripts/cleanup.sh` | `loom-daemon cleanup logs` |
| `loom-recover-orphans`, `./.loom/scripts/recover-orphaned-shepherds.sh` | `loom-daemon recover-orphans` |

The same commit deleted the Python implementations (`loom_tools/clean.py`,
`cleanup.py`, `orphan_recovery.py`) and their console-script entry points, so
**there is no fallback**: a `loom-daemon` binary built before `dba33666` leaves
no working path at all. The wrappers capability-probe the binary and now fail
loudly with the remedy rather than degrading into a traceback.

**Check whether your binary is the problem**:

```bash
loom-daemon --version          # note the commit
loom-daemon clean --help       # "error: unrecognized subcommand 'clean'" => stale
```

**Remedy — rebuild or update `loom-daemon`, then retry**:

```bash
cargo build --release -p loom-daemon        # source checkout
./.loom/scripts/cli/loom-daemon-update.sh   # installed host (self-update)
```

Use `loom-daemon clean --force` / `loom-daemon recover-orphans --recover`
directly in the meantime — the native subcommands take the same flags.

**If a stale pip-era shim is shadowing the current one**: the installer writes
`loom-clean` / `loom-recover-orphans` shims next to the provisioned
`loom-daemon` (usually `~/.local/bin`). A pre-#4301 pip/homebrew install can
leave `from loom_tools.clean import main` shims earlier on `PATH` that will
never work again. Confirm with `command -v loom-clean` and remove them (e.g.
`pip uninstall loom-tools`, or delete the stale shim) so the daemon-backed one
resolves.

### Corrupted local git identity (`...github.comecho`, "cannot overwrite multiple values") (#4369)

**Symptom**: `git config user.email <value>` fails with `error: cannot
overwrite multiple values`, or a commit/merge ships with a garbled author
email like `loom-reviewer@users.noreply.github.comecho`.

**Root cause**: a now-deleted Tauri-era code path once pushed two shell lines
into an agent's terminal to set a per-role git identity — `git config
user.email "<email>"` immediately followed by an `echo "✓ Git identity
configured..."` line. A lost newline between the two glued the `echo`
token onto the email, corrupting it, and because `git config user.email
<v>` *replaces* rather than appends, repeated writes/`--add` improvisation
could also stack multiple values for the same key. That code was removed in
`d61acab0` (#3353) — **nothing on `main` writes `user.email`/`user.name`
anymore** — but the corrupted/stacked values persist as residue in any
checkout that predates the removal, and worktrees inherit them from the
parent repo's shared `.git/config`.

**Detect it**:

```bash
./.loom/scripts/check-git-identity.sh
```

This reads LOCAL (repo + per-worktree) scope only — never your global
identity — so a normal setup with no repo-local override never
false-positives. It exits `0` when clean, `1` (warning) when it finds
plain stacked values with no corruption, and `3` (hard fail) when it finds
the glued-token corruption pattern. `./.loom/scripts/worktree.sh` runs this
check automatically at worktree creation: it hard-fails on the corruption
pattern (a garbled commit author would otherwise ship silently — see PR
#4303) and warns-but-proceeds on a plain multi-value.

**Fix it** — preferred, falls back to your global identity (Loom no longer
sets a per-role local identity, so the local values are pure residue):

```bash
git config --unset-all user.email && git config --unset-all user.name
```

**Alternative** — keep one specific value (e.g. no global identity is
configured on this host):

```bash
git config --replace-all user.email <value-to-keep>
git config --replace-all user.name  <value-to-keep>
```

After either fix, re-run `./.loom/scripts/check-git-identity.sh` to confirm
it now reports clean, and verify a new commit picks up the intended author
with `git commit --allow-empty -m test && git log -1 --format='%an <%ae>'`
(then drop the test commit).

### Labels out of sync

```bash
# Re-sync labels from configuration
gh label sync --file .github/labels.yml
```

Label sync is a manual/install-time step (`./scripts/install/sync-labels.sh .`),
not something CI re-applies when `.github/labels.yml` changes. If a label is
defined in `labels.yml` but missing from the live repo, applying it fails with
`failed to update 1 issue` (the standard `gh` error for "label does not exist").
Run the sync script — or create the one label directly — to reconcile:

```bash
gh label list --search operator                      # empty => not provisioned
gh label create "loom:operator-only" --color F97316 \
  --description "Requires human action or ruling outside automation (creds, infra, hardware); sweep skips"
```

**GitHub caps label descriptions at 100 characters.** A `labels.yml` entry with a
longer description fails to sync (HTTP 422 "description is too long") and the label
silently never gets created. Keep descriptions at or under 100 chars.

#### `loom:blocked` vs `loom:operator-only`

These two status labels look similar but mean different things to the automation:

- **`loom:blocked`** — work is *automatable* but currently waiting on a dependency
  (another issue, an unmerged PR, missing context). The intent is "unblock it, then
  a Builder can proceed."
- **`loom:operator-only`** — work requires a *human to act or rule outside
  automation entirely* (rotating credentials, infra changes, hardware access,
  manual deploys — or an owner-gated decision: an issue the code owner filed as a
  TODO on owner-tracked code, where the design direction is the owner's call).
  Sweep skips these in pre-flight rather than attempting them; a human must
  do the work off-automation before the issue can proceed.

Reaching for `loom:blocked` when you mean `loom:operator-only` conflates "waiting on
a dependency" with "needs a human action," which muddies the daemon/sweep skip
semantics. Use `loom:operator-only` for the human-must-act-off-automation case.

### An operator edit to `.loom/config.json` disappeared (#4641)

`.loom/config.json` has exactly one production writer: `loom-daemon init` (run by
`install.sh` reinstalls and by the `fleet add-worker` provisioning steps).
`resync-installed.sh` and `loom-daemon calibrate --write` never touch it.

`init`'s merge is **existing-wins** — consumer keys, including ones absent from the
shipped template such as `autonomous.workFinder.maxConcurrent`, survive — so a
tuned value vanishing means one of the other branches fired. Every `init` call now
emits one greppable line naming the branch it took:

```bash
grep 'init: config.json:' ~/.loom/daemon.log
```

| Branch | Level | Meaning |
|--------|-------|---------|
| `fresh-write` | `info` | No config existed; the template was written verbatim. |
| `merge-preserved` | `info` | Existing values kept. Carries a per-key diff (`+ added`, `~ changed`, `- dropped`) or "no effective config change". |
| `template-invalid-skip` | `warn` | The shipped `defaults/config.json` is unparseable; your file was left untouched (and got no template updates). |
| `invalid-JSON-fallback-overwrite` | `warn` | **Your config was replaced by the template.** The line names the discarded keys. |

A `~` or `-` entry on `merge-preserved` is unexpected under existing-wins semantics
and is worth investigating.

The fallback branch only fires when the file on disk does not parse as a JSON
object (a torn write from a concurrent writer, a hand-edit with a syntax error, or
a top-level array). Before overwriting, `init` copies the unparseable bytes to
`.loom/config.json.bak` — recover your tuned keys from there:

```bash
cat .loom/config.json.bak
```

`.loom/*.bak` is git-ignored, and `fleet add-worker` no longer re-runs
`loom-daemon init` against a workspace that already has a `.loom/config.json`, so
repeat provisioning passes cannot re-enter this path on a tuned host.

### `install.sh` refuses to run: "Another Loom install is already running" (#4928)

`install.sh`'s `--quick` / `--clean` paths take a per-target lock at
`<target>/.loom/.install.lock` before any destructive phase, because two
installers racing over one target interleave one run's uninstall (which stages
Loom file deletions and strips the Loom sections out of `CLAUDE.md` /
`.gitignore` **in place**) with the other's copy phase. The message names the
owning PID, host, and phase:

```bash
cat <target>/.loom/.install.lock   # pid / host / started / phase
```

- **The PID is alive** — a real install is in flight (a `cargo build --release`
  can run for minutes; it emits a progress line every 15s). Wait for it.
- **The PID is gone** — the next installer reclaims the lock automatically; you
  should never need to delete it. If you do (e.g. a lock written by another
  host, which cannot be liveness-probed and is only reclaimed after
  `LOOM_INSTALL_LOCK_MAX_AGE`, default 6h), `rm -f <target>/.loom/.install.lock`.

If the lock's `phase` is `uninstalling` / `installing` / `restoring`, that run
died **inside the destructive window** and the target may be partially
uninstalled. The next installer prints the recovery commands; the short form is:

```bash
git -C <target> status --short
git -C <target> restore --staged --worktree -- .loom .claude CLAUDE.md .gitignore
git -C <target> stash list | grep loom-install   # changes the installer stashed, if any
```

### Daemon won't start

```bash
# Check daemon logs
tail -f ~/.loom/daemon.log
```

### Claude Code not found

```bash
# Ensure Claude Code CLI is in PATH
which claude

# Install if missing (see Claude Code documentation)
```

### `loom-daemon: command not found` over plain ssh (#5393)

```
$ ssh loom-worker-2 'loom-daemon workspace list'
bash: line 1: loom-daemon: command not found
```

`loom-daemon` is installed at `~/.local/bin/loom-daemon`, which is added to PATH
by your **login shell's rc file**. `ssh host <cmd>` runs a *non-login,
non-interactive* shell that never sources that rc file, so `~/.local/bin` is not
on PATH and the bare name does not resolve. (The same mechanism produces the
false "missing dependency" from `install.sh` — see [`install.sh` reports a
dependency that is installed](#installsh-reports-a-dependency-that-is-installed-5393)
below.)

Three supported ways to drive `loom-daemon` over ssh, in order of preference:

1. **Source the login profile** so PATH is populated exactly as it is
   interactively:

   ```bash
   ssh loom-worker-2 'bash -lc "loom-daemon workspace list"'
   ```

2. **Call the fixed install location** directly — the machine-level install path
   is stable, so no PATH is needed:

   ```bash
   ssh loom-worker-2 '~/.local/bin/loom-daemon workspace list'
   ```

3. **Let Loom's own scripts resolve it** — every in-tree caller sources
   `defaults/scripts/lib/locate-daemon-bin.sh` (`loom_locate_daemon_bin`), which
   already probes `$LOOM_DAEMON_BIN` → PATH → `${LOOM_DAEMON_BIN_DIR:-$HOME/.local/bin}`
   → repo-local build output (#4875). Point new fleet automation at that helper
   rather than reimplementing per-caller path probing.

### `install.sh` reports a dependency that is installed (#5393)

```
$ ssh loom-worker-1 'cd ~/GitHub/loom && ./install.sh --quick -y ~/GitHub/repo'
✗ Error: Missing required dependencies: pnpm cargo -- cannot continue ...
```

Same root cause as the daemon case above: over a non-login ssh shell, PATH lacks
the per-user install roots (`~/.cargo/bin`, `~/.local/bin`, `/opt/homebrew/bin`,
…), so tools that are installed and runnable look absent. `install.sh` now
probes those roots directly: a tool found there is used (its directory is added
to PATH for the rest of the install) and reported with a `not on this shell's
PATH` warning rather than as missing. Only tools that are absent from **every**
probed root are treated as genuinely missing — a distinction that matters
because the two need different fixes (install the tool vs. fix PATH). If you
prefer to fix PATH once up front, run the whole install under a login shell:

```bash
ssh loom-worker-1 'bash -lc "cd ~/GitHub/loom && ./install.sh --quick -y ~/GitHub/repo"'
```

### Sweep output invisible when invoked with `2>&1`

When `claude -p "/loom:sweep N"` is run with `2>&1` redirection (e.g., from Claude Code's Bash tool for long-running processes), output may be silently dropped. This is because the Bash tool's capture buffer can be exhausted by a long-running child process when both stdout and stderr are forced through the same pipe.

**Workaround** — use a file redirect:

```bash
# Redirect to file, then cat the result
claude -p "/loom:sweep 123" --dangerously-skip-permissions > /tmp/sweep-123.log 2>&1
cat /tmp/sweep-123.log
```

**Built-in log file** — when a sweep child runs, it automatically tees all output to `.loom/logs/sweep-issue-N.log`. If output is invisible in your terminal, check this log file:

```bash
cat .loom/logs/sweep-issue-123.log
# or follow in real time:
tail -f .loom/logs/sweep-issue-123.log
```

### API Error: 400 due to tool use concurrency issues

This error occurs when Claude Code's parallel tool execution causes malformed API message structures. See the dedicated guide: [Tool Use Concurrency Errors](./tool-use-concurrency-errors.md)

**Quick recovery**:
```bash
# In Claude Code, run:
/rewind
```

**Prevention** - Add to `~/.claude/CLAUDE.md`:
```markdown
# Force Sequential Tool Execution
Execute tools sequentially, never in parallel.
Process one tool call at a time.
Wait for tool_result before initiating next tool execution.
```

**Common triggers**:
- Multiple parallel file operations (Read, Write, Edit)
- Using print mode (`-p` flag) instead of interactive mode
- PostToolUse hooks that interfere with message structure
- Editing files while they're open in an IDE

### Orphaned issues stuck in loom:building state

When an agent crashes or is cancelled while building, issues can get stuck in `loom:building` state without a PR. The shell script that historically handled this was deleted in `b811fca8` (#3433, "delete shepherd brain + /shepherd skill + milestone writers") during the v0.10.0 shepherd deprecation. Its successor, `loom-recover-orphans`, detects and recovers these:

```bash
# Check for stale building issues (dry run)
loom-recover-orphans

# Show detailed progress
loom-recover-orphans --verbose

# Auto-recover stale issues (resets to loom:issue)
loom-recover-orphans --recover

# JSON output for automation
loom-recover-orphans --json
```

`loom-recover-orphans` is a thin shim for `loom-daemon recover-orphans` — if it
fails with `No module named loom_tools.orphan_recovery` or a "stale build"
error, see [`loom-clean` / `loom-cleanup` / `loom-recover-orphans` fail on a
stale binary](#loom-clean--loom-cleanup--loom-recover-orphans-fail-on-a-stale-binary-4384).

**Run it from inside the checkout** (or pass `--workspace <path>`): repo-root
resolution requires an ancestor holding **both** `.git` and `.loom/`, so a
machine-level `~/.loom` (the token pool) is never mistaken for a repository. Run
from anywhere else it exits `1` naming the directory it searched — it does not
guess (#5140).

**Exit codes**: `0` = assessed, nothing orphaned · `2` = orphans found in
dry-run mode · `3` = **could not assess** (e.g. the `gh issue list --label
loom:building` query failed). A `3` is never reported as "No orphaned tasks
found" — a failed query is not evidence that nothing is stranded, and `--json`
carries `assessment_failed` / `assessment_errors` for automation.

**What it does**:
- Finds issues with `loom:building` label that have been stuck
- Checks if there's an associated PR (by branch name or body reference)
- Issues without PRs older than threshold are flagged/recovered
- Issues with stale PRs are flagged but not auto-recovered (need manual review)

### Uncommitted work in the primary clone can be quarantined at any time — branching does not protect it (#5194)

**Symptom**: uncommitted edits made directly in a Loom-managed repo's **primary
clone** (the main checkout, not a `.loom/worktrees/issue-N` linked worktree)
disappear. This happens even when the edits were made on a feature branch, not
on `main` — checking out a branch first feels like it should be "safe," but it
is not.

**Root cause**: `check-main-clean.sh --quarantine` (see the Wave Lifecycle
"Backstop" step in `defaults/.claude/commands/loom/sweep.md`) polices the
**primary clone's working tree as a whole**, keyed on `git rev-parse
--show-toplevel`/`--git-common-dir`, not on the currently checked-out branch.
A concurrent `/loom:sweep` (interactively, via the daemon, or via cron) that
snapshots the primary clone and later finds it dirty will stash-quarantine
**every** offending path in one `git stash push --include-untracked`, whichever
branch happens to be checked out at that moment. It has no way to know "this
dirt is on a branch I don't own" — from its point of view, any uncommitted
change in the primary clone that was not there at snapshot time is
contamination, full stop.

**Branching is NOT sufficient protection.** This is the first (and wrong)
inference people make: "I'm not on `main`, so a sweep can't touch my WIP."
Twice in one session, uncommitted work in the primary clone was lost this way
even though it lived on a dedicated branch — see #5185 and
[rjwalters/repo#89](https://github.com/rjwalters/repo/issues/89) for the
incidents that prompted this note. The quarantine backstop exists precisely
because *something* wrote to the primary clone's working tree outside of a
worktree — it does not, and should not, special-case "but it's on a branch."

**The only patterns that actually protect uncommitted work**:

1. **Create a worktree outside the policed clone** and do the work there:
   ```bash
   git worktree add /path/outside/the/clone some-branch
   ```
   A sibling directory (or anywhere off the main checkout's `git
   rev-parse --show-toplevel`) is never touched by `check-main-clean.sh`,
   because it only ever inspects the primary clone's own working tree. This is
   exactly what `./.loom/scripts/worktree.sh <issue-number>` gives you under
   `.loom/worktrees/issue-N` — use it (or a hand-rolled `git worktree add`
   pointed elsewhere) for any WIP you want to survive a sweep, rather than
   editing directly in the primary clone.
2. **Commit before a sweep can fire.** A commit is not "dirt" — the backstop
   only quarantines the working tree's uncommitted delta against its snapshot
   baseline, so committed history on any branch is unaffected regardless of
   which branch is checked out when a sweep runs.

Checking out a branch and leaving changes **uncommitted** in the primary clone
protects against neither: the working tree is still what gets swept into a
stash rescue ref, and recovering it means digging through `git stash list`
in the primary clone (`stash_ref`/`stash_commit` are logged to
`.loom/logs/main-quarantine.log`) rather than simply finding your branch
intact.

**Discovering an outstanding quarantine without prior knowledge that one
occurred (#5185).** Both #5185 incidents above were noticed only by chance —
an unrelated hygiene command happened to count stashes and flag one that had
not existed at session start. `git stash list` and the structured
`.loom/logs/main-quarantine.log` are both authoritative, but neither is
something an operator thinks to check unprompted. `/loom:sweep` now runs
`./.loom/scripts/check-quarantine-stashes.sh` before its first wave (see
"Outstanding Quarantine Stashes" in `defaults/.claude/commands/loom/sweep.md`)
— a non-blocking, read-only advisory that lists every outstanding
`loom-quarantine:` stash (its `stash@{N}` selector, age, and run/issue label)
whenever one exists. Run it manually at any time to check without waiting for
a sweep:
```bash
./.loom/scripts/check-quarantine-stashes.sh
```

**A note on the quarantine's stash message**: `check-main-clean.sh` passes an
explicit `-m "loom-quarantine: $QUARANTINE_LABEL"` message to `git stash
push`, but `git stash list` always prefixes stash entries with `On
<branch>:` regardless of the `-m` message supplied — so a quarantined stash
reads as e.g. `On some-branch: loom-quarantine: run=... issue=...`, which can
itself read as "this was scoped to `some-branch`" even though the quarantine
is whole-working-tree, not branch-scoped. Changing that format is out of
scope here — `stash_message` is also emitted as a structured field in the
`main-clean.quarantine` JSON log line, and other tooling may parse either
form — but if you are debugging a quarantine, read the `On <branch>:` prefix
as "which branch happened to be checked out at quarantine time," not as
"which branch's changes were protected."

### Finding outstanding quarantine stashes (#5185)

**Symptom**: none — that is the problem. A quarantine is loud at the moment it
happens (a `main-clean.quarantine` line on stderr and in
`.loom/logs/main-quarantine.log`) and silent forever afterwards. Nothing drops
the rescue stash, nothing reminds anyone it exists, so entries pile up: 29 on
one host, oldest 7 days; five in a consumer repo inside 24 hours, none
reconciled. They were noticed only because an unrelated hygiene command
happened to count stashes.

**List them**:

```bash
./.loom/scripts/check-main-clean.sh --list-quarantined          # human-readable
./.loom/scripts/check-main-clean.sh --list-quarantined --json   # machine-readable
```

The same count (and the newest few entries) appears as a `Quarantined work`
section in `./.loom/bin/loom status`, so an outstanding quarantine is
discoverable without knowing one happened. The report is read-only — it never
pops, drops, or reorders anything — and exits 0 whether or not anything is
outstanding.

It covers every Loom producer that pushes to the stash stack, not just
`check-main-clean.sh --quarantine`'s `loom-quarantine:` entries (the Auditor's
`auditor-tmp-drift-stash-<epoch>` shelf is the other one), and flags any entry
that captured **nothing** — those are pure noise and safe to drop.

**Reconciling an entry**:

1. **Identify by commit, not by index.** `stash@{N}` shifts every time anything
   pushes, and `refs/stash` is one stack shared by the primary clone and every
   linked worktree — so the index you read a minute ago may name a different
   entry now. The report prints each entry's commit sha for exactly this reason.
2. **Read it**: `git -C <main> stash show -p --include-untracked <commit>`.
3. **Check liveness before dropping anything.** Each quarantine entry carries
   `run=sweep-<...>` and `issue=<N>`. An entry naming a **finished** run whose
   issue is **closed** is almost certainly superseded; one naming a live sweep
   or an open issue may be the only copy of that work. This is the judgement a
   human cannot make by eye across dozens of entries, and it is why bulk
   "prune anything that looks stale" is unsafe on a busy host.
4. **Replay, don't pop.** To recover the work, apply the diff **inside the
   owning issue worktree** rather than `git stash pop`-ing it back into the
   primary clone — a pop in a shared stack can restore someone else's entry
   (see "Never use bare `git stash` for ad-hoc WIP" in `builder.md`), and it
   puts the contamination straight back where the backstop will quarantine it
   again.

**Empty entries**: an entry flagged `[EMPTY]` recorded nothing. These came from
a race between the contamination snapshot and the stash push; the push is now
preceded by a fresh re-derivation of the offending paths, so new ones should
not appear (and a quarantine with nothing left to rescue logs
`"result":"no_op"` and creates no stash at all). Existing empty entries carry
no work and can be dropped once you have confirmed the flag.

## Several unrelated things hang at once (macOS Gatekeeper / `syspolicyd`)

**Symptom:** several unrelated processes — a `cargo` build, a sweep child, a
shell wrapper, a timing-sensitive test — appear to hang *simultaneously*, all
sitting at **zero CPU time** even after minutes of wall-clock. It looks like
host-wide CPU starvation, but the CPUs are idle.

**Zero CPU means nothing on its own.** Before chasing any single victim,
distinguish the two very different conditions that both present as "stuck at zero
CPU":

- *Many* unrelated processes at zero CPU, including short-lived `exec`s that never
  make progress → **macOS Gatekeeper / `syspolicyd` saturation** (this section).
- A *single sweep* at zero CPU with a flat log → almost certainly **healthy, not
  wedged**. Sweeps are network-bound and accrue almost no CPU while awaiting API
  responses; `%CPU`, cumulative `TIME`, a `sample <pid>` stack (which parks in
  `CFRunLoopRun → mach_msg`, indistinguishable from a block), and sweep-log size
  **cannot** tell a working sweep from a dead one. Verify liveness via the side
  effects a sweep actually produces — `ls .loom/worktrees/`, `git log` in the
  worktree, `git ls-remote --heads origin 'feature/issue-*'`, and the forge
  (`gh pr list`) — before concluding anything. (See "Stuck Agent Detection" below.)

### First diagnostic move

```bash
ps -axo %cpu,time,comm | sort -rn | head
```

If `syspolicyd` is at or near the **top** of that list, stop debugging the
individual victims — they are symptoms, not the cause. Under parallel `cargo`
builds, macOS's Gatekeeper daemon (`syspolicyd`) becomes the bottleneck: it
serializes code-signature / notarization checks on every `exec`, and when
saturated it stalls *every* new process launch host-wide.

### The exec-stall signature

A victim of `syspolicyd` saturation is a process caught mid-`exec` — launched, but
its target binary has not yet cleared Gatekeeper. Its tell-tale signature:

- **Zero CPU time** accumulated even after minutes of elapsed wall-clock.
- **`STAT S`** (interruptible sleep) — blocked, not spinning.
- **No child processes** — the wrapper never got far enough to fork its target.
- `lsof -p <pid>` shows `txt = /usr/bin/env` (or another wrapper) that **never
  exec'd its actual target** — the process is frozen inside the launch, waiting on
  the signature check.

### It is load-induced and self-recovering

The condition is caused by launch load, not by any one process, and it clears on
its own once the load is removed — **removing the build load (letting the parallel
`cargo` builds finish, or throttling them) is normally sufficient.** If you need to
unwedge it manually:

```bash
sudo killall syspolicyd
```

`syspolicyd` is a system daemon that `launchd` restarts immediately; killing it
drops its saturated in-flight queue and lets the stalled `exec`s proceed.

### Why this matters — a falsified contention story

This signature was originally *misdiagnosed* as CPU contention. The build gate's
timing-sensitive tests failed under host load and the failures were attributed to
sweeps starving the gate; #4044 / #4046 falsified that — the same tests passed
968/968 later with **no code change**, establishing the failures as `syspolicyd`
exec-latency artifacts rather than contention. The narrative and its consequences
for gate niceness live in [`build-gate.md`](build-gate.md) (the #4044 / #4046
falsification passage). ADR-0011 also records this as a macOS-specific defect that
is invisible to Linux CI by construction — see
[`docs/adr/0011-ci-runner-platform.md`](https://github.com/rjwalters/loom/blob/main/docs/adr/0011-ci-runner-platform.md)
(upstream Loom repo — not shipped to consumer installs).

> **Historical note on timeouts.** Contemporaneous incident writeups mention a
> "600s" build-gate budget; that figure is **incident history only**. #4048 raised
> the live budget to **1200s** — do not read 600s as the current value.

## Stuck Agent Detection

> **Note (post-#4274):** the Python `loom-stuck-detection` CLI was removed in
> epic #4081 phase 3. It read `.loom/spawn-loop-state.json::running[].last_heartbeat`,
> a file whose only writer (`spawn-loop.sh`) was deleted in v0.11.0 — so it had
> already been a safe no-op (report-only, never tears down work). Live sweep
> liveness is now owned by the Rust `loom-daemon` sweep registry.

### Check for stuck agents

The native surfaces replace the former CLI:

```bash
# Daemon + registry health summary (sweeps, PIDs, quarantine state)
loom health                # execs `loom-daemon status`
loom-daemon status --json  # machine-readable

# Per-sweep liveness (MCP): list sweeps and their last activity
#   mcp__loom__list_sweeps / mcp__loom__get_sweep_status
# plus the per-issue checkpoint timestamps under
#   .loom/sweep-checkpoint/issue-<N>.json
```

### Stuck indicators (post-v0.10.0)

| Indicator | Default Threshold | Description |
|-----------|-------------------|-------------|
| `stale_heartbeat` | 5 minutes | No checkpoint update for extended time |
| `dead_pid` | (instant) | PID in the daemon sweep registry is no longer alive |
| `error_spike` | 5 errors | Multiple errors in `.loom/logs/sweep-issue-N.log` |

The pre-v0.10.0 indicators `missing_milestone:worktree_created` and `extended_work` were retired when the Python daemon brain (`daemon_v2/`) was removed — see [the migration guide § Per-CLI breaking changes](https://github.com/rjwalters/loom/blob/main/docs/migration/v0.10.0-shepherd-deprecation.md#per-cli-breaking-changes) (upstream Loom repo — not shipped to consumer installs) for the field-level diff. The shell-level daemon surface (`./.loom/scripts/daemon.sh`) is preserved but does not write progress files, so milestone-based heuristics no longer apply.

### A killed Task/Agent-tool subagent leaves no teardown signal for external resources it held

**Symptom**: a Task/Agent-tool subagent dispatched mid-conversation (e.g. a
Builder or Judge subagent under `/loom:sweep`) is killed by a session cap or an
API error while it holds an external, non-Loom resource — a browser profile
lock, a hardware device claim, a DB advisory lock, a cloud lease, any arbitrary
mutex a caller's own tooling manages. The resource stays held indefinitely:
nothing in Claude Code notifies the parent session or Loom that the subagent
died, and nothing runs the subagent's own cleanup code.

**Root cause — no kill-time teardown hook exists for Task-tool subagents (as of
2026-08-04, this repo's Claude Code)**. This repo's `.claude/settings.json`
wires exactly four hook types: `PreToolUse`, `UserPromptSubmit`, `SessionStart`,
and `Stop` (the top-level `Stop` hook fires on the **outer** session ending, not
per-subagent). There is no `SubagentStop`-equivalent hook that fires when an
individual Task/Agent-tool dispatch is killed — a session-cap or API-error kill
of a subagent bypasses whatever teardown code that subagent would otherwise
have run on a graceful exit. **This was evaluated and found infeasible against
Claude Code's current hook taxonomy** (issue #5262) — if a future Claude Code
release adds a subagent-lifecycle hook, re-evaluate then; until it does, do not
re-propose a kill-time teardown hook or a daemon-side termination-signal
channel for Task-tool subagents without first confirming the taxonomy actually
changed upstream.

**Guidance for callers building agent-driven scripts against Loom**: because no
teardown signal is coming, any external lock a subagent might hold must be
designed so it recovers **without** relying on the holder's liveness or on any
kill notification:

- **Make locks self-expiring by wall-clock heartbeat, not by holder liveness.**
  A lock that is only released by its holder's own cleanup code stays held
  forever once that holder is killed outside its control (a session cap or API
  error gives it no chance to run cleanup at all). Instead, record a
  last-heartbeat timestamp and treat the lock as free once that timestamp is
  older than a bounded TTL — the same shape whether the lock lives in a file's
  mtime, a directory, a DB row, or a remote lease API.
- **Reap on a schedule, not only on the next acquire attempt.** A "stale-break
  on acquire" check (comparing an existing lock's age against a threshold only
  when something next tries to acquire it) never fires if nothing else ever
  tries to acquire that resource again — the lock silently stays stale
  forever. Pair it with a periodic reaper that proactively expires stale
  entries on its own cadence, independent of acquisition attempts.

**Loom's own internal precedent for this pattern** (worked examples, not a
reusable shared primitive external callers can import — they solve Loom's own
coordination problems, not a general-purpose external-mutex library):
- `heartbeat_claim` (`loom-daemon/src/activity/claims.rs`, wired into the IPC
  layer at `loom-daemon/src/ipc.rs:2712-2741`) keeps the `issue_claims` table's
  `last_heartbeat` column current for a live claim holder; `claim_issue` in the
  same file breaks a claim as stale only when `age_secs > stale_threshold` at
  the moment of the **next acquire attempt** — the "on next acquire, not
  scheduled" half of the pattern above, which is exactly why a resource nobody
  else ever tries to (re-)claim can stay stale-held indefinitely under this
  model alone.
- `PeerClaimView` (`loom-daemon/src/peer_claims.rs`) is the scheduled-reaper
  half: it expires a peer's soft claim once local receipt time exceeds a fixed
  TTL since the *last* observed heartbeat ad (`is_claimed_at` /
  `claimed_issues_at`), and a periodic re-advertisement
  (`sweep_registry::readvertise_peer_claims`) refreshes that clock well inside
  the TTL for a still-live holder — see the module doc comment's "TTL is
  measured against LOCAL receipt, never the advertiser's clock" section for why
  wall-clock skew across hosts rules out comparing timestamps directly.

**Prior art establishing this as Loom's actual mitigation pattern** (both
resolved without a kill-time hook, because none is available):
- #3683 — a role subagent killed mid-phase by an account rate limit left
  lifecycle steps dangling; the fix made the *lifecycle state* self-healing
  (resumable from a checkpoint) rather than depending on the killed subagent's
  own cleanup running.
- #4348 — a detached sweep killed by an external `SIGKILL` was never recovered;
  the fix was reaper-based detection of a dead PID against the sweep registry,
  not a termination signal from the kill itself.

**Out of scope**: implementing a `SubagentStop`-style hook, or any daemon-side
termination-signal channel for Task-tool subagents, is explicitly out of scope
for this section — see "Root cause" above for why.

## Sweep Dispatch Troubleshooting

Multi-issue dispatch is driven by the Rust `loom-daemon` binary via `mcp__loom__dispatch_sweep`. The daemon holds the sweep registry, event bus, and reaper in memory — there is no on-disk orchestration state file to inspect. (The v0.9.x `spawn-loop.sh` and its `.loom/spawn-loop-state.json` state file were removed in v0.11.0.)

### Sweep MCP tools missing (stale dist bundle)

**Symptom**: `mcp__loom__dispatch_sweep`, `mcp__loom__list_sweeps`, `mcp__loom__get_sweep_status`, `mcp__loom__tail_sweep_log`, `mcp__loom__cancel_sweep`, `mcp__loom__publish_event`, `mcp__loom__subscribe_to_events`, or `mcp__loom__tail_event_bus` are **not offered** in a live session — `/loom:sweep`'s Stage -1 daemon probe can't reach them even though `loom-daemon` is running.

**Cause**: the MCP client loads the **built bundle** `mcp-loom/dist/index.js`, never the TypeScript source. `dist/` is gitignored, so a checkout that predates the sweep tools (Phase A #3452 / Phase C #3455) keeps serving an old bundle. The source (`mcp-loom/src/index.ts` → `sweepTools`) is correct; the on-disk artifact is stale (#3803).

**Diagnose**:

```bash
# 0 means the sweep tools are absent from the built bundle -> stale
grep -c dispatch_sweep mcp-loom/dist/index.js

# Compare build vs source timestamps
ls -la mcp-loom/dist/index.js
find mcp-loom/src -type f -newer mcp-loom/dist/index.js   # any output => dist is stale
```

**Fix** — rebuild, then **reconnect**:

```bash
cd mcp-loom && npm install && npm run build
grep -c dispatch_sweep dist/index.js   # should now be > 0
```

`scripts/setup-mcp.sh` now auto-rebuilds when `dist/index.js` is missing **or** older than any file under `mcp-loom/src/` (#3803), so `./scripts/setup-mcp.sh` is the safe one-shot path. Rebuilding the bundle does **not** refresh an already-running session — an MCP client caches its tool list at connect time, so you must **restart the Claude Code session** (or respawn the `loom` MCP subprocess) for the new tools to appear. See [`mcp-loom/README.md`](https://github.com/rjwalters/loom/blob/main/mcp-loom/README.md#rebuilding-after-source-changes-reconnect-required) (upstream Loom repo — not shipped to consumer installs) for the full rebuild + reconnect procedure and a raw `tools/list` verification snippet.

### MCP tools hang with no response (~1800s), then abort

**Symptom**: `mcp__loom__dispatch_sweep`, `mcp__loom__get_sweep_status`, `mcp__loom__list_sweeps`, or `mcp__loom__cancel_sweep` return **no response and no progress**, and are eventually aborted by the client (`sent no response or progress for 1800s; aborting`) — even though the underlying operation **succeeded** (the sweep child spawned, the PR opened, etc.). The CLI path (`loom-daemon status` / `dispatch`) stays fast throughout, which isolates the fault to the MCP/IPC response path, not a wedged daemon.

**Cause** (#4043): the MCP bridge's unary request transport (`mcp-loom/src/shared/daemon.ts` `sendDaemonRequest`) historically settled its promise only in the socket's `end` handler. The real `loom-daemon` holds each connection **open by design** (a persistent per-connection read loop, `loom-daemon/src/ipc.rs` `handle_client`): it writes one newline-delimited JSON response frame per request and never closes after answering. So the response sat complete-but-unparsed in the client buffer while the promise waited forever for an `end` that never came. A **stale bundle** (`mcp-loom/dist/` older than `mcp-loom/src/`) compounds it by discarding the per-call timeout, turning a diagnosable failure into the full ~1800s idle hang.

**Diagnose** — probe the raw socket (bypassing the MCP layer) and check the bundle for the timeout string:

```bash
# Does the bundle even carry the bounded-timeout fix? 0 => stale, pre-timeout bundle
grep -c "did not respond within" mcp-loom/dist/index.js

# Is dist/ older than src/? (any output => stale, rebuild needed)
find mcp-loom/src -type f -newer mcp-loom/dist/index.js
```

**Fix** — the transport now settles on the **first newline-delimited response frame** (in the `data` handler) and closes the socket after settling, so it no longer depends on the daemon closing the connection. If you are on a pre-fix bundle, rebuild and reconnect:

```bash
cd mcp-loom && npm install && npm run build   # then restart the Claude Code session
```

The `claude-wrapper.sh` MCP pre-flight (`check_mcp_server`) now rebuilds a stale bundle before the smoke test, so a fresh session on an up-to-date checkout self-heals; the regression is guarded by `mcp-loom/scripts/verify-daemon-timeout.mjs`'s respond-without-close stub case (`npm run verify:daemon-timeout`).

### Inspect running sweeps

```bash
# List all running sweeps in the daemon registry
mcp__loom__list_sweeps

# Inspect a specific sweep's state
mcp__loom__get_sweep_status --sweep_id <id>

# Tail a per-sweep log
mcp__loom__tail_sweep_log --sweep_id <id>
# (per-sweep logs also live at .loom/logs/sweep-issue-<N>.log)
```

### Cancel a sweep

```bash
# From a Claude session with the MCP server attached:
mcp__loom__cancel_sweep --sweep_id <id>

# From a shell — including over ssh to a fleet worker (#4980). Same IPC
# request, same daemon-side termination path:
loom-daemon cancel <sweep-id>
loom-daemon cancel --issue 123          # resolves the live sweep for that issue
loom-daemon cancel --issue 123 --grace 5
```

Both send SIGTERM to the sweep's **process group**, wait the grace window, then
SIGKILL — so the wrapper, the `claude` agent, and every descendant (build tools,
simulations, watcher loops) die together.

**Never hand-`kill` a sweep's pids instead.** The registry tracks the
`claude-wrapper.sh` pid; killing it leaves the underlying agent alive. On
2026-08-03 that surviving agent noticed its subprocesses had died and
*relaunched* them, against an issue whose claim the crash path had already
returned to `loom:issue` — a zombie agent the registry reported as
`in_flight: 0`. If you have already done this, `loom-daemon status` will not show
the survivors: find them with `pstree -p <pid>` / `ps -eo pid,pgid,args` and kill
the whole group (`kill -TERM -<pgid>`).

The daemon's reaper task detects dead PIDs (every 30s) and removes them from the registry, emitting `sweep.issue.*.exited` / `sweep.issue.*.crashed` events. Since #4980 it also reaps a dead leader's *surviving* process group on that same tick, so an orphaned agent no longer keeps running unclaimed work.

### Stuck sweep child

A sweep child whose pid is alive but whose `.loom/sweep-checkpoint/issue-<N>.json` mtime is stale is likely stuck. To recover:

```bash
# Check checkpoint mtime
ls -la .loom/sweep-checkpoint/issue-123.json

# Look at the child's log for errors
tail -200 .loom/logs/sweep-issue-123.log

# Cancel it through the daemon (MCP tool, or `loom-daemon cancel <id>` from a
# shell / over ssh):
mcp__loom__cancel_sweep --sweep_id <id>

# The checkpoint survives cancellation, so re-dispatching the issue resumes
# from its last completed phase:
mcp__loom__dispatch_sweep --issue 123
```

### Dispatch is not producing sweeps

Issues need the `loom:issue` label (human-approved, ready for work) to be eligible for dispatch. If a dispatch isn't producing a sweep, check:

```bash
# 1. Confirm there are ready issues
gh issue list --label "loom:issue" --state open

# 2. Confirm the daemon is reachable and running
mcp__loom__list_sweeps

# 3. Confirm the multi-account token pool is bootstrapped (dispatch requires it)
ls -la .loom/tokens/

# 4. Look at recent sweep activity on the event bus
mcp__loom__tail_event_bus
```

Note: by default the daemon does not poll the forge for `loom:issue` items — dispatch is operator-driven via `mcp__loom__dispatch_sweep`. To dispatch a ready issue, call `mcp__loom__dispatch_sweep --issue <N>` explicitly. (The opt-in autonomous work finder (#3810, `LOOM_WORK_FINDER` / `autonomous.workFinder`, default-off) *does* poll and auto-dispatch open `loom:issue` items when enabled — see [daemon-reference.md](daemon-reference.md#autonomous-work-finder-3810).)

### Work generation (Architect / Hermit) not running

**This is by design post-v0.10.0.** The daemon does not generate work — Architect and Hermit cadence is tracked under follow-up #3381. If you need new work generated automatically, run Architect/Hermit on a cron via the Phase 2a GitHub Actions pattern (`.github/workflows/loom-*.yml`); the existing five shipped workflows cover Champion / Curator / Judge / Auditor / Guide, but Architect and Hermit cron workflows are not yet shipped.

For now, trigger them manually when the queue is empty:

```bash
claude -p "/loom:architect" --dangerously-skip-permissions
claude -p "/loom:hermit"    --dangerously-skip-permissions
```

## Overnight / long-running orchestration

> **Supervising a long window: `/loom:watch` (#4762).** The tick loop that probes
> fleet health, applies a closed set of remediations, and prints an end-of-window
> summary is a skill: `/loom:watch [--until 07:00] [--interval 25m]`
> (`--dry-run` for a single read-only tick). It assumes — and does not restate —
> the host-sleep and `.loom/` resync procedures in this section. See
> `.claude/commands/loom/watch.md`. Note the host-sleep check above does **not**
> cover *session* suspension stalling a mode-A `ScheduleWakeup` loop — see
> `watch.md` → Loop mechanics → A for that hazard and its preflight (#4930).

### Keeping the host awake (#3350)

`/loom:sweep` automatically runs `./.loom/scripts/check-host-sleep.sh` at startup
and warns when the host can sleep. This is **advisory only** — Loom never blocks
on it. Heed the warning before walking away from a long run.

- **macOS:** user-idle sleep assertions (Amphetamine, `caffeinate -dimsu`, etc.)
  do **not** reliably defeat Maintenance Sleep on Apple Silicon. Use `sudo pmset
  -c sleep 0` for AC-only sleep disable, or flip your sleep manager's "allow
  system sleep when display is off" toggle to OFF. Restore with `sudo pmset -c
  sleep 1` afterwards.
- **systemd Linux:** wrap the session in `systemd-inhibit --what=idle:sleep
  --who=loom --why=loom -- <cmd>`.

Manual invocation:

```bash
./.loom/scripts/check-host-sleep.sh         # full warning (or success line)
./.loom/scripts/check-host-sleep.sh --quiet # stderr warning only, no stdout line
```

### Keeping installed `.loom/` copies fresh after a pull (#3770 detect → #3777/#4239 resync)

The installed Loom surfaces the harness actually executes/reads are synced from
`defaults/` **at install time**. A `git pull` that merges a fix updates `defaults/`
but **not** the installed copies — so a session can run stale hooks/scripts/roles/
docs/commands indefinitely (the incident: a merged `guard-destructive.sh` fix kept
prompting until hand-copied). Before #4239 the only full-surface update path was a
destructive `install.sh --confirm-reinstall`.

This is a **detect → fix** pair:

- **Detect (#3770)** — `/loom:sweep` runs `./.loom/scripts/check-main-freshness.sh`
  at startup. When local `main` is behind `origin/main` it prints a non-blocking
  warning and flags any installed file that differs from its `defaults/`
  counterpart. Advisory only; it never pulls, merges, or resets.
- **Fix (#3777, widened #4239)** — `./.loom/scripts/resync-installed.sh` refreshes
  the installed pure-copy surfaces from the recorded Loom source (note the
  asymmetric source→target mapping):

  | Installed surface | Source under `defaults/` |
  |-------------------|--------------------------|
  | `.loom/hooks/` | `defaults/hooks/` (top-level `*.sh`) |
  | `.loom/scripts/` | `defaults/scripts/` |
  | `.loom/roles/` | `defaults/roles/` |
  | `.loom/docs/` | `defaults/docs/` |
  | `.loom/bin/` | `defaults/.loom/bin/` (live consumer CLI) |
  | `.claude/commands/loom/` | `defaults/.claude/commands/loom/` |

  It is idempotent (a no-op when in sync), reports per-file
  `updated`/`created`/`unchanged`/`skipped`, only ever touches files that exist in
  the source (repo-specific files with no source counterpart — e.g. custom roles —
  are left alone), and **never clobbers a symlinked install target** (in this
  dogfood repo the `.loom/docs/*.md` entries are symlinks back into `defaults/`;
  those are reported `skipped`, never overwritten). Loom-internal files declared in
  `defaults/.loom-internal.list` are skipped (never resurrected into a consumer).
  On a successful non-dry-run it also re-stamps `loom_version`, `loom_commit`, and
  a new `last_resync` date into `.loom/install-metadata.json`. Since that file is a
  machine-local stamp every host's resync rewrites, resync also ensures (every
  run, self-healing existing installs) a `merge=ours` attribute for it in a
  Loom-managed `.gitattributes` block plus the required local (never committed)
  `git config merge.ours.driver true` — so two hosts that each committed a resync
  no longer conflict on `git merge`/`git pull`; the file is fully re-derived by the
  next resync regardless of which side "wins" (#4528). One guard exception
  (#4041): the vendored generic guard `hooks/guard-destructive-generic.sh` is
  **not** resynced (and any stale copy is removed) in a repo where the canonical
  Repo Skills guard (`.claude/skills/repo/hooks/guard-destructive.sh`, carrying the
  rjwalters/repo#29 fix) is installed — the `guard-destructive.sh` dispatcher
  defers to the canonical guard there.

**Out of scope** (resync never touches these — they update by other mechanisms):
`.loom/config.json` (operator-owned; needs merge-semantics design), `CLAUDE.md`
(repo-customized; needs managed-section markers), `.github/labels.yml` +
`.github/workflows/*` (covered by `gh label sync` + install-time opt-ins), the
`loom-daemon` binary (#4055 self-update), `.mcp.json` (regenerated by
`scripts/setup-mcp.sh`), and the metadata `install_date` + `installed_files`
fields (installer-owned).

**Run it from the main checkout only (#4563).** The installed `.loom/` is always
resolved against the **primary** worktree (via `git rev-parse --git-common-dir`),
so a resync launched from a linked worktree — an issue/PR worktree under
`.loom/worktrees/` — writes to the **main checkout**, not to the worktree you are
standing in. That is exactly how a wave-2 Builder contaminated `main` mid-sweep on
2026-07-30 (four installed paths written into `main` and quarantined by
`check-main-clean.sh`). The script therefore **refuses to run** (exit `1`,
including under `--dry-run`) when its own `git rev-parse --show-toplevel` differs
from the resolved main-checkout root. Landing a `defaults/` change does **not**
require you to resync: installed-copy propagation is the periodic
`chore: resync installed Loom surfaces` commit's job, made from the main checkout
after the change merges. An operator who really does mean "rewrite the main
checkout's installed copies from this worktree" can pass `--allow-worktree` (or
export `LOOM_RESYNC_ALLOW_WORKTREE=1`); it then proceeds with a warning naming the
main-checkout target. Running from the main checkout — including any subdirectory
of it — is unaffected.

**It can safely update itself (#4669).** `resync-installed.sh` is one of the files
under `defaults/scripts/`, so every run copies a newer version over the very path
the running Bash process is still reading from. It used to do that with an
in-place `cp`, which truncates and rewrites the destination: Bash then resumed
reading its own (now shorter) script at a stale byte offset and either died with
`syntax error near unexpected token` or fell off the end mid-run — leaving dozens
of surfaces refreshed, the rest stale, and no summary saying so. Now every file
is staged beside its destination and `rename(2)`-d into place (atomic; the
already-open inode is left intact), and the self-copy is additionally **deferred
until every other surface has settled**. If a file cannot be staged or renamed
(read-only mount, permissions, no disk space) the run still finishes the
remaining files and then prints an explicit **`PARTIAL REFRESH`** block naming
every failed path and **exits `1`** — nothing is ever half-written, so fixing the
cause and re-running completes the refresh.

The intended flow is **"freshness warning says you're stale → run resync"**:

```bash
cd <main checkout>                              # NOT .loom/worktrees/issue-N (#4563)
git merge --ff-only origin/main                 # bring defaults/ current
./.loom/scripts/resync-installed.sh --dry-run   # preview what would change (exits 2 on drift)
./.loom/scripts/resync-installed.sh             # apply
```

`--dry-run` makes no changes and exits `2` when drift is detected (so it doubles
as a check). To pin an intentional per-repo customization so resync never
overwrites it, list its relative path (e.g. `hooks/guard-destructive.sh`,
`roles/custom-role.md`, `bin/loom`, or `commands/loom/mine.md`) — one per line — in
`.loom/resync-ignore`; matching files are reported `skipped`. A full `loom-daemon
init` / installer run already performs the equivalent recursive copy, so a normal
reinstall keeps the copies current too.
