# Sweep

Process an explicit list of issues — **or an explicit/NL-described set of open PRs** — through the appropriate lifecycle from the current Claude session, no external daemon required. Runs sequentially by default, or in **parallel waves** of up to `N` builders when `--builders-per-wave N` is supplied (issue-set modes only). Supports `--dry-run` to preview the candidate plan without mutating anything.

> **Scope.** This skill accepts either an explicit list of issue numbers, a natural-language description of which issues to process, **or an explicit/NL-described list of open PRs** (Mode C, the "back half" of the lifecycle: Judge → Doctor → Merge per PR's current label). Runs the appropriate lifecycle in waves. Supports `--dry-run` to preview the plan without mutations. Other knobs sketched in #3298 are **deliberately deferred** — see "Limitations" below.
>
> If you need multi-account autonomous dispatch across many issues, use `/loom:loom` (it drives the `loom-daemon`). `/loom:sweep` is itself the single-issue lifecycle, and also covers the in-between case: "I have these N issues (or PRs), run them in this session, without spinning up a daemon."

## Arguments

**Arguments**: $ARGUMENTS

`$ARGUMENTS` is interpreted in one of **three modes** (A/B/C), chosen by inspection of the non-flag tokens and the presence of a `--prs` flag — plus a dedicated **build-everything sentinel** for the bare, sole token `all`. Before classifying, **strip all recognized flag tokens** (`--builders-per-wave N`, `--dry-run`, `--prs`, `--no-daemon`) from the token list — flags are honoured in their respective modes.

**`/loom:sweep all` (the build-everything sentinel).** When the non-flag token list is exactly `["all"]` (case-insensitive), `/loom:sweep` takes a dedicated, deterministic path that resolves the **entire open backlog** — every open issue, regardless of its current label — via a single fixed `gh issue list` query (no Mode B NL translation), then aggressively promotes and drives each toward a merged PR. This is the **fast/sloppy "just build everything" command**: uncurated issues get curated and promoted, stale `loom:building` claims are reclaimed, `loom:blocked` issues are probed for whether their blocker has cleared, `loom:epic` containers fan out to their `loom:epic-phase` children, and issues that already have an open PR are driven through Judge / Doctor → Merge. The only issues it skips outright are `loom:operator-only` (genuinely need a human — credentials, hardware, infra). The resolved set is handed to the same confirmation gate and wave machinery every other mode uses. `/loom:sweep all --prs` resolves the open **PR** set and drives Mode C. Only the bare, sole `all` token triggers this; `all open loom:issue items` and every other multi-token `all …` phrase still route to Mode B (or Mode C for PR phrases) exactly as before. See "Build-everything sentinel (`all`)" and "Aggressive candidate taxonomy" under Validation rules.

**Mode selection summary** (full rules below):

| Trigger | Mode | Subject |
|---------|------|---------|
| Non-flag tokens == `["all"]` (case-insensitive, single token) | **Build-everything** (evaluated first — step 0) | Every open issue, aggressively promoted (or every open PR with `--prs`, via Mode C) |
| `--prs` flag present | **Mode C** (PR-set) | Open PRs, routed per their current label |
| No `--prs`, all non-flag tokens match `^#?\d+$` | **Mode A** (numeric issue list) | Issues, full lifecycle |
| No `--prs`, any non-flag token does not match `^#?\d+$` | **Mode B** (NL) | Issues (default) **or** PRs (if NL clearly indicates PRs — see Mode C NL triggers below) |

> **Build-everything sentinel (bare `all`).** The row above fires **only** when the non-flag token list is exactly `["all"]` (case-insensitive — `all`, `ALL`, `All`). Any multi-token phrase that merely begins with `all` (`all open loom:issue items`, `all merge-ready PRs`) has length > 1 and falls through to Mode B / Mode C **unchanged**. See "Build-everything sentinel" under Validation rules for the deterministic query and taxonomy.

### Mode A — Explicit numeric list (fast path, regression guard)

If **every** whitespace-separated non-flag token matches the regex `^#?\d+$` (a positive integer with an optional leading `#`), treat the arguments as today's explicit issue list. **No LLM interpretation, no extra `gh` calls.** This is the MVP behaviour and must remain bit-for-bit compatible — `/loom:sweep 123 456` and `/loom:sweep #123 #456` continue to work exactly as before.

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

**Mixed mode is supported.** `/loom:sweep #3310 #3312 and any other loom:issue with 'docs' in the title` should be interpreted as the union of `{3310, 3312}` and the `gh issue list --label loom:issue --search "docs in:title"` result. Because the tokens contain non-numeric words, this falls into Mode B and the orchestrator handles the union.

**Unknown-label guard.** Loom never invents labels (CLAUDE.md "Never create new GitHub labels" — that rule is about label *creation* via `gh label create`, which is separate from validating that a label the user already named actually exists on the repo). To validate label tokens in the user's description, query the **live repo label set** as the source of truth:

```bash
gh label list -R <repo> --limit 200 --json name --jq '.[].name'
```

Run this query **once at the start of Mode B label-token validation** and reuse the result for every subsequent token check within the same `/loom:sweep` invocation (at most one `gh label list` call per invocation, regardless of how many label tokens appear in the description). Pass `--limit 200` explicitly (do not rely on `gh`'s default of 30, matching the explicit-limit convention used elsewhere in this skill for `gh issue list`). Scope the query to the repo currently being swept.

If a label token in the description is not in the repo's actual label set, **do not** silently fabricate a `--label <name>` filter — ask the user to clarify which existing label they meant, or supply explicit issue numbers.

**Offline fallback.** If `gh label list` fails (non-zero exit — network outage, auth failure, rate limit), fall back to consulting `.github/labels.yml` and log a warning to stderr (e.g., `warning: gh label list failed, falling back to .github/labels.yml (Loom-managed subset only)`). This keeps the skill functional in offline or restricted environments. Note that `.github/labels.yml` is only the Loom-managed subset, so the fallback may produce false "unknown-label" rejections for labels added via the GitHub UI, Dependabot, or other project conventions; this is the trade-off for offline operation.

### Mode C — PR-set mode (back half of the lifecycle: Judge → Doctor → Merge)

When the user wants to drive a known set of open PRs through Judge / Doctor / Merge **without** spawning Curator or Builder, use Mode C. This is the symmetric counterpart to Mode A/B: same wave/dry-run/checkpoint machinery, different unit-of-work (PR instead of issue) and a different per-unit routing table.

**Mode C entry triggers** (any of these select Mode C):

1. **Explicit flag with explicit list**: the user passes `--prs` **and** every non-flag token matches `^#?\d+$`. Tokens are interpreted as **PR numbers** (not issue numbers). Example: `/loom:sweep --prs 100 101 102`.
2. **Explicit flag with NL description**: the user passes `--prs` **and** at least one non-flag token is non-numeric. The orchestrator translates the description into one or more `gh pr list` invocations (NOT `gh issue list`) — see the PR-side translation guide below. Example: `/loom:sweep --prs all open loom:pr`.
3. **NL trigger without `--prs`**: the user's description **clearly** indicates PRs ("PRs", "pull requests", "review-requested PRs", "all open `loom:pr`", "merge-ready PRs", etc.) — see the NL trigger list below. The orchestrator infers Mode C and proceeds as if `--prs` had been passed. If the description is ambiguous between issues and PRs, ask for clarification rather than guess.

**PR-side NL trigger phrases** (any of these in the description selects Mode C, even without `--prs`):

- `PRs`, `pull requests`, `pull request`
- `review-requested PRs`, `loom:review-requested`
- `changes-requested PRs`, `loom:changes-requested`
- `merge-ready PRs`, `loom:pr` (in a PR context)
- `all open loom:pr`
- `judge-pending PRs`, `judge-ready PRs`
- `pending review`

When uncertain whether the description means issues or PRs (e.g., `/loom:sweep all loom:review-requested` — the label only applies to PRs but the user did not say "PRs"), ask for clarification rather than infer.

**PR-side translation guide — common NL fragments to `gh pr list` flags** (verified against `gh` v2):

| NL fragment | `gh pr list` flag(s) |
|-------------|----------------------|
| "all `loom:review-requested` PRs" / "PRs awaiting Judge" | `--label loom:review-requested` |
| "all `loom:changes-requested` PRs" / "PRs needing Doctor" | `--label loom:changes-requested` |
| "all `loom:pr` PRs" / "merge-ready PRs" / "PRs approved for merge" | `--label loom:pr` |
| "filed by rjwalters" | `--author rjwalters` |
| "all my agent-filed PRs" | `--author @me` |
| "open" (always assumed) | `--state open` (the default) |
| "in the last week" / "from the last N days" | `--search "created:>=YYYY-MM-DD"` (compute the date) |

Combine flags as needed. Always pass `--state open` explicitly (Mode C operates exclusively on open PRs — closed/merged PRs are skipped). Default to `--limit 100` rather than the `gh` default of `30` to avoid silent truncation. The same **unknown-label guard** (one `gh label list` call per invocation, with `.github/labels.yml` offline fallback) applies to PR labels too — PR and issue labels are in the same repo-wide label set.

**Mode C validation rules:**

- `--prs` strips from the token list before classification, exactly like `--builders-per-wave N`, `--dry-run`, and `--no-daemon`.
- Numeric tokens (after stripping `--prs`): same `^#?\d+$` regex as Mode A. Strip leading `#`, parse as positive integers, deduplicate (preserve first-seen order). Reject any token that fails to parse, with a clear error citing the offending token, and EXIT.
- NL tokens (after stripping `--prs`): translate to one or more `gh pr list` invocations per the guide above. Run the command, deduplicate the resulting PR list, and **display the candidate set to the user before spawning any agents**. Await confirmation. If the user declines, EXIT cleanly.
- **`--builders-per-wave N` is silently ignored in Mode C**. The Builder phase is skipped wholesale for PR-set mode; per-PR Judge is sequential within a wave (matching the existing issue-side wave policy). If the user passes both `--prs` and `--builders-per-wave N`, print a one-line note that the flag has no effect in Mode C and proceed without it. Mode C waves are size-1 by default — one PR settles fully (Judge → optional Doctor → optional Merge) before the next PR is touched. This may relax in a future issue; today it is size-1 because parallel Judge/Doctor across PRs is unbenchmarked and every wave member's Task result is read back into this same orchestrator session (context-window pressure). This is a **width** choice — it is **not** the #3289 rule, which is about nested (grandchild) dispatch depth, not wave width.
- Mixed Mode C and Mode A/B is **not** supported in this skill — if the user wants to sweep some issues and some PRs in one invocation, ask them to run two `/loom:sweep` calls (one for each mode). Implementing PR/issue mixing would require routing logic for the cross product of (issue-state × PR-state); cleanly out of scope.

### Edge cases (prose rules, applied in either mode but mostly relevant to Mode B)

1. **Zero matches.** Print the derived `gh issue list` command and its empty result, then EXIT cleanly. Do not spawn any agents and do not fall through to Mode A.
2. **More than the result cap.** `gh issue list` defaults to `--limit 30`; this skill should pass `--limit 100` explicitly. If results still hit the cap (100 candidates), print a warning that the result set was truncated and ask the user to narrow the description before proceeding. Do not silently process only the first 100.
3. **Out-of-band queries** (anything `gh issue list` cannot express by itself — body-content searches, file-touch queries like "issues touching `loom-daemon`", "issues without tests", repository-diff inspection). These require per-issue body or diff inspection, which is **out of scope for this skill**. Ask the user to clarify or supply explicit issue numbers. Do **not** attempt heuristic per-issue inspection here.
4. **Ambiguous time windows** ("recent", "lately", "this sprint"). Ask the user to specify a concrete date or duration rather than guessing. The translation table above only covers concrete forms ("last week", "last N days") which compute deterministically.

### Optional flags

- **`--builders-per-wave N`** — dispatch up to `N` builders in parallel per wave. When **omitted**, the wave size is `auto` — resolved at Stage -1 from the chosen backend and scratch-volume disk headroom (see "Resolve auto wave size"): the daemon detached-process path targets up to 10 concurrent sweeps, while the in-session subagent path core-scales within the #3289-safe `[3, 6]` band (`clamp(floor((cores-2)/4), 3, 6)`, #3693). When **present**, `N` must be an integer `>= 1` and the explicit value overrides auto entirely (operator wins). Honoured in Modes A and B (issue-side); **silently ignored in Mode C** (PR-set mode has no Builder phase — see Mode C validation rules above). Flag tokens are stripped before classification.
- **`--dry-run`** — print the planned candidate list (with wave grouping) and EXIT without performing any mutation. Recognized as a bare flag token (no value). May appear anywhere in `$ARGUMENTS`. Default is off. Honoured in **all three** modes — stripped before classification along with other flags. Mode C dry-run prints the PR-set plan (per-PR routing) instead of the issue-set plan.
- **`--prs`** — switch into Mode C (PR-set mode). Recognized as a bare flag token (no value). May appear anywhere in `$ARGUMENTS`. Default is off. When present, non-flag tokens are interpreted as **PR numbers** (numeric tokens) or as a **PR-list description** (NL tokens). When absent, an NL trigger phrase listed in the Mode C section can still select Mode C. See "Mode C" above for full semantics.
- **`--no-daemon`** — force in-process subagent dispatch even when the daemon is running with a multi-account token pool. Recognized as a bare flag token (no value). May appear anywhere in `$ARGUMENTS`. Default is off. When present, **Stage -1 (Backend detection) skips the `PROBE_DAEMON` step entirely** and the skill always falls through to the existing Mode A/B/C subagent dispatch path. Honoured in **all three** modes — stripped before classification along with other flags. Use this when you want the predictable single-process behaviour even though daemon dispatch is available (e.g., debugging, demoing the subagent path, or running under a token configuration that you don't want shared with daemon-spawned sweeps). See "Stage -1: Backend detection" below.
- **`--depends-on <parent>`** — stacked-PR mode (issue #3729, v1). Declares that this sweep's issue is stacked on the single parent issue `<parent>`: the Builder branches its worktree off `feature/issue-<parent>` (not the default branch) and opens its PR with `--base feature/issue-<parent>`, so the child's Curator→Builder→Judge can run **concurrently** with the parent's review. Takes **one value** (a positive integer parent issue number) — this is the sole, authoritative *operator-declared* dependency source (no `Depends on #A` body parsing unless `--auto-stack` is passed, see below). A single optional parent makes diamonds / multi-parent stacks unrepresentable. Recognized anywhere in `$ARGUMENTS` as `--depends-on N`; strip it (and its value) before classification and store `DEPENDS_ON=N`. Default **unset** — absent the flag, behavior is byte-for-byte unchanged (branches off the default branch as always). Intended for **daemon `dispatch_sweep`-only** use (`mcp__loom__dispatch_sweep` with `depends_on`); absent `--auto-stack`, the wave lifecycle does **not** auto-detect or auto-create stacks. See "Stacked dependency (auto-reconciliation on parent merge)" below. **Reconciliation after the parent squash-merges now fires automatically** from `merge-pr.sh` (#3747 v2 item 1) — a best-effort, live-forge-discovered pass that reconciles safe children and defers the ones whose issue is still `loom:building`; `./.loom/scripts/reconcile-stack.sh` remains available for manual/deferred runs.
- **`--auto-stack`** — opt-in auto-election of same-candidate-set stacking (issue #3759, v1). A bare flag (no value); default **off**. When present in Modes A/B (issue-set), the Stage 0 candidate survey additionally reads each candidate's issue `body` and detects **same-candidate-set** dependency edges declared in body text (`Depends on #A` / `Requires #A`) — see "Auto-stack detection and wave ordering (`--auto-stack`, #3759)". A detected edge is honored **only when `#A` is also a member of this sweep invocation's own deduplicated candidate list**; a `Depends on #A` naming an issue outside the set is left completely untouched (it flows through existing `loom:blocked` handling, unaffected). This generalizes the single-value `--depends-on` mechanics to a **per-issue** `DEPENDS_ON[N]` map: each child branches its worktree off `feature/issue-<parent>` and opens its PR with `--base feature/issue-<parent>`, exactly as a manually-dispatched `--depends-on` chain, and reconciliation on parent merge fires automatically (unchanged, #3747/#3752). **Absent the flag, behavior is byte-for-byte unchanged** (no body read, no edge detection, no wave reordering, no prompt). **No-op in Mode C** (PR-set mode has no Builder phase to stack — the flag is silently ignored, like `--builders-per-wave`). Scope is deliberately narrow: edges are **linear, single-parent** (no diamonds/multi-parent), **same-sweep only** (cross-`/loom:sweep` coordination is #3768's concern), and inferred from the **authoritative body-text signal only** — file-overlap-heuristic detection is explicitly out of scope (#3729 rejected file paths as a topology signal; the reactive #3647 in-wave overlap gate stays the backstop for accidental collisions). Recognized anywhere in `$ARGUMENTS`; strip it before classification and store `AUTO_STACK=true|false`.

### Validation rules

- Recognize `--dry-run`, `--prs`, `--no-daemon`, `--builders-per-wave N`, `--depends-on N`, and `--auto-stack` as flag tokens anywhere in `$ARGUMENTS`, strip them from the candidate list before validation, and store them as flags / parameters (`DRY_RUN=true|false`, `PRS_MODE=true|false`, `NO_DAEMON=true|false`, `BUILDERS_PER_WAVE=N`, `DEPENDS_ON=N|unset`, `AUTO_STACK=true|false`). When `--builders-per-wave` is **absent**, set the sentinel `BUILDERS_PER_WAVE=auto` (not `1`) — Stage -1 resolves the concrete wave size from the backend + disk headroom. An explicit integer is stored verbatim and overrides auto. `--depends-on N` consumes its following token as the parent issue number (a positive integer); reject a missing/non-numeric value with `Error: --depends-on requires a positive integer parent issue number` and EXIT. When absent, `DEPENDS_ON` is unset (no base override — default-branch behavior). `--auto-stack` is a bare flag (consumes no value); default `AUTO_STACK=false`. It applies to Modes A/B only — in Mode C it is silently ignored (no Builder phase to stack). `--auto-stack` and a single-issue `--depends-on N` may both be present: `--depends-on` seeds `DEPENDS_ON[N]` for its named issue and auto-stack detection fills in the rest of the map; a detected edge never overrides an explicit `--depends-on` for the same issue.
- At least one candidate (numeric token or NL description) must be supplied. If `$ARGUMENTS` (after stripping flag tokens) is empty, display:
  ```
  Usage: /loom:sweep <issue-number> [<issue-number> ...] [--builders-per-wave N] [--dry-run] [--no-daemon]
         /loom:sweep <natural-language description>     [--builders-per-wave N] [--dry-run] [--no-daemon]
         /loom:sweep all                                [--builders-per-wave N] [--dry-run] [--no-daemon]   # build everything (whole open backlog)
         /loom:sweep all --prs                          [--dry-run]                                         # every open PR (Mode C)
         /loom:sweep --prs <pr-number> [<pr-number> ...] [--dry-run]
         /loom:sweep --prs <natural-language PR description> [--dry-run]
         /loom:sweep <natural-language PR description>       [--dry-run]   # PR NL triggers select Mode C

  See #3298, #3384, #3454, and #3568 for the full design.
  ```
  and EXIT.
- **Mode-selection precedence** (apply in order):
  0. **Build-everything sentinel.** If the non-flag token list (after flag-stripping) is **exactly `["all"]`** — a single token, case-insensitive (`all`, `ALL`, `All`) — take the dedicated **build-everything** path (see "Build-everything sentinel (`all`)" below). This step is evaluated **before** every other step so the bare `all` token can never be swallowed by the Mode B NL classifier (step 4):
     - `--prs` **absent** → resolve the deterministic **entire open-issue** set (no label filter), set `SWEEP_ALL_AGGRESSIVE=true`, then hand off to the Mode A/B issue-set wave machinery (confirmation gate, Stage -1 backend detection, wave partition — all as today, but with the aggressive pre-flight overrides).
     - `--prs` **present** → resolve the deterministic **entire open-PR** set and drive the existing **Mode C** PR-set lifecycle (subagent path); C0 pre-flight filters non-actionable PRs.

     The guard is `lowercased(non_flag_tokens) == ["all"]` — length exactly 1. Any additional non-flag token (`all open loom:issue items`, `all my agent-filed ...`, `all merge-ready PRs`) has length > 1 and falls straight through to steps 1–4 unchanged. This is the backward-compatibility contract.
  1. If `--prs` is present, classify as **Mode C** (numeric → explicit PR list; NL → translated `gh pr list`).
  2. Else if any non-flag token does not match `^#?\d+$` AND the description contains a PR-side NL trigger phrase (see Mode C "PR-side NL trigger phrases"), classify as **Mode C** (NL-inferred).
  3. Else if every non-flag token matches `^#?\d+$`, classify as **Mode A** (numeric issue list).
  4. Else classify as **Mode B** (NL issue list).

  This ordering is deliberate: the bare `all` sentinel is intercepted first (else it would land in step 4), an explicit `--prs` flag is the next strongest signal, an unambiguous NL trigger is next, and the existing Mode A/B classifier (regression-guarded) handles everything else.

- **Build-everything sentinel (`all`)** — the deterministic path taken by step 0 above. This is the **fast/sloppy "promote and sweep everything" command**: it resolves the *entire* open backlog and aggressively drives each item toward a merged PR rather than filtering to a pre-curated subset.
  - **Trigger**: non-flag tokens exactly `["all"]` (case-insensitive). Flags (`--dry-run`, `--builders-per-wave N`, `--prs`, `--no-daemon`) are stripped first and compose normally, so `all --dry-run` and `all --builders-per-wave 2` still trigger the sentinel.
  - **Aggressive-mode flag**: resolving the candidate set via this sentinel sets the internal flag `SWEEP_ALL_AGGRESSIVE=true`, carried into the Wave Lifecycle. It **overrides the conservative pre-flight skip rules** (Wave Lifecycle step 1) with the recovery routing in the "Aggressive candidate taxonomy" table below. Mode A/B explicit-list and NL invocations never set this flag — their skip rules are unchanged.
  - **Candidate resolution (issues, `--prs` absent)** — one deterministic `gh issue list` call, **no label filter**, no LLM/NL translation:
    ```bash
    gh issue list --state open --limit 100 --json number,title,labels,updatedAt
    ```
    Every open issue is a candidate regardless of label — promotion, unblocking, stale-claim recovery, and epic fan-out happen per-issue per the "Aggressive candidate taxonomy" table below, not by pre-filtering the query (`updatedAt` feeds the staleness rule). Pass `--limit 100` explicitly (never rely on gh's default of 30) and apply the existing **edge-case rules**: zero matches → print the resolved query + empty result and EXIT cleanly (edge case #1, do **not** fall through to any other mode); 100 candidates returned → warn about truncation and ask the operator to narrow (or deliberately raise `--limit`) before proceeding (edge case #2).
  - **Orphaned-claim recovery pass (run once, AFTER the confirmation gate, before per-issue pre-flight)** — reclaim `loom:building` labels left behind by dead workers so stale claims don't mask buildable issues:
    ```bash
    ./.loom/scripts/recover-orphaned-shepherds.sh --recover
    ```
    Best-effort: a non-zero exit is logged and ignored (never abort the sweep). Any issue still labeled `loom:building` after this pass is re-checked inline by the staleness rule in the taxonomy table. **Ordering is load-bearing**: this pass mutates labels, so it runs *only after* the operator confirms the resolved plan at the mandatory confirmation gate — never before. It is **skipped entirely under `--dry-run`** (the dry-run gate is read-only and EXITs before any mutation). This preserves the file-wide "gate before mutation" invariant: nothing on disk or on the forge changes until the operator has confirmed (or `--dry-run` has printed and exited).
  - **Candidate resolution (PRs, `--prs` present)** — every open PR, handed to the Mode C PR-set lifecycle (subagent path):
    ```bash
    gh pr list --state open --limit 100 --json number,title,labels
    ```
    Mode C's C0 pre-flight already skips PRs with no actionable label, `loom:operator-only`, or `loom:blocked`, and routes the rest by current label (Judge / Doctor → Judge / Merge) — so grabbing every open PR and letting C0 filter matches the "get every in-flight PR over the finish line" intent. Same zero-match / truncation edge-case rules apply.
  - **Existing-PR routing (issues path)**: the sentinel adds **no** new PR-detection logic. Issues with an open linked PR are handed to the wave machinery, which routes an issue with one open linked PR to Judge (or Merge if the PR is already `loom:pr`) via the per-issue existing-PR probe (Wave Lifecycle step 1, #3359 + #3677 — the union of `closedByPullRequestsReferences` filtered to `state == OPEN` and timeline `cross-referenced` open-PR events, so a non-closing `Part of #N` PR is detected too). This is the single source of truth for existing-PR routing and **takes precedence over the label routing** in the taxonomy table (an issue with an open PR is driven to merge, never rebuilt).
  - **Mandatory confirmation gate**: the sentinel path **always** displays the resolved candidate set (with the per-issue planned action from the taxonomy table) and awaits operator confirmation before spawning any agent — identical to Mode B/C's "display candidate set before spawning any agents" rule. A whole-backlog sweep must never auto-dispatch silently. Declining EXITs cleanly.
  - **Flag composition**: `--dry-run` resolves the candidate set, prints the standard issue-set (or PR-set) dry-run plan with wave grouping + the aggressive per-issue actions, and EXITs with no mutation (the Stage-0 dry-run contract is backend-independent — the orphaned-claim recovery pass is skipped under `--dry-run`). `--builders-per-wave N` and `--no-daemon` compose with the wave / Stage -1 machinery exactly as for Mode A/B. Stage -1 backend detection is unchanged: after `all` resolves the issue set, the normal strict-AND daemon/pool probe decides daemon-dispatch vs subagent fallthrough; `all --prs` (Mode C) always routes to the subagent path per the existing Mode C short-circuit.

- **Aggressive candidate taxonomy** (the single source of truth for what `all` resolves and how each label class is routed — lives here beside the Mode B label logic so there is one definition). When `SWEEP_ALL_AGGRESSIVE=true`, **every** open issue is a candidate and is routed by its current label class:

  | Label class | Aggressive routing |
  |-------------|--------------------|
  | `loom:issue` | Build directly (already promoted). |
  | `loom:curated` | Promote to `loom:issue` (Approval gate, step 3) → build. |
  | Uncurated: none / `loom:triage` / `loom:curating` | Curate (step 2) → promote → build. |
  | Stale `loom:building` | Reclaim → build. "Stale" = no **open** linked PR **and** `updatedAt` older than `LOOM_STALE_BUILDING_HOURS` (default 2). "Open linked PR" here means the **union** probe (step 1, #3359 + #3677) — `closedByPullRequestsReferences` **and** timeline `cross-referenced` open-PR events — so an in-flight non-closing `Part of #N` slice PR counts and blocks reclaim. Fresh `loom:building` (recently updated, or has an open PR) is genuinely in flight → route its open PR (if any) to Judge/Merge, else skip with `in flight (fresh loom:building)`. |
  | `loom:blocked` | Probe the blocker: if every `#N` it depends on (parsed from the blocker comment / issue body via GitHub's reference parser) is CLOSED/MERGED, remove `loom:blocked` → build. If a dependency is still open → skip with `still blocked by #N`. If no dependency is parseable → remove `loom:blocked` and attempt anyway (fast/sloppy). |
  | `loom:epic` | Fan out: build its open `loom:epic-phase` children (already in the candidate set). Skip the container with `expanded to #a #b …`. If it has **no** open phase children → skip with `needs decomposition (run Champion/Architect)` — a container is not directly buildable. |
  | `loom:epic-phase` | Build directly (a phase issue is a normal buildable unit). |
  | Has an **open** linked PR (any label) | Drive the existing PR through Judge / Doctor → Merge via the step-1 union probe (#3359 + #3677 — closing-keyword **and** non-closing `Part of #N` timeline references) — do not build a duplicate. Takes precedence over every row above. |
  | `loom:abort` | Reclaim like a stale claim only if `updatedAt` is stale; otherwise skip with `abort flag set`. |
  | `loom:operator-only` | **Skip** — the one hard exclusion. Requires a human (credentials, hardware, infra); automation cannot complete it. Log `operator-only (human required)`. |

  - Every recovery action (reclaim, unblock, promote, fan-out) only *removes* or *swaps among* labels that already exist on the repo — the sentinel invents no labels.
  - **PR variant (`--prs`)**: the candidate set is every open PR; C0 pre-flight routes `loom:review-requested` → Judge, `loom:changes-requested` → Doctor → Judge, `loom:pr` → Merge, and skips PRs with no actionable label, `loom:operator-only`, or `loom:blocked`.
- **Mode A** (every non-flag token matches `^#?\d+$`, `--prs` absent, no PR NL trigger):
  - Strip leading `#` from each token, parse as a positive integer.
  - Reject any token that fails to parse as a positive integer (after stripping). Display an error showing the offending token and EXIT.
  - Deduplicate the issue list (preserve first-seen order).
- **Mode B** (any non-flag token does not match `^#?\d+$`, `--prs` absent, no PR NL trigger):
  - Translate the description to `gh issue list` invocation(s) per the guide above.
  - Run the command, deduplicate, and **display the candidate set to the user before spawning any agents.** Await confirmation. If the user declines, EXIT cleanly.
  - If the description is ambiguous, hits an out-of-band query, or references an unknown label, ask for clarification first — do not guess.
- **Mode C** (`--prs` flag present, OR PR-side NL trigger detected):
  - If every non-flag token matches `^#?\d+$`: strip leading `#`, parse as positive integers, deduplicate (preserve first-seen order). Reject any non-parseable token with a clear error and EXIT. Resolved list is **PR numbers**.
  - If any non-flag token does not match `^#?\d+$`: translate to `gh pr list` invocation(s) per the PR-side guide above. Run the command, deduplicate, and **display the candidate set to the user before spawning any agents.** Await confirmation. If the user declines, EXIT cleanly.
  - If the description is ambiguous between issues and PRs (e.g., `loom:review-requested` is PR-only but the description omits "PRs" / "pull requests"), ask the user to clarify before proceeding. Do not guess.
  - If `--builders-per-wave N` was supplied, print a one-line note that the flag has no effect in Mode C and proceed without it (Mode C waves are size-1; see Mode C section).
- **`--builders-per-wave N` validation:**
  - **Absent flag → `auto`.** When the operator did not pass `--builders-per-wave`, `BUILDERS_PER_WAVE=auto`; skip the integer validation below and resolve the concrete size at Stage -1 ("Resolve auto wave size"). The rules below apply **only** when an explicit value was passed — an explicit integer always overrides auto and is validated verbatim as before.
  - Parse `N` as an integer. Reject non-integer values with a clear error and EXIT.
  - Reject `N < 1` (including `0` and negative values) with: `Error: --builders-per-wave must be >= 1 (got: <N>)` and EXIT. Do **not** silently default to `1`.
  - If `N > 6`, print a warning and continue: `WARNING: --builders-per-wave=<N> is unvalidated. N<=6 is recommended; N>=7 may exhaust context or hit rate limits. Proceeding at your own risk.`
  - If `N` exceeds the number of candidates at any wave, **silently clamp** to the candidate count for that wave. Do not warn, do not stall.
- **`--no-daemon` validation:**
  - Bare flag, no value. If a value is supplied (`--no-daemon=true`, `--no-daemon something`), treat the `=value` form as an error and EXIT (`Error: --no-daemon takes no value`). The standalone-token form is the only accepted spelling.
  - Honoured in all three modes (A, B, C). The flag is a no-op in Mode C (Mode C is always subagent-side — see Stage -1's `DECIDE` precedence below) but is accepted without error so operators can pass it unconditionally from scripts.
  - When `NO_DAEMON=true`, Stage -1 short-circuits to the subagent path **before** issuing the daemon Ping probe. No daemon-state files are read or written, and no `mcp__loom__*` calls are made for backend probing.

**Wave-size guidance:**

| `N` | Status |
|-----|--------|
| `auto` | **Default** (flag omitted). Resolved at Stage -1 from the backend + scratch-volume disk headroom: daemon detached-process path targets up to 10; in-session subagent path **core-scales** in `[3, 6]` via `clamp(floor((cores-2)/4), 3, 6)` (#3693). Clamped by candidate count and disk, floor 1. See "Resolve auto wave size". |
| `1` | Fully sequential (MVP-compatible). Explicit override of `auto`. |
| `2` | **Recommended** explicit starting point for parallel subagent waves. |
| `3` | Tested and validated. The #3289-safe **floor** for the **subagent** path — the default auto-resolved target now scales with cores up to 6. |
| `4`–`6` | Evidence-supported for the **subagent** path on multi-core hosts (#3693); reached automatically by the core-scaled `auto` default. Operator discretion via explicit override. |
| `>= 7` | Unvalidated **for the subagent path**. Warns at parse time. Operator discretion. |

The subagent-path target is **soft** — there is no hard upper bound and the warning is the only guard. Its auto default core-scales within `[3, 6]` (floor 3 on small/shared hosts, ceiling 6 on big ones). The `[3, 6]` band is a **width** decision: at one level deep, width is bounded by the harness concurrency cap (`min(16, cores-2)`), not by #3289. The #3289 nested-dispatch stall is specifically about parallel *grandchildren* (`parent → nested /loom:sweep → builder`), which `/loom:sweep` never does — it dispatches builders directly, one level deep (see "CRITICAL: One level deep"). The ceiling stays 6 (not 8/10) because single-account rate-limit burn and orchestrator context-window pressure (every wave member's Task result is read back into the same session) both bind before #3289 does. High parallelism toward 10 is reached **only** via the daemon detached-process path (`mcp__loom__dispatch_sweep`), where each sweep is an isolated OS process — not a nested subagent. Never raise the subagent ceiling toward 10; route through the daemon instead.

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
# loom:operator-only issues are hard-skipped. Displays the resolved plan and
# awaits confirmation before dispatching.
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

## Execution Model

`/loom:sweep` processes the candidate list in **waves**:

- **Mode A/B (issue-set)**: the candidate list is partitioned into waves of up to `N = --builders-per-wave` issues, where an omitted flag resolves to the Stage -1 auto wave size (see "Resolve auto wave size" — up to 10 on the daemon path, core-scaled within `[3, 6]` on the subagent path, disk-clamped). Issues are picked into waves in order. Within a wave, builders are dispatched in parallel; across waves, processing is sequential. Each wave fully settles (all builders → per-PR Judge → optional Doctor → merge) before the next wave starts.
- **Mode C (PR-set)**: the candidate list is processed in **size-1 waves** (one PR per wave). `--builders-per-wave` is ignored because there is no Builder phase. Each PR is routed per its current label (Judge / Doctor→Judge / Merge — see "PR-set Wave Lifecycle" below) and fully settles before the next PR is touched. Sequential per-PR processing is a **width** choice — parallel Judge/Doctor across PRs is unbenchmarked and every wave member's Task result is read back into this orchestrator session (context pressure) — and parallels the issue-side "per-PR Judge is sequential within a wave" policy. It is **not** the #3289 rule, which governs nested (grandchild) dispatch depth, not wave width.

### CRITICAL: Only Builders parallelize — issue-creating roles must be serialized (issue #3707)

**Waves parallelize Builders only.** The reason a wave can safely fan out `N` agents at once is that each Builder works in an isolated git worktree and produces **exactly one PR at the end** — no shared mutable forge state is touched mid-run, so two concurrent Builders never collide. `/loom:sweep` itself only ever dispatches Builders (plus per-issue Curator/Judge/Doctor, which run **sequentially within a wave**), so today's wave loop is safe by construction.

**Never dispatch two or more issue-creating agents concurrently.** Agents that **create issues** — Architect proposals, Curator oversized-issue decomposition, Champion epic-phase creation — mutate the forge's **shared, server-assigned issue-number space** with no client-side coordination, transaction, or idempotency key. When two such agents run `gh issue create` bursts at the same time they **race on issue numbers and cross-contaminate bodies** (one epic's title paired with another's body), and any recovery/retry loop that PATCHes-by-title amplifies the damage by winning every write race against the other still-active filer. This is not hypothetical: it was observed 2026-07-21 on a 4-wide wave (1 builder + 3 architects) — 2 duplicate issues, 3 with mismatched title/body, and a corrupted roadmap comment, all needing manual reconciliation (#3707).

Concrete rules for anyone extending this skill or hand-driving a wave:

- **Do NOT construct a mixed wave** that places any issue-creating role (Architect / Curator-decomposition / Champion epic-phase) alongside Builders — or alongside another issue-creating agent. That exact `1 builder + 3 architects` shape is the footgun this section forbids.
- **Serialize issue-creating agents**: one must finish its entire `gh issue create` burst before the next starts. A recovery/retry loop must never run against a still-active concurrent filer. **"Serialize" here means awaited-to-completion, not merely dispatched-with-a-sync-flag** — see "Subagent dispatch is async-only" below (#3822).
- Parallel **Builders** remain safe and are the only role `/loom:sweep` fans out — this is unchanged.

Heavier mitigations (a per-wave issue-filing lock, an epic-scoped idempotency UUID + post-create reconciliation, or a serialized issue-filing sub-phase inside `/loom:sweep`) are **deferred, out-of-scope follow-ups** to this documentation guardrail — build them only if serialization-by-convention proves insufficient in practice (#3707).

### CRITICAL: Subagent dispatch is async-only — you MUST block explicitly (issue #3822)

**The harness may launch every Agent/Task subagent asynchronously regardless of the dispatch flags.** In particular, `run_in_background: false` is **not** a guarantee of synchronous return — it has been observed ignored, with the agent launched async anyway (2026-07-23, Claude Code harness). An orchestrator that trusts a sync-flag and proceeds immediately can start a downstream serialized phase before the upstream agent has finished — e.g. begin Judge before builders finish, or overlap two issue-creating agents (the exact #3707 race this skill forbids).

Therefore, at **every** dispatch site where this skill sequences one phase after another, the orchestrator **MUST explicitly await each subagent's completion** — block on its `TaskOutput` / completion notification — before advancing. Do not rely on any dispatch flag to enforce ordering. Concretely, this makes the skill's sequencing rules load-bearing on an explicit await, not on the harness:

- **Sequential Curator per issue** (step 2) — await each Curator before the next.
- **"Await all builders before Judge"** (step 4) — collect every builder's `TaskOutput` before any Judge dispatch.
- **Sequential per-PR Judge / Doctor within a wave** (steps 5–6) — await each PR's Judge (and its Doctor→Judge cycle) before the next PR's Judge.

**"Serialized" therefore means awaited-to-completion, not merely dispatched-with-a-sync-flag.** The #3707 rule above depends on this: serializing issue-creating agents is only safe if each is explicitly awaited to completion before the next is dispatched — a `run_in_background: false` that the harness ignores would silently overlap them.

### CRITICAL: One level deep — never spawn a nested orchestrator (`/loom:sweep`) as a subagent

`/loom:sweep` dispatches `loom-builder`, `loom-judge`, and `loom-doctor` subagents **directly from this orchestrator session** in a single tool-call block. This is **one level deep** and is empirically safe for `N` up to at least 3.

**Do NOT, under any circumstances, dispatch a nested orchestrator skill (`/loom:sweep`) as a subagent from `/loom:sweep`.** That would be two levels deep (parent Claude → `/loom:sweep` Task → builder/judge Task) and triggers the nested-dispatch stall hazard tracked in #3289 (stream-pump dies on parallel grandchildren). The wave loop in this skill is the architectural answer to that race — preserve it.

Concretely, when this skill says "dispatch builders for the wave", that means: in a single tool-call block, invoke `loom-builder` once per issue in the wave (e.g., three parallel `Task` calls if `N=3`). It does **not** mean invoke `/loom:sweep` three times.

If a future maintainer is tempted to "simplify" by replacing the wave-loop with parallel `/loom:sweep` calls: don't. Read #3289, then read this section again.

### Model selection for subagent dispatch (issue #3477, Phase 1)

Every role subagent dispatched by this skill (`loom-curator`, `loom-builder`, `loom-judge`, `loom-doctor`) gets its model resolved through a fixed precedence chain. Resolve once per role at dispatch time and pass the result via the Task tool's `model` parameter:

1. **Explicit dispatch param** — a model explicitly requested by the operator for this sweep (e.g., an operator instruction in the invoking prompt).
2. **Workspace override** — `.loom/config.json` → the `terminals[]` entry whose `roleConfig.roleFile` matches the role (e.g., `builder.md`) → its optional `roleConfig.model` field.
3. **Role default** — `.loom/roles/<role>.json` → `suggestedModel` (ships as an alias: `sonnet`, `opus`, or `haiku`).
4. **Session default** — if none of the above resolves (or resolves to an empty string), **omit the `model` parameter entirely** so the subagent inherits the parent session's model. Never pass `model: ""`.

**Tier 2.5 — Curator complexity marker (issue #3702, Builder dispatch only)**: between tier 2 and tier 3, at **Builder** dispatch, grep the issue body for the Curator-emitted marker `<!-- loom:complexity=complex -->` (an HTML comment, values `routine` | `complex`; see `curator.md`). When it is present and reads `complex`, bump the Builder's tier-3 (`suggestedModel`) resolution up **exactly one model tier** — `sonnet → opus` — before dispatch. Hard bounds, all enforced here:

> **Experiment-mode suppression (issue #3725).** When `sweep.modelExperiment` resolves to `experiment` (see "Model-cost experiment mode" below), the forced arm **overrides and SUPPRESSES this tier-2.5 bump** for the Builder: the marker is still *read* (same grep), but it is used **only as the stratification key**, never as a `sonnet → opus` bump. This is load-bearing — without it, a `complex`-marked issue on Arm B (sonnet-first) would silently become opus and confound the A/B. The bump behaves exactly as documented here whenever the experiment is `off`/`observe`.

- **One bump maximum, and never to `fable`.** The marker can lift `sonnet → opus` and nothing further; it can never reach the top (`fable`) rung. Fable is reached only via the escalation ladder (objective Judge-rejection evidence) or an explicit operator param, never on a Curator's speculation.
- **It is not a label** and creates no label — it lives only in the issue body.
- **Tier-1 and tier-2 pins still win.** The marker sits *strictly between* tiers 2 and 3: an explicit dispatch param (tier 1) or a `roleConfig.model` workspace pin (tier 2) overrides it, exactly as they override tier 3.
- **Absent / `routine` / malformed marker ⇒ no bump** — behaviour is byte-for-byte identical to today's precedence chain. Existing curated issues (which carry no marker) are unaffected.
- The marker applies **only to the Builder path**. It never influences Curator, Judge, or Doctor resolution.

**No-Fable-Judge hard invariant (issue #3702)**: **Judge model resolution can never resolve to `fable`, regardless of `sweep.escalation` contents or any marker.** The escalation ladder and the tier-2.5 marker apply only to the Curator-marker→Builder path and to the rejection-triggered Doctor — never to Judge. The Judge is the escalation sensor (see #3481); reviewing security-adjacent diffs is precisely Fable's refusal surface, and a refusing Judge would deadlock the control loop. If a resolved Judge model would ever be `fable` (alias or pinned ID), fall back to `opus` for the Judge dispatch and log the substitution.

Rules:

- Aliases (`sonnet`/`opus`/`haiku`) and pinned IDs (`claude-sonnet-4-6`) are both valid at every tier. Shipped role JSONs use aliases; workspaces that need determinism pin exact IDs in `roleConfig.model`.
- A retry of the same role for the same issue (e.g., Builder re-dispatch after a mid-builder kill, or a second Judge pass after Doctor) **reuses the same resolved model**. Transport-level retries inside `claude-wrapper.sh` (token exhaustion, crashes, 5xx) likewise always keep the model — they are not quality signals and never trigger escalation.
- **Exception — Judge-rejection escalation (issue #3481, Phase 2)**: a Doctor dispatched *because of* a `loom:changes-requested` transition escalates one rung up the capability ladder. See "Model escalation on Judge rejection" below.
- Resolution failures are soft: if a role JSON is missing or unparseable, fall through to the next tier silently. Model selection must never block a sweep.
- The daemon path has its own equivalent: `mcp__loom__dispatch_sweep` accepts an optional `model` param which the daemon forwards to the spawned child as `claude --model <value>`. When delegating to the daemon (Stage -1 `use_daemon`), you MAY pass a resolved model; when omitted, the child inherits the spawning environment's default — the daemon emits no `--model` flag at all.

### Model escalation on Judge rejection (issue #3481, Phase 2)

When the Judge requests changes and this orchestrator dispatches a Doctor for the rejected PR — the Doctor phase at issue-side step 6 and at Mode C step C1b — the Doctor's model escalates one rung up a capability ladder instead of resolving through tiers 3/4 of the precedence chain.

**The ladder** lives in `.loom/config.json` under `sweep.escalation`:

```json
{
  "sweep": {
    "escalation": ["sonnet", "opus"]
  }
}
```

Three states:

| `sweep.escalation` value | Behavior |
|--------------------------|----------|
| Key absent | Default ladder `["sonnet", "opus"]` applies |
| `[]` or `false` | Escalation disabled — pure Phase 1 behavior; the rejection-triggered Doctor resolves through the unmodified precedence chain |
| Non-empty array | As configured; rungs accept aliases or pinned IDs, same as every other tier |

Rules:

1. **Trigger**: escalation fires **only** on a real Judge rejection — the `loom:changes-requested` transition that routes into the Doctor phase. First attempts of every role (Curator, Builder, the first Judge pass) always use the unmodified Phase 1 precedence chain. `ladder[0]` never overrides anything — it documents what attempt 1 is *expected* to run on, it is not applied.
2. **Precedence interaction**: the rejection-triggered Doctor resolves to `ladder[1]`, but only when its model would otherwise come from tier 3 (role `suggestedModel`) or tier 4 (session default). Tier 1 (explicit dispatch param) and tier 2 (`roleConfig.model` workspace pin) still win — pins are pins; operators who pinned want determinism.
3. **Composes with the cap, does not extend it**: escalation composes with the configurable Doctor→Judge cycle cap (`sweep.max_doctor_cycles`, default 1 — see "Doctor-cycle cap" below); it never raises the cap on its own. Consume the ladder generically as `ladder[min(attempt - 1, len - 1)]`: cycle 1 (attempt 2) resolves `ladder[1]`, cycle 2 (attempt 3) resolves `ladder[2]`, and so on. When the cap is at its default of 1, only `ladder[1]` is reached on the normal path (a configured third rung stays dormant); raising `max_doctor_cycles` above 1 — or granting the default-cap distinct-defect grace cycle — activates deeper rungs automatically, with no change here.
4. **Mode C inherits the rule** — C1b runs the identical Doctor phase under the identical cap, so the identical `ladder[1]` rule applies. No separate policy.
5. **Resume safety**: the escalation decision derives from the `loom:changes-requested` label/phase, **not** from a stored counter — so a sweep killed between Doctor dispatch and the follow-up Judge resumes correctly: re-entry routes back through the Doctor/Judge phases per the checkpoint skip rules, and any re-dispatched rejection-triggered Doctor escalates again. The optional `attempt` field on the sweep checkpoint (`sweep-checkpoint.sh write N doctor-done ... --attempt 2`) is forward-compat bookkeeping for a future cap raise; readers treat an absent field as attempt 1.
6. **The orchestrator decides, never the wrapper**: escalation is resolved here at Doctor-dispatch time. `claude-wrapper.sh` / `spawn-claude.sh` retries always keep their model (transport failures are not quality signals), and no wrapper change is involved.

### Effort-aware rung grammar, the `fable` rung, and refusal fallback (issue #3702)

This subsection extends the `sweep.escalation` ladder above with an optional richer rung grammar and a top `fable` rung. It is **fully additive and opt-in**: the shipped default ladder stays `["sonnet", "opus"]` with `max_doctor_cycles` `1`, and bare-alias configs parse and behave byte-for-byte as documented above. Nothing here changes default behaviour.

**Rung grammar — `model@effort`.** Each rung in `sweep.escalation` is either:

- a **bare alias** (or pinned ID) — `"opus"` resolves to `(model=opus, no effort override)`, exactly as today; or
- an **`alias@effort`** form — `"sonnet@xhigh"` resolves to `(model=sonnet, effort=xhigh)`. The part before `@` is the model (alias or pinned ID); the part after `@` is the effort level passed through to the dispatched role.

Escalating the cheaper dimension first (`sonnet → sonnet@xhigh → opus → fable`) retries at ~Sonnet cost before committing to Opus's higher output pricing. A rung with no `@` never carries an effort override, so existing arrays are unaffected.

**Effort passthrough vs. graceful degradation (issue #3705).** Whether an `alias@effort` rung's effort half actually reaches the dispatched role depends on **which dispatch surface** carries it — the two surfaces differ, and only one exposes a per-call effort knob:

- **`claude` CLI / process-spawn / daemon path — effort IS passed through.** The `claude` CLI exposes `--effort <level>`, and `spawn-claude.sh` threads it via a `LOOM_EFFORT` env → `--effort` passthrough (mirroring the `LOOM_MODEL` → `--model` plumbing; #3705). This is reachable whenever a whole `/loom:sweep` child is spawned as an OS process (`mcp__loom__dispatch_sweep` / a direct `spawn-claude.sh` invocation). It sets a **session-default** effort for that child, and `spawn-claude.sh` logs a structured `spawn-claude: effort=<level>` line for greppable per-run observability (model-parity, #3482). Note this is session-wide for the child, **not** per-rung.
- **In-session Task tool — effort DEGRADES to the bare model.** The sweep dispatches its per-role subagents (Builder/Judge/Doctor) through the **Task tool**, one level deep (see "CRITICAL: One level deep"), and the Task tool exposes **no** effort / reasoning-effort parameter alongside `model`. Because the escalation ladder's per-rung `@effort` is consumed at that per-role dispatch time, a resolved `alias@effort` rung on this path **resolves to the bare model** (the `@effort` suffix is dropped) and the orchestrator emits a **loud log line** noting the degradation, e.g. `escalation: effort plumbing unavailable on Task-tool dispatch — rung 'sonnet@xhigh' degraded to bare model 'sonnet'`. Never treat a malformed or empty effort (`sonnet@`, `sonnet@@x`) as an error — it falls back to bare-model dispatch with the same loud line; model resolution must never block a sweep.

The grammar ships either way so configs stay stable across environments: a `sonnet@xhigh` rung raises effort wherever the CLI/process path carries it, and degrades cleanly to bare `sonnet` on the Task-tool path — no config edit needed in either case, and if the Task tool later gains an effort parameter the same config activates the per-rung bump automatically.

**The `fable` rung.** `fable` (alias, or a pinned frontier-model ID where alias resolution is unavailable at a given tier — do not hard-code a specific ID into shipped defaults) is a valid **top** rung. Because the ladder is consumed as `ladder[min(attempt - 1, len - 1)]` under the Doctor-cycle cap, a `fable` rung placed at index ≥ 3 is only ever reached when `max_doctor_cycles ≥ 3` — i.e. it is **opt-in** and never appears on the shipped default ladder.

**Recommended opt-in deep-ladder recipe** (`.loom/config.json`) — pairs a 4-rung ladder with the cap raise required to reach its deeper rungs (see "Doctor-cycle cap" below):

```json
{
  "sweep": {
    "escalation": ["sonnet", "sonnet@xhigh", "opus", "fable"],
    "max_doctor_cycles": 3
  }
}
```

**Refusal-aware fallback for the `fable` rung.** Fable-class safety classifiers refuse some legitimate security-adjacent work (guard hooks, OAuth token handling, credential scanning) with `stop_reason: "refusal"` — which `classify_error` (`.loom/scripts/lib/classify-error.sh`) reports as `MODEL_REFUSAL`. On a `MODEL_REFUSAL` at a `fable` rung, the orchestrator **re-dispatches the same attempt one rung down** (`fable → opus`) **without consuming a Doctor cycle**. A refusal is a *routing error*, not a quality signal, so it must not eat the escalation / `max_doctor_cycles` budget: the `attempt` counter is unchanged, and the retried Doctor is still the same cycle `k`. This is distinct from a Judge rejection (which advances the attempt and escalates *up*). Only the `fable` rung has a rung below it to fall to; a `MODEL_REFUSAL` at a non-`fable` rung is handled by the normal error path.

**No-Fable-Judge invariant (restated).** Judge dispatch never resolves to `fable`, regardless of ladder contents — see the invariant under "Model selection for subagent dispatch". The ladder here governs only the rejection-triggered Doctor.

### Doctor-cycle cap (`sweep.max_doctor_cycles`, issue #3668)

The Doctor→Judge cycle cap bounds how many times a single PR can bounce between Judge and Doctor before it is blocked for human attention. It exists to stop Judge/Doctor disagreement loops and bound worst-case latency. The cap is configurable in `.loom/config.json` under `sweep.max_doctor_cycles`, read once at lifecycle-entry time the same way `sweep.escalation` is:

```json
{
  "sweep": {
    "max_doctor_cycles": 1
  }
}
```

Three states:

| `sweep.max_doctor_cycles` value | Behavior |
|---------------------------------|----------|
| Key absent | Default cap of **1** applies — one Doctor→Judge cycle per PR (the historical behavior) |
| Invalid (non-integer, or `< 1`) | Falls back to the default cap of **1** and logs a warning; a malformed config never blocks a sweep |
| Valid integer `>= 1` | Up to that many Doctor→Judge cycles per PR before the PR is blocked |

**Counting.** A "cycle" is one Doctor pass plus the re-Judge that evaluates it. The cap reuses the existing `attempt` checkpoint field: attempt 1 is the Builder's PR (or the PR as it enters Mode C); the Doctor dispatched after the first Judge rejection is attempt 2 (cycle 1), the Doctor after the second rejection is attempt 3 (cycle 2), and so on. Doctor cycle `k` is permitted while `k <= max_doctor_cycles` (equivalently `attempt <= max_doctor_cycles + 1`). When the cap is reached and Judge still requests changes, block the PR (`PR #P blocked: doctor cycle exhausted after <k> Doctor→Judge round(s); human attention required`) and advance to the next candidate. The `attempt` value written on each Doctor cycle is `k + 1`; the checkpoint schema already accepts any positive integer, so no plumbing change is needed to reach attempt 3+.

**Escalation composes.** Because the ladder is consumed as `ladder[min(attempt - 1, len - 1)]`, raising the cap activates deeper rungs automatically (see "Model escalation on Judge rejection" point 3). The cap and the ladder are independent knobs.

**Distinct-defect exception (default cap only).** When `max_doctor_cycles` is at its **default of 1** and the *second* Judge rejection names a defect that is demonstrably **distinct** from the first rejection's defect — forward progress (the first fix worked and uncovered a genuinely new problem), not thrash (the same disagreement re-litigated) — the orchestrator MAY grant **exactly one** additional bounded Doctor→Judge cycle before blocking. This is a judgment call made by comparing the two Judge rejection comments:

- **Distinct defect** (e.g. rejection 1 = "duplicate ampacity rules"; rejection 2 = "root-only test-permission flaw uncovered after the dedup fix") → grant one grace cycle, and **emit a required log line** naming the distinction, matching the block-log convention so the grant is auditable:
  `PR #P: granted one extra Doctor cycle — second rejection is a distinct defect (<short reason>)`.
- **Same defect re-rejected, or ambiguous** → **block immediately** per the cap. The anti-thrash guarantee is unchanged for the thrash case.

Constraints that keep the exception from becoming an unbounded loop:

- It is **single-use per PR** — one grace cycle only. A *third* rejection after the grace cycle always blocks, even if it too looks distinct.
- It applies **only at the default cap** (`max_doctor_cycles == 1`). When an operator has already raised the cap above 1, the exception does **not** compose on top — the configured cap is the entire budget. (Layering a per-rejection grace cycle onto an operator-raised cap would reintroduce the indefinite-thrash risk the cap exists to prevent.)
- The distinction MUST be stated in the log line. An unlogged grace cycle is a bug.

### Model-cost experiment mode (`sweep.modelExperiment` / `LOOM_MODEL_EXPERIMENT`, issue #3725)

This mode instruments a sweep to produce the balanced A/B evidence #3718 needs to decide the Builder `opus → sonnet` retune. **It is off by default and is byte-for-byte a no-op when unset** — every deterministic instruction below runs only when the mode resolves to `observe` or `experiment`. All the arithmetic (mode resolution, arm assignment, the durable append, the harvest) lives in `./.loom/scripts/sweep-experiment.sh` (a thin stub over `loom_tools.sweep_experiment`); this skill never computes a modulo by hand.

**Tri-state resolution (read once at lifecycle entry, same point as `sweep.escalation`).** Resolve `./.loom/scripts/sweep-experiment.sh resolve-mode` → one of `off` | `observe` | `experiment`. Precedence follows the **string-valued** guard pattern (`guards.rmScope` / `guards.forceScope`), not the boolean one:

- highest: `LOOM_MODEL_EXPERIMENT` env (`off`/`observe`/`experiment`) → then `.loom/config.json` → `sweep.modelExperiment` → default `off`.
- Unknown/malformed value → treated as `off` with a stderr warning; a bad value **never** aborts the sweep.

The three states:

| Mode | Behavior |
|------|----------|
| `off` | No instrumentation. Zero behavior change. No `.loom/stats/` file is created. |
| `observe` | Passive measurement. No model forcing, no arm. One JSONL record appended per phase (`arm` null). Safe to run anywhere. |
| `experiment` | Active A/B. Builder is forced to the assigned arm's model; records are tagged with the `arm`. **Canary-only** (see Guardrails). |

**Two arms map onto #3718's inequality.** `resolve-mode` in `experiment` picks a per-issue arm via `./.loom/scripts/sweep-experiment.sh assign-arm --issue N --complexity <routine|complex>` → prints `<arm> <model>`:

- **Arm A = opus-first** — Builder forced to `opus`; the normal escalation ladder still applies on Judge rejection.
- **Arm B = sonnet-first + escalate** — Builder forced to `sonnet`; on Judge rejection the Doctor escalates via the existing `sweep.escalation` ladder (#3481), exactly as documented in "Model escalation on Judge rejection". Arm B *is* the candidate policy #3718 is evaluating.

**Deterministic, resume-safe, stratified assignment.** The arm is a pure function of the issue number and the #3702 complexity stratum, so a killed-and-resumed sweep re-running the same issue **lands on the same arm**. The complexity marker is read once (the same grep at the tier-2.5 site) and serves two purposes: the **stratification key** (so both arms see a comparable difficulty mix) and — **only when the experiment is off/observe** — the tier-2.5 bump. In `experiment` mode the bump is suppressed (see the "Experiment-mode suppression" note under tier 2.5).

**Forced-arm precedence.** The forced arm slots into the Builder model-resolution chain **above tier 2.5 / tier 3** but **below tier 1 / tier 2 operator pins**: an explicit dispatch param (tier 1) or a `roleConfig.model` workspace pin (tier 2) still wins — a pinned canary is intentionally opted out of the experiment. The forced arm only ever replaces what tier 2.5 / tier 3 would have resolved for the Builder.

**Durable stats store.** Instrumentation appends one JSONL record per role phase invocation to `.loom/stats/sweep-model-stats.jsonl` (gitignored; survives the merge that deletes the transient checkpoint). Immediately after each phase's `sweep-checkpoint.sh write`, also run:

```bash
./.loom/scripts/sweep-experiment.sh record --mode <mode> --issue N --phase <curator|builder|judge|doctor|merge> \
  --role <role> --model <resolved-model> --arm <A|B|"" > --attempt <k> --complexity <routine|complex> \
  --verdict <pass|changes|""> --agent-id <agent-id> --stats-file .loom/stats/sweep-model-stats.jsonl
```

Each record carries the **HARD deterministic outcome-chain** (`arm`, `model`, `attempt`, `judge_verdict`, `cycle_count`, `complexity`) — which alone answers #3718's inequality (first-attempt Judge-pass rate + mean Doctor cycles × model price) — **plus the `agent-id` join key** for the role invocation (available in the Task-result metadata at dispatch/return time), which the harvest joins against #3726's transcript index to attribute exact cost.

**Token fidelity.** Live per-phase token capture is **not** available at the Task-result boundary; the exact input/output + cache split is recovered at **harvest** time by parsing each role subagent's `agent-<id>.jsonl` `usage` blocks (see below). Each record stamps a `token_fidelity` tag naming the source (`none` | `sweep-aggregate-log` | `transcript`). The deterministic outcome-chain is the load-bearing signal; exact cost just makes it precise.

**Guardrails (load-bearing).** `off` by default; `observe` is safe anywhere. `experiment` is **canary-only**: `resolve-mode` refuses to honor it on a non-canary target and **loudly downgrades to `observe`** unless the operator confirms a canary via an **uncommitted** signal — the `LOOM_MODEL_EXPERIMENT_CANARY=1` env var or the gitignored `.loom/CANARY` sentinel file. The committed `sweep.modelExperimentCanary` config flag is **no longer** an accepted confirmation (#3731): it would propagate with a copied config and fire experiment on production. A git-tracked `.loom/CANARY` is refused for the same reason. The `sweep.modelExperiment` *mode* may still live in committed config — it stays inert without the uncommitted confirmation. At lifecycle entry, print the loud banner naming the active mode, the canary confirmation source, and — in `experiment` — the arm assigned to the issue:

```bash
./.loom/scripts/sweep-experiment.sh banner --issue N --complexity <routine|complex>
```

**Harvest (exact per-role cost).** After a canary run, aggregate the store into the per-arm inequality inputs #3718 consumes — first-attempt Judge-pass rate, mean Doctor cycles, exact cache-aware cost per arm, and the merge-rate quality floor — via the reader alongside `agent-metrics.sh`:

```bash
./.loom/scripts/agent-metrics.sh --model-experiment --archive-dir "$LOOM_TRANSCRIPT_ARCHIVE"
# equivalently: ./.loom/scripts/sweep-experiment.sh harvest --archive-dir "$LOOM_TRANSCRIPT_ARCHIVE"
```

The harvest parses each joined `agent-<id>.jsonl` transcript's `usage` blocks (input/output + `cache_read_input_tokens`/`cache_creation_input_tokens`) and prices them with the same **cache-aware** per-model table as `loom-daemon`'s `resource_usage.rs`. Transcripts are located through #3726's `loom.transcript-index/v1` archive index (`--archive-dir` = `LOOM_TRANSCRIPT_ARCHIVE`); harvest should run periodically (cron) over a multi-day canary so usage is extracted into the compact stats store before `~/.claude/projects` is pruned.

> **Daemon detached-child path (honest finding, verified against on-disk transcripts).** The role-subagent transcripts of a daemon-dispatched `claude -p "/loom:sweep N"` child land under that child's own `${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects/<cwd-slug>/<child-session-uuid>/subagents/agent-<id>.jsonl` tree — the **durable** location, not the ephemeral `/tmp/.../tasks/` scratch — and each carries the full per-message `usage` (input/output + cache split) and `model`. Confirmed present on disk for real detached-child sessions. So they are archivable/harvestable via the same #3726 periodic sync. What the daemon reaper does **not** yet know is the child's session-uuid, so it cannot trigger a precise single-session archive on exit — the cron periodic sync is the backstop, exactly as for the completion hook (see "Session Transcript Archival").

### Other constraints

- **Do NOT write to `.loom/daemon-state.json`.** That file is owned by the standalone daemon. `/loom:sweep` runs independently and must not race with the daemon on shepherd-slot bookkeeping. Reading `daemon-state.json` for situational awareness is fine; writing is not.

## Sweep Run Identity + Peer-`/loom:sweep` Detection (#3768)

Before **any** other stage — including Backend detection (Stage -1), the dry-run gate, and all wave lifecycles — establish a **stable identity for this sweep invocation** and probe for a concurrently-running peer `/loom:sweep`. This runs for **all modes (A, B, and C)** — it is *not* short-circuited by Mode C or `--no-daemon` (those only affect the Stage -1 backend probes below).

This section exists because `/loom:sweep` was originally hardened (#3373 checkpoints, #3648 baseline) assuming a single sweep instance per repo. Two concurrent `/loom:sweep` runs in the same repo (observed live 2026-07-22) collided on shared run-state: they shared the single fixed main-clean baseline path (one clobbered the other's pre-sweep snapshot), and their checkpoints were indistinguishable because `task_id` was `sweep-$$` — the PID of each Bash *subshell*, which varies *within* a single sweep across tool calls, not a stable per-invocation id.

### Step 0a: Generate the stable run id (once, at sweep start)

Run this **exactly once**, before anything else:

```bash
RUN_ID=$(./.loom/scripts/sweep-run-registry.sh new)
echo "sweep run id: $RUN_ID"
```

`sweep-run-registry.sh new` generates a portable (macOS/Linux, no `uuidgen`) run id combining a UTC timestamp + PID + random suffix (e.g. `sweep-20260722T231500Z-84213-a3f9c1`), and registers it under `.loom/sweep-run/<RUN_ID>.json` (gitignored) with a liveness PID (the orchestrator `$PPID`) for peer detection.

**Treat the printed `RUN_ID` as a fixed literal for the entire rest of this sweep.** Thread it — as that literal string — into every `--task-id "$RUN_ID"` checkpoint write and into the main-clean baseline path below. Do **NOT** regenerate it per Bash tool call, and do **NOT** fall back to `sweep-$$` (that is the exact bug this fixes: `$$` is a fresh subshell PID on every tool call). If you ever lose track of the literal mid-sweep, recover it from the registry rather than minting a new one:

```bash
RUN_ID=$(./.loom/scripts/sweep-run-registry.sh list | awk -v p="$PPID" '$2==p {print $1; exit}')
```

At sweep completion (or abort), remove this run's registry entry:

```bash
./.loom/scripts/sweep-run-registry.sh cleanup "$RUN_ID"
```

This is best-effort cleanup — a dead run's entry is also pruned automatically by any later sweep's peer scan (dead-PID liveness check), so a crash that skips cleanup never leaves a permanent false-positive.

### Step 0b: Peer-`/loom:sweep` detection (loud, NON-BLOCKING)

Immediately after registering, probe for other **live** `/loom:sweep` runs in this repo and warn if any are found — never block, never auto-stop (mirroring the Daemon Coexistence contract):

```bash
PEERS=$(./.loom/scripts/sweep-run-registry.sh peers "$RUN_ID")
if [[ -n "$PEERS" ]]; then
  echo "⚠️  ANOTHER /loom:sweep IS RUNNING IN THIS REPO:" >&2
  echo "$PEERS" | while read -r rid pid ts; do
    echo "       run $rid (pid $pid, started $ts)" >&2
  done
  echo "   Two concurrent sweeps merge into a moving default branch unaware of" >&2
  echo "   each other. Per-issue loom:building claims still prevent double-builds," >&2
  echo "   and each sweep now keys its own main-clean baseline + checkpoints by its" >&2
  echo "   own RUN_ID, so they will not clobber each other's run-state — but you" >&2
  echo "   should be aware both are advancing main. Proceeding (non-blocking)." >&2
fi
```

The `peers` subcommand only reports runs whose recorded PID is still alive (`kill -0`); it prunes any dead-PID entry as a side effect, so a sweep killed with SIGKILL mid-run does not produce a false-positive warning forever. Empty output → no peer → the single-sweep case, no warning printed (byte-for-byte the prior behaviour). **Do not block, do not auto-stop the peer, do not abort** — the peer sweep is legitimate; this is situational awareness only. See "Coexistence (peer `/loom:sweep` and legacy daemon)" for how this relates to the legacy daemon-PID check.

## Stage -1: Backend detection (Phase D of #3449)

Before the dry-run gate and all wave lifecycles (but **after** Sweep Run Identity above), decide whether to **delegate dispatch to the in-process loom-daemon** or **fall through to the existing in-process subagent dispatch**. This stage is prose for the LLM running this skill; it does not run a separate binary. Implementation is small, side-effect-free probes followed by a single routing decision.

This stage exists because Phase A of epic #3449 (#3452) shipped `mcp__loom__dispatch_sweep`, an MCP tool that queues a sweep on the daemon's spawn queue and returns immediately. When the daemon is reachable **and** a multi-account token pool is configured, dispatching to the daemon means each sweep runs in its own detached process with its own rotated OAuth token — load is balanced across accounts, and the orchestrator session exits sub-2-second after dispatch. When either precondition is missing, today's Mode A/B/C subagent path is the right choice — it works on a solo token, it doesn't depend on a running daemon, and it is the verified behaviour for the v0.9.x line.

The contract is **strict AND between two preconditions**, with an explicit Mode C short-circuit and an explicit `--no-daemon` opt-out. There is **no implicit auto-start** of the daemon if the pool exists but the daemon is down; there is **no implicit "use daemon if reachable even without a pool"** branch. Either probe failing → subagent fallthrough.

### Decision tree (the contract)

```text
PROBE_MODE:
  If --prs flag present OR any PR-side NL trigger detected → Mode C (subagent always)

PROBE_DAEMON:
  Ping ~/.loom/loom-daemon.sock with 500ms timeout. Pong → reachable.

PROBE_POOL:
  Count *.token files in .loom/tokens/ OR ACCOUNT_KEY_* lines summed across the merged
  claude-monitor / .loom/accounts.env / legacy .env account sources. Pool exists if count >= 2.

DECIDE:
  if Mode C: use_subagent()
  elif --no-daemon: use_subagent()
  elif LOOM_SWEEP_CLAIM_OWNED is set: use_subagent()   # daemon-owned child — skip re-probe entirely (#3829)
  elif PROBE_DAEMON AND PROBE_POOL: use_daemon()
  else: use_subagent()
```

The precedence is deliberate:

1. **Mode C → subagent** (always, regardless of daemon/pool state). The daemon's dispatch surface is **issue-keyed only** in v0.10.0 (`mcp__loom__dispatch_sweep --kind '{"Issue":N}'`); PR-set dispatch is an explicit non-goal of the parent epic and is not on the v0.10.0 roadmap. PR-set sweeps therefore route to the existing in-process subagent path, which already supports Mode C end-to-end.
2. **`--no-daemon` → subagent** (operator opt-out, after Mode C but before any probes). When this flag is present, do not even attempt the `PROBE_DAEMON` Ping — saves a 500ms ceiling and produces predictable behaviour for debug/demo/scripted runs.
3. **`LOOM_SWEEP_CLAIM_OWNED` set → subagent** (daemon-owned child self-detection, #3829 — after `--no-daemon`, still **before** any probes). This env var is exported **only** into a child that `loom-daemon` itself dispatched (`SweepRegistry::dispatch` → `spawn_child`, `sweep_registry.rs`), carrying the issue number the daemon already claimed on this child's behalf (same marker the "1. Per-issue pre-flight" self-claim exception from #3823 consumes one stage later). A daemon-dispatched child is **by construction** running in the exact environment that makes `PROBE_DAEMON ∧ PROBE_POOL` true — a live daemon plus a multi-account pool, since that is *why* it was dispatched there — so without this rule it would always land on `use_daemon` and issue a **circular** MCP round-trip back into the very daemon that spawned it (`mcp__loom__list_sweeps`, or worse a self-re-dispatch of its own issue number). In headless `claude -p` mode there is no operator to interrupt a stuck tool call and Stage -1's "500ms timeout" is LLM-directed prose, not a mechanically-enforced transport guard, so that round-trip can hang the whole session idle before it ever reaches the Builder phase. The child is already the daemon's work — it must run the lifecycle **itself**, in-process, exactly like `--no-daemon`. This short-circuit removes the entire class of hang. Mirrors `--no-daemon`: do not even attempt the `PROBE_DAEMON` Ping.
4. **`PROBE_DAEMON ∧ PROBE_POOL → daemon`** (the only way to land on the daemon path). **Strict AND**: both probes must succeed. Either missing → fallthrough.
5. **Else → subagent** (the universal fallthrough, equivalent to v0.9.x behaviour).

### The three probes

#### PROBE_MODE — mode classification (already done)

Mode classification happens in the existing "Mode-selection precedence" rules above (Arguments → Validation rules). By the time Stage -1 runs, the skill knows whether it is in Mode A, B, or C. **If the mode is C, the decision is already made — go straight to the subagent path** (the "Stage 0: Dry-run gate" section below, then "PR-set Wave Lifecycle"). Do not run the daemon or pool probes for Mode C.

#### PROBE_DAEMON — is the loom-daemon reachable?

The daemon listens on `~/.loom/loom-daemon.sock` (a Unix-domain socket). A reachability probe is a cheap `mcp__loom__list_sweeps` invocation — the daemon answers with the current sweep list (which may be an empty array if the daemon is up but no sweeps are queued). Either a successful response **or** an empty-list response is a "pong" — the daemon is reachable.

Use a **500ms timeout** on this probe. The MCP layer accepts a timeout parameter; do not raise it. The 500ms ceiling covers two failure modes simultaneously:

- **No daemon running.** The Unix socket file does not exist, or the connection refused immediately. The MCP call returns an error in well under 500ms; treat as `PROBE_DAEMON = false`.
- **Stale socket.** The socket file exists but no process is listening (e.g., the daemon crashed without cleanup). The connection hangs until the OS times out — that's the 500ms guard. Timeout → treat as `PROBE_DAEMON = false`. **Do not retry, do not auto-clean the stale socket, do not auto-start the daemon.** Those behaviours belong in operator tools, not in this skill.

A successful response (any well-formed `EventStream`/sweep-list payload, including the empty case) → `PROBE_DAEMON = true`.

```text
PROBE_DAEMON pseudocode (LLM-directed):

  if NO_DAEMON or LOOM_SWEEP_CLAIM_OWNED is set:
      PROBE_DAEMON = false   # short-circuit; do not even issue the call
                             # (LOOM_SWEEP_CLAIM_OWNED: daemon-owned child, #3829 —
                             #  re-probing the spawning daemon is circular)
  else:
      try:
          response = mcp__loom__list_sweeps(timeout_ms=500)
          PROBE_DAEMON = true        # any structured response = reachable
      except timeout, connection_error, no_such_tool:
          PROBE_DAEMON = false
```

The `no_such_tool` case covers older Loom installs without Phase A's MCP additions — treat as "daemon not reachable" and fall through. Do not try to detect the daemon by other means (no `ps` parsing, no PID file reads — the socket probe is the authoritative reachability test).

#### PROBE_POOL — does a multi-account token pool exist?

A pool exists if **either** of these is true (logical OR, both checked):

1. **Materialized pool**: `.loom/tokens/*.token` contains **two or more** files. The bootstrap step (`loom-tokens bootstrap`) writes one `*.token` file per `ACCOUNT_KEY_*` triple in the merged account set; a count `>= 2` means at least two distinct accounts are available for rotation.
2. **Configured pool**: **two or more** `ACCOUNT_KEY_*` lines are declared across the **merged account sources** — the claude-monitor master (`${LOOM_CLAUDE_MONITOR_DIR:-$HOME/.claude-monitor}/accounts.env`), the repo-local file (`.loom/accounts.env`, falling back to the legacy `.env`), and — **only when `LOOM_ACCOUNTS_ENV` is set** — the opt-in home master at that path. This catches the case where the operator has configured multiple accounts (in the post-#3695/#3704 claude-monitor-first layout, not just the legacy `.env`) but hasn't yet run `loom-tokens bootstrap` — the daemon's spawn-time selector can still pick a token, and the pool will be materialized on demand.

Both checks are cheap, local, and side-effect-free. The configured-pool count mirrors `bootstrap.py`'s source precedence but does **not** dedupe by email — a raw sum of `ACCOUNT_KEY_*` lines is an accepted approximation for this boolean `>= 2` gate (worst case a single account declared in two sources double-counts at the `== 1` vs `== 2` boundary, a false-positive toward daemon use that still requires `PROBE_DAEMON` to also be true):

```bash
TOKEN_FILE_COUNT=$(ls .loom/tokens/*.token 2>/dev/null | wc -l | tr -d ' ')

# Repo-local (mirrors bootstrap.py: .loom/accounts.env if present, else legacy .env)
# NOTE: `grep -c` prints `0` AND exits non-zero on an existing-but-empty file, so a
# `|| echo 0` fallback would emit a two-line "0\n0" and abort the arithmetic below under
# bash 3.2. Use `|| true` + `${var:-0}` so an existing-empty source yields exactly `0`.
if [[ -f .loom/accounts.env ]]; then
  REPO_KEY_COUNT=$(grep -c '^ACCOUNT_KEY_' .loom/accounts.env 2>/dev/null || true); REPO_KEY_COUNT=${REPO_KEY_COUNT:-0}
else
  REPO_KEY_COUNT=$(grep -c '^ACCOUNT_KEY_' .env 2>/dev/null || true); REPO_KEY_COUNT=${REPO_KEY_COUNT:-0}
fi

# claude-monitor master (primary source per CLAUDE.md; LOOM_CLAUDE_MONITOR_DIR override)
MONITOR_DIR="${LOOM_CLAUDE_MONITOR_DIR:-$HOME/.claude-monitor}"
MONITOR_KEY_COUNT=$(grep -c '^ACCOUNT_KEY_' "$MONITOR_DIR/accounts.env" 2>/dev/null || true); MONITOR_KEY_COUNT=${MONITOR_KEY_COUNT:-0}

# Opt-in home master — only consulted when LOOM_ACCOUNTS_ENV is set and non-empty (per #3704)
HOME_KEY_COUNT=0
if [[ -n "${LOOM_ACCOUNTS_ENV:-}" ]]; then
  HOME_KEY_COUNT=$(grep -c '^ACCOUNT_KEY_' "$LOOM_ACCOUNTS_ENV" 2>/dev/null || true); HOME_KEY_COUNT=${HOME_KEY_COUNT:-0}
fi

ENV_KEY_COUNT=$(( REPO_KEY_COUNT + MONITOR_KEY_COUNT + HOME_KEY_COUNT ))
if (( TOKEN_FILE_COUNT >= 2 )) || (( ENV_KEY_COUNT >= 2 )); then
  PROBE_POOL=true
else
  PROBE_POOL=false
fi

# Discoverable signal: accounts configured but not yet bootstrapped. Only fires when
# the merged sources declare a pool (ENV_KEY_COUNT >= 2) yet .loom/tokens/ has < 2
# token files — NOT on every subagent fallthrough.
if (( ENV_KEY_COUNT >= 2 )) && (( TOKEN_FILE_COUNT < 2 )); then
  echo "Configured account pool detected but not bootstrapped — run 'loom-tokens bootstrap' to materialize .loom/tokens/." >&2
fi
```

A single-token configuration (`TOKEN_FILE_COUNT == 1` and `ENV_KEY_COUNT <= 1`) is **not** a pool — the daemon dispatch path needs at least two accounts to make rotation meaningful, and a single-token operator gets no benefit from delegating to the daemon. Fall through to the subagent path in that case.

> **Why >= 2 and not >= 1?** A pool of one is not a pool — it is a single token, and rotation requires alternatives. The daemon's dispatch advantage (per-sweep token selection, weekly-quota recovery) only materializes once two-or-more accounts are configured. Single-token operators see no degradation in the subagent path; this preserves the existing solo-token experience.

### Resolve auto wave size (when `BUILDERS_PER_WAVE = auto`)

Run this **after `DECIDE` is known** (both probes done) and **before** taking the daemon-dispatch or subagent-fallthrough branch below. If `BUILDERS_PER_WAVE` is a concrete integer (the operator passed `--builders-per-wave N`), **skip this entire block** — the explicit value wins and flows into the wave-partition consumers unchanged. Mode C also never reaches this block: Mode C is size-1 and ignores `--builders-per-wave` (the `DECIDE` precedence already routed it to the subagent path).

The disk math lives in a small sourceable helper so it is deterministic and unit-tested (`defaults/scripts/lib/disk-headroom.sh`, tested by `defaults/scripts/tests/test-disk-headroom.sh`). The skill sources it and calls two functions; it does not do the arithmetic inline:

```bash
source ./.loom/scripts/lib/disk-headroom.sh
REPO_ROOT="$(git rev-parse --show-toplevel)"
FREE_GB="$(loom_worktree_root_free_gb "$REPO_ROOT")"   # df's the RESOLVED worktree root (scratch volume), not the repo drive
```

Then resolve by branch (`CAND` = number of surviving candidate issues):

```bash
if [[ "$DECIDE" == use_daemon ]]; then
    # Detached-process path: each sweep is its own OS process with its own
    # rotated token. NOT nested subagents, so #3289 does not apply — scale to 10.
    MECH=daemon;   MECHANISM="daemon detached-process"
else  # use_subagent (no daemon, single-token pool, --no-daemon, daemon-owned child, or Mode C)
    # In-session Task subagents, one level deep. WIDTH is bounded by the harness
    # concurrency cap (min(16, cores-2)), NOT by #3289 (which is a nesting rule,
    # not a width rule). Core-scale the subagent target within [3, 6] via
    # loom_subagent_target_from_cores (#3693); an operator-set LOOM_SUBAGENT_WAVE_CAP
    # always wins (the `:=` only fills an unset/empty value).
    : "${LOOM_SUBAGENT_WAVE_CAP:=$(loom_subagent_target_from_cores "$(loom_detect_cores)")}"
    export LOOM_SUBAGENT_WAVE_CAP
    MECH=subagent; MECHANISM="in-session subagent"
fi
# The helper prints two lines: size on line 1, reason token on line 2.
# Capture both without `mapfile` (a bash-4.0+ builtin) so this works under
# macOS's default /bin/bash 3.2: grab stdout once, then split by line.
_WS_OUT="$(loom_wave_size_from_disk "$MECH" "$CAND" "$FREE_GB")"
WAVE_SIZE="$(sed -n '1p' <<<"$_WS_OUT")"; REASON="$(sed -n '2p' <<<"$_WS_OUT")"
```

`loom_wave_size_from_disk` prints two lines — the clamped size `K = min(target, floor(free_gb / LOOM_PER_WORKTREE_GB), CAND)` with a floor of 1 (never 0, even on a full disk) on line 1, and a machine reason token (`target` / `candidates` / `disk` / `floor`) on line 2. `LOOM_PER_WORKTREE_GB` defaults to a conservative 2 GB and is env-overridable for large-repo operators. The target is **10** for the daemon path; for the subagent path it is the **core-scaled** `clamp(floor((cores-2)/4), 3, 6)` (#3693) — resolved into `LOOM_SUBAGENT_WAVE_CAP` just above via `loom_subagent_target_from_cores` / `loom_detect_cores`, floor 3 on small/shared hosts, ceiling 6 on big ones — and an operator-set `LOOM_SUBAGENT_WAVE_CAP` env value always overrides it.

**Emit a one-line reason** so the operator understands any reduction. Map the reason token to a human sentence, adding the backend-specific context:

| `DECIDE` / reason | One-line log |
|-------------------|--------------|
| `use_daemon`, `target` | `wave size 10, mechanism=daemon: daemon + multi-account pool → detached-process path (target 10)` |
| `use_subagent`, `target`, daemon not reachable | `wave size K, mechanism=subagent: daemon not reachable → subagent path (core-scaled target K, floor 3, ceiling 6)` |
| `use_subagent`, `target`, no pool | `wave size K, mechanism=subagent: single-token pool → subagent path (core-scaled target K, floor 3, ceiling 6)` |
| any, `candidates` | `wave size K, mechanism=<m>: reduced to K (only K candidate issues)` |
| any, `disk` | `wave size K, mechanism=<m>: reduced to K (only <FREE_GB> GB free on <worktree-root>)` |
| any, `floor` | `wave size 1, mechanism=<m>: reduced to 1 (only <FREE_GB> GB free on <worktree-root>)` |

The resolved `WAVE_SIZE` replaces `--builders-per-wave` everywhere the wave-partition consumers below reference it. On the **daemon path** `WAVE_SIZE` is the concurrency **target** the operator should expect (and that `--dry-run` reports) — the daemon runs each candidate as an independent detached process, so it is not a hard in-session partition. On the **subagent path** `WAVE_SIZE` is the literal wave partition size feeding the `min(...)` dispatch expression in the Wave Lifecycle. In both cases, **never raise the subagent ceiling toward 10** — the subagent auto default core-scales within `[3, 6]` (#3693), and true high parallelism toward 10 is the daemon path's job. (This is a width ceiling; the #3289 "one level deep" nesting rule is a separate, unchanged constraint the daemon path exists to route around.)

### The daemon-dispatch path (when `DECIDE = use_daemon`)

When `DECIDE` lands on `use_daemon`, the skill **dispatches each candidate issue** to the daemon and **exits sub-2-second**. There is no in-session orchestration after dispatch — operators monitor with `mcp__loom__list_sweeps` (Phase A) or the richer Phase C tools once they land.

For each candidate issue `N` in the candidate set:

```text
mcp__loom__dispatch_sweep(kind={"Issue": N})
```

**When `AUTO_STACK=true` and edge detection populated `DEPENDS_ON[N]` for candidate `N`** (see "Auto-stack detection and wave ordering"), forward the detected parent on the dispatch:

```text
mcp__loom__dispatch_sweep(kind={"Issue": N}, depends_on=<parent>)
```

This is purely "start populating a parameter that already exists" — the daemon and the `mcp__loom__dispatch_sweep` schema already accept `depends_on` (#3729/#3742), forwarding it to the child as `--depends-on <parent>`, so there is **no daemon-side code change**. Candidates with no detected edge dispatch exactly as today (no `depends_on` argument). To respect the parent-before-child topological ordering on the daemon path, dispatch the reordered candidate list in order (a parent stacked-before its child is dispatched first so its `feature/issue-<parent>` branch exists when the child's Builder resolves the base).

The daemon enqueues the sweep, returns a sweep ID, and the skill logs the dispatch (`Dispatched sweep <sweep-id> for issue #N to daemon`). The daemon's spawn-time logic picks an OAuth token from the rotation pool, detaches a `claude -p "/loom:sweep N"` child, and runs the sweep in that child's session — completely independent of this orchestrator session.

**The skill does NOT subscribe to events.** Phase B's pub/sub bus is consumed by long-running monitors and the spawn loop, not by the skill itself. The skill is fire-and-forget: dispatch, log, exit.

**Mode C is excluded.** Mode C uses `--prs` (or NL triggers); the daemon does not handle PR-set dispatch in v0.10.0. If `PROBE_MODE` returned Mode C, this branch is unreachable — the `DECIDE` precedence sends Mode C to subagent before this branch is evaluated.

**Exit immediately after the last `mcp__loom__dispatch_sweep` returns.** Do **not** run the dry-run gate, the issue-side wave lifecycle, or any of the "0." through "8." stages below — those are subagent-path-only and would double-orchestrate. The skill's job in the daemon path is dispatch and exit; the daemon-side child runs the full Curator → Builder → Judge → Doctor → Merge lifecycle in its own session.

**Dry-run interaction:** when `--dry-run` is passed alongside the daemon path, **the dry-run gate (Stage 0) still runs and the skill EXITs without dispatching**. Dry-run is a read-only contract independent of backend choice; it prints the candidate plan and exits without mutation regardless of whether the daemon would have been used. This is intentional — operators previewing a sweep should see the plan before any backend dispatches.

### The subagent fallthrough (when `DECIDE = use_subagent`)

Otherwise — `DECIDE` is `use_subagent` for **any** of the reasons above (Mode C, `--no-daemon`, `LOOM_SWEEP_CLAIM_OWNED` set (daemon-owned child, #3829), daemon unreachable, no pool, or any probe error) — **continue to "0. Dry-run gate" below and run the existing Mode A/B/C lifecycle in-process exactly as today**. This is the v0.9.x behaviour, unchanged. The skill prose from "0. Dry-run gate" onward is the canonical subagent path.

No behaviour change for solo-token operators: their `PROBE_POOL` returns `false`, the `DECIDE` lands on `use_subagent`, and the rest of the skill runs as it always has.

### Smoke tests (documented expectations)

These are the AC #3 and AC #4 contracts, written for the operator.

**Daemon-on + multi-account pool (AC #3):**

```bash
# Preconditions:
#   - loom-daemon is running (`pgrep loom-daemon` matches, ~/.loom/loom-daemon.sock exists)
#   - At least 2 accounts configured — in .loom/tokens/, or ACCOUNT_KEY_* lines across
#     the merged claude-monitor / .loom/accounts.env / legacy .env account sources

/loom:sweep 123 456

# Expected:
#   1. Stage -1 runs: PROBE_MODE=A, PROBE_DAEMON=true, PROBE_POOL=true.
#   2. DECIDE = use_daemon.
#   3. Skill calls mcp__loom__dispatch_sweep for issue 123 → logs sweep ID.
#   4. Skill calls mcp__loom__dispatch_sweep for issue 456 → logs sweep ID.
#   5. Skill exits in < 2 seconds.
#   6. Daemon runs the two sweeps independently in detached processes.
#   7. Operator monitors progress via mcp__loom__list_sweeps or Phase C tools.
```

**Daemon-off OR single-token (AC #4):**

```bash
# Preconditions:
#   - Either loom-daemon is not running, OR the merged account sources
#     (claude-monitor / .loom/accounts.env / legacy .env) have < 2 ACCOUNT_KEY_* lines total.

/loom:sweep 123 456

# Expected:
#   1. Stage -1 runs: PROBE_MODE=A, PROBE_DAEMON or PROBE_POOL is false.
#   2. DECIDE = use_subagent.
#   3. Skill continues to "0. Dry-run gate" → "Resolve auto wave size" → "Wave Lifecycle".
#   4. Auto wave size resolves to the subagent path (core-scaled target in [3,6],
#      candidate- and disk-clamped): both issues land in one wave of 2 (clamped
#      to the candidate count, or fewer if the scratch volume is tight).
#   5. Each issue runs Curator→Builder→Judge→Doctor→Merge in-session.
#      (Pass an explicit --builders-per-wave 1 to force the old fully-sequential behaviour.)
#   6. Skill exits when both issues have settled (potentially many minutes).
```

**`--no-daemon` opt-out:**

```bash
# Preconditions: any. The flag forces the subagent path.

/loom:sweep 123 456 --no-daemon

# Expected:
#   1. Stage -1 sees NO_DAEMON=true → PROBE_DAEMON skipped entirely.
#   2. DECIDE = use_subagent.
#   3. Skill continues to "0. Dry-run gate" → "Wave Lifecycle" → ... exactly as today.
```

**Mode C (PR-set):**

```bash
# Preconditions: any. Mode C short-circuits Stage -1's daemon path.

/loom:sweep --prs 200 201

# Expected:
#   1. PROBE_MODE = C (because --prs is present).
#   2. DECIDE = use_subagent (regardless of daemon/pool state).
#   3. Skill continues to "0. Dry-run gate" → "PR-set Wave Lifecycle" → ... exactly as today.
```

**Daemon-owned child (`LOOM_SWEEP_CLAIM_OWNED` set, #3829):**

```bash
# Preconditions: this session is itself a child that loom-daemon dispatched, so
#   LOOM_SWEEP_CLAIM_OWNED=<N> is exported into its environment (by
#   SweepRegistry::dispatch → spawn_child). The daemon and multi-account pool are
#   therefore reachable BY CONSTRUCTION — but this child must NOT re-dispatch.

# (the daemon internally runs, for the issue it claimed:)
#   LOOM_SWEEP_CLAIM_OWNED=123 claude -p "/loom:sweep 123" --dangerously-skip-permissions

# Expected:
#   1. Stage -1 sees LOOM_SWEEP_CLAIM_OWNED is set → PROBE_DAEMON skipped entirely
#      (never issues mcp__loom__list_sweeps back into the spawning daemon).
#   2. DECIDE = use_subagent regardless of daemon/pool reachability.
#   3. Skill continues to "0. Dry-run gate" → "Wave Lifecycle" → runs the full
#      Curator→Builder→Judge→Doctor→Merge lifecycle IN-PROCESS, exactly like --no-daemon.
#   4. No circular re-dispatch of its own issue number; no idle-hang on a stuck
#      MCP round-trip. This is the #3829 fix — every daemon-dispatched child
#      progresses to build rather than stalling in Stage -1.
```

### What Stage -1 does NOT do

- **Does not auto-start the daemon** if the pool exists but the daemon is unreachable. Auto-start is operator policy, not skill policy.
- **Does not write `~/.loom/loom-daemon.sock` cleanup** for stale sockets. Stale-socket cleanup belongs to the daemon's own startup logic and to operator tools.
- **Does not subscribe to the Phase B event bus.** Subscription is consumed by long-running monitors and the spawn loop, not by this skill. Phase D is dispatch-only.
- **Does not retry probe failures.** Either probe returns within 500ms (or its natural latency) and is treated as authoritative; no retry, no backoff.
- **Does not mutate any forge state** during the probes. `mcp__loom__list_sweeps` and the local pool checks are read-only. Even in the daemon path, mutation happens inside the daemon-side child sweep, not in this orchestrator session.
- **Does not log to `.loom/daemon-state.json` or any daemon-owned state file.** Read-only access is fine for situational awareness; writes are forbidden (same constraint as the legacy-daemon subsection of "Coexistence (peer `/loom:sweep` and legacy daemon)").
- **Does not re-probe or re-dispatch to the daemon when it is itself a daemon-dispatched child (#3829).** If `LOOM_SWEEP_CLAIM_OWNED` is set, the child is already the daemon's work — the `DECIDE` tree short-circuits to `use_subagent()` **before** `PROBE_DAEMON` runs, so no `mcp__loom__list_sweeps` (and no `mcp__loom__dispatch_sweep` of its own issue) is ever issued back into the spawning daemon. Re-probing/re-dispatching there is circular by construction and, in a headless `-p` session with no operator to interrupt a stuck tool call, was the cause of the idle-hang this rule removes.

## 0. Dry-run gate (if `--dry-run`)

If `--dry-run` was supplied, **this stage runs before any mutation** and EXITs after printing the plan. The dry-run gate is the single inviolable contract of `--dry-run`: no label edits, no `worktree.sh` invocation, no `gh pr create`, no `merge-pr.sh`, no daemon-state writes, no Task/subagent dispatch. This contract is uniform across Modes A, B, and C.

### Procedure — Modes A and B (issue-set)

1. **Survey each candidate (read-only).** For every deduplicated, validated issue number `N` in the candidate list:
   ```bash
   gh issue view N --json number,title,labels,state --jq '{number, title, state, labels: [.labels[].name]}'
   ```
   This is a `gh issue view` read — it does not mutate anything. (If `gh` is unauthenticated or the issue is unreachable, log the error against that candidate and continue surveying the rest.)

   **When `AUTO_STACK=true`, add `body` to this same read** (`gh issue view N --json number,title,labels,state,body ...`) — no extra API call, one field added — and run the edge-detection pass described in "Auto-stack detection and wave ordering (`--auto-stack`, #3759)". Absent `--auto-stack`, `body` is not fetched and no detection runs (byte-for-byte unchanged).

1a. **Resolve stacking edges (only when `AUTO_STACK=true`).** Detect `Depends on #A` / `Requires #A` edges, keep only those whose `#A` is a member of this candidate set, reduce to a single parent per child (first-match-wins), drop cyclic edges — all per "Auto-stack detection and wave ordering". Populate the per-issue `DEPENDS_ON[N]` map. When zero edges survive, the run proceeds exactly as if `--auto-stack` were absent.

2. **Compute wave partition.** Partition the candidate list into waves of size `--builders-per-wave`, or the Stage -1 resolved auto wave size when the flag was omitted (see "Resolve auto wave size"), preserving input order. Record `(issue, wave_index, total_waves)` for each candidate. Apply the same silent-clamp and pre-flight-skip rules that the live path uses (closed / `loom:building` / `loom:blocked` issues are tagged as "would skip" in the plan but still appear in the output for transparency). **When stacking edges were resolved in step 1a, first reorder** so every parent's wave is at or before its child's wave (a parent/child pair may share a wave — the child still branches off the parent's branch, not the shared pre-wave `main` snapshot) per "Auto-stack detection and wave ordering", then partition the reordered list.

3. **Print the plan.** Emit a table or block per the issue-set format below.

4. **EXIT.** Do not proceed to "Wave Lifecycle". The shell must return as soon as the plan is printed.

**Issue-set output spec** (Modes A and B; minimum useful — do **not** add token-pool selection or agent dispatch internals):

```
/loom:sweep --dry-run plan: M candidate(s) across W wave(s) (wave size 10, auto; mechanism=daemon detached-process)
  Wave sizing: daemon + multi-account pool → detached-process path (target 10)

  Wave 1:
    #123  "Add foo widget"                labels: loom:issue                    → would build
    #124  "Fix bar bug"                   labels: loom:curated                  → would curate, build
    #199  "Tweak gizmo"                   labels: loom:issue                    → would route to Judge (existing PR #200 in flight)
  Wave 2:
    #125  "Refactor baz module"           labels: loom:building                 → would skip (already in flight)
    #126  "Document quux"                 labels: (none)                        → would curate, build
    #198  "Polish frobnicator"            labels: loom:issue                    → would merge (existing PR #201 already loom:pr)

Total: 3 would-build, 1 would-route-to-judge, 1 would-merge, 1 would-skip. No issues were modified.
```

When `--builders-per-wave` was passed explicitly, the header shows the number without `auto` and the "Wave sizing" line reads `explicit --builders-per-wave=N` (no mechanism/disk reason). A disk- or candidate-clamped auto run reads e.g. `(wave size 3, auto; mechanism=in-session subagent)` with `Wave sizing: reduced to 3 (only 6 GB free on /Volumes/scratch/loom)`.

**Per-candidate fields (required):**
- Issue number
- Title (truncated reasonably if very long)
- Current labels (comma-separated, or `(none)`)
- Planned action (`would build`, `would curate, build`, `would skip (<reason>)`, `would route to Judge (existing PR #X in flight)`, `would merge (existing PR #X already loom:pr)`). Under the `all` sentinel (`SWEEP_ALL_AGGRESSIVE=true`) the aggressive actions also appear: `would reclaim (stale loom:building), build`, `would unblock (#N merged), build`, `would skip (still blocked by #N)`, `would expand epic (→ #a #b)`, `would skip (needs decomposition)`, `would reclaim (stale loom:abort), build`, `would skip (abort flag set)`, `would skip (operator-only)`.
- Wave assignment (shown via the `Wave N:` group header)

**Header/footer (required):** the header states the resolved wave size (and whether it is `auto` or explicit), the chosen **mechanism** (`daemon detached-process` vs `in-session subagent`), and — on the second line — the one-line **gating reason** from "Resolve auto wave size". The footer states total candidates, total waves, count of `would-build` vs `would-skip`, and an explicit confirmation that nothing was modified. (Dry-run resolves the auto wave size via the same Stage -1 helper but performs no dispatch — it prints the plan and EXITs.)

**Detected stacking pairs block (only when `AUTO_STACK=true` and ≥1 edge survived).** When auto-stack resolved at least one in-set edge, print a `Detected stacking pairs:` block above the wave listing, one line per honored edge, naming the child, its declared dependency phrase, and the parent it will stack on:

```
Detected stacking pairs (--auto-stack):
  #125 "Fix Y"  — Depends on #124 (in this sweep's candidate set) → will stack on #124's branch (feature/issue-124)
  #126 "Add Z"  — Requires #125 (in this sweep's candidate set) → will stack on #125's branch (feature/issue-125)
```

Each stacked child's per-candidate action then reads e.g. `→ would build (stacked on #124)` and the wave grouping reflects the parent-before-child ordering. When `--auto-stack` was passed but **zero** edges survived (no in-set `Depends on`, or every candidate independent), print **no** stacking block — the plan is identical to a run without the flag. Dropped edges (a second in-set parent on the same child, or a cycle) are surfaced as one-line warnings above the block (e.g. `WARNING: #127 declares multiple in-set parents (#124, #125) — honoring #124 only (single-parent edges)` / `WARNING: dropped cyclic stacking edges among #128 #129 — building independently`).

### Procedure — Mode C (PR-set)

1. **Survey each PR candidate (read-only).** For every deduplicated, validated PR number `P` in the candidate list:
   ```bash
   gh pr view P --json number,title,labels,state --jq '{number, title, state, labels: [.labels[].name]}'
   ```
   This is a `gh pr view` read — it does not mutate anything. (If `gh` is unauthenticated or the PR is unreachable, log the error against that candidate and continue surveying the rest.)

2. **Compute wave partition.** Mode C waves are size-1 (`--builders-per-wave` is ignored). Each PR is its own wave. Record `(pr, wave_index=N, total_waves=M)` for each candidate. Apply the same skip rules the live path uses (closed PRs, multiple-label conflicts, missing required label all tagged "would skip" in the plan but still listed for transparency).

3. **Print the plan.** Emit the PR-set output spec below.

4. **EXIT.** Do not proceed to "PR-set Wave Lifecycle". The shell must return as soon as the plan is printed.

**PR-set output spec** (Mode C):

```
/loom:sweep --prs --dry-run plan: M candidate(s) across M wave(s) (PR-set mode, --builders-per-wave ignored)

  Wave 1:
    PR #200  "Add foo widget"                labels: loom:review-requested        → would Judge
  Wave 2:
    PR #201  "Fix bar bug"                   labels: loom:changes-requested       → would Doctor → Judge (cycle 1/max_doctor_cycles)
  Wave 3:
    PR #202  "Refactor baz"                  labels: loom:pr                      → would merge (via merge-pr.sh --auto)
  Wave 4:
    PR #203  "Polish frobnicator"            labels: (none)                       → would skip (no actionable label)
  Wave 5:
    PR #204  "Document quux"                 state: MERGED                        → would skip (PR already merged)

Total: 1 would-judge, 1 would-doctor-then-judge, 1 would-merge, 2 would-skip. No PRs were modified.
```

**Per-PR fields (required):**
- PR number (prefixed `PR #` to distinguish from issue numbers)
- Title (truncated reasonably if very long)
- Current labels (comma-separated, or `(none)`)
- Planned action (`would Judge`, `would Doctor → Judge (cycle 1/max_doctor_cycles)`, `would merge (via merge-pr.sh --auto)`, `would skip (<reason>)`). The `cycle 1/N` form substitutes the resolved `sweep.max_doctor_cycles` value for `N` (default 1).
- Wave assignment (one PR per wave; shown via the `Wave N:` group header)

**Footer (required):** total candidates, total waves, count of `would-judge` / `would-doctor-then-judge` / `would-merge` / `would-skip`, and an explicit confirmation that nothing was modified.

**Mode C skip reasons** (action column should clearly state which applies):
- `would skip (no actionable label)` — PR has neither `loom:review-requested`, `loom:changes-requested`, nor `loom:pr`.
- `would skip (PR already merged)` — `gh pr view` reports `state: MERGED`.
- `would skip (PR closed without merge)` — `state: CLOSED` (non-merged).
- `would skip (loom:blocked)` — PR carries `loom:blocked` (do not act on operator-flagged PRs).
- `would skip (multiple actionable labels)` — PR carries two or more of `{loom:review-requested, loom:changes-requested, loom:pr}` simultaneously (human-attention case — which transition is canonical?).

### Out of scope for dry-run output (all modes)

**Explicitly out of scope for dry-run output** (do not add these — see Limitations):
- Token-pool / account selection internals
- Subagent dispatch order or parallelism counts beyond wave size
- Persisting the plan to disk
- Diffing this plan against a previous or actual sweep

**Verifying "nothing mutates":**

```bash
# Before:
LABELS_BEFORE=$(gh pr view P --json labels --jq '[.labels[].name]|sort')   # Mode C
ISSUE_LABELS_BEFORE=$(gh issue view N --json labels --jq '[.labels[].name]|sort')  # Modes A/B
PRS_BEFORE=$(gh pr list --state open --json number --jq '[.[].number]|sort')
WORKTREES_BEFORE=$(ls .loom/worktrees/ 2>/dev/null | wc -l)
# Run: /loom:sweep --dry-run ...   (any mode)
# All three (or four, for Mode C) must be unchanged after the dry-run returns.
```

These checks — label set per candidate (issue or PR), open PR set, worktree count — are the acceptance criteria. If any of them differ pre/post a `--dry-run` invocation, the dry-run gate is broken.

## PR-set Wave Lifecycle (Mode C only)

If Mode C was selected, the wave lifecycle is the **back half** of the issue-side lifecycle: **no Curator, no Approval gate, no Builder**. Each PR is routed by its current label to Judge, Doctor→Judge, or Merge directly.

> **Stage skip is explicit and load-bearing for Mode C.** The issue-side "MANDATORY: do not skip any stage" rule applies to the **issue** lifecycle. For an existing open PR, the Curator and Builder stages already ran (the PR exists, so the issue was implemented). Re-running them would be incorrect and wasteful. Mode C's wave lifecycle is the symmetric counterpart that handles the post-Builder phases without touching the front half.

For each PR `P` in the candidate list, processed sequentially one PR per wave (size-1 waves):

### C0. Per-PR pre-flight (before any role dispatch)

```bash
gh pr view P --json number,state,labels,closingIssuesReferences \
  --jq '{number, state, labels: [.labels[].name], closes: [.closingIssuesReferences[].number]}'
```

Apply the following skip rules (each "skip" logs the reason; the PR does NOT contribute to any further phase; advance to the next PR):

| Condition | Action | Reason |
|-----------|--------|--------|
| `state != OPEN` (MERGED or CLOSED) | skip | PR is not open; nothing to do |
| Has `loom:blocked` | skip | Operator-flagged; do not act |
| Has none of `{loom:review-requested, loom:changes-requested, loom:pr}` | skip | No actionable label — Mode C only handles these three states |
| Has two or more of `{loom:review-requested, loom:changes-requested, loom:pr}` simultaneously | skip | Conflicting state; human-attention case |
| Has `loom:operator-only` | skip | Operator-only PR; do not act |

Determine the **closing issue number** (used for checkpoint scope below) from `closingIssuesReferences`. This is the GitHub-native `Closes/Fixes/Resolves #N` parser (matches the convention used by the issue-side pre-flight via `closedByPullRequestsReferences`). Record up to one closing issue number per PR:

- **0 closing issues** → no checkpoint scope for this PR. Log a warning at PR start (`PR #P lacks a Closes #N reference; skipping per-issue checkpoint for this PR`) and proceed without checkpointing. Mid-phase resume after a kill will not be available for this PR — Judge / Doctor / Merge will simply re-run from scratch on the next sweep, which is acceptable since the operations are idempotent at the GitHub-state level (Judge re-runs if `loom:review-requested` is still set; Merge re-runs only if the PR is still open and labeled `loom:pr`).
- **1 closing issue** → use that issue number `N` as the checkpoint key. The existing `./.loom/scripts/sweep-checkpoint.sh` is keyed by issue number (#3373) and is reused as-is. **Read the existing checkpoint** before dispatching Judge:
  ```bash
  CHECKPOINT_PHASE=$(./.loom/scripts/sweep-checkpoint.sh phase N)
  ```
  If `CHECKPOINT_PHASE == "merge-done"`, the closing issue was already merged in a previous sweep — skip this PR with `already merged (per checkpoint)` and delete the stale checkpoint.
- **2 or more closing issues** → log all closing issue numbers and skip checkpointing (multi-closing PRs are uncommon; a follow-up issue can add a multi-key checkpoint variant if needed). Proceed with Judge/Doctor/Merge as normal.

### C1. Per-PR routing by current label

Apply exactly one of the three branches below, based on the PR's current label:

#### C1a. `loom:review-requested` → Judge phase only

- Load and follow the instructions in `.claude/commands/loom/judge.md` for this PR.
- Dispatch `loom-judge` as a **single subagent Task** from this orchestrator session. Do **NOT** invoke `/loom:sweep` or `/judge` slash-commands as subagents — see "CRITICAL: One level deep" in the Execution Model.
- If a previous Judge attempt for this PR died mid-flight without a fresh checkpoint (rate limit, crash), re-verify forge state and complete only the missing steps before re-dispatching — see "Mid-phase-death recovery" in the Wave Lifecycle (the rule is phase-generic; Mode C inherits it, same as the Doctor-cycle cap).
- Expected exit states:
  - **Approve** → PR labeled `loom:pr` by Judge. If a closing-issue checkpoint is in scope, write `judge-done`:
    ```bash
    # Append --model <resolved> when you passed a model param to the judge subagent (#3482).
    ./.loom/scripts/sweep-checkpoint.sh write N judge-done --task-id "$RUN_ID" --pr-number P
    ```
    Continue to **C2 (Merge)** for this PR.
  - **Request changes** → PR labeled `loom:changes-requested` by Judge. Continue to **C1b (Doctor → Judge)** for this PR (inline Doctor → Judge cycle(s), up to `sweep.max_doctor_cycles`, matching the issue-side cap).

#### C1b. `loom:changes-requested` → inline Doctor → Judge (up to `sweep.max_doctor_cycles` cycles)

If the PR entered the wave already labeled `loom:changes-requested` (e.g., from a previous Judge run), or just transitioned there from C1a, run inline Doctor → Judge cycles for this PR — **up to `sweep.max_doctor_cycles`** (default 1; see "Doctor-cycle cap" in the Execution Model):

- Load and follow the instructions in `.claude/commands/loom/doctor.md` for this PR.
- Dispatch `loom-doctor` as a **single subagent Task** from this orchestrator session. Do **NOT** invoke `/loom:sweep` or `/doctor` slash-commands as subagents — see "CRITICAL: One level deep".
- If a previous Doctor attempt for this PR died mid-flight without a fresh `doctor-done` checkpoint (rate limit, crash), re-verify forge state (pushed commit? already re-labeled `loom:review-requested`?) and complete only the missing steps rather than duplicating the pushed fix — see "Mid-phase-death recovery" in the Wave Lifecycle (inherited here, same as the Doctor-cycle cap).
- **Model escalation (#3481)**: Mode C inherits the issue-side rule unchanged — this Doctor is dispatched because of a `loom:changes-requested` rejection, so resolve its model per "Model escalation on Judge rejection" in the Execution Model: pass `ladder[1]` from `sweep.escalation` (default ladder: `opus`) via the Task tool's `model` parameter, **unless** a tier-1/tier-2 pin applies (pins win) or escalation is disabled (`[]`/`false`).
- Doctor addresses the judge feedback, commits the fixes, pushes, and re-labels the PR `loom:review-requested`.
- If a closing-issue checkpoint is in scope, write `doctor-done` (with the attempt counter and the model the Doctor actually ran on — escalated or pinned, #3482) **before** the follow-up Judge:
  ```bash
  # <attempt> is the cycle index + 1: 2 for the first Doctor cycle, 3 for the second, etc.
  ./.loom/scripts/sweep-checkpoint.sh write N doctor-done --task-id "$RUN_ID" --pr-number P --attempt <attempt> --model <doctor-model>
  ```
- Re-dispatch `loom-judge` for the PR (now `loom:review-requested` again).
- Expected exit states:
  - **Approve** → PR labeled `loom:pr`. Write `judge-done` checkpoint (if in scope), continue to **C2 (Merge)**.
  - **Request changes again, cap not yet reached** (`sweep.max_doctor_cycles > 1`) → run the next Doctor → Judge cycle for this PR (incrementing `--attempt`), up to the configured cap.
  - **Request changes again, cap reached** → PR labeled `loom:changes-requested`. **Do NOT run another Doctor** — mark this PR as blocked (log `PR #P blocked: doctor cycle exhausted after <k> Doctor→Judge round(s); human attention required`), advance to the next PR in the candidate list. Do NOT block the rest of the candidate list on it. **Distinct-defect exception (default cap only):** when `max_doctor_cycles` is at its default of 1 and this second rejection is a demonstrably distinct defect from the first, you MAY grant exactly one additional bounded cycle (single-use per PR, log `PR #P: granted one extra Doctor cycle — second rejection is a distinct defect (<short reason>)`) — see "Doctor-cycle cap". Same-defect / ambiguous still blocks.

This configurable cap matches the issue-side Wave Lifecycle §6 — Mode C inherits the same rule (and the same default-cap distinct-defect exception) for the same reason (bounds worst-case latency, prevents Judge/Doctor disagreement loops).

#### C1c. `loom:pr` → Merge phase only

If the PR entered the wave already labeled `loom:pr`, skip Judge and Doctor entirely — the PR has already been judged. Continue directly to **C2 (Merge)**.

### C2. Merge (per PR)

Use the dedicated merge script (CLAUDE.md "Merging PRs" mandate — never `gh pr merge`):

```bash
./.loom/scripts/merge-pr.sh P --auto
```

The script merges via the forge API and cleans up the worktree. `--auto` enables GitHub's server-side auto-merge queue (queues the merge until required checks pass); on PRs that are already in `CLEAN` state, the script transparently falls back to an immediate merge — see #3371. **On a repo with GitHub auto-merge disabled** (`allow_auto_merge:false`), `merge-pr.sh` now detects the setting up front and degrades `--auto` gracefully to wait-for-checks-then-merge (immediate if already CLEAN) instead of failing (#3820) — so you can pass `--auto` uniformly regardless of the repo's auto-merge setting; no per-repo branching is needed here.

**On successful merge** (script returns 0):
- If a closing-issue checkpoint is in scope, delete it:
  ```bash
  ./.loom/scripts/sweep-checkpoint.sh delete N
  ```
- Advance to the next PR in the candidate list.

**On merge failure** (script returns non-zero):
- Log the failure (`PR #P merge failed: <reason>`).
- Do **NOT** delete the checkpoint — leave it at `judge-done` (or earlier) so the next sweep retries.
- Advance to the next PR in the candidate list (do not block the rest of the list).

### C3. Wave settled → advance to next PR

Mode C waves are size-1, so "wave settled" is synonymous with "this PR reached a terminal state (merged, blocked, or skipped)". Advance to the next PR in the candidate list and repeat from C0. Do not parallelize PRs (sequential per-PR processing is load-bearing — see "CRITICAL: One level deep" in the Execution Model).

### Mode C summary output

When the entire PR list has been processed, print a per-PR summary:

```
/loom:sweep --prs complete. Processed M PR(s):

  PR #200  → merged                                                                  [judged, merged]
  PR #201  → blocked (judge requested changes after doctor cycle exhausted)          [judged, doctor, judged]
  PR #202  → merged  (was already loom:pr; no judge or doctor)                       [merge-only]
  PR #205  → merged  (rate-limited (resumed: doctor TOKEN_EXHAUSTED mid-phase — fix already pushed, re-labeled + re-judged))  [judged, doctor, judged, merged]
  PR #206  → rate-limited (unresumable: judge TOKEN_EXPIRED mid-phase, human attention required)  [judged]
  PR #203  → skipped (no actionable label)                                           [pre-flight skip]
  PR #204  → skipped (PR already merged)                                             [pre-flight skip]

Total: 3 merged, 1 blocked, 2 skipped, 1 rate-limited (unresumable).
```

`rate-limited (...)` here carries the same meaning as in the issue-set Summary Output (see "`rate-limited` vs `blocked`" there): the reason reuses `TOKEN_EXPIRED` / `TOKEN_EXHAUSTED` from `.loom/scripts/lib/classify-error.sh`, a `resumed:` outcome already succeeded via mid-phase-death recovery, and only an `unresumable:` outcome needs a human — distinct from `blocked (...)`, which means the work itself failed.

## Wave Lifecycle (Modes A and B only — issue-set)

For each wave `W` (partition of the issue list into chunks of up to `--builders-per-wave` candidates, processed in given order), execute the full lifecycle below. **All stages are mandatory** for every issue — do not skip any stage (CLAUDE.md "Sweep Lifecycle (MANDATORY)"). This section applies to Modes A and B only — Mode C uses the shorter "PR-set Wave Lifecycle" section above.

> **Auto-stack pre-partition pass (only when `AUTO_STACK=true`, #3759).** Before partitioning the candidate list into waves, run the detection + edge-resolution + topological-ordering pass in "Auto-stack detection and wave ordering (`--auto-stack`, #3759)": read each candidate's `body` (one added field on the per-issue `gh issue view` already issued at pre-flight step 1), resolve same-candidate-set `Depends on #A` / `Requires #A` edges into the per-issue `DEPENDS_ON[N]` map, reorder so every parent's wave is at or before its child's wave, and — if ≥1 edge survived — print the "Detected stacking pairs" block and (Mode A) prompt for confirmation. When zero edges survive (or the flag is absent), partition proceeds on the original input order exactly as today. The per-issue `DEPENDS_ON[N]` map then feeds the Builder-phase gated path below.

The numbered phases below (Curator → Builder → Judge → Doctor → Merge) are the canonical phase-by-phase reference for this skill — including the label state machine and mid-phase-death recovery procedures. Each phase step tells you which subagent to dispatch and what forge state it should leave behind.

### 0. Snapshot the main-worktree baseline (once, before wave 1) (#3648)

**Before dispatching the first wave's builders**, snapshot main's current working-tree state so the per-wave contamination backstop (step 4's `check-main-clean.sh`) can distinguish builder contamination from dirt that predated the sweep:

```bash
MAIN_CLEAN_BASELINE=".loom/sweep-checkpoint/main-clean-baseline-${RUN_ID}.txt"
./.loom/scripts/check-main-clean.sh --snapshot "$MAIN_CLEAN_BASELINE"
```

Capture this **once, before wave 1 — never per-wave**. The baseline must reflect the pre-sweep state so that if an early wave contaminates main and the dirt is not reverted, every later wave's backstop still flags it (a per-wave re-snapshot would silently absorb that contamination into the "pre-existing" set). The baseline path is **keyed by this sweep's `RUN_ID`** (`main-clean-baseline-${RUN_ID}.txt`, not a fixed `main-clean-baseline.txt`) so that a **concurrent peer `/loom:sweep` never reads or clobbers this run's baseline** (#3768): before the RUN_ID keying, a second sweep re-snapshotting the shared fixed path mid-run of the first could silently absorb real contamination into the "pre-existing" set. The path is a per-sweep-run transient under `.loom/sweep-checkpoint/` whose lifetime is this sweep invocation. `.loom/sweep-checkpoint/` is gitignored in a current install, but a consumer repo's installed loom-managed `.gitignore` block can drift and omit it — so rather than depend on the consumer's `.gitignore` being up to date, `check-main-clean.sh` also excludes `.loom/sweep-checkpoint/` (and the other Loom-owned transient state paths) internally (#3778), so a stale consumer `.gitignore` no longer false-positives the backstop on it. `check-main-clean.sh` needs no change — it already accepts an arbitrary `--snapshot FILE` / `--baseline FILE` path; only this caller-side path construction is keyed by `RUN_ID`. If the snapshot step fails for any reason, proceed anyway — step 4's backstop falls back to the whole-status hard-fail when the baseline file is missing (fail-safe, never a silent pass).

### Checkpoint-driven resume (#3373)

Sweep persists a per-issue phase checkpoint after each successful lifecycle phase so that a killed-and-relaunched sweep can pick up where it left off. The checkpoint is the **only** state required to resume — worktree preservation is handled by `worktree.sh`'s idempotency (re-running for an existing worktree is a no-op).

- **Checkpoint file**: `.loom/sweep-checkpoint/issue-<N>.json` (gitignored).
- **Schema**: `{phase: "<curator-done|builder-done|judge-done|doctor-done|merge-done>", task_id, timestamp, pr_number?, attempt?, model?}`.
- **Helper**: `.loom/scripts/sweep-checkpoint.sh {write|read|phase|attempt|model|exists|delete|list}` — wraps the read/write/delete operations with atomic writes (`.tmp` + `mv`) and validates the phase enum.
- **Model field (#3482, Phase 3a observability)**: when you resolved a model for the phase's subagent (i.e., you actually passed a `model` param to the Task tool — any tier above session default), record it on the checkpoint write with `--model <resolved>` (alias or pinned ID). When the subagent inherited the session default (tier 4, no `model` param passed), omit `--model` entirely. This is observability-only bookkeeping for per-model metrics — readers MUST tolerate checkpoints without the field (legacy checkpoints predate it; absence means default/unknown), and the field never feeds back into model selection or escalation decisions.
- **Write timing**: After the *successful completion* of each lifecycle phase below. Never write a checkpoint speculatively before the phase finishes — a kill mid-phase must resume at the start of that phase.
- **Read timing**: At the start of per-issue pre-flight (step 1) for every issue in the candidate list, before any worktree or label mutation for that issue.
- **Delete timing**: On `merge-done` (step 7) and on stale-checkpoint detection (step 1).
- **Scope limit (no mid-builder recovery)**: A kill during the Builder phase resumes at *builder start* — the worktree state and partial diff survive, but sweep does not inspect the diff or attempt to resume mid-edit. This is intentional per #3372/#3373.

The skip rules per `phase` value are documented inline in each step below.

#### Mid-phase-death recovery (rate limit or crash, issue #3683)

A checkpoint is written only after a phase *completes* (see "Write timing"), so a subagent that is killed mid-phase — an account-level rate-limit kill (`TOKEN_EXPIRED` / `TOKEN_EXHAUSTED`, the same vocabulary `.loom/scripts/lib/classify-error.sh` uses), a crash, an API error, or any other abnormal termination — leaves **no fresh checkpoint** even though it may already have pushed a commit, moved a label, or posted a comment. When you resume a **Judge, Doctor, or Merge** phase whose subagent was not observed to exit cleanly and no new checkpoint was written for it, **do not assume no work happened, and do not blindly re-run the whole phase.**

Instead, before re-dispatching anything for that phase, **re-verify the PR's actual forge state against that phase's already-documented "Expected exit state(s)"** (Judge: step 5's Approve / Request-changes bullets; Doctor: step 6's push → relabel → re-Judge sequence; Merge: step 7's merge-then-checkpoint-delete). Specifically check:

- whether a **new commit** landed on the PR branch since the checkpoint's timestamp (`gh pr view <PR> --json commits`, or `git log <checkpoint-ts>..`),
- whether the **PR label** already reflects a later state than the checkpoint implies (e.g. `loom:review-requested` after a Doctor, or `loom:pr` after a Judge approval), and
- whether there are **PR comments** from the dead subagent describing work it already completed.

Then **complete only the missing steps** to reach that phase's expected exit state — never redo steps that already landed. Example (the exact #3676 incident this rule is drawn from): a Doctor pushed its fix but was rate-limit-killed before re-labeling `loom:changes-requested` → `loom:review-requested` and handing back to Judge. The correct recovery is to re-label and re-run Judge — **not** to dispatch a fresh Doctor that would duplicate the already-pushed commit.

- **Builder is exempt — unchanged.** This rule covers Judge / Doctor / Merge only. The Builder's "Scope limit (no mid-builder recovery)" above stands as-is: a Builder kill intentionally resumes from *builder start* and relies on `worktree.sh` idempotency for the builder to decide whether to commit / amend / discard its partial diff. Do not apply the forge-state-reverification rule to Builder.
- **Should-prefer (optional): resume the same subagent when the parent survives.** When the **orchestrator's own session** is still alive and the dead phase's Task-tool subagent conversation is still resumable (e.g. via `SendMessage` back into that same subagent thread rather than a brand-new Task dispatch), prefer that path — the original subagent already knows exactly what it committed / pushed / labeled, which is strictly more context than a fresh subagent re-deriving intent from a partial diff. This is a preference, not a requirement: an account-level rate-limit kill often takes the whole process (parent included) down, in which case no resumable thread exists. The mandatory forge-state-reverification rule above must **never** depend on this being available.

#### Stale-checkpoint cleanup

A "stale checkpoint" is one whose issue is already closed on the forge (e.g., the merge happened in a different sweep invocation, or the issue was closed manually after sweep was killed). Detect and clean these up on entry — see step 1.

### 1. Per-issue pre-flight (still per-issue, before the wave dispatch)

> **Aggressive-mode override (`all` sentinel).** When `SWEEP_ALL_AGGRESSIVE=true` (set **only** by the build-everything `all` sentinel — see "Build-everything sentinel (`all`)" under Validation rules), the hard-skip rules below are replaced by the recovery routing in the "Aggressive candidate taxonomy" table: stale `loom:building` is reclaimed (after the one-time `recover-orphaned-shepherds.sh --recover` pass), `loom:blocked` is probed and cleared where the blocker has resolved, `loom:epic` containers fan out to their `loom:epic-phase` children, and uncurated / `loom:triage` / `loom:curating` issues are curated inline before promotion. The existing-PR probe still runs first and still wins (an issue with an open PR is driven to Judge/Merge, never rebuilt). Only `loom:operator-only` remains a hard skip. Mode A/B explicit-list and NL sweeps leave the flag unset and use the conservative skips exactly as written below.

For each issue `N` in the wave, before any role skill is invoked:

0. **Read the resume checkpoint (if any).** Before any other pre-flight work for this issue:
   ```bash
   CHECKPOINT_PHASE=$(./.loom/scripts/sweep-checkpoint.sh phase N)
   ```
   `CHECKPOINT_PHASE` is one of: empty string (no checkpoint), `curator-done`, `builder-done`, `judge-done`, `doctor-done`, `merge-done`. Carry this value through the rest of the lifecycle and use it at each phase to decide whether to skip.

   **Stale-checkpoint cleanup.** If a checkpoint exists for `N` *and* the issue's `state` (from step 1's `gh issue view`) is `CLOSED`, the checkpoint is stale (the issue was closed out-of-band — most commonly because a different sweep invocation already merged it, or a human closed it manually). Remove it with a warning and skip the issue entirely:
   ```bash
   if [[ -n "$CHECKPOINT_PHASE" && "$ISSUE_STATE" == "CLOSED" ]]; then
     echo "WARNING: stale sweep checkpoint for closed issue #N (phase=$CHECKPOINT_PHASE) — removing"
     ./.loom/scripts/sweep-checkpoint.sh delete N
     # Skip issue — does NOT contribute to this wave.
   fi
   ```

   **`merge-done` short-circuit.** If `CHECKPOINT_PHASE == "merge-done"`, the issue was already merged in a previous sweep run but the checkpoint was not deleted (rare — e.g., sweep was killed between the merge call and the delete call). Delete the checkpoint and log `already complete; skipping`. The issue does NOT contribute to this wave.

1. **Verify the issue is open and not already in flight.**
   ```bash
   gh issue view N --json state,labels,closedByPullRequestsReferences \
     --jq '{state, labels: [.labels[].name], linked_prs: [.closedByPullRequestsReferences[].url]}'
   ```
   - If the issue is closed, skip it (log a warning). It does NOT contribute to this wave.
   - If the issue already has `loom:building`, skip it — another shepherd or builder is working on it. Log a warning. Does NOT contribute to this wave. **Daemon self-claim exception (#3823):** when this run was dispatched by `loom-daemon`, `SweepRegistry::dispatch` flips `loom:issue → loom:building` on the forge *before* spawning this child (for immediate external visibility of the claim) and exports the claim-ownership marker env var **`LOOM_SWEEP_CLAIM_OWNED=<issue>`** into the child. So if `LOOM_SWEEP_CLAIM_OWNED` is set and equals the issue number `N` being pre-flighted, the existing `loom:building` is **this sweep's OWN daemon claim** — do NOT skip; **proceed to build** (treat it exactly as if you had just claimed it yourself). The skip rule still applies unmodified when the marker is unset (an operator-run `/loom:sweep N` from a manual terminal or GH Actions cron) or names a *different* issue — in those cases a `loom:building` label genuinely means another worker owns it. `LOOM_SWEEP_CLAIM_OWNED` is set only on daemon-dispatched children; it is never exported by an operator invocation, so manual sweeps keep honoring any `loom:building` claim as before.
   - If the issue has `loom:blocked`, skip it. Log a warning. Does NOT contribute to this wave.
   - If the issue has `loom:operator-only`, skip it — requires human action outside automation (credentials, infra rotations, manual deploys, hardware access). Log a warning with reason "operator-only". Does NOT contribute to this wave. **Checked before the existing-PR probe** so operator-only issues aren't probed at all.
   - **Existing-PR probe (#3359, #3677).** The set of open PRs for issue `N` is the **union of two GitHub-computed sources** — no body-grep. Both are additive and deduped by PR number before routing:

     1. **Closing-keyword PRs (`closedByPullRequestsReferences`, unchanged since #3359).** The `linked_prs` from the `gh issue view` above. GitHub's native `Closes/Fixes/Resolves #N` parser — populated only by closing keywords.
     2. **Non-closing cross-reference PRs (timeline, #3677).** PRs that reference `N` with a **non-closing** phrase (`Part of #N` / `Contributes to #N`, the #3599 partial-increment convention — see `defaults/roles/builder-pr.md`) never appear in `closedByPullRequestsReferences` by design, so probe the issue's timeline for `cross-referenced` events whose source is a PR:
        ```bash
        gh api "repos/OWNER/REPO/issues/N/timeline" --paginate \
          --jq '[.[] | select(.event == "cross-referenced"
                              and .source.issue.pull_request != null
                              and .source.issue.repository.full_name == "OWNER/REPO")
                 | {number: .source.issue.number, state: .source.issue.state}]
                | unique_by(.number)'
        ```
        This is GitHub's own reference parser (the same engine behind `closedByPullRequestsReferences`) surfacing **every** `#N` mention as a `cross-referenced` event, with `source.issue.pull_request` non-null when the referrer is a PR and `source.issue.state` giving its live state. Keep only entries whose `state == "open"` (lowercase — the timeline API returns lowercase issue/PR states, unlike the uppercase `closedByPullRequestsReferences` field). **Same-repo guard (required):** `cross-referenced` events include references from *other* repositories in a multi-repo ecosystem (e.g. a sibling repo's PR that mentions `OWNER/REPO#N`); the `.source.issue.repository.full_name == "OWNER/REPO"` filter (the field is reliably populated on every event) scopes the result to this repo so a foreign PR number is never misrouted to `gh pr view` below. This mirrors `closedByPullRequestsReferences`, which is inherently same-repo. No local regex is involved; GitHub does the text parsing (the #3267 lesson: don't hand-roll what GitHub already parses).

     **Union + filter.** Merge the two source lists and dedupe by PR number. For any PR discovered only via source 1, filter to `state == "OPEN"` (uppercase — `closedByPullRequestsReferences` includes MERGED and CLOSED PRs, which are not the duplicate-builder hazard); source 2 is already filtered to open. For each surviving open PR, fetch its labels for routing:
     ```bash
     gh pr view <pr_number_or_url> --json state,labels --jq '{state, labels: [.labels[].name]}'
     ```
     Apply the routing rules below based on the count of distinct **open** linked PRs (from either source):

     | Open linked PRs | Action |
     |-----------------|--------|
     | 0 | Continue with pre-flight (no behavior change). |
     | 1, no `loom:pr` label | **Skip Builder phase.** Log `skip (existing PR #X in flight)` with the PR URL. The existing PR is routed into the Judge phase (step 5) **for this wave** in place of a freshly-built PR; the Builder is not dispatched. Wave size shrinks by one per the pre-flight skip rule. |
     | 1, has `loom:pr` label | **Skip Curator + Builder + Judge.** Route the PR directly to Merge (step 7). The PR has already been judged. |
     | 2 or more | Log all PR URLs and skip the issue. This is a human-attention case (which PR is canonical?) — sweep does not pick one. |

     The closing-keyword path (`closedByPullRequestsReferences`, verified working in `gh` 2.93.0; matches the convention used in `champion-reference.md` and `champion-pr-merge.md`) is **untouched** — this is purely additive. It uses GitHub's native parser for `Closes/Fixes/Resolves #N` (and correctly excludes `Updates #N` / `Related to #N`); the timeline source covers exactly the non-closing references that field deliberately omits. Do **not** body-grep PRs for closing keywords (re-introduces the #3267 bug). Per-issue the open-PR count is 0 or 1 in practice, so the timeline call + a secondary `gh pr view` is one or two extra calls per surviving candidate, not N×M.

2. **Read the issue body before briefing any builder.** This is a non-negotiable rule from prior sweep sessions (a misleading title hid the real requirement in the body). Skipped only if pre-flight already routed the issue to Judge/Merge via the existing-PR rules above — those branches use the PR as the source of truth, not the issue body.
   ```bash
   gh issue view N --json title,body
   ```

> **Pre-flight skip rule.** If `K` of the wave's `N` candidates are skipped at pre-flight (closed, `loom:building`, `loom:blocked`, `loom:operator-only`, or multi-PR ambiguity), dispatch only `N - K` builders for this wave. Issues routed to Judge or Merge via the existing-PR rules consume a wave slot but skip the Builder dispatch. **Do not pull a candidate forward** from the next wave to backfill. Wave boundaries stay clean, and the next wave runs at its originally planned size.

### 2. Curator phase (still per-issue, before the wave dispatch)

For each surviving issue `N` in the wave:

- **Checkpoint skip.** If `CHECKPOINT_PHASE` is one of `curator-done`, `builder-done`, `judge-done`, `doctor-done`, skip the curator phase entirely (it already completed in a prior sweep run). Do NOT re-invoke the curator skill — re-curating is wasted work and can produce churn on an issue that's already mid-lifecycle.
- Otherwise (no checkpoint, or `CHECKPOINT_PHASE` is empty): if the issue does not already have `loom:curated` or `loom:issue`, run the curator skill on it.
  - Load and follow the instructions in `.claude/commands/loom/curator.md` for issue `N`.
  - Expected exit state: issue has `loom:curated`.
- If the issue already has `loom:curated` or `loom:issue`, skip the curator skill invocation but still write the checkpoint below (so future sweep runs can skip the redundant label probe).
- **On successful completion** (curator ran, or curator-skip-because-already-curated), write the checkpoint:
  ```bash
  # Append --model <resolved> when you passed a model param to the curator subagent (#3482).
  ./.loom/scripts/sweep-checkpoint.sh write N curator-done --task-id "$RUN_ID"
  ```

Curator runs sequentially per-issue within wave setup — it is cheap and does not benefit from parallelism here. **Await each Curator's completion explicitly** (blocking `TaskOutput`) before advancing — the harness may launch the subagent async even with `run_in_background: false`, so the sequencing here depends on an explicit await, not the dispatch flag (see "Subagent dispatch is async-only", #3822).

### 3. Approval gate (per-issue)

Each issue must reach `loom:issue` before the Builder can claim it.

- If the issue already has `loom:issue`, proceed.
- Otherwise, promote it:
  ```bash
  gh issue edit N --remove-label "loom:curated" --add-label "loom:issue"
  ```

### 4. Builder phase (parallel within the wave)

**Checkpoint skip.** For each surviving issue, if `CHECKPOINT_PHASE` is one of `builder-done`, `judge-done`, `doctor-done`, the Builder phase has already completed for this issue. Read the `pr_number` from the checkpoint and route the PR directly into the Judge phase (step 5) — do NOT dispatch a builder subagent.

```bash
EXISTING_PR=$(./.loom/scripts/sweep-checkpoint.sh read N | sed -n 's/.*"pr_number"[[:space:]]*:[[:space:]]*\([0-9]*\).*/\1/p')
```

If `CHECKPOINT_PHASE` is `judge-done` or `doctor-done`, see the corresponding skip rules in steps 5/6 — the PR is routed further along, not back to Builder.

For issues without `builder-done`-or-later checkpoints, proceed with the normal Builder dispatch:

Dispatch up to `min(resolved-wave-size, surviving-candidates-in-wave-needing-builder)` `loom-builder` subagents **in a single tool-call block** from this orchestrator session, where `resolved-wave-size` is the explicit `--builders-per-wave` value or, when the flag was omitted, the Stage -1 auto wave size ("Resolve auto wave size"). Note this Wave Lifecycle is the **subagent** path, so the auto size here is core-scaled within `[3, 6]` (#3289-safe floor 3, ceiling 6, #3693) — the daemon path never runs this section (it dispatches detached processes and exits at Stage -1). **Do NOT invoke `/loom:sweep` as a subagent here** — see the "One level deep" rule in Execution Model above.

Each builder is responsible for:

- Claiming its issue (`loom:issue` → `loom:building`).
- Creating an issue worktree via `./.loom/scripts/worktree.sh N` (idempotent — re-entering after a kill reuses the existing worktree and branch).
- Implementing the change, running tests, committing.
- Pushing the branch and opening a PR labeled `loom:review-requested`.
- Closing references: `Closes #N` in the PR body.

**Stacked-dependency gated path (`--depends-on`, #3729 v1; per-issue map generalization, `--auto-stack`, #3759).** This gate fires **only** when a parent is set for the issue being built — look it up in the per-issue map `DEPENDS_ON[N]` (which subsumes the historical single global `DEPENDS_ON`: for a single-issue `--depends-on <parent>` dispatch, `DEPENDS_ON[N]` is just that one entry; for an `--auto-stack` wave, it is one entry per detected child). When `DEPENDS_ON[N]` is unset for issue `N`, the two steps below are byte-for-byte the default behavior. When `DEPENDS_ON[N]=<parent>` is set, the builder for issue `N` must:
  - Create its worktree branched off the parent's branch: `./.loom/scripts/worktree.sh N --base feature/issue-<parent>` (instead of the bare `./.loom/scripts/worktree.sh N`). `worktree.sh` resolves `feature/issue-<parent>` from `origin/feature/issue-<parent>` (or a local branch), so the parent sweep must have created/pushed its branch first; if the base cannot be resolved, `worktree.sh` hard-fails rather than silently branching off the default branch.
  - Open its PR against the parent branch: `gh pr create --base feature/issue-<parent> --label "loom:review-requested" --body "Closes #N ..."` (instead of the default base). The PR stays stacked on the parent until reconciliation, which now fires automatically when the parent squash-merges (see "Stacked dependency (auto-reconciliation on parent merge)").
  The **only** thing `--auto-stack` changes here is how `DEPENDS_ON[N]` is *sourced* — the `worktree.sh --base` / `gh pr create --base` mechanics are untouched. Two sources feed the map: (a) an explicit single-issue `--depends-on <parent>` (unchanged, typically a daemon `dispatch_sweep` forwarding `depends_on` as `--depends-on`), and (b) an auto-stack-detected same-candidate-set edge (see "Auto-stack detection and wave ordering"). Absent both, the wave lifecycle does not auto-create stacks.
  **Same-wave parent/child.** When the topological ordering placed a parent and its child in the **same** wave, the child's Builder branches off `feature/issue-<parent>` even though the parent's Builder is running concurrently in that wave — `worktree.sh --base` resolves the parent branch as soon as the parent Builder has pushed it. The child does **not** branch off the shared pre-wave `main` snapshot its unstacked wave-mates use.

**Await all builders in the wave** before proceeding to Judge. Collect each builder's PR number (or failure marker). This await is **mandatory and explicit** — block on every builder's `TaskOutput` / completion notification. The harness may launch each Task async regardless of `run_in_background: false`, so proceeding to Judge on a dispatch flag alone can start Judge before builders finish; the "await all builders before Judge" rule is enforced by this explicit block, not by any dispatch flag (see "Subagent dispatch is async-only", #3822).

**Backstop: verify the main worktree is clean after the builders return (#3513).** A builder subagent runs without `LOOM_WORKTREE_PATH` injected, so the `guard-worktree-paths.sh` hook does not fire on this path. If a builder used repo-relative paths after a cwd reset, it may have written to the **main** worktree instead of its issue worktree. After the wave's builders return and before advancing any PR to Judge, run:

```bash
./.loom/scripts/check-main-clean.sh --baseline "$MAIN_CLEAN_BASELINE"   # exit 3 ⇒ NEW main dirt (builder contamination)
```

The `--baseline` argument points at the snapshot taken once at step 0 (before wave 1). With it, the check subtracts any dirt that predated the sweep and exits `3` **only** on changes that appeared after the snapshot — so pre-existing working-tree dirt (a regenerated lockfile, an operator scratch edit) no longer false-positives as contamination on every wave (#3648). If the baseline file is missing or unreadable, the check warns and falls back to the whole-status hard-fail (fail-safe).

If it exits `3`, the main worktree carries **new** uncommitted changes a builder left behind. Surface this loudly in the wave summary — **quote the specific offending paths** the check printed under `Offending changes:` so the operator can see exactly which files escaped a worktree — and **hard-block the wave from advancing any PR to Judge** until the contamination is investigated and the stray changes reverted (move them into the owning issue worktree, then restore main). This is a backstop only — the builder guidance (capture the absolute worktree path once, use absolute paths everywhere) is the primary defense. Note the mechanical reason it is *only* a backstop: a builder subagent is dispatched via the Task tool ("one level deep", step 4 above) and inherits the orchestrator's single shared process env, which has **no** `LOOM_WORKTREE_PATH` — and the Task tool exposes no per-subagent env-injection parameter — so `guard-worktree-paths.sh` structurally cannot fire per builder on this path (#3719; same-shape harness limitation as #3705). Detection here plus the builder-side absolute-path contract are the achievable defenses.

**On successful PR creation**, write the `builder-done` checkpoint for that issue (record the PR number):
```bash
# Append --model <resolved> when you passed a model param to the builder subagent (#3482).
./.loom/scripts/sweep-checkpoint.sh write N builder-done --task-id "$RUN_ID" --pr-number <PR>
```

If the builder failed (no PR opened), do NOT write a checkpoint — leave the checkpoint at the previous phase (typically `curator-done`) so the next sweep retries the builder from scratch.

**Per-builder failure isolation.** If builder for issue `#A` fails to open a PR (build error, test failure, unrecoverable conflict, etc.), log it and **continue** with the other builders' PRs in this wave. The failed issue is recorded as `blocked (builder failed)` in the summary. Do NOT abort the wave. Do NOT skip Judge for the other PRs.

**Mid-builder kill semantics (#3373).** If sweep is killed during the Builder phase, the next invocation will see `CHECKPOINT_PHASE == "curator-done"` (no `builder-done` was written), so the Builder dispatches again from scratch. The worktree from the killed run is preserved by `worktree.sh`'s idempotency — `./.loom/scripts/worktree.sh N` is a no-op if `.loom/worktrees/issue-N` already exists. The builder re-enters the worktree, sees the partial diff, and decides whether to commit / amend / discard. **Sweep itself does not introspect the partial diff** — that's the builder's job.

### Stacked dependency (auto-reconciliation on parent merge) — #3729 (v1), #3747 (v2 items 1 & 2)

Stacked-PR mode pipelines a genuine dependency: when issue B consumes issue A's output (schema, file, manifest), B is built on `feature/issue-A` so B's lifecycle runs concurrently with A's review instead of serializing behind A's merge. **The dispatch surface is opt-in, daemon-`dispatch_sweep`-only, and linear-chains-only.**

**How to dispatch a chain.** A chain is N independent `dispatch_sweep` calls, each naming its immediate predecessor — there is no multi-node planner:

```text
# Parent A (independent):
mcp__loom__dispatch_sweep  kind={"Issue": A}
# Child B stacked on A:
mcp__loom__dispatch_sweep  kind={"Issue": B}  depends_on=A
# Grandchild C stacked on B (A→B→C works because each hop names only its parent):
mcp__loom__dispatch_sweep  kind={"Issue": C}  depends_on=B
```

The daemon forwards `depends_on` to the child as `--depends-on <parent>`; the child's Builder branches off `feature/issue-<parent>` and opens its PR with `--base feature/issue-<parent>` (see the gated path in the Builder phase above). A single optional parent makes diamonds / multi-parent stacks **unrepresentable** — there is no rejection logic because the type itself forbids them.

**Block-the-subtree on parent failure (daemon-side, #3729 item 4).** If the parent sweep ends in `loom:blocked` (Doctor-cycle budget exhausted, or an operator cancel), the daemon's reaper does **not** let a child whose `depends_on` names that parent auto-progress: it publishes `sweep.issue.{child}.blocker` on the existing frozen topic (no new topic) so the stuck stack surfaces to the operator. Auto-detach (rebasing an orphaned child onto the default branch) is **not** implemented in v1 — block-the-subtree is the only cascade behavior.

**Reconciliation now fires automatically on parent merge (v2 item 1, #3747).** The repo squash-merges, so after the parent squash-merges to the default branch as one commit, the child branch still carries the parent's original pre-squash commits. `merge-pr.sh` now reconciles child PRs automatically at its post-merge choke point (alongside the partial-increment label reset, before branch deletion): it discovers open child PRs via a **live forge query** (`gh pr list --base feature/issue-<parent>` — never the daemon registry, which is ephemeral and may not be running under a Champion cron or interactive merge), then splits safe/unsafe per child on the child **issue's** `loom:building` label (fresh, uncached `gh api` read):

- **Safe** (child issue not `loom:building`): invokes `./.loom/scripts/reconcile-stack.sh <child-pr> feature/issue-<parent>` for you.
- **Unsafe** (child issue still `loom:building`): a live Builder likely holds the child branch checked out, so the auto-rebase is **skipped** and a comment is posted on the child PR flagging deferred reconciliation. A later parent-merge-triggered pass (once the issue is no longer `loom:building`), or a manual run, picks it up.

The step is **best-effort** — a reconciliation failure never fails the parent merge — and idempotent (once a child's base is retargeted away from the parent branch, the query returns zero rows).

**Pre-merge merge-ordering guard now ships too (v2 item 2, #3747).** Item 1's reconciliation runs *after* the parent has already merged, and the repo setting Loom itself recommends (`delete_branch_on_merge:true`, applied by `setup-repository-settings.sh`) makes GitHub delete `feature/issue-<parent>` **synchronously during the merge API call** — before the post-merge reconcile pass runs, and once the ref is gone `reconcile-stack.sh`'s `git rebase --onto <default> <parent-branch>` can no longer resolve `<parent-branch>`. So item 1 could race and *lose* against the repo's own settings. To close that race, `merge-pr.sh` now runs a **pre-merge guard** (before both the auto-merge and synchronous-merge paths) that discovers open child PRs with the same live-forge query (`gh pr list --base feature/issue-<parent> --state open`) and, by default, **hard-blocks the merge** (`exit 1`, naming the blocking child PR number(s) and the `reconcile-stack.sh` unblock command) rather than letting the parent merge create the race. This is a normal, recoverable failure — Champion's cron retries it next tick, exactly like any other merge-blocking condition. Unlike item 1's post-merge pass, the guard keys **purely on "does an open child PR still target this branch"** — never on the child's `loom:building` label, since a "safe" child is just as exposed to branch deletion as an "unsafe" one. Pass **`--allow-stacked-children`** to `merge-pr.sh` to bypass the guard once you have manually reconciled/verified the children (operator asserts responsibility, mirroring `--worktree-path`); `--dry-run` still runs the guard and reports the would-be block without exiting 1.

`reconcile-stack.sh` remains available for **manual** invocation — for the unsafe/deferred case once the Builder finishes, or to reconcile ahead of a merge (`--dry-run` previews the surgery):

```bash
./.loom/scripts/reconcile-stack.sh <child-pr> feature/issue-<parent>
# = git rebase --onto <default-branch> feature/issue-<parent> <child-branch>
#   git push --force-with-lease
#   gh pr edit <child-pr> --base <default-branch>
```

**Rebase-on-parent-amend now ships too (v2 item 3, #3747).** Items 1 and 2 both handle the *parent-merge* moment; item 3 closes the far more common *pre-merge* case: while a stacked parent's PR (`feature/issue-<parent>`) is still open under review and Doctor amends the parent branch (interactive rewrite or additive commits + force-with-lease), any child that branched off the parent's *pre-amend* tip goes silently stale. The standalone `./.loom/scripts/rebase-stacked-children.sh feature/issue-<parent>` discovers open child PRs with the same live-forge query (`gh pr list --base feature/issue-<parent> --state open`), detects staleness per child via `git merge-base --is-ancestor origin/<parent> origin/<child>` (up-to-date children are skipped), and reuses item 1's safe/unsafe split on the child **issue's** `loom:building` label: safe stale children are rebased onto the parent's current tip (`git rebase origin/<parent> <child>` + `git push --force-with-lease`, **base NOT retargeted** — the child stays stacked on the parent), while unsafe children (issue still `loom:building`) get a deferred-rebase comment instead. It is manual-first (like v1's `reconcile-stack.sh`): **Doctor runs it as a documented workflow step 9a** after pushing to a `feature/issue-<N>` branch (see `doctor.md`), best-effort — a failure never fails the Doctor's own work. `--dry-run` previews the per-child outcome.

**Deferred (v2 epic #3747, not yet implemented):** **dependency auto-detection** (broader cross-reference union-probe form — the narrower same-`/loom:sweep`-candidate-set case already ships as `--auto-stack`, #3759; see the section below), **diamonds / multi-parent**, and **auto-detach**. (The **merge-ordering guard** shipped as v2 item 2, and **rebase-on-parent-amend** as v2 item 3 — see above.)

### Auto-stack detection and wave ordering (`--auto-stack`, #3759)

This section is the single home for the opt-in `--auto-stack` behavior. It is entered **only** when `AUTO_STACK=true` (Modes A/B). Absent the flag, none of this runs and the sweep is byte-for-byte unchanged. It **generalizes the single-value `--depends-on` / `worktree.sh --base` / auto-reconcile mechanics above (already shipped, #3729/#3747/#3752) from one global value to a per-issue dependency map** — it does **not** introduce any new worktree/PR/merge machinery. Mode C never runs this (no Builder phase to stack).

**1. Detection — authoritative body-text signal, same-candidate-set only.** During the Stage 0 candidate survey (which already reads each candidate's `title,labels,state` — auto-stack adds `body` to that same `gh issue view N --json` read, **no new API call**), grep each candidate's body for the dependency phrases. **Reuse the exact regex vocabulary already established in `defaults/roles/guide.md` (`parse_dependencies`, the `(Blocked by|Depends on|Requires|\- \[.\]) #[0-9]+` convention), restricted here to `Depends on` / `Requires` only:**

```bash
# Modeled on guide.md's parse_dependencies — restricted to the two declaration phrases.
# Deliberately EXCLUDES `Blocked by` (that phrase drives the distinct loom:blocked
# unblock machinery in guide.md / champion-reference.md and is NOT repurposed here)
# and EXCLUDES the `- [ ]` task-list form (not a stacking declaration).
echo "$BODY" | grep -oE '(Depends on|Requires) #[0-9]+' | grep -oE '#[0-9]+' | tr -d '#' | sort -u
```

A matched `#A` becomes a **stacking edge only when `#A` is also a member of this sweep invocation's own deduplicated candidate list.** A `Depends on #A` naming an issue **outside** the candidate set is left completely untouched — it is not an edge, it does not stack, and it flows through the existing `loom:blocked` handling exactly as today (this feature never touches out-of-set references). This "same-candidate-set only" restriction is load-bearing: it is what keeps auto-stack scoped to one sweep's own resolved set and prevents it from silently reaching out to arbitrary external issues.

**2. Linear, single-parent edges only — no diamonds.** A candidate may declare at most **one** in-set parent, mirroring #3729's `Option<u32>` design (do **not** build a `Vec` of parents). If a body matches multiple in-set `#A` references, take the **first** and log a warning that only single-parent edges are honored (`WARNING: #<child> declares multiple in-set parents (#a, #b) — honoring #a only (single-parent edges)`). Diamonds / multi-parent stacks are structurally unrepresentable, consistent with #3729/#3747.

**3. Cycle guard — drop, never silently orient.** If the surviving edges form a cycle within the candidate set (e.g. `#128 Depends on #129` and `#129 Depends on #128`), **drop every edge in the cycle** and log a warning (`WARNING: dropped cyclic stacking edges among #128 #129 — building independently`), falling back to default (unstacked) behavior for those issues. Never silently pick a direction.

**4. Wave ordering — topological, parent at-or-before child.** After edges resolve, order candidates so **every parent lands in a wave at or before its child's wave** — a topological pass restricted to the linear-chain shape above (no general DAG solver). A parent/child pair **may** land in the *same* wave: in that case the child's Builder still branches off the parent's branch via `--base feature/issue-<parent>`, **not** off the shared pre-wave `main` snapshot its other wave-mates use. This ordering feeds the existing wave-partition pass (Stage 0 step 2 / the wave partition consumed by the Wave Lifecycle) — it reorders the candidate list, then the normal `--builders-per-wave` chunking applies.

**5. Per-issue `DEPENDS_ON[N]` map.** The detected edges populate a per-issue lookup `DEPENDS_ON[N] = <parent>`. This is the sole new data structure — it **generalizes** the pre-existing single global `DEPENDS_ON` value (from `--depends-on <parent>`) into a map keyed by child issue. Sourcing precedence: an explicit single-issue `--depends-on N` seeds `DEPENDS_ON[N]` and a detected auto-stack edge for `N` never overrides it; every other entry comes from detection. The **Builder-phase gate** and the **daemon-dispatch loop** consume `DEPENDS_ON[N]` (see those sections) — the underlying `worktree.sh N --base feature/issue-<parent>` and `gh pr create --base feature/issue-<parent>` mechanics are **not** touched, only how the per-issue parent value is sourced.

**6. Operator confirmation — reuse the existing gate.** When `--auto-stack` finds ≥1 edge, the "Detected stacking pairs" block (see the Stage 0 dry-run output spec) is printed as part of the same candidate-set display Mode B / `--dry-run` already show before awaiting confirmation. **Mode A** (explicit numeric list, today's no-prompt fast path) gains a confirmation prompt **only when `--auto-stack` actually found ≥1 edge** — a zero-edge `--auto-stack` run on Mode A stays prompt-free (identical to the flag being absent). Declining exits cleanly, matching every other gate in this skill. Mode B already prompts, so this adds only the stacking block to its existing display.

**Explicitly out of scope for v1** (do not attempt here): file-overlap-heuristic auto-detection (#3729 rejected file paths as a topology signal — the reactive #3647 in-wave overlap-and-revalidate gate stays the backstop for *accidental* same-file collisions this feature doesn't stack); diamonds / multi-parent stacks; cross-`/loom:sweep` coordination (two independently-running sweeps stacking each other's candidates is #3768's scope — this feature only ever stacks within one sweep invocation's own resolved candidate set); `Part of #A` / `Blocked by #A` timeline cross-reference detection; and any change to `merge-pr.sh` / `reconcile-stack.sh` / `worktree.sh` (reconciliation is reused unchanged).

### 5. Judge phase (sequential per PR within the wave)

For each PR in the wave (including PRs whose Builder just ran *and* PRs routed in via a `builder-done` checkpoint), in the order the builders completed (or any deterministic order — wave-internal ordering is not load-bearing), run the Judge phase sequentially. **"Sequentially" means await each Judge's completion explicitly** (blocking `TaskOutput`) — and, when Judge requests changes, await the inline Doctor→Judge cycle (step 6) — before dispatching the next PR's Judge. The harness may launch each Judge/Doctor Task async regardless of `run_in_background: false`, so this per-PR ordering is enforced by an explicit await, never by a dispatch flag (see "Subagent dispatch is async-only", #3822):

```
WAVE_MERGED_FILES = {}                          # union of changed paths merged so far this wave (#3647)
for pr in wave_prs:
    judge(pr)                                   # may approve or request changes — against the PR's own pre-wave base
    if changes_requested:
        doctor(pr)                              # Doctor->Judge cycle(s), up to the cap (see step 6)
    if still_approved:
        revalidate_if_overlaps(pr, WAVE_MERGED_FILES)   # step 7 gate — re-judge / Doctor if pr shares a file with an already-merged sibling
        merge(pr)                               # step 7
        WAVE_MERGED_FILES |= changed_files(pr)  # feed the next PR's overlap probe
post_wave_integration_gate()                    # step 8 — buildGate-against-main backstop for cross-file coupling
```

`WAVE_MERGED_FILES` is the load-bearing state for the intra-wave collision guard (#3647): it accumulates the changed-file paths of every PR already merged **in this wave**, so the step 7 gate can tell whether the next PR overlaps a sibling that has already landed. Seed it empty at the start of each wave (it does **not** carry across waves — each wave rebases onto a settled `main`). The `post_wave_integration_gate()` call (step 8) is the backstop for the cross-file case the file-path probe cannot see.

**Checkpoint skip.** For each PR:
- If `CHECKPOINT_PHASE == "judge-done"` for the corresponding issue, the Judge already approved the PR in a prior sweep run. Skip the Judge invocation and route the PR straight to Merge (step 7). The PR should already carry `loom:pr` (judge writes that label as part of the approve path); if it doesn't, the checkpoint and forge state have diverged — log a warning and re-run Judge.
- If `CHECKPOINT_PHASE == "doctor-done"`, Doctor has already addressed Judge's earlier feedback. **Re-run the Judge phase** for this PR — Judge has not yet evaluated the post-doctor diff in the current sweep run. (The previous Judge result that led to Doctor was `changes-requested`, not `judge-done`.)
- Otherwise (`builder-done`, or no checkpoint yet because Builder just ran in this wave), run Judge normally.

- Load and follow the instructions in `.claude/commands/loom/judge.md` for the PR.
- The judge uses `gh pr comment` (NOT `gh pr review --approve`) because GitHub's self-review API restriction applies — see `judge.md` for the full explanation.
- **If a previous Judge attempt for this PR died mid-flight without writing a fresh checkpoint** (rate limit, crash), re-verify forge state and complete only the missing steps before re-dispatching — see "Mid-phase-death recovery" above.
- Expected exit states per PR:
  - **Approve** → PR labeled `loom:pr`. Write the `judge-done` checkpoint for this issue (carrying the PR number), then continue to Merge (step 7) for this PR, then advance to the next PR in the wave.
    ```bash
    # Append --model <resolved> when you passed a model param to the judge subagent (#3482).
    ./.loom/scripts/sweep-checkpoint.sh write N judge-done --task-id "$RUN_ID" --pr-number <PR>
    ```
  - **Request changes** → PR labeled `loom:changes-requested`. Continue to Doctor (step 6) **inline for this PR**, then re-judge, then merge or block. Do **not** write a `judge-done` checkpoint here — the PR is not yet approved, and a resume after a kill should re-enter Doctor, not skip Judge.

**Why sequential and not parallel?** Parallel Judges add coordination complexity without clear benefit — each judge needs to checkout the PR and reason about it independently. Defer parallel-judge to a future issue if benchmarks justify it.

### 6. Doctor phase (inline per PR, only if Judge requested changes)

If Judge requests changes on PR `#X` mid-wave, run inline Doctor→Judge cycles for `#X` — **up to `sweep.max_doctor_cycles`** (default 1; see "Doctor-cycle cap" in the Execution Model) — before moving to the next PR's Judge:

- Load and follow the instructions in `.claude/commands/loom/doctor.md` for PR `#X`.
- **If a previous Doctor attempt for `#X` died mid-flight without writing a fresh `doctor-done` checkpoint** (rate limit, crash — the #3676 shape), re-verify forge state (pushed commit? already re-labeled `loom:review-requested`?) and complete only the missing steps rather than dispatching a fresh Doctor that would duplicate the pushed fix — see "Mid-phase-death recovery" above.
- **Model escalation (#3481)**: this Doctor is dispatched because of a Judge rejection, so resolve its model per "Model escalation on Judge rejection" in the Execution Model — pass `ladder[min(attempt - 1, len - 1)]` from `sweep.escalation` (cycle 1 → `ladder[1]`, default `opus`) via the Task tool's `model` parameter, **unless** a tier-1/tier-2 pin applies (pins win) or escalation is disabled (`[]`/`false`).
- Doctor addresses the judge's feedback, commits the fixes, and pushes.
- **On successful Doctor completion**, write the `doctor-done` checkpoint for the issue (carrying the PR number, the attempt counter, and the model the Doctor actually ran on — escalated or pinned, #3482) **before** re-invoking Judge:
  ```bash
  # <attempt> is the cycle index + 1: 2 for the first Doctor cycle, 3 for the second, etc.
  ./.loom/scripts/sweep-checkpoint.sh write N doctor-done --task-id "$RUN_ID" --pr-number <PR> --attempt <attempt> --model <doctor-model>
  ```
  This way, if sweep is killed between Doctor and the follow-up Judge, the resume run will see `doctor-done` and re-enter at the Judge phase (step 5), not redo the Doctor work.
- On completion, re-label the PR from `loom:changes-requested` back to `loom:review-requested` and **re-run the Judge phase** (step 5) for this PR.
- **Cap: up to `sweep.max_doctor_cycles` Doctor→Judge cycles per PR (default 1).** If Judge still requests changes after the configured number of Doctor passes, mark this PR as blocked (`PR #X blocked: doctor cycle exhausted after <k> Doctor→Judge round(s); human attention required`), log the reason, and proceed to the next PR in the wave (do NOT block the wave on it).
- **Distinct-defect exception (default cap only).** When `max_doctor_cycles` is at its default of 1 and the second Judge rejection is a demonstrably distinct defect from the first (forward progress, not the same disagreement re-litigated), you MAY grant **exactly one** additional bounded Doctor→Judge cycle before blocking — single-use per PR, never composing with an operator-raised cap. Emit the required log line naming the distinction (`PR #X: granted one extra Doctor cycle — second rejection is a distinct defect (<short reason>)`). Same-defect or ambiguous rejections still block immediately. See "Doctor-cycle cap" for the full rule.

The Doctor cycle for `#X` does **not** block other PRs in the wave — but because Judge runs sequentially per-PR within the wave, the next PR's Judge waits for `#X`'s Doctor→Judge cycle to settle before it starts. This is the intended sequencing. "Waits for … to settle" means **await the Doctor Task's completion explicitly** (blocking `TaskOutput`) and then await the re-run Judge — the harness may launch the Doctor async regardless of `run_in_background: false`, so this ordering is enforced by an explicit await, not a dispatch flag (see "Subagent dispatch is async-only", #3822).

### 7. Merge (per PR)

**Intra-wave overlap revalidation — run this BEFORE the merge below (#3647).** Every builder in this wave branched off the *same pre-wave `main`* (step 0's snapshot), and Judge (step 5) validated each PR against that shared base — never against the `main` that a *sibling* PR in the same wave just produced. So two PRs that both touch the same file can each pass independently and then break `main` once both land — a *semantic* merge conflict git reports as clean. The repo's branch ruleset gives **no** server-side protection here: it has no `required_status_checks` and no "require branches up to date" rule, so `merge-pr.sh --auto` merges a clean-but-stale PR immediately without re-running checks against the new base. This gate closes that hole for overlapping PRs; the step 8 integration gate closes the cross-file case this probe cannot see.

Before calling `merge-pr.sh` for PR `#X`:

1. **Cheap read-only overlap probe.** Fetch `#X`'s changed-file set and compare it against `WAVE_MERGED_FILES` (the union of paths already merged in this wave — see the step 5 loop):
   ```bash
   gh pr view X --json files -q '.files[].path'
   ```
   - **Disjoint** (no path shared with `WAVE_MERGED_FILES`) → **keep the fast path**: fall straight through to the merge below. Two PRs touching disjoint files are safe (the issue confirms this), so no revalidation latency is added. This is the common case. *(Caveat: file-path granularity cannot see cross-file semantic coupling — e.g. a `to_dict()` in a source file vs. an exact-dict assertion in a test file, which are disjoint paths. That class is the step 8 integration gate's job, not this probe's.)*
   - **Any shared path** → enter the revalidation path (step 2) before merging.
2. **Revalidate `#X` against the freshly-merged `main`.** Update `#X`'s branch onto the current `main` so it actually contains the already-merged sibling's changes:
   ```bash
   gh pr update-branch X    # or the forge equivalent (forge_update_branch)
   ```
   Re-check `mergeStateStatus` (`gh pr view X --json mergeStateStatus`) and route:
   - **`DIRTY`** (the merge introduced a textual conflict) → run an inline Doctor→Judge cycle for `#X` (step 6 — Doctor rebases onto the updated `main` and fixes, then re-Judge), then merge. Reuse — do **not** extend — the step 6 Doctor-cycle budget (a revalidation Doctor counts against `sweep.max_doctor_cycles` for `#X`, same as any other cycle).
   - **Clean, but the branch was updated** → the update pulled the sibling's changes into `#X`'s branch, so **re-run the Judge phase (step 5) against the integrated branch** before merging. Judge checks out the PR and runs its build/tests, which is what catches a *same-file* semantic break the pre-wave Judge could not. If the integrated build/tests fail → route to Doctor (or, if `#X`'s Doctor-cycle budget is already spent, mark `#X` `loom:blocked`, surface it, and do **not** merge a known-red change).
   - **A real break Doctor cannot clear in one cycle** → mark `#X` `loom:blocked`, log the reason, skip its merge, and continue with the rest of the wave (do not block the whole wave on it). Consistent with the step 6 cap.

Overlapping PRs in a wave are thus **serialized-with-revalidation**; disjoint PRs keep the parallel fast path. Under `--dry-run` nothing here runs — the plan may simply note that overlapping PRs in a wave will be serialized-with-revalidation.

Use the dedicated merge script (CLAUDE.md "Merging PRs" mandate — never `gh pr merge`):

```bash
./.loom/scripts/merge-pr.sh <PR_NUMBER> --auto
```

The script merges via the forge API and cleans up the worktree. `--auto` enables GitHub's server-side auto-merge queue (queues the merge until required checks pass); on PRs that are already in `CLEAN` state (fast CI), the script transparently falls back to an immediate merge — see #3371. **On a repo with GitHub auto-merge disabled** (`allow_auto_merge:false`), `merge-pr.sh` now detects the setting up front and degrades `--auto` gracefully to wait-for-checks-then-merge (immediate if already CLEAN) instead of failing (#3820) — so `--auto` is safe to pass uniformly here regardless of the repo's auto-merge setting; no per-repo branching is needed.

**If a previous Merge attempt for this PR died mid-flight without deleting the checkpoint** (rate limit, crash between `merge-pr.sh` success and the delete call), re-verify forge state first: if the PR is already **merged**, just delete the stale checkpoint — do **not** re-run the merge. See "Mid-phase-death recovery" above. (The step 1 stale-checkpoint cleanup is the belt-and-suspenders backstop for this.)

**On successful merge** (script returns 0), add `#X`'s changed-file paths to `WAVE_MERGED_FILES` (so the next PR's overlap probe sees them), then delete the issue's sweep checkpoint:
```bash
./.loom/scripts/sweep-checkpoint.sh delete N
```

This is the terminal state. The checkpoint must be removed so a future `/loom:sweep` invocation that references the same issue number (e.g., as part of a wider candidate set) doesn't take a `merge-done` short-circuit on the stale state. The stale-checkpoint cleanup in step 1 is the belt-and-suspenders defense if this delete is missed (e.g., sweep killed between `merge-pr.sh` success and the delete call); on the next sweep run that touches the issue, step 1 detects the closed-issue + checkpoint mismatch and removes it.

If `merge-pr.sh` fails (e.g., the merge queue rejects the PR, or required checks haven't passed and `--auto` is rejected), do **not** delete the checkpoint — leave it at `judge-done` so the next sweep retries the merge from a clean state.

### 8. Wave settled → post-wave integration gate → advance to next wave

Once every PR in the wave has reached a terminal state (merged, blocked, or builder-failed), run the integration gate below **before** starting the next wave's builders.

**Post-wave integration gate (#3647).** The step 7 overlap probe is file-path-granular: it catches two PRs that edit the **same** file, but it **cannot** see cross-file semantic coupling. That is exactly the shape of the #3647 incident — PR A changed a `to_dict()` in a *source* file and PR B added an exact-dict assertion in a *test* file. Their changed-file sets are **disjoint**, so step 7 took the fast path for both, yet `main` went red once both landed. File-path overlap alone therefore cannot protect the original incident; this gate is the load-bearing backstop for it:

- **If a build/test command is configured** (`buildGate.command`, honoring `buildGate.enabled`, in `.loom/config.json`), run it once against the post-wave `main` — pull/refresh `main` to its just-merged state and run the command there. On failure, **halt the sweep**: do not start the next wave, log the failing command and its output, and surface the red `main` (e.g. leave a clear error in the summary and/or open a recovery issue). A red `main` must stop the run rather than compound across subsequent waves.
- **If no such command is configured**, the step 7 overlap revalidation is the only intra-wave protection — same-file collisions are caught, but cross-file semantic coupling (source-vs-test) is **not**. Log a one-line advisory recommending a `buildGate.command` for waves that cluster on one subsystem, and — per the issue's mitigation #3 — prefer placing issues likely to touch a shared serialization/schema surface in **separate size-1 waves** rather than parallelizing them.

Under `--dry-run` the gate does not run (no checkout, no command execution); the plan may note that a post-wave integration check would run if `buildGate.command` is configured.

Once the gate has passed (or is not configured), advance to the next wave. Do not start the next wave's builders until the current wave's PRs are all settled and the integration gate (if configured) is green.

## Summary Output

When the entire list has been processed, print a summary table that includes wave membership for each issue:

```
/loom:sweep complete. Processed M issue(s) across W wave(s):

  #123  → merged  (PR #456)                                              [wave 1]
  #124  → blocked (judge requested changes, doctor cycle exhausted)      [wave 1]
  #125  → skipped (already in flight: loom:building)                     [wave 1]
  #126  → blocked (builder failed: build error)                          [wave 2]
  #127  → merged  (PR #459)                                              [wave 2]
  #128  → merged  (PR #460; rate-limited (resumed: doctor TOKEN_EXHAUSTED mid-phase — fix already pushed, re-labeled + re-judged))  [wave 2]
  #129  → rate-limited (unresumable: judge TOKEN_EXPIRED mid-phase, human attention required)  [wave 2]
  #199  → routed  (existing PR #200, judged in this wave)                [wave 2]
  #198  → merged  (existing PR #201, was loom:pr)                        [wave 2]
  #197  → skipped (multiple open PRs reference issue: #210, #211)        [wave 2]

Total: 5 merged, 2 blocked, 2 skipped, 1 rate-limited (unresumable).
```

Wave annotation makes it easier to triage failures (e.g., "every issue in wave 2 failed → probably a base-branch problem, not the issues themselves").

**`rate-limited` vs `blocked` (issue #3683).** These are semantically distinct — reuse the `TOKEN_EXPIRED` / `TOKEN_EXHAUSTED` vocabulary from `.loom/scripts/lib/classify-error.sh` for the reason. `blocked (...)` means the **work itself** failed (build error, doctor cycle exhausted) and a human must fix the actual problem. `rate-limited (...)` means only that a role subagent was killed by an account rate limit mid-phase, so an **extra orchestrator pass** was needed to reach the phase's expected exit state — it says nothing about work quality. A `rate-limited (resumed: <what completed>)` outcome already succeeded (the mid-phase-death recovery finished the missing steps); only a `rate-limited (unresumable: ...)` outcome — where the forge state cannot be recovered without human help — needs attention.

## Session Transcript Archival (completion hook, #3726)

After the entire sweep has settled (issue list exhausted / all PRs processed) and just before printing the Summary Output, run the transcript archiver once so this session's transcript and all its subagent transcripts are captured to durable storage:

```bash
./.loom/scripts/archive-transcripts.sh
```

This is **safe to run unconditionally** — the archiver is a **no-op unless archival is opted in** (env `LOOM_TRANSCRIPT_ARCHIVE=<dir>` or `.loom/config.json → loom.transcriptArchive.enabled`). When enabled it copies `${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects/<cwd-slug>/` (the session's own `<uuid>.jsonl` plus the sibling `<uuid>/subagents/agent-*.jsonl` + `.meta.json` sidecars) into `<dir>/<repo>/<date>/<uuid>/`, emits an `agent-<id>`-keyed `index.json` join key, and is **idempotent** (a re-run copies nothing new).

**Caveat (why the cron backstop still matters):** at this completion point the session's own top-level `<uuid>.jsonl` may still be mid-flush — the final orchestrator messages can lag. The completion hook reliably captures finished subagents; the durable tail is guaranteed only by the cron-friendly periodic sync documented in CLAUDE.md ("Session Transcript Archival"). Run both.

**Guardrails apply** (off by default; destination `0700`/files `0600`; refuses if the destination is inside a git repo but not gitignored; prints a loud banner naming the destination when enabled). See CLAUDE.md → "Session Transcript Archival" for the full contract and the secrets caveat.

> **Daemon detached-child path (v1 refinement, not a blocker).** For sweeps dispatched by the daemon as detached children, the reaper in `loom-daemon/src/sweep_registry.rs` knows the child's PID and issue but **not** its Claude session-uuid, so it cannot yet trigger a precise single-session archive on exit. v1 relies on the **periodic sync** (which copies all recent sessions for the repo regardless of uuid mapping) as the backstop for the detached path; a reaper-triggered auto-invoke keyed on the child's session-uuid is a documented follow-on.

## Stop Conditions

Stop processing and print the summary when any of these conditions hold:

- The issue list is exhausted.
- The user interrupts (Ctrl-C or explicit stop).
- An unrecoverable error occurs (e.g., `gh` is not authenticated, repository state is broken). Log the error and exit.

This skill does **not** implement a disk-pressure *stop* condition (aborting an in-flight sweep when the disk fills), max-waves caps, or doctor-cycle global limits — those are deferred (see Limitations). It **does** apply a disk-headroom *gate* when resolving the auto wave size at Stage -1 (see "Resolve auto wave size"): the scratch-volume free space clamps the initial wave size down, but does not stop a running sweep.

## Host Sleep Readiness (#3350)

Long sweeps run for many minutes — sometimes hours overnight — and the host going to sleep mid-run tears down in-flight subagent sockets to `api.anthropic.com`, killing curator / builder / judge subagents and losing all their work (see #3350 for the incident report).

**Before the first wave**, run the host-sleep readiness check and surface its output to the user:

```bash
./.loom/scripts/check-host-sleep.sh
```

This is advisory-only. The script always exits `0` and **must not block** the sweep — proceed regardless of what it prints. It prints a platform-aware warning to stderr when the host is configured in a way that allows it to sleep:

- **macOS:** even with a user-idle sleep assertion (Amphetamine, `caffeinate -dimsu`, etc.), macOS Maintenance Sleep can still fire and tear down sockets. The reliable defenses are `sudo pmset -c sleep 0` or flipping your sleep manager's "allow system sleep when display is off" toggle to OFF.
- **systemd Linux:** wrap the session in `systemd-inhibit --what=idle:sleep --who=loom --why=sweep -- <cmd>`, which IS reliable.

If the user is running an overnight sweep, they should heed the warning before walking away.

## Main Branch Freshness (#3770)

During a long sweep, other PRs can merge to `origin`'s default branch. Because the installed `.loom/scripts/` and `.loom/hooks/` copies are synced from `defaults/` at install time, a local default branch that has drifted behind `origin` means the session may be executing **stale orchestration scripts** that silently lack recently-merged logic. This actually happened (#3770): during a 2026-07-22 sweep, `worktree.sh --base` (#3742) and `merge-pr.sh` auto-reconcile (#3752) were absent from the copies the session was running even though both had merged to `origin/main` — a running sweep had no signal it was behind.

**Before the first wave**, run the main-freshness check and surface its output to the user (same timing and sibling role as the Host Sleep Readiness check above):

```bash
./.loom/scripts/check-main-freshness.sh
```

This is advisory-only. The script always exits `0` and **must not block** the sweep — proceed regardless of what it prints. It is strictly **read-only**: it never runs `git pull` / `git merge` / `git reset` and never auto-reconciles. It does a bounded `git fetch` of the default branch (degrading gracefully to the last-known ref when offline), then compares the local default branch against `origin/<default-branch>`:

- **Behind by N commits:** prints a bordered warning to stderr noting that installed `.loom/scripts/` / `.loom/hooks/` copies may be stale, with the remediation `git merge --ff-only origin/<default-branch>`. When it can resolve both trees it also best-effort notes any installed script/hook whose content differs from its `defaults/` counterpart.
- **Up to date:** prints nothing to stderr; a one-line stdout confirmation (suppressible with `--quiet`, matching `check-host-sleep.sh`).

If the check warns, the operator should refresh local `main` (and re-sync installed copies if their install flow does so) before relying on stacked-dependency or auto-reconcile behavior mid-sweep.

## Coexistence (peer `/loom:sweep` and legacy daemon)

`/loom:sweep` coexists with two **distinct** kinds of other runner, detected by two **separate** mechanisms. Do not conflate them: "another `/loom:sweep` is running" (peer detection, #3768) is not the same as "the legacy daemon is running" (daemon-PID check). Both warnings are **loud but non-blocking** — warn once, never auto-stop, never block.

### Peer `/loom:sweep` detection (#3768)

The primary coexistence case in the current architecture is **another live `/loom:sweep` invocation in the same repo**. This is handled at sweep start by "Sweep Run Identity + Peer-`/loom:sweep` Detection" (Step 0b, above): `sweep-run-registry.sh peers "$RUN_ID"` lists other runs whose registered liveness PID is still alive (pruning dead-PID entries so a SIGKILL'd peer never warns forever), and a loud non-blocking warning fires when any are found.

Two concurrent sweeps are now **run-state isolated**, not just label-isolated:

- **Per-issue `loom:building` claims** (step 1 pre-flight) already prevent two sweeps from building the same issue — if a peer claimed an issue first, this sweep sees `loom:building` and skips. The existing-PR probe (#3359) is the complementary defense when a PR exists but the `loom:building` label was never set / since removed.
- **Main-clean baseline** is keyed by `RUN_ID` (`main-clean-baseline-${RUN_ID}.txt`), so a peer sweep's `--snapshot` can never clobber this run's pre-sweep baseline (the #3648 contamination backstop stays correct under concurrency).
- **Checkpoints** carry this run's `RUN_ID` as `task_id`, so a sweep can tell its own `.loom/sweep-checkpoint/issue-<N>.json` writes apart from a peer's.

What remains a shared, un-isolated surface is the **default branch itself**: both sweeps merge into a moving `main`, unaware of each other's in-flight PRs. The peer warning exists so the operator knows that; isolating the merge target is out of scope for #3768 (stacking is #3759's concern).

### Legacy daemon coexistence

> **Note**: the legacy `./.loom/scripts/daemon.sh` was removed in #3432 and is not restored. The historical PID-file daemon (`.loom/daemon-loop.pid`) is not part of the current architecture; the check below is a defensive coexistence guard that fires only if such a process is somehow already running — normally a no-op. The Tier 2 dispatch backend is now the Rust `loom-daemon` binary (observed via `mcp__loom__list_sweeps`); the background agent-pool control surface is `.loom/bin/loom start|status|stop`.

`/loom:sweep` does not require the daemon and does not interact with `.loom/daemon-state.json` for writes. If a legacy daemon process is running, `/loom:sweep` and the daemon may both try to claim the same `loom:issue` label. This is a **different** mechanism from peer-`/loom:sweep` detection above — the daemon is identified by its own `.loom/daemon-loop.pid`, not by the `.loom/sweep-run/` registry.

**Coexistence behavior:** before the first wave, check whether the daemon is running. If it is, warn the user once at the start of the sweep:

```bash
PID=$(cat .loom/daemon-loop.pid 2>/dev/null)
if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
  echo "⚠️  Loom daemon is running (PID $PID). /loom:sweep will race with the daemon"
  echo "   for issues in the loom:issue queue. Consider stopping the pool first:"
  echo "       ./.loom/bin/loom stop"
fi
```

Do not auto-stop the daemon. Do not block on this warning — proceed with the sweep. The same dead-PID liveness pattern (`kill -0`) is used by peer-`/loom:sweep` detection.

## Constraints

- **Wave model, one level deep.** When `--builders-per-wave > 1` (Modes A/B only), dispatch `loom-builder` / `loom-judge` / `loom-doctor` subagents **directly from this orchestrator session** in a single tool-call block. In Mode C, dispatch `loom-judge` and `loom-doctor` as **single subagent Tasks** per PR (size-1 waves). **Never invoke `/loom:sweep`, `/judge`, or `/doctor` as a subagent from `/loom:sweep`** — that is the two-levels-deep pattern that triggers the #3289 stall. See "CRITICAL: One level deep" in the Execution Model.
- **Per-PR Judge is sequential within a wave.** Builders parallelize (Modes A/B); judges do not. Mode C inherits this: PRs are processed one per size-1 wave. Don't parallelize judges or PRs without a separate design pass.
- **Configurable Doctor→Judge cycle cap per PR (`sweep.max_doctor_cycles`, default 1).** Inline within the wave (Modes A/B issue-side and Mode C PR-side both enforce this). If Judge still requests changes after the configured number of Doctor passes, the PR is blocked — do not retry indefinitely. At the default cap of 1, the orchestrator may grant one extra bounded cycle when the second rejection is a demonstrably distinct defect (logged, single-use, never on an operator-raised cap) — see "Doctor-cycle cap".
- **Mode C skips Curator, Approval gate, and Builder.** These phases already ran (the PR exists). Re-running them would be incorrect.
- **No new labels.** Use only the existing Loom label set (see `.github/labels.yml`). Mode C operates entirely on `loom:review-requested`, `loom:changes-requested`, `loom:pr`, `loom:blocked`, `loom:operator-only` — all existing.
- **No `gh pr merge`.** Always use `./.loom/scripts/merge-pr.sh` (uniform across Modes A/B/C).
- **No daemon-state writes.** Read-only access to `daemon-state.json` for situational awareness.
- **Read the issue body** (`gh issue view N --json body`) before briefing the builder (Modes A/B). Mode C uses the PR diff + comments as the source of truth and does not need the issue body.
- **Skip operator-only items.** Issues labeled `loom:operator-only` (Modes A/B, see issue-set Wave Lifecycle step 1) and PRs labeled `loom:operator-only` (Mode C, see C0) are skipped. Log and move on.

## Limitations (Deferred for Follow-up Issues)

The full `/loom:sweep` design in #3298 includes many features that are intentionally **not** part of this skill yet. Each of these is a candidate follow-up issue:

| Feature | Status | Notes |
|---------|--------|-------|
| Parallel waves (`--builders-per-wave N`) | **Implemented (#3316, auto default #3566, core-scaled #3693)** | Omitted flag resolves to an auto wave size at Stage -1 (#3566): up to 10 on the daemon detached-process path, core-scaled within `[3, 6]` (`clamp(floor((cores-2)/4), 3, 6)`, #3693) on the in-session subagent path. The `[3, 6]` band is **subagent-path-specific** (floor 3 is the #3289-safe validated minimum, ceiling 6 keeps a margin below single-account rate-limit burn and orchestrator context pressure — warns above only on explicit override `>= 7`); the daemon path scales to 10 because each sweep is an isolated process, not a nested subagent. This is a **width** knob — the #3289 "one level deep" nesting rule is unchanged: no nested `/loom:sweep` subagent. Issue-side only; ignored in Mode C. |
| Natural-language selectors (label/author/title/time-window filters via NL description) | **Implemented (#3318)** | Mode B in Arguments. Out-of-band queries (body/diff inspection, file-touch filters) still trigger clarification. |
| Build-everything sentinel (`/loom:sweep all`) | **Implemented (#3568; aggressive whole-backlog redefinition)** | Bare, sole `all` token (case-insensitive) resolves **every** open issue via `gh issue list --state open` (no label filter) and aggressively drives each toward a merged PR: curates uncurated/`loom:triage`/`loom:curating` issues, reclaims stale `loom:building` claims (one-time `recover-orphaned-shepherds.sh --recover` pass + `updatedAt` staleness), probes `loom:blocked` for a cleared blocker, fans `loom:epic` out to its `loom:epic-phase` children, and routes existing open PRs to Judge/Doctor/Merge via the #3359 probe (which takes precedence). Only `loom:operator-only` is hard-skipped. `all --prs` resolves every open PR (Mode C C0 filters non-actionable). Mandatory confirmation gate; `--dry-run` / `--builders-per-wave` / `--no-daemon` compose unchanged (recovery pass skipped under `--dry-run`). Multi-token `all …` phrases still route to Mode B/C. |
| `--dry-run` | **Implemented (#3319, extended in #3384)** | Prints the candidate plan (with wave grouping) and exits without mutating labels, worktrees, or PRs. Issue-set (Modes A/B) and PR-set (Mode C) output formats. |
| Existing-PR detection in pre-flight | **Implemented (#3359, #3677)** | Pre-flight probes the union of `closedByPullRequestsReferences` (closing-keyword PRs) **and** timeline `cross-referenced` open-PR events (non-closing `Part of #N` / `Contributes to #N` PRs); routes existing open linked PRs to Judge (or Merge if already `loom:pr`) instead of dispatching a duplicate Builder. Multi-PR ambiguity skips with a log. |
| `loom:operator-only` enforcement | **Implemented (#3360)** | Pre-flight skips issues with `loom:operator-only` (human action required: credentials, infra, hardware). Champion `--merge` mode also refuses to auto-promote them. |
| Checkpoint/resume after kill | **Implemented (#3373)** | Per-issue phase checkpoint at `.loom/sweep-checkpoint/issue-<N>.json`. Sweep reads on entry and skips completed phases. No mid-builder recovery — kill during Builder resumes at builder start, worktree preserved by `worktree.sh` idempotency. Mode C reuses the helper keyed by the PR's closing-issue number (`closingIssuesReferences`); PRs without a `Closes #N` reference run without checkpointing. |
| PR-set mode (`--prs` flag and PR NL triggers; Judge/Doctor/Merge from current PR label) | **Implemented (#3384)** | Mode C. Skips Curator, Approval gate, Builder. Size-1 waves. `--builders-per-wave` ignored. Reuses issue-keyed checkpoint via `closingIssuesReferences`. |
| Daemon backend detection (Stage -1) | **Implemented (#3454, daemon-owned-child short-circuit #3829)** | Strict-AND between daemon reachability and multi-account pool. Mode C, `--no-daemon`, and a daemon-dispatched child (`LOOM_SWEEP_CLAIM_OWNED` set, #3829) short-circuit to subagent — the last **before** any probe, so a daemon child never re-probes/re-dispatches the daemon that spawned it (the circular-round-trip idle-hang fix). No implicit auto-start. Dispatch-only — Phase D does not subscribe to the event bus. See "Stage -1: Backend detection". |
| Concurrent-`/loom:sweep` run-state isolation + peer detection | **Implemented (#3768)** | A stable per-sweep-run id (`sweep-run-registry.sh new`) is generated once at sweep start and threaded through all `--task-id` checkpoint writes and the main-clean baseline path (`main-clean-baseline-${RUN_ID}.txt`), so two concurrent sweeps no longer clobber each other's baseline or share an ambiguous `sweep-$$` `task_id`. Stage 0b adds a loud, NON-BLOCKING peer-`/loom:sweep` warning via a dead-PID-pruned run registry (`.loom/sweep-run/`). Merge-target (default-branch) isolation is out of scope — that is #3759's stacking concern. See "Sweep Run Identity + Peer-`/loom:sweep` Detection". |
| `--max-waves` cap | Deferred | Operator-level brake on long sweeps. |
| `--paused-merge` / `--no-judge` | Deferred | Merge-mode variants for trusted batches. |
| `--include-blocked` (unblock pass) | Deferred | Currently `/loom:sweep` skips `loom:blocked` issues outright. |
| `--curator-also` (parallel curators on `loom:triage`) | Deferred | Parallel triage is a separate orchestration question. |
| Config-driven defaults (`.loom/config.json` keys `sweep.*`) | **Partially implemented** | `sweep.escalation` (#3481, model ladder) and `sweep.max_doctor_cycles` (#3668, Doctor-cycle cap, default 1) are live and read at lifecycle-entry time. Other `sweep.*` knobs (e.g. `--max-waves` persistence) remain deferred. |
| Disk-pressure *gate* on auto wave size | **Implemented (#3566)** | Stage -1 resolves the auto wave size against free space on the **worktree-root filesystem** (via `loom_worktree_root`, so it measures the dedicated scratch volume when `LOOM_WORKTREE_ROOT` / `worktree.root` is set — #3539/#3541), clamping the target down and logging the reason. `LOOM_PER_WORKTREE_GB` (default 2) is the per-worktree estimate. |
| Disk-pressure *stop* condition (abort a running sweep on low disk) | Deferred | Only the initial auto wave size is gated (above); no mid-sweep abort. Wave sequencing limits disk usage; revisit if waves grow large. |
| Doctor-cycle counting across PRs | Deferred | The per-PR cap is now configurable (`sweep.max_doctor_cycles`, #3668, default 1) with a default-cap distinct-defect grace cycle, enforced inline. A *cross-PR aggregate* cycle budget (e.g. "at most K total Doctor cycles across a whole sweep") is still deferred. |
| Parallel Judges within a wave | Deferred | Sequential per-PR Judge today; needs benchmarking before parallelizing. Mode C is also strictly sequential per PR (size-1 waves). |
| Parallel PRs in Mode C | Deferred | Mode C uses size-1 waves. Multi-PR-per-wave is feasible (one judge per PR in parallel) but inherits the same #3289 risk that gated parallel issue-side Judges. |
| Mixed-mode invocations (some issues + some PRs in one `/loom:sweep`) | Won't fix (split into two calls) | Routing logic for the cross product of issue-state × PR-state is complex; cleaner to require two invocations. |
| Multi-closing-issue PRs (PR with `Closes #N` + `Closes #M`) | Partial — runs without checkpoint | Mode C logs all closing issues and proceeds with Judge/Doctor/Merge but skips checkpointing for the PR. Multi-key checkpoint variant is a follow-up. |
| PRs without `Closes #N` references | Partial — runs without checkpoint | Mode C logs a warning and processes the PR without checkpointing. Judge/Doctor/Merge are idempotent at the GitHub-state level so re-running on the next sweep is safe. |
| Cross-wave backfill on pre-flight skips | Won't fix | Intentionally clean wave boundaries — see step 1 of the Wave Lifecycle. |
| Intra-wave collision guard (overlapping PRs off a shared base) | **Implemented (#3647)** | Step 7 runs a read-only file-path overlap probe before each in-wave merge; overlapping PRs are updated onto the just-merged `main` and re-Judged (or Doctor→re-Judge on `DIRTY`) before merging, disjoint PRs keep the fast path. Step 8 adds a post-wave `buildGate.command`-against-`main` integration gate — the load-bearing backstop for cross-file semantic coupling (source-vs-test) that path-overlap cannot see; halts the sweep on a red `main`. Symbol/AST-level overlap detection is out of scope. |
| Spinoff-issue filing for out-of-scope discoveries | Deferred | Build it once we have richer summary output to surface them cleanly. |
| Daemon `pipeline_state` situational awareness reads | Deferred | Skill only warns when the daemon is running. |
| Top-level vs namespaced naming (`/loom:sweep` vs `/loom:sweep`) | **Resolved** | Ships as the namespaced `/loom:sweep` (and `/loom:loom` for the daemon operator), matching CLAUDE.md and `help.md`. Originally #3298 open question #1. |

For the full design discussion (including the open questions raised by the curator), see issue #3298.

## Daemon event bus (Phase B of #3449 — #3453)

When the in-process **loom-daemon** is running, the sweep child **must** publish phase-transition events onto the daemon's in-memory pub/sub bus so monitoring tools, the spawn loop, and any subscribed MCP layer can react in real time. This is the **wire-protocol contract** the skill exposes to the daemon (and via the daemon to the rest of Loom).

The bus is an in-process `tokio::sync::broadcast::channel<Event>` with a default capacity of **1024** events. It is **not** NATS/ZeroMQ — it lives only inside the running daemon and is gone the moment the daemon exits. Subscribers route by **topic prefix** (segment-aligned — `sweep.issue` matches `sweep.issue.123.phase` but not `sweep.issuetype.foo`). Slow subscribers receive a synthetic `topic_lag` event when they fall behind, then resume at the current channel head (pass-through, no silent drops; matches tokio's `Receiver::Lagged` semantics).

### When to publish

Publish a `sweep.issue.{N}.phase` event **immediately after the sweep skill commits a phase transition** — i.e. once the phase is durable in the forge (label flipped, comment posted, checkpoint written via `sweep-checkpoint.sh`). Do not publish before the side effects have landed; downstream subscribers treat the event as the authoritative signal that the phase is complete.

Publish a `sweep.issue.{N}.blocker` event when the skill chooses to mark the issue with a Loom-recognized blocker label (e.g., `loom:blocked`, `loom:operator-only`) and exits the lifecycle without proceeding to the next phase.

The daemon publishes `sweep.issue.{N}.exited`, `sweep.issue.{N}.crashed`, `sweep.global.dispatch`, and `sweep.global.completed` itself — the sweep child does **not** publish those.

### Topic taxonomy (frozen for v0.10.0)

The following six topics are the **entire** event vocabulary for v0.10.0. New topics require a follow-up issue — do not invent topics outside this table.

| Topic | Publisher | Payload (JSON) |
|-------|-----------|----------------|
| `sweep.issue.{N}.phase` | Sweep child via `PublishEvent` | `{"phase": "<phase-name>", "pr_number": <int or null>}` |
| `sweep.issue.{N}.blocker` | Sweep child | `{"reason": "<short-text>", "label_added": "<label>"}` |
| `sweep.issue.{N}.exited` | Daemon reaper | `{"exit_code": <int or null>, "duration_sec": <int>}` |
| `sweep.issue.{N}.crashed` | Daemon reaper | `{"checkpoint_phase": "<phase-name or null>"}` |
| `sweep.global.dispatch` | Daemon | `{"sweep_id": "<id>", "kind": {"type": "Issue", "value": <N>}}` |
| `sweep.global.completed` | Daemon | `{"sweep_id": "<id>", "outcome": "exited" | "crashed"}` |

`{N}` is the issue number (a positive integer). Phase names match the sweep-checkpoint schema (#3373): `curator`, `builder`, `judge`, `doctor`, `merge`, etc.

### How to publish — IPC contract

The daemon exposes a `Request::PublishEvent { topic, payload }` variant over its line-delimited JSON Unix-socket framing (the same socket used for `DispatchSweep`, `ListSweeps`, etc. — see `loom-daemon/src/ipc.rs`). One request → one `Response::EventPublished { topic, receivers }` ack frame.

**Sample wire frame** — sweep child advertises that it just finished the builder phase and opened PR #501:

```json
{"type": "PublishEvent", "payload": {"topic": "sweep.issue.123.phase", "payload": {"phase": "builder", "pr_number": 501}}}
```

The daemon responds with:

```json
{"type": "EventPublished", "payload": {"topic": "sweep.issue.123.phase", "receivers": 2}}
```

If no subscribers are listening, `receivers` is `0` and the event is dropped. **This is not an error condition** — the sweep child treats `receivers: 0` as "best-effort delivery, nobody home" and continues. Do not retry; the event is fire-and-forget.

### Sample payloads for the six initial topics

The following six samples are the authoritative reference for the payload schema of each frozen topic.

```json
{"type": "PublishEvent", "payload": {"topic": "sweep.issue.123.phase", "payload": {"phase": "curator", "pr_number": null}}}
{"type": "PublishEvent", "payload": {"topic": "sweep.issue.123.phase", "payload": {"phase": "builder", "pr_number": 501}}}
{"type": "PublishEvent", "payload": {"topic": "sweep.issue.123.phase", "payload": {"phase": "judge", "pr_number": 501}}}
{"type": "PublishEvent", "payload": {"topic": "sweep.issue.123.phase", "payload": {"phase": "merge", "pr_number": 501}}}
{"type": "PublishEvent", "payload": {"topic": "sweep.issue.123.blocker", "payload": {"reason": "missing credentials", "label_added": "loom:operator-only"}}}
{"type": "PublishEvent", "payload": {"topic": "sweep.issue.456.blocker", "payload": {"reason": "dependent on #999", "label_added": "loom:blocked"}}}
```

The daemon-side events (these are **emitted by the daemon**, not by the sweep child — included here as the contract for subscribers):

```json
{"type": "EventStream", "payload": {"events": [{"type": "SweepExited", "issue": 123, "exit_code": 0, "duration_sec": 1842}]}}
{"type": "EventStream", "payload": {"events": [{"type": "SweepCrashed", "issue": 456, "checkpoint_phase": "judge"}]}}
{"type": "EventStream", "payload": {"events": [{"type": "SweepGlobalDispatch", "sweep_id": "sweep-issue-789-1717599600", "kind": {"type": "Issue", "value": 789}}]}}
{"type": "EventStream", "payload": {"events": [{"type": "SweepGlobalCompleted", "sweep_id": "sweep-issue-123-1717599600", "outcome": "exited"}]}}
```

### Subscription (for tooling, not the sweep child)

Long-running monitors subscribe with a single `Request::SubscribeEvents { topics }` frame and receive a stream of `Response::EventStream { events }` frames on the same open connection. Topic matching is prefix-aligned: `["sweep.issue.123"]` matches every event for issue 123; `["sweep.global"]` matches the two global topics; `[]` (empty list) matches everything on the bus.

```json
{"type": "SubscribeEvents", "payload": {"topics": ["sweep.issue.123", "sweep.global.completed"]}}
```

The sweep child itself does **not** subscribe — it only publishes. Subscription is consumed by the operator-facing monitoring tools that shipped in Phase C (#3455) and any custom MCP-bridged tool an operator wires up.

### Failure modes (publisher side)

- **Daemon not running**: the Unix-socket connect fails. The sweep child must treat this as a soft error and continue without publishing — Loom is designed to run without the daemon. Log a single `debug` line and proceed.
- **Daemon running but no subscribers**: `Response::EventPublished { receivers: 0 }`. Fire-and-forget; continue.
- **Bus capacity exhausted on the subscriber side**: the slow subscriber sees a `topic_lag` event; **the publisher is unaffected** and never blocks. The bus is bounded but tokio's broadcast channel has pass-through overflow on the receiver, not the sender.

### Out-of-scope for Phase B

These live in the daemon, not the sweep skill — do **not** implement them in the sweep skill. The operator-facing MCP tools shipped in Phase C (#3455); the rest are frozen non-goals:

- Operator-facing MCP tools (`get_sweep_status`, `subscribe_to_events`, `tail_event_bus`) — daemon-side, shipped in Phase C (#3455).
- New topics beyond the six listed — frozen for v0.10.0 per epic #3449; file a follow-up issue if you "need" one.
- Distributed bus / cross-daemon coordination — explicit non-goal (single broker, in-process).
- Persistent event log or replay — explicit non-goal (transient bus).
- Consumer groups / durable subscriptions — explicit non-goal.

## Reference Documentation

- **Per-issue lifecycle**: the "Wave Lifecycle (Modes A and B only — issue-set)" section of this skill — canonical phase-by-phase reference (Curator → Builder → Judge → Doctor → Merge).
- **Builder skill**: `.claude/commands/loom/builder.md`
- **Judge skill**: `.claude/commands/loom/judge.md`
- **Doctor skill**: `.claude/commands/loom/doctor.md`
- **Curator skill**: `.claude/commands/loom/curator.md`
- **Label definitions**: `.github/labels.yml`
- **Merge script**: `./.loom/scripts/merge-pr.sh`
- **Sweep checkpoint helper**: `./.loom/scripts/sweep-checkpoint.sh` — read/write/delete per-issue phase checkpoints for resume after kill (#3373). Mode C reuses this via the PR's closing-issue number when available.
- **Original proposal & open questions**: issue #3298
- **PR-set mode (Mode C) design**: issue #3384
- **Nested-dispatch stall hazard**: issue #3289
- **Checkpoint/resume design**: issue #3373 (Phase 0 of #3372 shepherd/daemon deprecation epic)
- **Daemon backend detection (Stage -1)**: issue #3454 (Phase D of #3449 daemon rebuild epic)
- **Daemon dispatch MCP tool (`mcp__loom__dispatch_sweep`)**: issue #3452 (Phase A of #3449)
- **Daemon event bus (Phase B)**: issue #3453 (Phase B of #3449)
