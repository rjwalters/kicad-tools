---
name: "sudo"
description: "Opt-in passwordless sudo for a dev machine — install a visudo-validated /etc/sudoers.d drop-in (ALL or a scoped command list) so an agent over SSH isn't blocked on password prompts, always confirmed first"
domain: repo
type: command
user-invocable: true
---

# /repo:sudo — Passwordless-sudo setup for dev machines

Install (or inspect, or remove) a `NOPASSWD:` **sudoers drop-in** for the
current user on **this machine**, so an agent driving the box over SSH isn't
stopped dead by a `sudo` password prompt it can't answer. A non-interactive SSH
session has no TTY to type a password into, so every root-level remediation —
`launchctl bootout`, killing a runaway root process, `spctl`, `renice` — stalls
until a human runs the command by hand. This command installs the standard fix
once, safely, so agent-driven remote sessions can finish end-to-end.

This is **machine-global, consequential setup**: the drop-in grants anyone
holding the user's SSH key effective root on this box. It is therefore in the
same **always-confirm-first** class as `/repo:remote`, `/repo:followups`, and
`/repo:update-tools` — it shows the **exact file contents** it will write and
**never applies silently**, even without `--ask`. There is nothing to opt into;
confirmation is the only mode. The reasonable trade this makes — passwordless
root for personal dev machines on a private tailnet — is a deliberate operator
choice, so the operator makes it explicitly every time a scope changes.

> **Touch ID is the better answer for *local, interactive* use** — enabling
> `pam_tid.so` in `/etc/pam.d/sudo` lets a Mac authorize `sudo` with a
> fingerprint. But Touch ID does **not** work over SSH (there is no local
> fingerprint reader on the remote end), which is exactly why a `NOPASSWD:`
> drop-in is the right tool for **agent sessions** and this command exists.

The **destructive-command guard still applies on top.** Passwordless sudo
removes the *password* gate, not the guard's `ask`/`block` gates on risky
commands (`rm -rf /`, force-push to `main`, cloud destruction, …). Granting
passwordless root does not weaken any of those checks.

**Run this from a host session, not from inside a Loom-managed repo session.**
`/repo:sudo` configures *the machine*, not this repository's contents, and its
write step (step 5) is deliberately built around `mktemp` — a path this repo's
worktree-write-confinement guard can never resolve, so it is denied
**fail-closed** whenever a managed worktree exists here. That denial is
expected and must not be worked around: invoke `/repo:sudo` from a session
outside a Loom-managed checkout, or have the operator run step 5's commands
by hand in a plain shell/SSH login. Full explanation, and why the temp file
must stay `mktemp`-created, is in the guard note in
[step 5](#5-confirm-then-write--validate--roll-back).

## Usage

```
/repo:sudo                 # Guided: explain the grant, confirm, install an ALL-scoped drop-in
/repo:sudo --scoped        # Confirm, then install a command-list-scoped drop-in
                            #   (launchctl, pkill, spctl, renice, …) instead of ALL
/repo:sudo --status        # Report whether a drop-in exists, its scope, and validity — writes nothing
/repo:sudo --remove        # Confirm, then remove this command's drop-in file
```

## How it works

The drop-in lives at a well-known, per-user path — **the file itself is the
marker**:

```
/etc/sudoers.d/<user>-nopasswd
```

Both macOS and Linux read this directory automatically, so **no OS branching is
needed**:

- **macOS** — `/etc/sudoers` (a symlink target of `/private/etc/sudoers`)
  ships `@includedir /private/etc/sudoers.d` by default, and `/etc/sudoers.d`
  is `/private/etc/sudoers.d`.
- **Linux** — nearly every distro ships `#includedir /etc/sudoers.d` in
  `/etc/sudoers`.

The candidate file is always syntax-checked **in isolation** with
`sudo visudo -cf "$TMP"` *before* it is copied into `/etc/sudoers.d/` — a
malformed drop-in never becomes live policy. After the validated file is
installed, a full-chain `sudo visudo -c` (it validates `/etc/sudoers` **and**
every file it includes) implicitly proves the include chain is active on either
platform, so there is no need to detect the OS or hand-verify the directive.

The written file always carries a **discoverability header** on its first line
so `--status`, `--remove`, and any later host-posture audit (e.g.
`/repo:host-optimize`) can find and attribute it without guessing from the
filename:

```
# Installed by /repo:sudo (rjwalters/repo) — see .claude/commands/repo/sudo.md
```

Two scopes are supported:

- **ALL** (default) — blanket passwordless root:

  ```
  <user> ALL=(ALL) NOPASSWD: ALL
  ```

- **Scoped** (`--scoped`) — passwordless only for a small allowlist of
  service-management commands, so agents are unblocked on the operations that
  actually stall over SSH without granting blanket root:

  ```
  <user> ALL=(ALL) NOPASSWD: /bin/launchctl, /usr/bin/pkill, /usr/sbin/spctl, /usr/bin/renice
  ```

  Anything outside that list still prompts for a password. Adjust the allowlist
  to what the machine actually needs before confirming.

## Steps

### 1. Resolve the target and read current state

Resolve the drop-in path and read what's already installed — every mode below
starts here, and it is the whole of `--status`.

```bash
USER_NAME="$(id -un)"
DROPIN="/etc/sudoers.d/${USER_NAME}-nopasswd"

if [ -f "$DROPIN" ] || sudo test -f "$DROPIN" 2>/dev/null; then
  CURRENT="$(sudo cat "$DROPIN" 2>/dev/null)"        # readable only as root (0440)
else
  CURRENT=""
fi
```

Classify the installed scope from `$CURRENT`, ignoring the header/comment lines:

- **absent** — no file.
- **ALL** — the effective grant is `NOPASSWD: ALL`.
- **scoped** — the effective grant is a `NOPASSWD:` command list.

Determine the **requested** scope from the flags: `--scoped` → scoped list;
otherwise → ALL. (`--status` and `--remove` don't request a scope.)

### 2. `--status` — report only, write nothing

Report and **stop** — this mode never writes, never prompts, and must not
change the file's mtime:

- Whether `$DROPIN` exists, and its scope (ALL vs. the scoped command list).
- Whether it currently passes validation — run `sudo visudo -c` and report
  pass/fail (this reads the config; it does not modify it).
- Whether passwordless sudo is actually in effect right now:
  `sudo -n true 2>/dev/null && echo 'passwordless sudo works' || echo 'sudo still requires a password'`.

Then exit. Do not fall through to any write path.

### 3. `--remove` — delete this command's drop-in (confirm first)

Only ever remove **this command's own** file at `$DROPIN` (identified by the
header from step 1) — never touch other files in `/etc/sudoers.d/`.

> **Guard note**: `sudo rm -f "$DROPIN"` below is denied inside a
> Loom-managed repo session under default `guards.rmScope=repo` — see the
> consolidated guard note in [step 5](#5-confirm-then-write--validate--roll-back)
> for the full explanation and the manual remedy.

1. If the file is absent, say so and stop (nothing to remove — idempotent).
2. Show the file that will be deleted and its current contents, and get an
   explicit **yes**.
3. Remove it, then re-validate so the removal didn't break anything:

   ```bash
   sudo rm -f "$DROPIN"
   sudo visudo -c
   ```

4. Confirm the effect: a subsequent `sudo` now prompts for a password again
   (`sudo -n true` should fail). Report done.

### 4. Idempotency check (install modes)

Before writing anything, compare the **installed** scope (step 1) against the
**requested** scope (step 2):

- **Same scope already installed** → **no-op.** Report "already configured
  (`<scope>`) at `$DROPIN`" and stop. Do not re-prompt and do not re-write —
  re-running with the same scope must make no changes.
- **Different scope installed** (e.g. `--scoped` requested but `ALL` is
  installed, or vice-versa) → this is a real change of the grant. Say so
  explicitly ("currently `ALL`; you asked for `scoped`"), and require
  confirmation in step 5 before overwriting.
- **Nothing installed** → proceed to step 5 as a fresh install.

### 5. Confirm, then write / validate / roll back

Always show the **exact file contents** (including the header line) that will be
written, name the target path, and get an explicit **yes** before touching
anything — even without `--ask`. Never write silently.

Build the file in a temp location and **validate the candidate in isolation
before it can go live** — only a syntactically valid file is ever copied into
`/etc/sudoers.d/`. There is no separate "enable" step for a sudoers.d file: the
`@includedir` / `#includedir` directive picks it up on the very next `sudo`
parse, so a broken file that reaches the directory is *already* live policy —
and because sudo fails closed on a syntax error, the rollback `sudo rm` could be
denied by the very file it is trying to remove. Validating `$TMP` first makes
that failure mode impossible; the post-install full-chain check is then only a
belt-and-suspenders proof that the include wiring is sound:

> **Guard note — these two writes, and all four `rm` calls in this file, are
> denied inside a Loom-managed repo session, by design.**
>
> **The two writes below.** The worktree-write-confinement guard
> (`hooks/repo/guard-destructive.sh`, tag
> `worktree-write-confinement-unresolved-var`) fails **closed** on any Bash
> write whose target is an unexpanded shell variable, because it cannot tell
> where the write will land. Both writes below are exactly that shape:
>
> - `} > "$TMP"` — `$TMP` holds `$(mktemp)`, a command substitution the guard's
>   same-command variable resolution can never substitute (it only resolves a
>   *literal* assigned value, and only for an **unquoted** `$NAME` token — a
>   double-quoted `"$VAR"` target is left unresolved even when its value is a
>   plain literal).
> - `sudo cp "$TMP" "$DROPIN"` — `$DROPIN` is per-user by construction
>   (`/etc/sudoers.d/$(id -un)-nopasswd`), so it cannot be spelled literally in
>   this file at all.
>
> The write deny is keyed to whether **any** managed worktree exists in the
> repository, not to whether the current session is inside one, so it fires
> from the main checkout too. With zero managed worktrees the write guard
> fails open and these two writes run unchanged.
>
> **All four `rm` calls in this file** — step 3's (`--remove`)
> `sudo rm -f "$DROPIN"` (uninstall), this step's first `rm -f "$TMP"`
> (candidate-validation-failure cleanup) and second `rm -f "$TMP"`
> (post-install cleanup), and this step's `sudo rm -f "$DROPIN"`
> (post-install rollback). A separate guard, the repo-scoped `rm`
> guard (same `hooks/repo/guard-destructive.sh`, tag
> `rm-scope-unresolved-var`), fails **closed** on all four for the same
> underlying reason as the write guard above: `$DROPIN` and `$TMP` are each
> the **entire** rm target with no literal path preceding them, so the guard
> cannot tell where the delete resolves and refuses to guess.
>
> **This guard's trigger is broader than the write guard's, and the
> zero-managed-worktree escape hatch above does NOT apply to it.** Unlike
> write confinement, `rm_scope_repo_enabled()` checks **config only**
> (`guards.rmScope` / `LOOM_RM_SCOPE` / `REPO_RM_SCOPE`) and **defaults to
> `repo`** — there is no "only when a managed worktree exists in this repo"
> condition. So running `/repo:sudo` exactly as recommended at the top of
> this file — from a host session outside any Loom-managed checkout — avoids
> the write denials above but does **not** avoid these `rm` denials.
>
> Narrowing the rule so `$DROPIN` resolves doesn't help either: fully
> resolved, `$DROPIN` is `/etc/sudoers.d/<user>-nopasswd`, which is outside
> the repo under *any* resolution and would still be denied — just by the
> ordinary out-of-repo `rm-scope-outside-repo` rule instead. Recovering the
> variable only changes which rule fires, not the outcome. (Making it
> succeed would require an `/etc/sudoers.d` allowlist entry, which is just a
> narrower version of exempting this one command — the shape the guard
> exists to prevent.)
>
> **What this means per call site, and the manual remedy for each:**
>
> - **Uninstall (`--remove`, step 3)**: denied under default
>   `guards.rmScope=repo`, so `--remove` cannot complete automatically.
>   Manual remedy: from a host session (or any shell outside this guard) run
>   `sudo rm -f /etc/sudoers.d/<user>-nopasswd`, then `sudo visudo -c` to
>   confirm the removal didn't break anything.
> - **This step's validation-failure cleanup and post-install cleanup** (both
>   `rm -f "$TMP"` — the first immediately after the `visudo -cf` candidate
>   check, the second immediately after the `sudo cp` into
>   `/etc/sudoers.d/`): denied the same way, but low-stakes — `$TMP` is a
>   `mktemp`-created scratch file in the OS temp directory that the OS
>   reclaims eventually regardless. A denial here leaves harmless clutter,
>   not a functional problem.
> - **This step's post-install rollback (`sudo rm -f "$DROPIN"`, guarding the
>   final full-chain `sudo visudo -c` check) — the serious case.** This runs
>   when a candidate that already passed `visudo -cf` in isolation still
>   fails the full-chain `sudo visudo -c` *after* being copied into
>   `/etc/sudoers.d/`. **That trigger is the unreproduced part**: `visudo -cf`
>   validates the candidate alone, so failing the full chain right afterwards
>   takes something only the whole chain sees — a cross-file conflict (an
>   alias or `Defaults` already defined in another drop-in), or the mode and
>   ownership rules a single-file check never applies. Both are plausible;
>   neither has been observed here, and reproducing one means breaking `sudo`
>   on a real machine. The *consequence* is not hypothetical, though. If the
>   guard denies this rollback, **the drop-in stays
>   installed** — and it stays installed *silently*, because the deny blocks
>   the whole Bash call: nothing in the `if ! sudo visudo -c; then … fi` block
>   runs, not the re-check, not the `rm`, and not the block's own `exit 1`.
>   The operator sees the guard's denial, never this step's "post-install
>   validation failed" message, while the grant stays live. This is
>   **exercised, not inferred**: section 8 of
>   `commands/repo/tests/test-sudo-rm-guard-contract.sh` drives this exact
>   block through the real hook against a stand-in drop-in and asserts the
>   file survives the denial — with the identical block under
>   `guards.rmScope=off` deleting it as the control. Stranding does require
>   the `sudo cp` to have landed in an **earlier, separate** call: run the
>   whole step 5 fence as one Bash call and the same `rm` rule denies that
>   call *before* the `cp`, so nothing is installed and there is nothing to
>   strand.
>   Manual remedy: run `sudo visudo -c` yourself to see the failure, then
>   `sudo rm -f /etc/sudoers.d/<user>-nopasswd` by hand to remove the
>   stranded file, then re-run `sudo visudo -c` to confirm it now passes
>   clean.
>
> **Do not "fix" the write side by hardcoding a predictable temp path.**
> `mktemp`'s atomic `O_EXCL` creation is load-bearing here: the candidate is
> `chmod 440`'d and then `sudo cp`'d into `/etc/sudoers.d/` **as root**. A
> guessable name in a world-writable `/tmp` lets an attacker pre-create (and
> therefore own) the path, redirect it through a symlink, or swap its
> contents between `visudo -cf` and the `cp` — turning a guard-friendliness
> tweak into a root-policy TOCTOU hole. A guard denial is strictly the safer
> failure, and the same reasoning applies to the `rm` side: an unresolvable
> delete target fails closed rather than guessing.
>
> **What to do when any of the above is denied**: stop and run the step from
> a host session — a plain shell/SSH login, or any Claude session outside a
> Loom-managed checkout — where these guards are not installed, or have the
> operator run it by hand using the manual remedy above. Never route around
> it by switching tools, weakening the temp path, or disabling
> `guards.worktreeIsolation` / `guards.rmScope` (or their `REPO_*`/`LOOM_*`
> env equivalents) to push the write or delete through; those escape hatches
> are the operator's call, not the agent's.

```bash
USER_NAME="$(id -un)"
DROPIN="/etc/sudoers.d/${USER_NAME}-nopasswd"
TMP="$(mktemp)"

# Header first (discoverability), then the grant line for the chosen scope:
{
  echo "# Installed by /repo:sudo (rjwalters/repo) — see .claude/commands/repo/sudo.md"
  # Default (ALL):
  echo "${USER_NAME} ALL=(ALL) NOPASSWD: ALL"
  # --scoped instead:
  # echo "${USER_NAME} ALL=(ALL) NOPASSWD: /bin/launchctl, /usr/bin/pkill, /usr/sbin/spctl, /usr/bin/renice"
} > "$TMP"
chmod 440 "$TMP"

# Validate the candidate BEFORE it can affect live policy. visudo -cf checks a
# single file's syntax without installing it, so a malformed drop-in never
# becomes active and we never depend on a possibly-broken sudo to undo it.
if ! sudo visudo -cf "$TMP"; then
  rm -f "$TMP"
  echo "candidate sudoers failed validation — nothing installed" >&2
  exit 1
fi

# Only a validated file reaches /etc/sudoers.d/:
sudo cp "$TMP" "$DROPIN"
sudo chmod 440 "$DROPIN"
rm -f "$TMP"

# Belt-and-suspenders: full-chain re-check proves the include wiring too. If it
# somehow fails, remove the drop-in and exit non-zero — never leave an
# unvalidated or broken file in place. (This rollback is now reliable because
# the file was already known-valid before it landed.)
if ! sudo visudo -c; then
  sudo rm -f "$DROPIN"
  echo "post-install validation failed — removed ${DROPIN}, no change made" >&2
  exit 1
fi
```

The order is load-bearing: the candidate is syntax-checked with
`visudo -cf "$TMP"` **before** the `cp`, so a malformed drop-in never becomes
live policy and the rollback never has to fight a broken sudo. The post-install
`visudo -c` is the final full-chain gate; if it fails for **any** reason the
drop-in is deleted and the command exits non-zero — there is no path in which a
failed-validation file survives, and no path in which an unvalidated file goes
live even momentarily.

### 6. Verify and report

On success, prove the grant actually took and report a compact block:

```bash
sudo -n true 2>/dev/null && echo 'passwordless sudo now works' \
                         || echo 'WARNING: drop-in written and validated but sudo -n still prompts'
```

Report: the path written, the scope (ALL vs. the scoped list, with the exact
command list for the scoped case), that permissions are `0440`, that
`visudo -c` passed, and the removal command (`/repo:sudo --remove`) for undoing
it later.

## Safety Rules

1. **Always confirm before writing** — show the exact file contents (header +
   grant line) and the target path, and act only on an explicit yes. There is
   no silent/auto-apply mode; confirmation is the only mode.
2. **Validate the candidate before it goes live, then roll back on failure** —
   the candidate is built at `0440` in an `mktemp` file (the unpredictable,
   `O_EXCL`-created path is a security property of this flow, not an
   incidental detail — see the guard note in step 5) and syntax-checked in
   isolation with `sudo visudo -cf "$TMP"` **before** it is copied into
   `/etc/sudoers.d/`, so a malformed drop-in never becomes active policy (a
   sudoers.d file is live the instant it lands, and sudo fails closed on a
   syntax error — validating pre-install is what keeps the rollback reliable). A
   post-install full-chain `sudo visudo -c` then re-checks the include wiring;
   on any failure the file is removed and the command exits non-zero. A broken
   sudoers file can lock the user out of root, so an unvalidated file is never
   installed or left behind.
3. **Idempotent** — re-running with the same scope detects the existing drop-in
   and no-ops; only a scope *change* prompts to overwrite.
4. **Only ever touch this command's own drop-in** at
   `/etc/sudoers.d/<user>-nopasswd` (identified by its `Installed by /repo:sudo`
   header) — never other files in `/etc/sudoers.d/`.
5. **The destructive-command guard still applies** — passwordless sudo removes
   the password gate, not the guard's `ask`/`block` gates on risky commands.
6. **Prefer the smallest grant that unblocks the work** — offer `--scoped`
   (launchctl / pkill / spctl / renice) when blanket `ALL` root isn't needed.
7. **Run it from a host session, and never weaken step 5 to satisfy a guard**
   — this is machine-global setup, so it belongs outside a Loom-managed repo
   session, where the worktree-write-confinement guard denies step 5's
   `$TMP`/`$DROPIN` writes fail-closed. If that denial happens, hand the step
   to the operator; do not hardcode a predictable temp path, switch tools, or
   disable the guard to get the write through (see the guard note in step 5).
