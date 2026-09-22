# Native-Runtime Usage Attribution

How a sweep or role tick that ran on a **non-Claude** runtime gets a model badge
and per-model token numbers on the fleet feed (Issue #8507).

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

## Adding another runtime

`UsageSource` in `loom-daemon/src/usage_source.rs` is the one place that maps a
runtime to a store. Pi and Codex each have their own usage source and are not
wired up: they fall through to the Claude reader, which finds nothing, so they
get labels but no numbers. Adding one means a new `UsageSource` variant, a
reader module beside `opencode_usage.rs`, and an arm in `sweep_tokens_by_model`
— no change at any of the three call sites.
