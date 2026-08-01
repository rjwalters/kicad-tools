---
name: "host-optimize"
description: "Audit and prepare a Mac (or Linux box) for heavy Loom/agent build use — Gatekeeper churn, backup-agent interference, build-tree bloat"
domain: repo
type: command
user-invocable: true
---

# /repo:host-optimize — Prepare a Build Host

Audit the **host** a machine presents to heavy Loom/agent build workloads, then
apply the safe fixes and gate the consequential ones. This is host preparation,
not repo hygiene: nothing here is discoverable from inside a repo, and none of it
is fixed by [[tidy]] or [[audit]]. It exists because a Mac Studio running six
concurrent release-build worktrees melted down — load average 118, VNC unusable,
a multi-hour remediation — and **every root cause was host configuration**:
`syspolicyd` burning 14+ CPU-hours a day re-scanning `target/` trees on every
Gatekeeper cache miss, a backup agent churning build dirs at 98% CPU, no
passwordless sudo, an empty Developer Tools exemption list, and ~10 GB of dead
worktree `target/` dirs the scanner kept re-walking.

Follows the pack's **inventory → categorize by consequence → report → apply**
shape (the same shape as [[tidy]] and [[branches]]). Audit checks are always
report-only and safe. Fixes are split into three consequence tiers: **safe /
default-on**, **confirm-first**, and **documented-but-manual** (printed only,
never executed).

**macOS-first, Linux-aware.** The Gatekeeper / SIP / Spotlight / Time Machine
material is macOS-only and is gated behind a `uname` check. The portable subset
— build-tree bloat, Loom concurrency vs core count, sudo posture, cache infra,
remote access — still runs and reports on Linux.

## Usage

```
/repo:host-optimize            # Audit, apply safe fixes, report; confirm-first items prompt
/repo:host-optimize --ask      # Review every finding and confirm before applying anything
/repo:host-optimize --audit    # Audit and report only — apply nothing (like /repo:audit)
```

(`--apply` is accepted as a synonym for the default, for muscle memory. On a
non-macOS host the macOS-only checks are skipped with a one-line note and the
portable subset runs unchanged.)

## Hard constraints (read before implementing)

1. **The two SIP-gated / global items — the ExecPolicy DB trim and
   `spctl --global-disable` — are PRINT-ONLY. Never shell out to perform
   either.** The ExecPolicy trim is SIP-gated (Recovery-mode only) and
   `spctl --global-disable` disables Gatekeeper machine-wide; both are printed
   as a recipe for a human to run deliberately. This is a hard constraint, not a
   nice-to-have — it is the highest-consequence part of the command.
2. **Never write to `/etc/sudoers.d/`.** The sudo-posture check only *detects*
   whether a passwordless drop-in exists and *delegates* to `/repo:sudo` (issue #49)
   or prints the manual `visudo`-validated recipe. It does not itself install a
   sudoers drop-in.
3. **Everything applied must have appeared in the report first**, and re-running
   after a clean pass must report a no-op (idempotency) rather than re-applying
   or erroring.

## Steps

### 1. Detect platform

```bash
OS="$(uname -s)"   # Darwin = macOS, Linux = Linux
CORES="$(getconf _NPROCESSORS_ONLN 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || nproc)"
```

Gate every macOS-only probe below behind `[ "$OS" = Darwin ]`. On Linux, print
`Skipping macOS-only checks (syspolicyd, Gatekeeper, Time Machine, Spotlight) on
$OS` and run only the portable subset (checks 4–8).

### 2. Audit checks (report-only, always safe)

Run each check and assign a severity consistent with [[audit]]'s scheme —
**critical** (actively causing or about to cause harm), **warn** (should fix,
not yet harmful), **info** (nice to fix). Report all findings in one table;
never mutate anything in this phase.

#### 2.1 syspolicyd health — macOS only

The feedback loop that caused the incident. Each freshly built, ad-hoc-signed
binary is a Gatekeeper cache miss (`errSecCSUnsigned`) that triggers a malware
scan re-walking enormous `target/` trees, and the ExecPolicy DB bloats over time
so every lookup gets slower.

```bash
# CPU time syspolicyd has burned vs system uptime (a high ratio = the loop is hot)
ps -axo pid,comm,time | grep -E 'syspolicyd$'
uptime
# ExecPolicy DB size — 53 MB in the incident; anything into the tens of MB is a warn
ls -lh /var/db/SystemPolicyConfiguration/ExecPolicy 2>/dev/null
# Recent malware-scan churn in the log (bounded window so this stays cheap)
log show --predicate 'process == "syspolicyd"' --last 30m --style compact 2>/dev/null \
  | grep -ci 'malware' || true
```

- **critical**: syspolicyd CPU-time / uptime ratio is high **and** malware-scan
  log churn is active — the loop is live.
- **warn**: ExecPolicy DB in the tens of MB (lookups are slowing) with no active
  churn yet.
- **info**: DB small, ratio low.

The remediation for a bloated DB is the SIP-gated trim — **printed only** in
step 4, never run here.

#### 2.2 Gatekeeper posture — macOS only

```bash
spctl --status                                   # assessments enabled/disabled
spctl developer-mode --status 2>/dev/null || true
# Developer Tools exemption list — empty list = every built binary is re-scanned
sqlite3 /var/db/SystemPolicyConfiguration/ExecPolicy \
  'select count(*) from developer_tools;' 2>/dev/null || echo 'unreadable'
```

- **warn**: Developer Tools list empty on a heavy build host (the exact
  incident condition — every ad-hoc-signed binary is a fresh scan).
- **info**: assessments enabled with a populated exemption list (normal, healthy).

#### 2.3 Backup / indexing agents — macOS only

Backup and indexing agents love churning build dirs (Backblaze ran a
`-completesync` at ~98% CPU mid-build-storm during the incident).

```bash
# Backblaze present?
ls /Library/Backblaze.bzpkg 2>/dev/null && echo 'backblaze present'
pgrep -l bztransmit 2>/dev/null || true
# Time Machine exclusions already covering build dirs?
tmutil isexcluded ./target 2>/dev/null || true
# Spotlight indexing status for this volume
mdutil -s . 2>/dev/null || true
```

- **warn**: a backup/indexing agent is present and the repo's `target/` /
  `.loom/worktrees/` dirs are **not** excluded — they will be re-walked.
- **info**: agents present but build dirs already excluded.

#### 2.4 sudo posture — portable

Detect only; **never** write to `/etc/sudoers.d/`.

```bash
USER_NAME="$(id -un)"
ls "/etc/sudoers.d/${USER_NAME}-nopasswd" 2>/dev/null && echo 'nopasswd drop-in present'
sudo -n true 2>/dev/null && echo 'passwordless sudo works' || echo 'sudo requires a password'
```

- **warn**: no passwordless drop-in on an unattended agent build host — a remote
  agent is blocked on every root-level fix.
- **info**: drop-in present and `sudo -n` succeeds.

Remediation is **delegated to `/repo:sudo` (#49)**, or the printed manual recipe —
see step 5. Never installed here.

#### 2.5 Build-tree bloat — portable

The directory trees the scanner keeps re-walking. Dead worktree `target/` dirs
are the safe-to-reclaim ones.

```bash
# target/ sizes across the repo and every loom worktree
du -sh target 2>/dev/null || true
for wt in .loom/worktrees/*/; do du -sh "$wt/target" 2>/dev/null || true; done
# Dead worktrees whose branch/issue is gone but whose target/ lingers
git worktree list --porcelain
```

- **warn**: multi-GB of `target/` under `.loom/worktrees/` belonging to worktrees
  git no longer lists (dead artifacts), or a very large main `target/`.
- **info**: only live-worktree build trees present.

Cross-reference [[tidy]] for the in-repo clutter half; this check is specifically
about the dead-worktree build artifacts the host scanner re-walks.

#### 2.6 Cache infra — portable

```bash
command -v sccache >/dev/null && sccache --show-stats 2>/dev/null || echo 'sccache not installed'
df -h . | tail -1     # disk headroom on the build volume
```

- **warn**: sccache absent (or installed but not wired into the build) on a
  repeat-build host, or disk headroom under ~10%.
- **info**: sccache working, ample headroom.

#### 2.7 Concurrency sanity — portable

```bash
# Loom's configured worktree concurrency vs core count
jq -r '.concurrency // .maxWorkers // "unset"' .loom/config.json 2>/dev/null || echo 'no .loom/config.json'
echo "cores: $CORES"
```

- **critical**: configured concurrency far exceeds cores (the incident: six
  concurrent release builds of a large Rust workspace drove load average to 118).
- **info**: concurrency at or below a sane multiple of the core count.

This check only *reports* the mismatch and suggests a value; it never edits
`.loom/config.json` (that is a deliberate operator call).

#### 2.8 Remote access — portable (with macOS specifics)

A headless build box that sleeps mid-build, or has no remote path in, is a
support problem waiting to happen.

```bash
# SSH reachable?
[ "$OS" = Darwin ] && sudo systemsetup -getremotelogin 2>/dev/null || systemctl is-active ssh sshd 2>/dev/null || true
if [ "$OS" = Darwin ]; then
  # Screen Sharing enabled?
  launchctl list 2>/dev/null | grep -qi screensharing && echo 'screen sharing on'
  # Will it sleep a headless box mid-build?
  pmset -g | grep -E 'sleep|disksleep'
fi
```

- **warn**: no remote-access path enabled, or power settings will sleep the
  machine (or its disk) during a long unattended build.
- **info**: reachable and configured to stay awake.

### 3. Report

Group findings by check and severity, in one table (mirrors [[audit]]'s format),
then list what the apply phase *will* do per tier before doing it.

```
## Host Optimize — <hostname> (Darwin, 20 cores)

### Findings
| Severity | Check            | Finding |
|----------|------------------|---------|
| critical | syspolicyd       | 14.2 CPU-hrs vs 26h uptime; malware-scan churn active |
| critical | concurrency      | .loom concurrency 6 vs 20 cores of a large Rust WS — load 118 risk |
| warn     | gatekeeper       | Developer Tools exemption list empty |
| warn     | backup agents    | Backblaze present; target/ not excluded |
| warn     | sudo posture     | no /etc/sudoers.d/<user>-nopasswd drop-in |
| warn     | build-tree bloat | 9.7 GB target/ across 4 dead worktrees; 77 GB main target/ |
| info     | cache infra      | sccache working; 41% disk free |
| info     | remote access    | SSH on, Screen Sharing on, sleep disabled |

### Will apply now (safe / default-on)
- Remove target/ of 4 dead loom worktrees (frees ~9.7 GB)
- tmutil addexclusion ./target and .loom/worktrees/*/target
- Spotlight: drop .metadata_never_index into those dirs
- Print Backblaze exclusion instructions (manual — GUI-gated)

### Will prompt (confirm-first)
- spctl developer-mode enable-terminal (+ GUI toggle you must flip)
- sudo drop-in via /repo:sudo (delegated; not yet installed)

### Printed only — never executed (documented-but-manual)
- ExecPolicy DB trim (SIP-gated, Recovery-mode recipe)
- spctl --global-disable trade-offs
```

Under `--audit`, stop here.

### 4. Documented-but-manual — PRINT ONLY, NEVER EXECUTE

These two items are printed as recipes for a human to run deliberately. The
implementation **must not** contain a code path that executes either — verify by
inspection.

**ExecPolicy DB trim (SIP-gated, Recovery mode).** When the DB has bloated (2.1),
print:

```
The ExecPolicy DB (/var/db/SystemPolicyConfiguration/ExecPolicy) is <size>.
Trimming it is SIP-gated and cannot be done from a normal session. To reset it:
  1. Reboot into Recovery (hold power on Apple silicon; Cmd-R on Intel).
  2. From Recovery Terminal, disable SIP:  csrutil disable
  3. Reboot, then from a normal session remove and let macOS rebuild the DB.
  4. Reboot into Recovery again and re-enable SIP:  csrutil enable
This is a deliberate, high-consequence procedure — do it during a maintenance
window, not mid-build.
```

**`spctl --global-disable` (disables Gatekeeper machine-wide).** Print the
trade-offs and the exact command, and require explicit human action:

```
spctl --global-disable turns off Gatekeeper assessment for the WHOLE machine.
It eliminates the ad-hoc-signing scan storm but removes malware screening for
every app on the host — a real security downgrade. If you accept that trade-off
on a dedicated, physically-secured build box, run it yourself:
  sudo spctl --global-disable
Prefer the Developer Tools exemption (step 5) first — it fixes the build-scan
churn without disabling Gatekeeper globally.
```

Never run either command from this skill.

### 5. Apply — safe / default-on (no confirmation)

Apply immediately (unless `--ask` or `--audit`), each reported as applied. All
must be idempotent — re-running skips anything already done.

- **Dead loom-worktree `target/` cleanup.** For each `target/` under
  `.loom/worktrees/` whose worktree git no longer lists, `rm -rf` it (these are
  regenerable build artifacts of a gone worktree; the [[tidy]] safety posture
  applies — never touch a live worktree's tree). Re-report bytes freed.
- **Time Machine exclusion** (macOS): `tmutil addexclusion` for `./target` and
  each `.loom/worktrees/*/target`. `tmutil isexcluded` first so a second run is a
  no-op.
- **Spotlight exclusion** (macOS): drop a `.metadata_never_index` marker into
  `target/` and the worktree build dirs (idempotent — skip if present); mention
  `mdutil -i off <volume>` as the volume-wide alternative for a dedicated box.
- **Backblaze exclusion instructions** (macOS): Backblaze's exclusions are
  GUI-gated, so **print** the steps (Backblaze Settings → Exclusions → add the
  repo `target/` and `.loom/worktrees/` paths) rather than editing its config.

### 6. Apply — confirm-first (explicit prompt before acting)

Each of these prompts for confirmation before doing anything (and under `--ask`
so does everything in step 5):

- **`spctl developer-mode enable-terminal`** (macOS): after confirmation, run it
  to add the terminal's toolchain to the Developer Tools exemption — then **tell
  the human** which GUI toggle to flip (System Settings → Privacy & Security →
  Developer Tools → enable your terminal), because the pane is GUI-gated and the
  CLI half alone is not always sufficient.
- **sudo drop-in** (portable): **delegated to `/repo:sudo` (#49)** — never
  reimplemented here. If `/repo:sudo` is installed, point the user at it (or its
  `--sudo` flag). Until #49 ships, print the manual recipe and stop:

  ```
  To grant passwordless sudo for unattended agent work, run visudo and add a
  validated drop-in (do NOT edit /etc/sudoers.d/ by hand without visudo -c):
    sudo visudo -f /etc/sudoers.d/<user>-nopasswd
    <user> ALL=(ALL) NOPASSWD: ALL
  visudo validates syntax before saving — a malformed sudoers file can lock you
  out. This skill will not write that file for you.
  ```

- **Pause / resume backup agents around a build storm** (macOS): after
  confirmation, offer to pause Backblaze (`bztransmit` / the menu-bar control)
  for the duration of a build and resume after — never silently, and always
  reversible.

### 7. Idempotency

After applying, re-run the relevant probes so the report reflects the new state.
A second back-to-back invocation must report a clean/no-op state (exclusions
already present, dead targets already gone, drop-in already detected) and must
not re-apply or error.

## Safety Rules

1. **PRINT-ONLY, NEVER EXECUTE** the ExecPolicy DB trim and
   `spctl --global-disable`. No code path may run either — this is the load-bearing
   constraint of the command.
2. **Never write to `/etc/sudoers.d/`.** Detect and delegate to `/repo:sudo` (#49)
   or print the `visudo` recipe; never install the drop-in from here.
3. **Everything applied must appear in the report first** (step 3), and safe-tier
   fixes are the only ones applied without confirmation.
4. **Never delete a live worktree's `target/`** — only `target/` belonging to
   worktrees git no longer lists. Regenerable build output only; never tracked
   files, never `.git/`.
5. **Confirm-first means confirm.** `spctl developer-mode enable-terminal`, the
   sudo delegation, and backup-agent pause/resume all require an explicit prompt;
   `--ask` extends confirmation to the safe tier too.
6. **macOS-only checks skip cleanly on non-macOS** with a one-line note; the
   portable subset (checks 4–8) still runs and reports.
7. **Idempotent** — a second run on a prepared host reports a no-op, never
   re-applies or errors.
8. **Never edit `.loom/config.json`** — the concurrency check reports a mismatch
   and suggests a value; changing it is a deliberate operator call.
