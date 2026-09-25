# Native-Runtime Usage Attribution

How a sweep or role tick that ran on a **non-Claude** runtime gets a model badge
and per-model token numbers on the fleet feed (Issue #8507, extended for Kimi
Code CLI by Issue #8564).

## The gap this closes

Per-model token attribution used to be Claude-Code-transcript-only end to end.
`transcript_tokens.rs` sums `${CLAUDE_CONFIG_DIR:-~/.claude}/projects/<cwd-slug>/*.jsonl`
and nothing else writes that file, so work dispatched on OpenCode produced **no**
`tokens_by_model` at all — and therefore **no badge**, not a wrong one. The
2026-09-21 GLM-5.3 trial merged 46 PRs whose completions carried nothing
identifying the model, the provider, or even the runtime.

Two independent fixes, because they fail independently:

1. **Labels.** `runtime` / `provider` / `profile` are now first-class fields on
   `sweep.outcome`, `role_tick.outcome` and the `completion-v1` meta, read off
   the launch's own `# LOOM_LAUNCH` record. They carry no token counts, so a
   completion is identifiable **even when no usage numbers exist anywhere**.
2. **Numbers.** The per-model token lookup is dispatched on that `runtime`
   through `loom-daemon/src/usage_source.rs`, so an OpenCode launch reads
   OpenCode's own session store instead of transcripts it never wrote.

## Where each value comes from

| Field | Source | Absent when |
|---|---|---|
| `runtime` / `provider` / `profile` | the `# LOOM_LAUNCH {…}` line `worker_spawn::run` writes into the launch's own log | the spawn wrote no launch record — i.e. Claude and the legacy script adapters |
| `tokens_by_model` (Claude) | `~/.claude/projects/<slug>/*.jsonl` | unchanged from before |
| `tokens_by_model` (OpenCode) | `~/.loom/opt/opencode-<ver>/xdg/data/opencode/opencode.db`, table `session` | no matching session in the directory set + window |
| `tokens_by_model` (Kimi) | `$KIMI_CODE_HOME/session_index.jsonl` → each session's `agents/<agentId>/wire.jsonl`, records `usage.record` + `llm.request` | no matching session in the directory set + window, or every matched record was counter-free |

**Never a fabricated default.** An unattributed launch omits all three labels
rather than guessing `"claude"`, and a source with nothing to report returns
"unknown", never a zero. That is what keeps every pre-#8507 Claude payload
byte-identical: no launch record ⇒ the three keys are simply absent.

## Reading the OpenCode session store

`loom-daemon/src/opencode_usage.rs` is the reader. Its whole SQL surface is one
constant naming `session` alone — the database also holds `credential` and
`account` tables, which this reader must never touch. That is enforced by two
tests, one asserting the query constant's text and one scanning the module's own
source for a second `.prepare(`, plus a fixture database that plants a secret in
`credential` and asserts it never appears in any returned row. The database is
opened `?mode=ro` with `SQLITE_OPEN_READ_ONLY`, so a live OpenCode process's
store cannot be mutated.

Schema, verified live against `opencode 1.18.31` on 2026-09-22: `model` is JSON
(`{"id":"zai-org/GLM-5.3","providerID":"friendli","variant":"default"}`), the
five `tokens_*` columns are `integer NOT NULL DEFAULT 0` session totals, and
`time_created` is **epoch milliseconds**. A schema change degrades to "no data",
never a panic.

### Which sessions count as yours

Sessions are attributed by `session.directory` (exact match against a
caller-supplied set) plus the caller's wall-clock window:

- **A sweep**: the workspace root *and* that issue's own worktree — a sibling
  issue's worktree is excluded, so two concurrent sweeps in one workspace never
  fold each other in (the window alone cannot separate them).
- **A role tick**: the workspace root alone; a tick never gets a worktree.

A session row whose five counters are all zero is skipped: OpenCode writes the
row at creation, so a launch that produced no turns would otherwise publish a
zero-token badge for a model that was never called.

The exact `sessionID`s a launch used are carried on its native JSON event
stream and would be a more precise key than directory+window. That refinement is
follow-up work; directory+window attributes correctly for every launch shape the
fleet runs today (one runtime process per directory at a time).

## Reading the Kimi session store (Issue #8564)

`loom-daemon/src/kimi_usage.rs` is the reader — the third implementation behind
the same seam, after the Claude transcripts and the OpenCode database.

**Where the numbers are.** Established on 2026-09-22 by reading the shipped
`@moonshot-ai/kimi-code@2.0.2` bundle (`npm pack`, `dist/main.mjs`) — the same
pinned CLI `defaults/runtimes/kimi.json` targets and the same credential-free
method `docs/experiments/kimi-harness-probe-2026-09-22.json` used:

| Candidate | Carries token counters? | Evidence in the bundle |
|---|---|---|
| `--output-format stream-json` stdout | **No** | `PromptJsonWriter` emits only `{"role":"assistant"…}`, `{"role":"tool"…}` and `{"role":"meta","type":…}`. It *does* emit `{"role":"meta","type":"session.resume_hint","session_id":…}` — the exact session id, and the only usage-adjacent thing on stdout |
| `<sessionDir>/state.json` | **No** | `normalizeSessionMeta` returns `id`/`version`/`cwd`/`title`/`titleKind`/`createdAt`/`updatedAt`/`archived` only |
| `<sessionDir>/agents/<agentId>/wire.jsonl` | **Yes** | `usage.record` (`usageRecordSchema = {agentId, model, usage, usageScope?}`) + `llm.request` (`llmRequestSchema = {agentId, kind, provider, model, modelAlias?, …}`), each flattened by `Event2.serialize()` into `{"type":…, …payload, "time":<epoch ms>}` |

`usage` is exactly the four integers `emptyUsage()` defines —
`{inputOther, output, inputCacheRead, inputCacheCreation}` — and the bundle's
own `inputTotal()` is `inputOther + inputCacheRead + inputCacheCreation`, so
`inputOther` maps to `input`, `inputCacheRead` to `cache_read` and `output` to
`output`. There is no separate reasoning counter: the provider folds reasoning
into `output`. `usage.record.model` is the **model alias**; `llm.request` in the
same log resolves that alias to the provider's own model id and provider name.
An alias that no `llm.request` resolves keeps the alias verbatim — never a
synthesised model name.

`usageScope` (`"turn"` / `"session"`) is a **partition**, not a duplicate: the
CLI's own reader adds every record into `byModel` regardless of scope, so
summing all of them is the correct total, not a double count.

`inputCacheCreation` is attributed to the **5-minute** cache-write bucket, not
the 1-hour one the Claude/OpenCode readers default to — Moonshot's published
billing note is "If no TTL is specified, the 5min tier applies by default", and
the CLI sends no TTL.

**Which sessions count as yours.** `$KIMI_CODE_HOME/session_index.jsonl` (an
append-only `{sessionId, sessionDir, workDir}` index, with `{sessionId,
deleted:true}` tombstones) carries an **absolute** `sessionDir`, so the reader
never reproduces Kimi's directory-slug scheme. Two filters, in the order #8564
specifies: an **exact session id** when a caller captured one from the launch's
`session.resume_hint` line, otherwise the launch's **working directories**
(exact `workDir` match, the same key the OpenCode reader uses) plus the window.
The window filters each `usage.record`'s own `time`, not the session's creation
time — one long-lived Kimi session can span several launches. Data roots are
resolved `LOOM_KIMI_CODE_HOME` → `KIMI_CODE_HOME` → `~/.kimi-code`, mirroring
the CLI's own `defaultHomeDir`.

**`wire.jsonl` is the whole conversation.** It holds user prompts, tool output,
and (when it differs from the bound profile) the system prompt. The reader
decodes exactly two `type`s and, from them, only the model/provider strings and
the four integers; a test plants secrets in the surrounding records and asserts
none of them can come out.

**Pricing is an estimate, deliberately.** The Moonshot K2/K3 rows added to
`defaults/pricing.json` and `resource_usage.rs` are the platform **API list
price** (verified against <https://platform.moonshot.ai/docs/pricing/chat>,
2026-09-22). A Kimi Code subscription is not billed per token, so for a
subscription launch the derived `cost_usd` is "what these tokens would have cost
on the API", never money that changed hands — the same "harness report ≠ billed
cost" honesty `runtime-model-trials.md` applies to the OpenCode/GLM trial. An
unrecognised Kimi id now logs a Moonshot-specific warning naming that page,
instead of being silently priced at the Anthropic Sonnet default.

**Fixture-verified, not live-verified.** The reader's tests are built from the
bundle's own serializers, not from a captured session: no Kimi credential exists
in the environment it was written in (the same gap
`docs/experiments/kimi-harness-probe-2026-09-22.json` records for #8561's
canary, tracked in #8606). The *shape* is verified; a live receipt — a real Kimi
sweep whose completion carries `runtime: kimi` plus provider and model — is
still owed.

## Backfill and verification: `loom-daemon opencode-usage`

The same reader, pointed at any directory and window — for reconciling
completions that were narrated before this shipped, and for checking what a
sweep's `tokens_by_model` will carry.

```bash
# Per-model totals for this repo over the last 30 days (the default window).
loom-daemon opencode-usage

# One workspace root plus one issue's worktree — the exact set a sweep uses.
loom-daemon opencode-usage --directory ~/GitHub/loom --issue 8507

# The 2026-09-20/21 trial window, with every contributing session listed.
loom-daemon opencode-usage --directory ~/GitHub/loom \
  --since 2026-09-20 --until 2026-09-22T00:00:00Z --sessions

# Everything the store still holds, machine-readable.
loom-daemon opencode-usage --directory ~/GitHub/loom --all-time --json
```

`--json` emits the databases scanned, the directory set, the window, the folded
`tokens_by_model` rows, and one object per contributing session (including its
`providerID`, which `ModelUsageTotals` has no field for). Nothing is written.

**Retention is not guaranteed.** The trial-window numbers exist only as long as
the hosts' session databases do — capture them before relying on them.

## This reader does not feed fleet telemetry directly

It feeds it *indirectly*: the folded `tokens_by_model` rides `sweep.outcome` /
`role_tick.outcome`, whose `loom.tokens_by_model` / `loom.models_used` /
`loom.runtime` / `loom.provider` / `loom.model` attributes are already
allowlisted by the gateway collector. So everything the **daemon dispatches** on
OpenCode is queryable by runtime, model and duration today.

There is deliberately no periodic job reading the session store straight onto
the OTLP wire, which would be the only way to observe an **interactive**,
human-launched `opencode` session (it writes a different store,
`$XDG_DATA_HOME/opencode/opencode.db`, which `discover_opencode_dbs` does not
scan). Issue #8670 decided that gap stays open for now, and
[`defaults/observability/collector/README.md`](https://github.com/rjwalters/loom/blob/main/defaults/observability/collector/README.md)
§"Known gap: OpenCode interactive sessions" records the evidence, the extract
design to use if it is ever built (watermark on `session.time_updated`, dedup on
`session.id` — the rows are mutable running totals, not append-only events), and
the two triggers that should re-open it.

## Adding another runtime

`UsageSource` in `loom-daemon/src/usage_source.rs` is the one place that maps a
runtime to a store. Pi and Codex each have their own usage source and are not
wired up: they fall through to the Claude reader, which finds nothing, so they
get labels but no numbers. Adding one means a new `UsageSource` variant, a
reader module beside `opencode_usage.rs` / `kimi_usage.rs`, and an arm in
`sweep_tokens_by_model` (plus `role_tick_tokens_by_model`) — no change at any of
the three call sites.

**Branch on `UsageSource::is_native_store()`, never on one variant.** A caller
that derives *more* than tokens from a Claude transcript — the role-tick
scanner's forge-mutating `actions` — has to record that extra signal as
**unmeasured** for any native store. Written as `== OpenCodeSessionDb` that test
silently routed the next runtime (Kimi) back onto the Claude reader and
published an unmeasured tick as a genuine `{issues_labeled: 0, …}`; the
predicate form cannot.
