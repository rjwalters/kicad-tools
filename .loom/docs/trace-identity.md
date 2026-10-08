# Trace identity and provenance policy

Loom traces are forensic records. Two rules apply to every span Loom emits:

1. **Deterministic IDs.** No production trace or span ID is random. Each ID is
   derived from the natural key of the work it records, so anyone holding that
   key can recompute the ID and find the trace.
2. **Provenance on every span.** Every span names the Loom build that created
   it, by version **and full git SHA**. Each sweep span also names the
   installed Loom surface and the exact prompt files the run used.

## 1. Deterministic IDs

Every derived ID is a prefix of SHA-256 over NUL-separated parts
(`derived_hex`). The first part is a tag naming the kind of ID:
`loom.<kind>.trace` for a trace, `loom.<kind>.root` for its root span, and
`loom.span.child` for a child span (`TraceContext::derived` /
`derived_child` in `loom-daemon/src/telemetry/trace/context.rs`). Instants
inside a key use RFC 3339 UTC with nanosecond precision.

| Trace | Trace ID is derived from |
|-------|--------------------------|
| Issue story (`loom.story.trace`) | the repo's GitHub numeric `repo_id` and the issue number: `loom-story/v1:github:<repo_id>:<issue>` (key version D32 v1, #9068) — stable across repo renames and transfers; see [`tracing.md`](tracing.md) |
| Sweep outside a story (`loom.execution.trace`) | lowercased repo key (`$LOOM_REPO`, else the checkout's GitHub `owner/repo`, else the workspace basename), sweep id — used for PR-set sweeps and checkouts with no GitHub origin. The span's `loom.repo` is that repo spelled as GitHub spells it (#10637); the key is its ASCII lowercase |
| Role-runner invocation (`loom.execution.trace`) | the same repo key, and the execution id `role-<role>-<start instant>` — carried as `loom.repo` (the key is its ASCII lowercase) and `loom.sweep_id` on the `loom.role_attempt` root. The role is in the key so two roles starting in the same instant differ; the repo key (not a host id) scopes it, because the span carries the repo but no host attribute, and one repo's role runner ticks each role serially |
| Dispatch tick (`loom.dispatch.tick`) | tick start instant |
| Pool hold (`loom.pool.hold`) | pool identity (`loom.pool.hold.pool`, a hash of the pool directory) and hold start instant (`since`) — every hold armed in one work-finder tick shares `since`, so the pool is what tells them apart |
| Rate-limit trip (`loom.ratelimit.trip`) | the tripping job (`loom.ratelimit.source`) and the trip instant (#10022) |
| Reader withdrawal (`forge.reader.withdrawn`) | the reader App (`forge.reader.app`), the owner and resource withdrawn (`forge.reader.owner`, `forge.reader.resource`) and the withdrawal instant (W4-A) |
| Read-pool spill-latch transition (`forge.reader.spill`) | the repo (`forge.spill.owner_repo`), resource, home reader, mode and the transition instant (W4-B) |
| `gh` invocation with no parent (`loom.github.invoke`, span `invoke github`) | `github.operation`, start instant, and `github.invocation` (`<pid>.<seq>`, so two calls in one clock tick differ); with a parent it is that span's child, keyed by the same facts (#9985) |
| CI run/job (`loom.ci.*`) | repo, run id, attempt (job: job id) — [`ci-observability.md`](ci-observability.md) |

Every sweep of an issue is a `loom.sweep` span in that issue's story trace.
Its span ID is derived from the story root and the sweep id
(`sweep-issue-42-1790000000`). A role-runner tick that wrote to an issue or PR
adds a `loom.role_attempt` span to that story (#9168) whose ID is derived from
the story root, `loom.role_tick`, the tick's execution id (`loom.sweep_id`)
and the target (`pr:<loom.pr_number>`, else `issue:<loom.issue>`).
The story's `story.*` phase spans are the 2am reconciler's, with D32 IDs keyed
by a GitHub timeline event (see [`tracing.md`](tracing.md)); Loom mints none.

**`loom.attempt`** has one meaning everywhere: the 1-indexed ordinal of a span
among the same-named spans (and the same `loom.role`, when present) under the
same parent, ordered by when each span **opened** — not by its start — with
ties broken by the opening event, PR number, then `source_event_id`. Round 1
of `story.review_wait` opens at PR-ready and later rounds at their `labeled
loom:review-requested`; every other kind opens at its opening event. The two
differ only for a `basis=ci` `story.review_wait`, whose start moves to CI's
conclusion when the round closes. Loom's `loom.role_attempt` / `loom.phase`
value — the Nth started attempt of a role within a sweep
(`Journal::start_linked`) — is the special case whose parent is the sweep;
there start and opening coincide. The 2am reconciler sets it on the repeating
story kinds (`story.queue_dwell`, `story.review_wait`, `story.rework`,
`story.reopened`, `story.operator_hold`) under the story root. Every opened
sibling consumes an ordinal: a still-open one, and also a round or rework
abandoned when its PR closes without closing it (closed as a duplicate or
superseded mid-review, or human-merged with no verdict). So ordinals can have
gaps, are never reissued, and are never renumbered (a span aged out of
retention also leaves a gap). Source of truth: 2AMLogic/2am
`infra/signoz/docs/story-trace.md` §"Review rounds, rework and operator holds
(v1)" and the D32 amendment of 2026-09-28 in `infra/ops/decisions.md`.

A child span's ID is derived from its trace ID, its parent span ID, its span
name, its `loom.role` (if any), its `loom.tool.name` (if any), and its start
instant. Every one of those inputs
is carried on the exported span, so each ID can be checked against its own data.
Dispatch admissions also key on `loom.issue`.

Consequences:

- Every sweep of an issue, on any host, lands in the **same** trace: the
  issue's story. Each retry is a distinct sibling span because the sweep id
  carries the dispatch time. The sweep id is in the registry, the lease
  comment, logs and the `loom.sweep_id` attribute.
- Re-emitting the same work (such as a replay, a restart re-report, or a second
  host) produces the same IDs, so a backend can deduplicate on them.
- The sweep span carries its derivation inputs: `loom.repo`, `loom.issue` and
  `loom.story_id` in a story, or `loom.repo` and `loom.sweep_id` outside one.
  `loom.repo` is GitHub's own `owner/name` spelling, the value
  `loom.dispatch.*` carries; a repo key is its ASCII lowercase (#10637).

**Attended runs (#10116).** A Loom role run as a subagent of an attended
Claude Code session has no dispatch and so no sweep id or sweep span. Its
live output (`session.output`) is grouped under `loom.sweep_id =
attended-<first 8 chars of the session id>-<agent id>`, derived from the
transcript's own identity, so a restarted
tailer reports the same attempt. Every record carries
`loom.session.output.launch = attended` (a daemon run says `daemon`); see
[`telemetry-schema.md`](telemetry-schema.md) → `session.output`. The attended
path mints no trace or span.

**Enforcement:** the random constructors `TraceContext::root()` and
`TraceContext::child()` only compile under `cfg(test)`, so production code that
tries to mint a random ID fails to build. A new root trace must choose a
natural key and call `TraceContext::derived`. If a derivation ever has to
change, give it a new tag rather than silently changing its inputs, so old and
new IDs cannot collide.

Both rules scope to **spans Loom emits**. The opt-in worker-native sub-spans
([`tracing.md`](tracing.md) → "Worker-native sub-spans", #9215) are minted by a
spawned session's own OTel SDK: their IDs are random and they carry none of the
provenance below. Loom's contribution there is the parent context alone, so they
appear under a `loom.sweep` span whose identity does follow both rules.

## 2. Provenance

| Attribute | On | Value |
|-----------|----|-------|
| `loom.daemon.version` | every span | `CARGO_PKG_VERSION` of the binary that created the span |
| `loom.daemon.revision` | every span | full git SHA that binary was built from (`build.rs`), `unknown` for a tarball build |
| `loom.daemon.tree_state` | every span | `clean`, `dirty` or `unknown`: whether tracked files matched that SHA at build time — the SHA pins the code only for a `clean` build |
| `loom.install.version` | sweep span | `loom_version` from the workspace's `.loom/install-metadata.json` |
| `loom.install.revision` | sweep span | `loom_commit` from the same file |
| `loom.prompts.digest` | sweep span | `sha256:` over every file under `.claude/commands/loom/` and `.loom/roles/` (sorted path, length, bytes), taken at dispatch |

Why all three: the daemon SHA identifies the code that dispatched and recorded
the work, but a sweep child runs the prompts **installed in the workspace**,
which can lag behind or differ from the daemon. `loom.install.revision` says
which Loom commit the prompts were installed from. `loom.prompts.digest`
detects local edits and partial resyncs that the install metadata cannot see.

Stamping happens once, when the span is created (`provenance::stamp`), and never
overwrites an existing value. A span restored from the export queue after an
upgrade therefore keeps the build that created it. New `SpanRecord`
construction sites must call `provenance::stamp`. `Journal::start` does it for
every lifecycle span.
