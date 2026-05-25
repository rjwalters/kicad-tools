# Sweep

Process an explicit list of issues through the full shepherd lifecycle from the current Claude session — no external daemon required. Runs sequentially by default, or in **parallel waves** of up to `N` builders when `--builders-per-wave N` is supplied. Supports `--dry-run` to preview the candidate plan without mutating anything.

> **Scope.** This skill accepts either an explicit list of issue numbers or a natural-language description of which issues to process, and runs them through the full lifecycle in waves. Supports `--dry-run` to preview the plan without mutations. Other knobs sketched in #3298 are **deliberately deferred** — see "Limitations" below.
>
> If you need fully autonomous orchestration with work generation, use `/loom`. If you need a single-issue lifecycle, use `/shepherd <N>`. `/sweep` exists for the in-between case: "I have these N issues, run them in this session, without spinning up a daemon."

## Arguments

**Arguments**: $ARGUMENTS

`$ARGUMENTS` is interpreted in one of **two modes**, chosen by inspection of the non-flag tokens. Before classifying, **strip all recognized flag tokens** (`--builders-per-wave N`, `--dry-run`) from the token list — flags are honoured in both modes.

### Mode A — Explicit numeric list (fast path, regression guard)

If **every** whitespace-separated non-flag token matches the regex `^#?\d+$` (a positive integer with an optional leading `#`), treat the arguments as today's explicit issue list. **No LLM interpretation, no extra `gh` calls.** This is the MVP behaviour and must remain bit-for-bit compatible — `/sweep 123 456` and `/sweep #123 #456` continue to work exactly as before.

### Mode B — Natural-language interpretation

Otherwise, treat `$ARGUMENTS` as an English description of which open issues to process. The orchestrator (Claude, this session) translates the description into one or more `gh issue list` invocations using the appropriate flags, surfaces the derived candidate set, awaits user confirmation, then proceeds with the rest of the lifecycle exactly as in Mode A.

**This is deliberately not a formal grammar.** There is no parser, no operator precedence, no fixed vocabulary. The orchestrator reads the description and picks reasonable `gh issue list` flags. The interpretation rules below are prose, not a spec.

**Translation guide — common NL fragments to `gh issue list` flags** (verified against `gh` v2):

| NL fragment | `gh issue list` flag(s) |
|-------------|------------------------|
| "labeled `loom:curated`" / "all `loom:curated` issues" | `--label loom:curated` |
| "filed by rjwalters" | `--author rjwalters` |
| "all my ..." / "my agent-filed ..." | `--author @me` (NOT `--assignee` — Loom files but does not self-assign) |
| "in the last week" / "from the last N days" | `--search "created:>=YYYY-MM-DD"` (compute the date) |
| "with 'docs' in the title" | `--search "docs in:title"` |
| "open" (always assumed) | `--state open` (the default) |
| "closed too" | `--state all` |

Combine flags as needed. Always pass `--state open` explicitly (default) unless the user asks for closed issues. Default to `--limit 100` rather than the `gh` default of `30` to avoid silent truncation (see edge case below).

**Mixed mode is supported.** `/sweep #3310 #3312 and any other loom:issue with 'docs' in the title` should be interpreted as the union of `{3310, 3312}` and the `gh issue list --label loom:issue --search "docs in:title"` result. Because the tokens contain non-numeric words, this falls into Mode B and the orchestrator handles the union.

**Unknown-label guard.** Loom never invents labels (CLAUDE.md "Never create new GitHub labels"). If the description mentions a label not present in `.github/labels.yml` (e.g. `bug` in this repo), **do not** silently fabricate a `--label bug` filter — ask the user to clarify which existing label they meant, or supply explicit issue numbers.

### Edge cases (prose rules, applied in either mode but mostly relevant to Mode B)

1. **Zero matches.** Print the derived `gh issue list` command and its empty result, then EXIT cleanly. Do not spawn any agents and do not fall through to Mode A.
2. **More than the result cap.** `gh issue list` defaults to `--limit 30`; this skill should pass `--limit 100` explicitly. If results still hit the cap (100 candidates), print a warning that the result set was truncated and ask the user to narrow the description before proceeding. Do not silently process only the first 100.
3. **Out-of-band queries** (anything `gh issue list` cannot express by itself — body-content searches, file-touch queries like "issues touching `loom-daemon`", "issues without tests", repository-diff inspection). These require per-issue body or diff inspection, which is **out of scope for this skill**. Ask the user to clarify or supply explicit issue numbers. Do **not** attempt heuristic per-issue inspection here.
4. **Ambiguous time windows** ("recent", "lately", "this sprint"). Ask the user to specify a concrete date or duration rather than guessing. The translation table above only covers concrete forms ("last week", "last N days") which compute deterministically.

### Optional flags

- **`--builders-per-wave N`** — dispatch up to `N` builders in parallel per wave. Default `1` (fully sequential, matching the MVP behaviour). `N` must be an integer `>= 1`. Honoured in both modes — flag tokens are stripped before the Mode A / Mode B classification.
- **`--dry-run`** — print the planned candidate list (with wave grouping) and EXIT without performing any mutation. Recognized as a bare flag token (no value). May appear anywhere in `$ARGUMENTS`. Default is off. Honoured in both modes — stripped before classification along with other flags.

### Validation rules

- Recognize `--dry-run` as a flag token anywhere in `$ARGUMENTS`, strip it from the candidate list before validation, and store it as a boolean (`DRY_RUN=true` if present, else `false`).
- At least one candidate (numeric token or NL description) must be supplied. If `$ARGUMENTS` (after stripping flag tokens) is empty, display:
  ```
  Usage: /sweep <issue-number> [<issue-number> ...] [--builders-per-wave N] [--dry-run]
         /sweep <natural-language description>     [--builders-per-wave N] [--dry-run]

  See #3298 for the full design.
  ```
  and EXIT.
- **Mode A** (every non-flag token matches `^#?\d+$`):
  - Strip leading `#` from each token, parse as a positive integer.
  - Reject any token that fails to parse as a positive integer (after stripping). Display an error showing the offending token and EXIT.
  - Deduplicate the issue list (preserve first-seen order).
- **Mode B** (any non-flag token does not match `^#?\d+$`):
  - Translate the description to `gh issue list` invocation(s) per the guide above.
  - Run the command, deduplicate, and **display the candidate set to the user before spawning any agents.** Await confirmation. If the user declines, EXIT cleanly.
  - If the description is ambiguous, hits an out-of-band query, or references an unknown label, ask for clarification first — do not guess.
- **`--builders-per-wave N` validation:**
  - Parse `N` as an integer. Reject non-integer values with a clear error and EXIT.
  - Reject `N < 1` (including `0` and negative values) with: `Error: --builders-per-wave must be >= 1 (got: <N>)` and EXIT. Do **not** silently default to `1`.
  - If `N > 3`, print a warning and continue: `WARNING: --builders-per-wave=<N> is unvalidated. N<=3 is recommended; N>=4 may exhaust context or hit rate limits. Proceeding at your own risk.`
  - If `N` exceeds the number of candidates at any wave, **silently clamp** to the candidate count for that wave. Do not warn, do not stall.

**Wave-size guidance:**

| `N` | Status |
|-----|--------|
| `1` | Default. Fully sequential (MVP-compatible). |
| `2` | **Recommended** starting point for parallel waves. |
| `3` | Tested and validated. Fine for routine use. |
| `>= 4` | Unvalidated. Warns at parse time. Operator discretion. |

The cap is **soft** — there is no hard upper bound. The warning is the only guard.

## Examples

### Mode A — Explicit numeric list (fast path)

```bash
/sweep 123                                    # Sequential lifecycle for issue 123
/sweep 123 456 789                            # Sequential lifecycle for three issues
/sweep #1083 #1080                            # Leading # is allowed
/sweep 123 456 789 --builders-per-wave 2      # Two builders per wave (recommended)
/sweep 1 2 3 4 5 6 --builders-per-wave 3      # Three builders per wave (validated)
/sweep 1 2 --builders-per-wave 5              # Silently clamps to 2 (candidate count)
/sweep 123 456 789 --dry-run                  # Print plan and EXIT without mutating
/sweep 1 2 3 4 5 --dry-run --builders-per-wave 2  # Preview with wave grouping
```

### Mode B — Natural-language description

```bash
# Label filter — translates to: gh issue list --label loom:curated --state open --limit 100
/sweep all loom:curated issues

# Compound label + author + time filter — translates to:
#   gh issue list --label loom:curated --author rjwalters \
#                 --search "created:>=2026-05-17" --state open --limit 100
/sweep all loom:curated issues filed by rjwalters in the last week

# Title search on a label-filtered set — translates to:
#   gh issue list --label loom:issue --search "docs in:title" --state open --limit 100
/sweep loom:issue items with 'docs' in the title

# "My" → --author @me (Loom files but does not self-assign):
/sweep all my agent-filed loom:issue items --builders-per-wave 2

# Mixed mode — union of explicit numbers AND an NL-derived set:
/sweep #3310 #3312 and any other loom:issue with 'docs' in the title

# Dry-run a NL-derived candidate set before committing to side effects:
/sweep all loom:curated issues --dry-run
```

### Clarification triggers (Mode B asks before spawning)

```bash
# Ambiguous time window — asks "what duration do you mean?"
/sweep recent loom:issue items

# Out-of-band query — gh issue list cannot inspect file paths in the diff
/sweep issues labeled loom:issue except the ones touching loom-daemon

# Unknown label — 'bug' is not in .github/labels.yml; ask which label was meant
/sweep all my agent-filed bugs that aren't blocked

# Pure nonsense — no derivable candidate set
/sweep nonsense gibberish
```

## Execution Model

`/sweep` processes the issue list in **waves**:

- The candidate list is partitioned into waves of up to `N = --builders-per-wave` issues (default `1`).
- Issues are picked into waves in the order given. Within a wave, builders are dispatched in parallel; across waves, processing is sequential.
- **Each wave fully settles** (all builders → per-PR Judge → optional Doctor → merge) before the next wave starts.

### CRITICAL: One level deep — never spawn `/shepherd` as a subagent

`/sweep` dispatches `loom-builder`, `loom-judge`, and `loom-doctor` subagents **directly from this orchestrator session** in a single tool-call block. This is **one level deep** and is empirically safe for `N` up to at least 3.

**Do NOT, under any circumstances, dispatch `/shepherd` as a subagent from `/sweep`.** That would be two levels deep (parent Claude → `/shepherd` Task → builder/judge Task) and triggers the parallel-shepherd stall hazard tracked in #3289 (stream-pump dies on parallel grandchildren). The wave loop in this skill is the architectural answer to that race — preserve it.

Concretely, when this skill says "dispatch builders for the wave", that means: in a single tool-call block, invoke `loom-builder` once per issue in the wave (e.g., three parallel `Task` calls if `N=3`). It does **not** mean invoke `/shepherd` three times.

If a future maintainer is tempted to "simplify" by replacing the wave-loop with parallel `/shepherd` calls: don't. Read #3289, then read this section again.

### Other constraints

- **Do NOT write to `.loom/daemon-state.json`.** That file is owned by the standalone daemon. `/sweep` runs independently and must not race with the daemon on shepherd-slot bookkeeping. Reading `daemon-state.json` for situational awareness is fine; writing is not.

## 0. Dry-run gate (if `--dry-run`)

If `--dry-run` was supplied, **this stage runs before any mutation** and EXITs after printing the plan. The dry-run gate is the single inviolable contract of `--dry-run`: no label edits, no `worktree.sh` invocation, no `gh pr create`, no `merge-pr.sh`, no daemon-state writes, no Task/subagent dispatch.

**Procedure:**

1. **Survey each candidate (read-only).** For every deduplicated, validated issue number `N` in the candidate list:
   ```bash
   gh issue view N --json number,title,labels,state --jq '{number, title, state, labels: [.labels[].name]}'
   ```
   This is a `gh issue view` read — it does not mutate anything. (If `gh` is unauthenticated or the issue is unreachable, log the error against that candidate and continue surveying the rest.)

2. **Compute wave partition.** Partition the candidate list into waves of size `--builders-per-wave` (default `1`), preserving input order. Record `(issue, wave_index, total_waves)` for each candidate. Apply the same silent-clamp and pre-flight-skip rules that the live path uses (closed / `loom:building` / `loom:blocked` issues are tagged as "would skip" in the plan but still appear in the output for transparency).

3. **Print the plan.** Emit a table or block per the format below.

4. **EXIT.** Do not proceed to "Wave Lifecycle". The shell must return as soon as the plan is printed.

**Output spec** (minimum useful — do **not** add token-pool selection or agent dispatch internals):

```
/sweep --dry-run plan: M candidate(s) across W wave(s) (--builders-per-wave=N)

  Wave 1:
    #123  "Add foo widget"                labels: loom:issue                    → would build
    #124  "Fix bar bug"                   labels: loom:curated                  → would curate, build
  Wave 2:
    #125  "Refactor baz module"           labels: loom:building                 → would skip (already in flight)
    #126  "Document quux"                 labels: (none)                        → would curate, build

Total: 3 would-build, 1 would-skip. No issues were modified.
```

**Per-candidate fields (required):**
- Issue number
- Title (truncated reasonably if very long)
- Current labels (comma-separated, or `(none)`)
- Planned action (`would build`, `would curate, build`, `would skip (<reason>)`)
- Wave assignment (shown via the `Wave N:` group header)

**Footer (required):** total candidates, total waves, count of `would-build` vs `would-skip`, and an explicit confirmation that nothing was modified.

**Explicitly out of scope for dry-run output** (do not add these — see Limitations):
- Token-pool / account selection internals
- Subagent dispatch order or parallelism counts beyond wave size
- Persisting the plan to disk
- Diffing this plan against a previous or actual sweep

**Verifying "nothing mutates":**

```bash
# Before:
LABELS_BEFORE=$(gh issue view N --json labels --jq '[.labels[].name]|sort')
PRS_BEFORE=$(gh pr list --state open --json number --jq '[.[].number]|sort')
WORKTREES_BEFORE=$(ls .loom/worktrees/ 2>/dev/null | wc -l)
# Run: /sweep --dry-run N
# All three must be unchanged after the dry-run returns.
```

These three checks — label set per candidate, open PR set, worktree count — are the acceptance criteria. If any of them differ pre/post a `--dry-run` invocation, the dry-run gate is broken.

## Wave Lifecycle

For each wave `W` (partition of the issue list into chunks of up to `--builders-per-wave` candidates, processed in given order), execute the full lifecycle below. **All stages are mandatory** for every issue — do not skip any stage (CLAUDE.md "Shepherd Lifecycle (MANDATORY)").

See `.claude/commands/loom/shepherd-lifecycle.md` for the canonical phase-by-phase reference, label state machine, and recovery procedures. The summary below tells you which skill to invoke at each phase; the lifecycle reference tells you what each phase does in detail.

### 1. Per-issue pre-flight (still per-issue, before the wave dispatch)

For each issue `N` in the wave, before any role skill is invoked:

1. **Verify the issue is open and not already in flight.**
   ```bash
   gh issue view N --json state,labels --jq '{state: .state, labels: [.labels[].name]}'
   ```
   - If the issue is closed, skip it (log a warning). It does NOT contribute to this wave.
   - If the issue already has `loom:building`, skip it — another shepherd or builder is working on it. Log a warning. Does NOT contribute to this wave.
   - If the issue has `loom:blocked`, skip it. Log a warning. Does NOT contribute to this wave.

2. **Read the issue body before briefing any builder.** This is a non-negotiable rule from prior sweep sessions (a misleading title hid the real requirement in the body).
   ```bash
   gh issue view N --json title,body
   ```

> **Pre-flight skip rule.** If `K` of the wave's `N` candidates are skipped at pre-flight, dispatch only `N - K` builders for this wave. **Do not pull a candidate forward** from the next wave to backfill. Wave boundaries stay clean, and the next wave runs at its originally planned size.

### 2. Curator phase (still per-issue, before the wave dispatch)

For each surviving issue `N` in the wave:

- If the issue does not already have `loom:curated` or `loom:issue`, run the curator skill on it.
  - Load and follow the instructions in `.claude/commands/loom/curator.md` for issue `N`.
  - Expected exit state: issue has `loom:curated`.
- If the issue already has `loom:curated` or `loom:issue`, skip the curator phase.

Curator runs sequentially per-issue within wave setup — it is cheap and does not benefit from parallelism here.

### 3. Approval gate (per-issue)

Each issue must reach `loom:issue` before the Builder can claim it.

- If the issue already has `loom:issue`, proceed.
- Otherwise, promote it:
  ```bash
  gh issue edit N --remove-label "loom:curated" --add-label "loom:issue"
  ```

### 4. Builder phase (parallel within the wave)

Dispatch up to `min(--builders-per-wave, surviving-candidates-in-wave)` `loom-builder` subagents **in a single tool-call block** from this orchestrator session. **Do NOT invoke `/shepherd` as a subagent here** — see the "One level deep" rule in Execution Model above.

Each builder is responsible for:

- Claiming its issue (`loom:issue` → `loom:building`).
- Creating an issue worktree via `./.loom/scripts/worktree.sh N`.
- Implementing the change, running tests, committing.
- Pushing the branch and opening a PR labeled `loom:review-requested`.
- Closing references: `Closes #N` in the PR body.

**Await all builders in the wave** before proceeding to Judge. Collect each builder's PR number (or failure marker).

**Per-builder failure isolation.** If builder for issue `#A` fails to open a PR (build error, test failure, unrecoverable conflict, etc.), log it and **continue** with the other builders' PRs in this wave. The failed issue is recorded as `blocked (builder failed)` in the summary. Do NOT abort the wave. Do NOT skip Judge for the other PRs.

### 5. Judge phase (sequential per PR within the wave)

For each PR successfully opened in the wave, in the order the builders completed (or any deterministic order — wave-internal ordering is not load-bearing), run the Judge phase sequentially:

```
for pr in wave_prs:
    judge(pr)               # may approve or request changes
    if changes_requested:
        doctor(pr)          # one Doctor->Judge cycle (see step 6)
    if still_approved:
        merge(pr)           # step 7
```

- Load and follow the instructions in `.claude/commands/loom/judge.md` for the PR.
- The judge uses `gh pr comment` (NOT `gh pr review --approve`) because GitHub's self-review API restriction applies — see `judge.md` for the full explanation.
- Expected exit states per PR:
  - **Approve** → PR labeled `loom:pr`. Continue to Merge (step 7) for this PR, then advance to the next PR in the wave.
  - **Request changes** → PR labeled `loom:changes-requested`. Continue to Doctor (step 6) **inline for this PR**, then re-judge, then merge or block.

**Why sequential and not parallel?** Parallel Judges add coordination complexity without clear benefit — each judge needs to checkout the PR and reason about it independently. Defer parallel-judge to a future issue if benchmarks justify it.

### 6. Doctor phase (inline per PR, only if Judge requested changes)

If Judge requests changes on PR `#X` mid-wave, run a **single inline Doctor→Judge cycle** for `#X` before moving to the next PR's Judge:

- Load and follow the instructions in `.claude/commands/loom/doctor.md` for PR `#X`.
- Doctor addresses the judge's feedback, commits the fixes, and pushes.
- On completion, re-label the PR from `loom:changes-requested` back to `loom:review-requested` and **re-run the Judge phase** (step 5) for this PR.
- **Limit: a single Doctor→Judge cycle per PR.** If Judge still requests changes after one Doctor pass, mark this PR as blocked, log a warning, and proceed to the next PR in the wave (do NOT block the wave on it).

The Doctor cycle for `#X` does **not** block other PRs in the wave — but because Judge runs sequentially per-PR within the wave, the next PR's Judge waits for `#X`'s Doctor→Judge cycle to settle before it starts. This is the intended sequencing.

### 7. Merge (per PR)

Use the dedicated merge script (CLAUDE.md "Merging PRs" mandate — never `gh pr merge`):

```bash
./.loom/scripts/merge-pr.sh <PR_NUMBER> --auto
```

The script merges via the forge API and cleans up the worktree.

### 8. Wave settled → advance to next wave

Once every PR in the wave has reached a terminal state (merged, blocked, or builder-failed), advance to the next wave. Do not start the next wave's builders until the current wave's PRs are all settled.

## Summary Output

When the entire list has been processed, print a summary table that includes wave membership for each issue:

```
/sweep complete. Processed M issue(s) across W wave(s):

  #123  → merged  (PR #456)                                              [wave 1]
  #124  → blocked (judge requested changes, doctor cycle exhausted)      [wave 1]
  #125  → skipped (already in flight: loom:building)                     [wave 1]
  #126  → blocked (builder failed: build error)                          [wave 2]
  #127  → merged  (PR #459)                                              [wave 2]

Total: 2 merged, 2 blocked, 1 skipped.
```

Wave annotation makes it easier to triage failures (e.g., "every issue in wave 2 failed → probably a base-branch problem, not the issues themselves").

## Stop Conditions

Stop processing and print the summary when any of these conditions hold:

- The issue list is exhausted.
- The user interrupts (Ctrl-C or explicit stop).
- An unrecoverable error occurs (e.g., `gh` is not authenticated, repository state is broken). Log the error and exit.

This skill does **not** implement disk-pressure checks, max-waves caps, or doctor-cycle global limits — those are deferred (see Limitations).

## Daemon Coexistence

`/sweep` does not require the daemon and does not interact with `.loom/daemon-state.json` for writes. If the daemon is running, `/sweep` and the daemon may both try to claim the same `loom:issue` label.

**Coexistence behavior:** before the first wave, check whether the daemon is running. If it is, warn the user once at the start of the sweep:

```bash
PID=$(cat .loom/daemon-loop.pid 2>/dev/null)
if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
  echo "⚠️  Loom daemon is running (PID $PID). /sweep will race with the daemon"
  echo "   for issues in the loom:issue queue. Consider stopping the daemon first:"
  echo "       ./.loom/scripts/daemon.sh stop"
fi
```

Do not auto-stop the daemon. Do not block on this warning — proceed with the sweep.

Per-issue, the pre-flight check (step 1) already detects `loom:building` and skips, which is the natural defense against races: if the daemon claimed an issue first, `/sweep` will see `loom:building` and skip.

## Constraints

- **Wave model, one level deep.** When `--builders-per-wave > 1`, dispatch `loom-builder` / `loom-judge` / `loom-doctor` subagents **directly from this orchestrator session** in a single tool-call block. **Never invoke `/shepherd` as a subagent from `/sweep`** — that is the two-levels-deep pattern that triggers the #3289 stall. See "CRITICAL: One level deep" in the Execution Model.
- **Per-PR Judge is sequential within a wave.** Builders parallelize, judges do not. Don't parallelize judges without a separate design pass.
- **Single Doctor→Judge cycle per PR.** Inline within the wave. If it still fails, the PR is blocked — do not retry indefinitely.
- **No new labels.** Use only the existing Loom label set (see `.github/labels.yml`).
- **No `gh pr merge`.** Always use `./.loom/scripts/merge-pr.sh`.
- **No daemon-state writes.** Read-only access to `daemon-state.json` for situational awareness.
- **Read the issue body** (`gh issue view N --json body`) before briefing the builder. Don't rely on the title.
- **Skip operator-only issues** (issues that require human action, such as releases or credential changes). Log and move on.

## Limitations (Deferred for Follow-up Issues)

The full `/sweep` design in #3298 includes many features that are intentionally **not** part of this skill yet. Each of these is a candidate follow-up issue:

| Feature | Status | Notes |
|---------|--------|-------|
| Parallel waves (`--builders-per-wave N`) | **Implemented (#3316)** | Soft cap at N=3 (warns above). One level deep — no `/shepherd` subagent. |
| Natural-language selectors (label/author/title/time-window filters via NL description) | **Implemented (#3318)** | Mode B in Arguments. Out-of-band queries (body/diff inspection, file-touch filters) still trigger clarification. |
| `--dry-run` | **Implemented (#3319)** | Prints the candidate plan (with wave grouping) and exits without mutating labels, worktrees, or PRs. |
| `--max-waves` cap | Deferred | Operator-level brake on long sweeps. |
| `--paused-merge` / `--no-judge` | Deferred | Merge-mode variants for trusted batches. |
| `--include-blocked` (unblock pass) | Deferred | Currently `/sweep` skips `loom:blocked` issues outright. |
| `--curator-also` (parallel curators on `loom:triage`) | Deferred | Parallel triage is a separate orchestration question. |
| Config-driven defaults (`.loom/config.json` keys `sweep.*`) | Deferred | No knobs to configure yet. |
| Disk-pressure stop condition | Deferred | Wave sequencing limits disk usage; revisit if waves grow large. |
| Doctor-cycle counting across PRs | Deferred | Single Doctor→Judge cycle limit per PR is enforced inline. |
| Parallel Judges within a wave | Deferred | Sequential per-PR Judge today; needs benchmarking before parallelizing. |
| Cross-wave backfill on pre-flight skips | Won't fix | Intentionally clean wave boundaries — see step 1 of the Wave Lifecycle. |
| Spinoff-issue filing for out-of-scope discoveries | Deferred | Build it once we have richer summary output to surface them cleanly. |
| Daemon `pipeline_state` situational awareness reads | Deferred | Skill only warns when the daemon is running. |
| Top-level vs namespaced naming (`/sweep` vs `/loom:sweep`) | Open question | Ships as `/sweep` per the original task brief; rename later if convention favors `/loom:sweep`. See #3298 open question #1. |

For the full design discussion (including the open questions raised by the curator), see issue #3298.

## Reference Documentation

- **Shepherd lifecycle**: `.claude/commands/loom/shepherd-lifecycle.md` — canonical per-issue lifecycle.
- **Builder skill**: `.claude/commands/loom/builder.md`
- **Judge skill**: `.claude/commands/loom/judge.md`
- **Doctor skill**: `.claude/commands/loom/doctor.md`
- **Curator skill**: `.claude/commands/loom/curator.md`
- **Label definitions**: `.github/labels.yml`
- **Merge script**: `./.loom/scripts/merge-pr.sh`
- **Original proposal & open questions**: issue #3298
- **Parallel-shepherd stall hazard**: issue #3289
