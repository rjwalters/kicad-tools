# `pick.decision` (Issue #10212)

One record per **role tick** (Judge, Doctor, Champion, Curator and every other
role-runner role) and per **work-finder tick**: what it looked at, in the order
it ranked it, what it acted on, and why it skipped the rest. A queue-aware estimator
needs an item's position *as the serving role sees it*; this is the only record
of that, and it cannot be reconstructed afterwards.

Role records carry the queue the role agent **actually consumed** and what it
**actually did** (#10432), captured through the per-tick pick journal described
below; the daemon's admission-gate listing is only the fallback when the agent's
queue was not observed. `candidate_source` and `decisions_observed` say which.

OTLP-only log record (`otlp: Logs`, `native: false`), envelope
`schema_version: 12` (`NEW_KIND_SCHEMA_VERSION`). The log **body** is the
record's JSON; the attributes are `loom.kind` (`pick.decision`, which the
queries below filter on), `loom.role` and, on a role record, `loom.repo`
(#11858; all already on the collector's `keep_keys`, so no collector change).
A role record of a tick that launched carries that tick's trace and span id
(its `loom.role_attempt` root span, #11858), so it joins the tick's trace. Implementation:
`telemetry/kinds/pick_decision.rs` (payload, caps, reason set),
`observability/pick_decision.rs` (builders and emitters),
`observability/pick_journal.rs` (the per-tick pick journal),
`observability/otlp/mapping/pick_decision.rs` (OTLP).

**No forge calls are added.** The work-finder record reads the
`WorkFinderTickSummary` the tick already published. A role record reads the
pick journal (the PR queue the daemon fetched at dispatch, rows the agent `gh`
front had already fetched, and the argv of writes the agent was already issuing) and the
queue listing the role's own gate probe already fetched. Nothing is emitted, and
no journal is created, when no OTLP exporter runs.

## Fields

| Field | Type | Notes |
|---|---|---|
| `schema_version` | integer | payload version, currently `2` (not the envelope gate). `2` added `candidate_source`, `decisions_observed`, the `not_selected` reason and role `acted[]` entries |
| `role` | string | `work_finder`, `judge`, `doctor`, `champion`, `curator`, ... |
| `host` | string | the deciding host |
| `tick_id` | string | role ticks: the role-execution id (`role-<role>-<instant>`), the same id as the tick's trace `loom.sweep_id` when it launched; work finder: `work_finder-<instant>` |
| `started_at` / `ended_at` | RFC 3339 | tick bounds. Present on **empty ticks too**, so cadence per host and gaps (stalls) are measurable |
| `repo` | string, optional | role records only (#11858): the repo the tick ran for, forge slug or `repo_unresolved`; present with an empty queue too, so empty-queue repos are a `loom.repo` filter. Absent on work-finder records and older daemons; additive, so `schema_version` stays `2` |
| `outcome` | string | role: the `role_tick.outcome` result (`success`, `skipped_queue_empty`, ...); work finder: `dispatched`, `halted`, `idle`, `none_dispatched` |
| `dispatch_result` | string, optional | work finder only (#11840): the completed tick's canonical `loom.dispatch.result`, the same token the tick's `loom.dispatch.tick` span carries, classified once from the tick's own report by `observability::ops::dispatch::tick_result` (first match wins): `dispatched`; a whole hold as `halted_<cause>` (one `work_finder::halt_cause` token, e.g. `halted_main_red`, `halted_drain`), `halted_ci_billing` (also a cause-less hold while billing-blocked) or `halted` (several causes, or none named); `error`; `build_backoff_held`; `no_eligible_work`; `capacity_full`; `all_skipped`. A tick skipped whole by the GitHub rate-limit breaker (#12083) is an empty record (`outcome` `skipped`, no candidates, `decisions_observed` false) with `dispatch_result` `rate_limit_held`. A partial hold falls through to the rows below it. It repeats on every chunk. `outcome` is unchanged beside it. Absent on role ticks and older daemons: absence means *unavailable*, never idle or ready (free slots alone do not establish readiness). Additive, so `schema_version` stays `2` |
| `candidates_total` | integer | candidates considered (the full count; `candidates[]` lists every one, across chunks) |
| `candidates[]` | array | in the ranker's order, **every candidate, uncapped** (#11567): `rank` (1-based), `repo` (forge slug, or `repo_unresolved`; never a local path), `number`, `stage`, `sort_key` (`name`, `value`) |
| `acted[]` | array | `repo`, `number`, `action`, uncapped. Work finder: `dispatched`. Roles: one entry per distinct observed write, from the closed set below; may name an item outside `candidates` |
| `skipped[]` | array | one `{repo, number, reason}` per skipped candidate listed in `candidates`, plus optional fields set only on work-finder rows. `detail`: a `deferred_file_overlap` row's shared `## Affected Files` paths (`file overlap: a, b`, one line, truncated at 160 chars, #9781), or a `workspace_halted` row's halt cause, only when it is a closed-vocabulary `work_finder::halt_cause` token (`main_red`, `drain`, `weight_zero`, ...; #11811), or an `already_landed` row's evidence (`forge_closed`, `forge_closed_not_planned`, `journal_landed`; #11858); no other disposition's detail (e.g. dispatch-error text) is carried. `disposition` (#11811): the row's own un-collapsed `QueueDisposition` wire name (e.g. `deferred_build_backoff` vs `recheck_interval`, both `reason = backoff`), so a reader can name which back-off or hold skipped it. `held_until` (#11811): RFC 3339 instant the row's time-boxed hold clears, as in `queue.snapshot` (#9311): set for the recheck, dispatch / open-PR back-off, cooldown and retry windows the work finder tracks. All additive (absent otherwise and on older daemons), so `schema_version` stays `2` |
| `candidate_source` | string | `ready_queue` (work finder), `serving_queue` (the role's `pr-queue` result), `listing` (the issue/PR listings the role read, e.g. Curator), `gate_listing` (fallback: the daemon's gate listing), `none` (nothing observed) |
| `decisions_observed` | boolean | `true` when the agent's writes were observed, so every candidate without an action is a real skip; `false` when only a ranking was seen, and an unexplained candidate is then in neither `acted` nor `skipped` |
| `workspace_draw` | object, optional | work finder only (#11103), absent when nothing was drawn: `seed` (decimal string), `draws_total`, optional `seed_scheme` / `seed_minute` / `seed_host` (#11683; absent when the seed was pinned: `seed` = first 8 bytes, big-endian, of `sha256(seed_scheme + ":" + seed_minute + ":" + seed_host)` for `seed_scheme` `"loom-draw/v1"`, `seed_minute` a decimal string of `floor(unix_utc_secs/60)`), and `draws[]` (every draw, in order; `seed`, `draws_total` and the three `seed_*` fields repeat on each chunk), each `{candidates[{repo, weight, base_weight, bonus}], total_weight, roll, picked, number}`. Since #11742 `weight` = `base_weight` (the repo's roster weight) + `bonus` (points for a verified red-main fix, very-important or important work), and the draw picked the repo with probability `weight / total_weight`. Records from before #11742 also carry a per-tick `temperature` and per-draw `pool` / `p_min`, with softmax weights on a 1,000,000 scale and no `base_weight` / `bonus`; readers must tolerate both, so `schema_version` stays `2`. Replaying the draws from `seed` and the logged weights reproduces the dispatch order; see [`priority-model.md`](priority-model.md). Absent on every step-engine tick, the only live dispatch path since #11909 (2026-10-14): live draws are in `item_draw.draws[]`. Records written before #12877 keep only the first 50 draws, so `len(draws) < draws_total` on a busy tick is that truncation |
| `item_draw` | object, optional | step engine only (#11896, one pool since #12185), absent elsewhere and on a tick that drew nothing: the item draws of a tick from one pool of every actionable item across the host's repos. Header (repeats on every chunk): `seed_scheme` (`loom-draw/v3`), `seed_minute`, `seed_host`, `tick_counter` (decimal strings), `draws_total`, `backlog_exponent` (α) and `busy_exponent` (β) (decimal strings that parse back to the same float), `repos[]` (one `{repo, weight, in_flight, items, pool_weight}` per repo in the starting pool, in pool order: `weight` = `w_repo`, `in_flight` = `in_flight_repo`, `items` = `n_items`, `pool_weight` = `S_r`), `closures[]` (`{repo, after_draw, steps}`: a repo the per-repo cap took out of the pool mid-tick is absent from every draw with `draw_index >= after_draw`; with `steps` (absent when empty), only that repo's rows of those steps are, e.g. a repo whose Champion merge the tick spawned loses its other `champion_merge` and `champion_hold_reevaluate` rows, #12494), `weight_overflow[]` (the repos in the pool when Σw overflowed `u64` and the tick's draws stopped), and `excluded[]`, one `{repo, reason, skip_reason, count}` per workspace and weight-0 reason (`reason` is the classifier's pass reason, e.g. `claimed`, `blocked`, `no_stage`, `draft`, or `pool_exhausted` (the step's role has no spawnable account) or `build_backoff`; `skip_reason` is its closed-set mapping, `PickSkipReason::from_exclusion`; a `refused_<reason>` row counts draws a gate or the claim refused; a `merge_in_flight` row counts the repo's merge steps held out of the pool because a Champion merge, this host's or a peer's, was in flight there, and a `deferred_merge_in_flight` row those a merge this tick spawned took out, #12494). `pools[]` (chunked in order): the pool before the first draw, repos in workspace order, each repo's items in item order, `{repo, index, number, issue, pr, step, weight, priority, age_permille, age_source}`; `index` counts across the whole pool and `weight` is the item weight `iw` = `priority × (divisor_min + min(age_minutes, cap_min))`. `draws[]` (chunked in order): `{repo, draw_index, seed, pool_size, total_weight, roll, picked_index, number, issue, pr, step, weight, draw_weight, repo_weight, repo_items, repo_pool_weight, in_flight_repo, actionable, priority, level, lane, age_permille, age_minutes, age_source}`. Draw `i` of the tick is seeded `sha256("loom-draw/v3:" + seed_minute + ":" + seed_host + ":" + tick_counter + ":" + draw_index)`, first 8 bytes big-endian (`v2` also keyed the repo). A pooled item's draw weight is `w_repo × iw × S_r^(α−1) × (1 + in_flight_repo)^(−β)`, computed in floating point over the items still in the pool, scaled so the heaviest weighs `2^40` and rounded to an integer (a positive weight never becomes 0); `draw_weight` is the drawn item's, `repo_items` and `repo_pool_weight` are its repo's `n_items` and `S_r` at that draw, and P = `draw_weight / total_weight`. Draws are without replacement. The roll is `PickRng(seed)` values with rejection of the low `2^64 mod total_weight` ones, then `% total_weight` (no modulo bias). Replay: start from `pools[]` and `repos[]`; for each draw, drop the repos (or, for a closure with `steps`, those rows of the repo) `closures[]` closed by then, weigh the pool left with the formula above, check `seed`, `total_weight`, `roll` and `draw_weight`, and select the first row whose cumulative weight exceeds the roll; that row's position is `picked_index` (`step_dispatch::item_draw::replay_record`). The tick counter is per process, so a restart within a minute reuses that minute's seeds; replay is unaffected. `age_source` is `mirror`, `events`, `created` or `none`. Additive (a `loom-draw/v2` record has no `repos[]`, exponents or `closures[]` and does not replay under v3), so `schema_version` stays `2` |
| `very_important_demoted` | array, optional | work finder only (#11509), absent when empty: one `{repo, number, holder}` per `loom:very-important` issue the per-workspace cap ranked as important this tick (`holder` keeps the slot; every one, uncapped). Additive, so `schema_version` is unchanged; see [`priority-model.md`](priority-model.md) |

## What "ranking" means per source

- **Work finder.** `candidates` are the ready-queue rows in comparator rank
  order (`work_finder::candidate_cmp`); since #11103 the order dispatch actually
  used is the workspace draw, recorded in `workspace_draw`. `stage` is `loom:issue`;
  `sort_key` is `candidate_cmp`, valued with the plan's comparator keys
  (`name=value,...`) or the rank. Each row is *acted* (`dispatched`) or *skipped*
  with a reason mapped from its `QueueDisposition`, and carries that
  disposition itself (`skipped[].disposition`, #11811).
- **Judge, Doctor, Champion** (`candidate_source = serving_queue`). The rows of
  the queue the daemon journaled when it launched the tick (#12119; the agent's
  own `pr-queue` calls are not journaled), in `pr_planning::ordered_queue` order (priority level,
  interactive preference, fallback admission and each role's tie-breaks
  applied), so a fallback-only Judge queue is recorded even though the gate
  listing was empty. Earlier snapshots are not merged in: a refresh that
  adds a priority item or drops one is ranked as the queue the role last served,
  and an item acted on from an earlier snapshot stays in `acted` (actions are
  recorded independently of the ranking). `stage` is the row's workflow
  label (`loom:review-requested`, `loom:changes-requested`, `loom:pr`) or
  `fallback`; `sort_key` is `pr_queue`, valued
  `level=<priorityLevel>,reason=<priorityReason>,origin=<origin>,mode=<mode>`
  (`priorityLevel` 0 default, 1 `loom:important`, 2 `loom:very-important`;
  named `operatorPriorityLevel`, over the retired star levels, before #11510).
- **Curator** and any role that reads its queue with `gh issue|pr list`
  (`candidate_source = listing`). The rows the agent `gh` front served, in the
  order the agent read its listings (Curator's priority queries run in priority
  order), in the order the agent's own `--jq` printed them (the expression runs over the
  whole array, so `sort_by` and `.[0]` count). A printed value must name its row
  (an object, a bare number, or a string led by `#<number>`); otherwise the
  listing is left unobserved rather than guessed. `stage` is the listed label; `sort_key` is
  `listing_order`. A `--json` listing the front cannot serve itself (one asking
  for `labels`, or a `--search` query) passes through to `gh`; inside a role tick
  it runs captured and its printed rows are journaled the same way (#11858;
  `stage` is the `--label` value(s), else `search`). A pass-through listing with
  `--jq`, `--template` or `--web` stays unobserved.
- **Fallback** (`candidate_source = gate_listing`): no journal was read (the
  agent's runtime does not inherit it, or the queue was never read). The gate's
  listing in **forge listing order**, which can differ from the serving order.
  A Judge/Doctor tick whose queue was empty was never spawned and carries
  `outcome = skipped_queue_empty`.

### The pick journal

When an OTLP exporter runs, the role runner gives each launched role agent a
private journal file (`$TMPDIR/loom-pick-journal/<role>-<uuid>.jsonl`, `0700`
directory, path in `LOOM_PICK_JOURNAL`). Three writers in the agent's process
tree append JSON lines; outside a role tick the variable is unset and all three
are no-ops:

| Writer | Line | What it records |
|---|---|---|
| the daemon, at Judge/Doctor/Champion dispatch | `queue` | the ordered rows `pr-queue` would print, with labels and sort key; whether the agent's `gh` resolves to the agent `gh` front |
| agent `gh` front, served `issue\|pr list` | `listing` | the rows served, after the caller's `--jq` |
| agent `gh` front, every call | `act` | the forge writes in the argv: `issue\|pr edit --add-label`, `pr merge`, `pr review --approve\|--request-changes`, `gh api` label POSTs and merge PUTs |

At the end of the tick the daemon reads and deletes the file; a leftover from a
crashed tick is pruned after a day. Acts are writes the agent *issued*; the
record does not confirm the forge accepted them.

A `queue` line keeps at most 200 rows but also records the queue's uncapped
size, which becomes `candidates_total`. If the file is missing or unreadable
(a tick whose launch journaled no queue and whose agent never used the `gh` front writes none) nothing was
observed: `decisions_observed` is `false` and unexplained candidates stay
undecided. A file that exists but is empty is an observed tick with no acts.

A `queue` line keeps at most 200 rows but also records the queue's uncapped
size, which becomes `candidates_total`. If the file is missing or unreadable
(a tick whose launch journaled no queue and whose agent never used the `gh` front writes none) nothing was
observed: `decisions_observed` is `false` and unexplained candidates stay
undecided. A file that exists but is empty is an observed tick with no acts.

**Role actions (closed set).** `claimed` (`loom:reviewing`, `loom:treating`,
`loom:curating`, `loom:evaluating`, `loom:building`), `approved` (`loom:pr`, or
`pr review --approve`), `changes_requested`, `merged`, `curated`, `promoted`
(`loom:issue`), `blocked`, `escalated` (`loom:operator`, `loom:operator-only`,
`loom:operator-decision`), `labeled` (any other `loom:*` label).

**Role skip reasons.** With `decisions_observed`, a candidate the agent did not
write to is skipped with the first that applies: `operator_hold` (a hold label:
the role's park labels, `loom:operator`, `loom:operator-only`,
`loom:operator-decision`, `loom:operator-mechanical`), `blocked`
(`loom:blocked`, `loom:ci-failure`), `overlap_chain` (`loom:sequenced`),
`in_flight` (a claim label held by someone else), else `not_selected`. Without
it, only a hold label names a reason; other candidates are undecided.

## Skip reasons (closed set)

`skipped[].reason` is one of these, and nothing else:

| Reason | Meaning | Work-finder dispositions mapped to it |
|---|---|---|
| `overlap_chain` | overlaps an in-flight chain of stacked work, or (work finder) its `## Affected Files` overlap same-repo work admitted earlier this tick or already in flight | `deferred_file_overlap` (#9781; `detail` names the shared paths); roles: the candidate carries `loom:sequenced` |
| `pr_open_skip` | the item already has an open PR | `open_pr`, `open_pr_backoff` |
| `operator_hold` | held for a human | `parked`, `hard_exclusion`, `declined`; roles: a merge-hold / park label on the PR |
| `quota` | token pool or quota would not admit it | roles: `skipped_no_token_pool`, `skipped_pool_exhausted` ticks |
| `cap` | the concurrency cap is full | `deferred_capacity` (`deferred_ramp_cap`, `deferred_saturation` and `deferred_repo_cap` mapped here until #12406 removed them; `deferred_out_of_slice` until the dispatch slice was retired, #11682) |
| `in_flight` | a live sweep or claim covers it | `in_flight`; roles: a claim label the agent did not write |
| `backoff` | inside a back-off, cooldown, recheck or retry window | `deferred_build_backoff`, `recheck_interval`, `dispatch_backoff`, `noop_cooldown`, `prless_retry` |
| `halted` | the repo's dispatch is halted (red main, gate, drain, ...) | `workspace_halted` |
| `host_constraint` | not for this host | `host_constraint`, `peer_claim` |
| `blocked` | blocked by something specific to the item | `workspace_commands_missing`, `quarantined`, `labelled_blocked`, `unknown`; roles: `loom:blocked`, `loom:ci-failure` |
| `error` | the dispatch attempt failed | `dispatch_error` |
| `tick_skipped` | the role tick did not run for another reason | roles: runtime rejected, model/runtime mismatch, load, queue gate |
| `not_selected` | the role ran, its writes were observed, and it did not act on this candidate | roles only, with `decisions_observed` |
| `already_landed` | the item's work already landed: the issue is closed, or this host recorded a `landed` sweep for it in the last 30 minutes (#11429) | `already_landed` |
| `already_closed` | the issue is closed as not planned (#11429) | `already_closed` |
| `no_step` | the item's state calls for no step: an epic, an unpromoted proposal, an unlabelled draft PR | step engine only (#11896): `item_draw.excluded[].skip_reason` for `epic`, `proposal`, `no_stage`, `draft` |

`QueueDisposition::Dispatched` is the one disposition with no reason: it is
*acted*. The mapping is a total match in
`PickSkipReason::from_disposition`, so a new disposition must be classified
there. The step engine's weight-0 exclusions map through the parallel total match
`PickSkipReason::from_exclusion`: `pool_exhausted` → `quota`, `build_backoff`
→ `backoff`, `ci_pending` (a Judge step whose PR head has CI running, #12042)
→ `backoff`, `frozen` / `operator` / `hard_exclusion` → `operator_hold`,
`claimed` / `queued` → `in_flight`, `shared_pr` → `pr_open_skip`, `sequenced`
→ `overlap_chain`, `blocked` → `blocked`, `capability_missing` →
`host_constraint`, `epic` / `proposal` / `no_stage` / `draft` → `no_step`.

## Rank of an item in each role's latest candidate list

For a record with `candidate_source = serving_queue` this is the item's rank in
the role's serving-queue order; for `listing`, in the order the role read it;
for `gate_listing`, only the forge listing order (select
`JSONExtractString(body, 'candidate_source')` to tell them apart).

Run against `signoz_logs.distributed_logs_v2` (ClickHouse). Substitute the repo,
number and instant. `rank = 0` means the item was **not** in that role's latest
list at that instant. `candidates_total` says how many it had; the list is no longer
truncated (#11567), so an item the role ranked is always listed. The query below
reads a single log row, so it is only correct when that record is unsplit
(`chunk_count = 1`): a chunked tick exports one row per chunk, all with the same
timestamp, so `LIMIT 1 BY role, host` picks one chunk arbitrarily and
`arrayFirstIndex` then returns the candidate's position *inside that chunk*, not
its rank. For a chunked record, take every chunk of the newest `tick_id`, order
them by `chunk_index`, concatenate their `candidates[]` lists, and read the
candidate's own `rank` field instead of an array position.

```sql
SELECT
  attributes_string['loom.role']                    AS role,
  JSONExtractString(body, 'host')                   AS host,
  fromUnixTimestamp64Nano(timestamp)                AS tick_at,
  JSONExtractUInt(body, 'candidates_total')         AS candidates_total,
  JSONExtractString(body, 'candidate_source')       AS source,
  arrayFirstIndex(
    c -> JSONExtractString(c, 'repo') = 'rjwalters/loom'
     AND JSONExtractUInt(c, 'number') = 10212,
    JSONExtractArrayRaw(body, 'candidates'))        AS rank
FROM signoz_logs.distributed_logs_v2
WHERE attributes_string['loom.kind'] = 'pick.decision'
  AND timestamp <= toUnixTimestamp64Nano(toDateTime64('2026-10-04 12:00:00', 9))
  AND timestamp >= toUnixTimestamp64Nano(toDateTime64('2026-10-04 12:00:00', 9) - INTERVAL 1 DAY)
ORDER BY timestamp DESC
LIMIT 1 BY role, host
```

(`LIMIT 1 BY role, host` keeps each role's newest record per host at or before
the instant.) This query is **not yet run against a live SigNoz or the pinned
ClickHouse** (no engine-level harness covers it).

Service cadence and stalls per host: `max(tick_at) - lag(tick_at)` over the same
filter grouped by `role, host`, restricted to `chunk_index = 0` so the chunks of
one tick are not counted as multiple ticks.

## Record volume

> **Step dispatch (#11894, live since #11909).** Item roles have left the fixed role-runner cadences, so this table no longer holds for Curator, Judge and Doctor. Work-finder records carry `item_draw` (the step, the weight terms, `age_source`, and `weight_overflow`), and each work-finder row's `stage` names its step. See [`docs/design/work-finder-step-dispatch.md`](https://github.com/rjwalters/loom/blob/main/docs/design/work-finder-step-dispatch.md) §12.

Derived from the shipped default cadences, **not measured on a live fleet**:

| Source | Default cadence | Records/day per workspace root |
|---|---|---|
| work finder | 60 s per tick, one record per tick (all roots) | 1,440 per host |
| judge, doctor, curator | 300 s | up to 288 each |
| champion | 600 s | up to 144 |
| auditor, hermit | 600 s | up to 144 each |
| guide | 900 s | up to 96 |
| architect | 3600 s | up to 24 |

The cadences above are the defaults with `autonomous.roleRunner.eventTriggers.enabled` off
and demand-weighted width at its defaults. Turning event triggers on lowers curator, auditor
and guide record volume (a quiet role launches only on a new input, or at `maxQuietSecs`),
and `demandWidth` can run several judge, doctor or champion lanes at once, which raises it.
The figures below do not model either.

A role record is only written for a tick that was admitted and run; a root held
back at admission writes none. With 1 root and the default roles, that is about
1,440 + 288*3 + 144*3 + 96 + 24 = roughly 2.9 k rows/day per host; each extra
root adds roughly 1.5 k. Lists are not capped (#11567): an empty tick is under 400 bytes of JSON, and a
work-finder tick with ~575 candidates and ~350 draws is several hundred KB.
At 1,440 work-finder ticks/day/host that is on the order of 0.5 GB/day/host
in the worst case (typical ticks with a handful of candidates stay around
1 KB). A record whose JSON exceeds `CHUNK_BYTES` (1,000,000) is split into
chunks, so each chunk is bounded
(`a_busy_tick_splits_into_bounded_chunks_and_reassembles_exactly` pins that);
the compact `[repo_idx, number, reason_code]` encoding is the fallback
if volume proves unacceptable.

**Chunking.** Every record carries `chunk_index` (0-based) and `chunk_count`
(`chunk_index` 0 and `chunk_count` 1 when unsplit; additive, `schema_version`
stays `2`). Chunks of one
tick share `tick_id` and repeat the header (`role`, `host`, timestamps,
`outcome`, `dispatch_result`, `candidates_total`, `candidate_source`, `decisions_observed`,
`workspace_draw.seed` / `draws_total` / `seed_scheme` /
`seed_minute` / `seed_host`); the list entries are packed in order, so
concatenating the chunks' lists in `chunk_index` order reproduces the original.
A candidate's `skipped` / `acted` entry may land in a different chunk than its
`candidates` entry, so reassemble before reading per-candidate verdicts. Role
ticks remain bounded upstream by the pick journal's 200-row cap
(`MAX_ROWS_PER_ENTRY`), unchanged here.

The pick journal adds no rows: it is a local file of a few KB per launched role
tick (each line is capped at 200 rows and the file at 1 MiB), deleted when the
tick's record is built.
