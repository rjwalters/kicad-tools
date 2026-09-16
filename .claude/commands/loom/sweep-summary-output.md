# Sweep — Summary output and session transcript archival

> **Reference file for [`sweep.md`](sweep.md)**, the `/loom:sweep` dispatcher.
>
> **Load when:** the run is settling — the outcome vocabulary for the summary table, and the transcript-archival completion hook that runs just before it.
>
> **Flat, one level deep.** `sweep.md` names every file a given run needs up
> front; nothing here requires opening a *third* file to follow its own
> procedure. Cross-references worded "above"/"below" that do not resolve inside
> this file point at a sibling `sweep-*.md` section — see the reference-file map
> in [`sweep.md`](sweep.md). The body below is **verbatim** from the pre-split
> `sweep.md` (#7726): no rule, warning, edge case, table, or code block was
> reworded, softened, reordered, or dropped.

## Contents

- [Summary Output](#summary-output)
- [Session Transcript Archival (completion hook, #3726)](#session-transcript-archival-completion-hook-3726)

---

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
  #130  → merged  (PR #461; rate-limited (downgraded: builder MODEL_CREDITS_EXHAUSTED on opus — same attempt re-dispatched on sonnet))  [wave 2]
  #131  → merged  (PR #462; rate-limited (downgraded: builder SPEND_LIMIT on opus — same attempt re-dispatched on session default))  [wave 2]
  #199  → routed  (existing PR #200, judged in this wave)                [wave 2]
  #198  → merged  (existing PR #201, was loom:pr)                        [wave 2]
  #197  → skipped (multiple open PRs reference issue: #210, #211)        [wave 2]
  #196  → completed externally (daemon/champion; PR #212 merged, closed before wave 3) [wave 3]
  #195  → completed externally (daemon/champion; issue closed, no PR)    [wave 3]

Total: 7 merged, 2 blocked, 2 skipped, 1 rate-limited (unresumable), 2 completed externally.
```

Wave annotation makes it easier to triage failures (e.g., "every issue in wave 2 failed → probably a base-branch problem, not the issues themselves").

**`rate-limited` vs `blocked` (issue #3683).** These are semantically distinct — reuse the `TOKEN_EXPIRED` / `TOKEN_EXHAUSTED` vocabulary from `.loom/scripts/lib/classify-error.sh` for the reason. `blocked (...)` means the **work itself** failed (build error, doctor cycle exhausted) and a human must fix the actual problem. `rate-limited (...)` means only that a role subagent was killed by an account rate limit mid-phase, so an **extra orchestrator pass** was needed to reach the phase's expected exit state — it says nothing about work quality. A `rate-limited (resumed: <what completed>)` outcome already succeeded (the mid-phase-death recovery finished the missing steps); only a `rate-limited (unresumable: ...)` outcome — where the forge state cannot be recovered without human help — needs attention.

**Third reason prefix: `rate-limited (downgraded: ...)` (issue #5687, extended #6518).** Reserved for a kill that a model-substitution fallback recovered by re-dispatching the same attempt on a different model — either a **`MODEL_CREDITS_EXHAUSTED`** kill recovered one rung down the cost ladder ("Credit-exhaustion fallback"), or a **`SPEND_LIMIT`** kill recovered by omitting the `model` param entirely ("Spend-limit fallback"). Keep it distinct from the other prefixes and from the classes they name, because they tell the operator different things:

| Reason prefix | What died | What fixed it | Operator signal |
|---|---|---|---|
| `resumed: <phase> TOKEN_EXPIRED\|TOKEN_EXHAUSTED …` | the Claude **account credential** | token rotation + forge-state re-verification | account pool is thin — check `.bad_tokens` / add accounts |
| `downgraded: <phase> MODEL_CREDITS_EXHAUSTED on <model> — same attempt re-dispatched on <cheaper>` | the account's credits for **one model tier** | one rung down the cost ladder, same account, same attempt | the sweep is running above the tier the account can sustain — consider lowering `sweep.tierModels` / `sweep.optimization` rather than adding accounts |
| `downgraded: <phase> SPEND_LIMIT on <model> — same attempt re-dispatched on session default` | possibly the whole account's **spend cap**, possibly just `<model>`'s tier (scope not knowable from the signature alone) | the `model` param dropped, inheriting the session default | a spend cap is binding on this account — raise it at claude.ai/settings/usage, or check whether it recurs on the session-default model too (account-wide) |
| `unresumable: …` | any of the above | nothing — human needed | act now |

Always name the **classifier category** and the model substitution in a `downgraded:` reason. `MODEL_CREDITS_EXHAUSTED` / `SPEND_LIMIT` is what makes it greppable across runs, and the `<model> → <cheaper>` (or `→ session default`) pair is what lets the operator see how the run recovered. A `downgraded:` outcome that ends in `merged` needs **no** human attention — like `resumed:`, it already succeeded. A downgrade that ran out of options is **not** a `downgraded:` outcome; it is `rate-limited (unresumable: … no cheaper model rung available)` for credit exhaustion, or `rate-limited (unresumable: … no session-default fallback available)` for a spend limit. Do **not** report either as a forge GraphQL rate limit or as an account rotation — those are separate axes (see "Mid-phase-death recovery").

**Fourth reason prefix: `rate-limited (forge-transient: ...)` (issue #6425).** A **different axis again** from the three above — those name the Claude account/model dying; this names a **forge write** (merge, label, comment, PR/issue create) failing on an outage-shaped signature (5xx, "No server is currently available", a connection reset) or on an unconfirmed permission-scope 403. See "Forge write failure diagnosis (#6425)" (Mode B, above) for the full classification procedure (`is_forge_transient_error` / `forge_write_permission_confirmed` in `lib/forge-helpers.sh`) and its own rule: **never** promote this to a `blocked` "needs operator attention" / credential outcome without positive evidence (a same-credential read succeeding while the write fails, persisting across a retry) — and when you do, cite the check in the log line rather than using the `forge-transient:` prefix at all. This is the fix for the 2026-08-17 incident where two sweeps wrote a confident "GitHub App lacks write permission" diagnosis during a confirmed GitHub outage; both diagnoses were wrong (the writes succeeded normally once GitHub recovered).

**`completed externally` vs sweep-driven outcomes (issue #4884).** A third axis, distinct from both of the above: `completed externally (daemon/champion; ...)` means the candidate reached a terminal state (merged or closed) **without this sweep run doing the work** — a daemon/champion (roleRunner/champion-on-idle, or the legacy daemon) merged its PR or closed it independently, and this sweep's "8a. Wave-boundary candidate re-verification" (or step 1's per-issue pre-flight) discovered that on re-read rather than performing the merge/build itself. It is **not** a `merged` (this sweep did not produce or land that PR), **not** a `blocked` (nothing failed), and **not** a plain `skipped` (the candidate did not fail a pre-flight condition — it simply finished elsewhere first). Keep the three axes separate in the summary: `merged`/`routed` = this sweep drove the outcome; `blocked`/`skipped`/`rate-limited` = this sweep could not or did not proceed; `completed externally` = some other actor already finished the job. A wave with several `completed externally` entries is a signal the operator may want to check whether a daemon/champion is racing the sweep (see "Modern daemon coexistence" in Coexistence, below), not a sign the sweep itself is malfunctioning.

## Session Transcript Archival (completion hook, #3726)

After the entire sweep has settled (issue list exhausted / all PRs processed) and just before printing the Summary Output, run the transcript archiver once so this session's transcript and all its subagent transcripts are captured to durable storage — or, on the daemon path (no in-session wave settle and no Summary Output printed here), immediately after the last `mcp__loom__dispatch_sweep` call returns, alongside Step 0a's registry cleanup (see "The daemon-dispatch path" above). On the daemon path this orchestrator session itself has no role subagents of its own to archive — the work happens in the daemon's detached children, not here — so this step only captures the thin orchestrator session transcript; the cron periodic sync remains the backstop for the detached children's transcripts (see the "Daemon detached-child path" caveat below).

```bash
./.loom/scripts/archive-transcripts.sh
```

This is **safe to run unconditionally** — the archiver is a **no-op unless archival is opted in** (env `LOOM_TRANSCRIPT_ARCHIVE=<dir>` or `.loom/config.json → loom.transcriptArchive.enabled`). When enabled it copies `${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects/<cwd-slug>/` (the session's own `<uuid>.jsonl` plus the sibling `<uuid>/subagents/agent-*.jsonl` + `.meta.json` sidecars) into `<dir>/<repo>/<date>/<uuid>/`, emits an `agent-<id>`-keyed `index.json` join key, and is **idempotent** (a re-run copies nothing new).

**Caveat (why the cron backstop still matters):** at this completion point the session's own top-level `<uuid>.jsonl` may still be mid-flush — the final orchestrator messages can lag. The completion hook reliably captures finished subagents; the durable tail is guaranteed only by the cron-friendly periodic sync documented in CLAUDE.md ("Session Transcript Archival"). Run both.

**Guardrails apply** (off by default; destination `0700`/files `0600`; refuses if the destination is inside a git repo but not gitignored; prints a loud banner naming the destination when enabled). See CLAUDE.md → "Session Transcript Archival" for the full contract and the secrets caveat.

> **Daemon detached-child path (v1 refinement, not a blocker).** For sweeps dispatched by the daemon as detached children, the reaper in `loom-daemon/src/sweep_registry.rs` knows the child's PID and issue but **not** its Claude session-uuid, so it cannot yet trigger a precise single-session archive on exit. v1 relies on the **periodic sync** (which copies all recent sessions for the repo regardless of uuid mapping) as the backstop for the detached path; a reaper-triggered auto-invoke keyed on the child's session-uuid is a documented follow-on.

