# Accelerate

Push **one project** forward fast with **this machine's** resources: an attended,
session-only loop over `/loom:sweep all` and `/loom:sweep all --prs`, sized to
localhost, running alongside whatever fleet already works the repo.

**Arguments**: `$ARGUMENTS`

```
/loom:accelerate [--dry-run] [--yes] [--max-parallel N]
```

This is a thin driver over the sweep skill (`sweep.md` and its `sweep-*.md`
siblings). It adds **no lifecycle logic** of its own: every stage, safety rail and
stop condition below is sweep's, reached by invoking sweep.

## Contract

- **Session-only.** Every sweep this skill runs carries `--no-daemon`:
  in-process subagents only. It makes no daemon dispatch call, and it never
  installs, replaces, restarts or reconfigures a daemon or any hook on this
  machine. It does not deregister the workspace from any fleet host.
- **Adds to the fleet, never races it.** `--no-daemon` governs dispatch only: a
  fleet daemon keeps working the same repo. Sweep's Step 1b lease check already
  skips any item a live peer holds (exit `4`); rely on it. Do not work around it.
- **Same rails as sweep.** `loom:operator` / `loom:frozen` items are skipped, merges
  go through `./.loom/scripts/merge-pr.sh` only, and sweep's Doctor-cycle cap and
  spend/credit fallbacks apply unchanged.
- **Attended.** Run it in a session an operator can interrupt. Unattended runs
  belong to `/loom:watch` or the daemon.

## Step 1 — Size to localhost

Source the helper and measure. An unmeasurable probe is *unknown*, not zero
(#4164): pass the literal `unknown` and that clamp is skipped.

```bash
source ./.loom/scripts/lib/disk-headroom.sh
CORES="$(loom_detect_cores)"
RAM_GB="$(loom_detect_ram_gb 2>/dev/null || echo unknown)"
FREE_GB="$(loom_worktree_root_free_gb "$(pwd)" 2>/dev/null || echo unknown)"
# A configured build-dir scratch volume (e.g. CARGO_TARGET_DIR) also takes the
# parallel builds: size to the tighter of the two volumes.
if [ -n "${CARGO_TARGET_DIR:-}" ] && [ -d "$CARGO_TARGET_DIR" ]; then
  T_GB="$(df -Pk "$CARGO_TARGET_DIR" 2>/dev/null | awk 'NR==2 && $4 ~ /^[0-9]+$/ {print int($4/1024/1024)}')"
  if [ -n "$T_GB" ] && { [ "$FREE_GB" = unknown ] || [ "$T_GB" -lt "$FREE_GB" ]; }; then FREE_GB="$T_GB"; fi
fi
```

Then resolve the issue-set parallelism once the candidate count is known (the
plan in Step 2 gives it). `MAX_PAR` is the `--max-parallel N` value, or empty:

```bash
{ read -r BUILDERS; read -r LIMIT; } < <(loom_localhost_parallelism "$CORES" "$RAM_GB" "$FREE_GB" "$CANDIDATES" "$MAX_PAR")
```

`BUILDERS` is passed to sweep as an explicit `--builders-per-wave "$BUILDERS"`,
which overrides sweep's own auto sizing. Without `--max-parallel` it stays inside
sweep's core-scaled band `[3, 6]`; an explicit `--max-parallel N` is the only way
past 6, and sweep's existing `N>6` warning still prints. `LIMIT` names what bound
the result: `cores`, `ram`, `disk`, `candidates`, `max_parallel` or `floor`.
Honour the repo's build-dir rules by passing the environment through unchanged.

The PR-set half (`--prs`) is size-1 and ignores `--builders-per-wave`; the plan
says so.

## Step 2 — Show the plan, ask once

Run `/loom:sweep all --dry-run --no-daemon` and `/loom:sweep all --prs --dry-run --no-daemon`
and print, together:

- candidate counts per phase (curate, promote, build, Judge, Doctor, merge) for both halves;
- operator-gated items skipped (`loom:operator`, `loom:frozen`);
- the resolved parallelism and its limiting factor (`BUILDERS`, `LIMIT`; and that it applies to the issue-set half only).

`--dry-run` stops here. Otherwise ask the operator **once** to proceed; `--yes`
prints the same plan and skips the wait. Declining exits cleanly. Sweep's own
per-pass gate is pre-approved with `--yes` from here on, since this is the one
confirmation.

## Step 3 — Loop

Repeat passes until a stop condition. Each pass runs, in order:

1. `/loom:sweep all --no-daemon --yes --builders-per-wave "$BUILDERS"` (issue set)
2. `/loom:sweep all --prs --no-daemon --yes` (open PRs)

Re-resolve `BUILDERS` at the start of each pass (the candidate count and host
load change). After each pass print a short summary:

```
pass <n>: issues <built/merged/skipped>, PRs <judged/merged/skipped>, peer-held skipped <k>, parallelism <BUILDERS> (<LIMIT>)
```

`peer-held skipped` is the number of items sweep skipped at Step 1b because a live
peer lease held them; read it from the sweep summary and sum both halves. If the
sweep summary does not expose it, count the Step 1b exit-`4` skips from the run.

**Stop** when any of these holds:

- both halves of a pass report **nothing actionable** (no candidate was built,
  reviewed, fixed or merged, other than peer-held or operator-gated skips);
- a sweep **stop condition** fires (see `sweep-run-hygiene.md`) or spend/credit
  limits are hit;
- the operator interrupts or declines.

Never loop without a bound: a pass that changed nothing ends the run.

## Step 4 — Final summary

Print the totals across passes, the peer-held skip total, and why the loop
stopped. Items left because they are operator-gated or peer-held are listed, not
retried.
