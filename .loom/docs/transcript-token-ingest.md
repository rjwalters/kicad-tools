# Transcript Token Ingestion

How `~/.loom/activity.db`'s token/cost tables get populated on a host whose work
arrives by `dispatch_sweep` (Issue #8059, part of #8052).

## The gap this closes

`resource_usage` had exactly one live writer — the IPC `GetTerminalOutput`
handler, which scrapes a **managed terminal's** scrollback. A dispatched sweep
(`claude -p` via `spawn-worker.sh`) issues no `SendInput`/`GetTerminalOutput`
round trips at all, so on every dispatch-driven host that table, the sibling
`token_usage` table, and all six `cost_by_*` views over them were not "empty
until correlation catches up" — they were **structurally empty forever**.
`loom-daemon stats` had no cost to report, and #8052's fleet-cost question ("what
is the fleet spending, by role x model x day?") could only be answered with a
hand-written scraper.

The data was on disk the whole time, in each sweep's own Claude Code transcripts
(`${CLAUDE_CONFIG_DIR:-~/.claude}/projects/<cwd-slug>/…`). This reads them.

## Running it

```bash
loom-daemon ingest-transcripts                     # every transcript, every project
loom-daemon ingest-transcripts --since 24h         # only recently-modified ones
loom-daemon ingest-transcripts --dry-run           # report, write nothing
loom-daemon ingest-transcripts --format json       # machine-readable summary
loom-daemon ingest-transcripts --workspace ~/GitHub/loom   # one repo's transcripts
```

`--since` takes `7d` / `12h` / `90m`, an RFC-3339 instant, or `all`.
`--projects-dir` and `--db` override the transcript and database locations
(defaults: `${CLAUDE_CONFIG_DIR:-~/.claude}/projects` and `~/.loom/activity.db`).

**Re-running is safe by construction.** Every ingested transcript is recorded in
a `transcript_ingest` ledger with its size and mtime: an unchanged file is
skipped, and a file that has grown (a sweep still running) is re-read in full
with its previous rows **replaced**, never appended to. A repeated pass can
therefore neither double-count nor miss a live session's tail. `--force`
re-reads even unchanged files; it still replaces rather than appends.

### In the daemon (opt-in, default off)

| Variable | Default | Meaning |
|---|---|---|
| `LOOM_TRANSCRIPT_INGEST` | unset (off) | `1`/`true`/`yes`/`on` starts the periodic pass |
| `LOOM_TRANSCRIPT_INGEST_INTERVAL` | `900` | Seconds between passes |
| `LOOM_TRANSCRIPT_INGEST_WINDOW_HOURS` | `24` | How far back each pass looks; `0` = full history |

Default-off follows the daemon's FLAGS-OFF convention: ingestion writes to a
database the IPC path also writes to, so a host opts in deliberately.

## What a row means

One `resource_usage` row per **(model, UTC day) per transcript** — fine enough
for `cost_by_day`/`cost_by_month` to be accurate across a session that spans
midnight, coarse enough that a 28-day backfill is thousands of rows rather than
millions.

Role, repo, session id and issue reach the row the way `cost_by_role` already
expects: through `input_id -> agent_inputs`. Ingestion writes **one
`agent_inputs` anchor row per transcript** (`terminal_id =
"transcript:<session-uuid>[/agent-<id>]"`, `input_type = system`, `agent_role` =
the attributed role, `context` = workspace/repo/branch/issue JSON) and hangs that
transcript's usage rows off it. Consequence worth knowing: `agent_inputs` is what
`stats` counts as "prompts", so an ingesting host counts one extra "prompt" per
ingested transcript.

### Method (the #8052 method notes, honoured)

- **Dedupe on `message.id`.** A streamed assistant message is written once per
  chunk, and every chunk repeats the id carrying the **cumulative** usage.
  Summing blocks over-counts: measured on a live fleet host (2026-09-18, a 24h
  window over 2,330 ingested transcripts) 62,643 non-synthetic usage blocks
  collapsed to 30,579 distinct messages — **51% of blocks were repeats**, so a
  naive sum would have roughly doubled the fleet's reported spend. Folding by id and
  taking the per-counter maximum is correct for identical repeats and for
  genuinely growing cumulative chunks.
- **Skip `model == "<synthetic>"`** — Claude Code's marker for internal/tool-echo
  messages, not billable consumption.
- **Role attribution by the first user message** (method note (a)): a
  `<command-name>/loom:NAME</command-name>` marker when present (how a
  `/loom:sweep` parent session and every role-runner session start), otherwise
  the earliest whole-word mention of a known role, which is how a subagent's
  dispatch prompt names itself ("Load and follow … `doctor.md`", "You are the
  Loom Builder…"). No match leaves the role NULL, which groups as `unknown`
  rather than guessing.
- **Issue attribution only from the slash command's own first argument.** A
  number in prose ("PR #7759, which closes #7726") is deliberately ignored — a
  wrong attribution is worse than an absent one.
- **Cost** uses the existing pricing table in `activity::resource_usage`
  (cache reads at 0.1x input, cache writes at 1.25x). Treat `cost_usd` as a
  **list-price proxy for limit weight**, not a bill — #8060 reports that table
  as one to two generations stale for several models.

### `resource_usage`, not `token_usage`

#8059 allowed either table. Rows go to **`resource_usage` only**: it is the table
every cost analytics view and the `agent_effectiveness` / `cost_per_issue` /
`daily_velocity` stats views already read, whereas `token_usage` has never had a
writer and carries no column that matters here. Writing both would double-book
the same tokens in one database and hand every future reporter the job of knowing
which table not to sum.

## Verifying

```bash
loom-daemon ingest-transcripts --since 24h
sqlite3 ~/.loom/activity.db 'SELECT COUNT(*) FROM resource_usage;'
sqlite3 ~/.loom/activity.db 'SELECT agent_role, request_count, ROUND(total_cost,2) FROM cost_by_role ORDER BY total_cost DESC;'
sqlite3 ~/.loom/activity.db 'SELECT * FROM cost_by_month;'
```
