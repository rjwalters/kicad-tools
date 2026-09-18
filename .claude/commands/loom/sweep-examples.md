# Sweep — Worked invocation examples

> **Reference file for [`sweep.md`](sweep.md)**, the `/loom:sweep` dispatcher.
>
> **Load when:** the resolved mode or flag combination is unclear and you want a worked example, or you are explaining the invocation surface to an operator. Never required to execute a run.
>
> **Flat, one level deep.** `sweep.md` names every file a given run needs up
> front; nothing here requires opening a *third* file to follow its own
> procedure. Cross-references worded "above"/"below" that do not resolve inside
> this file point at a sibling `sweep-*.md` section — see the reference-file map
> in [`sweep.md`](sweep.md). The body below is **verbatim** from the pre-split
> `sweep.md` (#7726): no rule, warning, edge case, table, or code block was
> reworded, softened, reordered, or dropped.

## Contents

- [Examples](#examples)
  - [Mode A — Explicit numeric list (fast path)](#mode-a--explicit-numeric-list-fast-path)
  - [Build everything — the `all` sentinel](#build-everything--the-all-sentinel)
  - [Mode B — Natural-language description](#mode-b--natural-language-description)
  - [Clarification triggers (Mode B asks before spawning)](#clarification-triggers-mode-b-asks-before-spawning)
  - [Mode C — PR-set mode (explicit `--prs` flag)](#mode-c--pr-set-mode-explicit---prs-flag)
  - [Mode C — PR-set mode (NL trigger, no flag)](#mode-c--pr-set-mode-nl-trigger-no-flag)
  - [Headless / non-interactive runs (`--yes`)](#headless--non-interactive-runs---yes)

---

## Examples

### Mode A — Explicit numeric list (fast path)

```bash
/loom:sweep 123                                    # Sequential lifecycle for issue 123
/loom:sweep 123 456 789                            # Sequential lifecycle for three issues
/loom:sweep #1083 #1080                            # Leading # is allowed
/loom:sweep 123 456 789 --builders-per-wave 2      # Two builders per wave (recommended)
/loom:sweep 1 2 3 4 5 6 --builders-per-wave 3      # Three builders per wave (validated)
/loom:sweep 1 2 --builders-per-wave 5              # Silently clamps to 2 (candidate count)
/loom:sweep 123 456 789 --dry-run                  # Print plan and EXIT without mutating
/loom:sweep 1 2 3 4 5 --dry-run --builders-per-wave 2  # Preview with wave grouping
/loom:sweep 123 456 --no-daemon                    # Force in-process subagent dispatch even when daemon is up (#3454)
```

### Build everything — the `all` sentinel

```bash
# Fast/sloppy "promote and sweep everything": resolves EVERY open issue and
# aggressively drives each toward a merged PR — curating uncurated issues,
# reclaiming stale loom:building claims, probing loom:blocked issues for a
# cleared blocker, fanning loom:epic containers out to their phase children,
# and driving any existing open PR through Judge / Doctor → Merge. Only
# loom:operator-only and loom:needs-capability issues are hard-skipped.
# Displays the resolved plan and awaits confirmation before dispatching.
/loom:sweep all

# Case-insensitive — ALL / All also trigger the sentinel
/loom:sweep ALL

# Preview the whole-backlog plan (per-issue action + wave grouping) without mutating
/loom:sweep all --dry-run

# Same aggressive set, two builders per wave
/loom:sweep all --builders-per-wave 2

# Every open PR, driven through Judge / Doctor → Merge per its current label (Mode C)
/loom:sweep all --prs
/loom:sweep all --prs --dry-run

# NOT the sentinel — >1 non-flag token, still routes to Mode B exactly as before
/loom:sweep all open loom:issue items
/loom:sweep all my agent-filed loom:issue items --builders-per-wave 2
```

### Mode B — Natural-language description

```bash
# Label filter — translates to: gh issue list --label loom:curated --state open --limit 100
/loom:sweep all loom:curated issues

# Compound label + author + time filter — translates to:
#   gh issue list --label loom:curated --author rjwalters \
#                 --search "created:>=2026-05-17" --state open --limit 100
/loom:sweep all loom:curated issues filed by rjwalters in the last week

# Title search on a label-filtered set — translates to:
#   gh issue list --label loom:issue --search "docs in:title" --state open --limit 100
/loom:sweep loom:issue items with 'docs' in the title

# "My" → --author @me (Loom files but does not self-assign):
/loom:sweep all my agent-filed loom:issue items --builders-per-wave 2

# Mixed mode — union of explicit numbers AND an NL-derived set:
/loom:sweep #3310 #3312 and any other loom:issue with 'docs' in the title

# Dry-run a NL-derived candidate set before committing to side effects:
/loom:sweep all loom:curated issues --dry-run
```

### Clarification triggers (Mode B asks before spawning)

```bash
# Ambiguous time window — asks "what duration do you mean?"
/loom:sweep recent loom:issue items

# Out-of-band query — gh issue list cannot inspect file paths in the diff
/loom:sweep issues labeled loom:issue except the ones touching loom-daemon

# Unknown label — 'bug' is not in the repo's label set (from `gh label list`); ask which label was meant
/loom:sweep all my agent-filed bugs that aren't blocked

# Pure nonsense — no derivable candidate set
/loom:sweep nonsense gibberish

# Ambiguous between Mode B (issues) and Mode C (PRs) — loom:review-requested
# is PR-only but the description does not say "PRs". Ask which was meant.
/loom:sweep all loom:review-requested
```

### Mode C — PR-set mode (explicit `--prs` flag)

```bash
# Explicit numeric PR list — each PR routed by its current label
# (review-requested → Judge, changes-requested → Doctor→Judge, loom:pr → Merge)
/loom:sweep --prs 100 101 102

# Leading # is allowed
/loom:sweep --prs #100 #101 #102

# Single PR — back-half-only handling (Judge → Doctor → Merge) for that PR
/loom:sweep --prs 100

# Dry-run a PR-set plan — prints per-PR action plan and EXITs without mutating
/loom:sweep --prs 100 101 102 --dry-run

# NL description with explicit flag — translates to: gh pr list --label loom:pr --state open --limit 100
/loom:sweep --prs all open loom:pr

# Compound filter — translates to:
#   gh pr list --label loom:review-requested --author @me --state open --limit 100
/loom:sweep --prs all my review-requested PRs
```

### Mode C — PR-set mode (NL trigger, no flag)

```bash
# "PRs" in the description selects Mode C even without --prs:
# translates to: gh pr list --label loom:pr --state open --limit 100
/loom:sweep all open loom:pr PRs

# "pull requests" also triggers Mode C:
/loom:sweep all loom:review-requested pull requests

# "merge-ready PRs" triggers Mode C:
/loom:sweep all merge-ready PRs
```

### Headless / non-interactive runs (`--yes`)

```bash
# A hand-rolled operator script, e.g. an unattended cron job on a box the
# operator has already told "launch a wave on the issues that are ready" and
# is not watching in real time. Without --yes this would stall forever on the
# Mode B confirmation gate (no one to answer the prompt); the pre-approval
# still prints the full resolved candidate set, overlap/operator-gate
# advisories, and orphan-claim recovery status before proceeding.
claude -p "/loom:sweep all loom:curated issues --yes" --dangerously-skip-permissions

# Same idea for a Mode C NL description:
claude -p "/loom:sweep all loom:review-requested PRs --yes" --dangerously-skip-permissions

# --dry-run wins over --yes with no special handling: prints the plan and
# EXITs before the confirmation gate is ever reached.
/loom:sweep all loom:curated issues --dry-run --yes

# CAUTION: `all --yes` is a whole-backlog auto-dispatch — every open issue,
# aggressively promoted and driven toward a merged PR, with nobody reading
# the plan first. Reserve it for operators who mean exactly that.
/loom:sweep all --yes
```

