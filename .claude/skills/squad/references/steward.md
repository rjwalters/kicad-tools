---
description: Inspect authoritative bank, review and outline state and run the shared steward workflow
---

Use the room and identity conventions before starting. The configured identity in
`squad_steward_status` / `squad steward status` is the steward; retain that exact
persona across restarts. Peers can read the report too. If no steward is configured,
show the missing integration configuration; do not silently adopt another identity
or rewrite configuration. The operator configures it through `squad integration set`
or `squad_integration_set`, with the expected current configuration revision.

## Answer from durable evidence

Read `squad_steward_status` / `squad steward status`. This snapshot answers:

1. **What is banked?** Each node's `banked` flag applies to its exact current
   revision and current integration configuration. Cite the verified integration
   commit/tree and build/publication evidence from `attempts`; preserve historical
   successes separately. `pending_attempt_ids` and `failed_attempt_ids` are durable
   queues, not successful banks. `unbanked_declared_node_ids` reports declared
   artifacts; private work that was never submitted is unobserved, never absent.
2. **What has independent review?** Read the current node `review_status`, its
   revision-specific receipts, `review_builds`, and directed `review_requests`.
   Banking/build success and chat acknowledgments do not approve science. Old
   receipts remain historical when content or integration configuration changes.
3. **Is the shared map fresh?** `outline.fresh` compares current room state with
   the latest recorded verified publication of `SQUAD_OUTLINE.md`. Remote visibility
   is unobserved. For another generated path, use `squad_outline_status` /
   `squad outline status <path>` explicitly.

Claims are advisory. The report flags claims older than 24 hours and overlapping
peer paths, without deleting claims, consuming messages or renewing presence.
Ask the owner to inspect an advisory conflict; preserve intentional long-lived
claims and user edits. An old timestamp alone does not authorize release.

## Diagnose room drift

Use `squad_room_doctor` / `squad doctor --room` for an evidence-backed drift
report with ages and next commands. It reads declared work, integration attempts,
outline freshness, outstanding reviews and advisory claims without changing state.
The chat scan covers at most the latest 2,000 messages by default; possible banking
claims are heuristic prompts to inspect the ledger, not proof of a false statement.
A message about an older revision cannot establish banking of the current one.

Local commit observations distinguish `verified_clean`, `observed_unbanked`,
`unreachable` and `unobserved`. An absent local object says nothing about unfetched
remote branches or undeclared private work. Cite the observed revision and evidence;
preserve uncertainty when participants have different local repository visibility.
Follow the explicit recovery actions below only after inspecting the finding.

## One bounded pass

As the configured steward, invoke `squad_steward_tick` / `squad steward tick`.
It sends at most five reminders per invocation. Each durable
condition + object + revision key has a 24-hour minimum cadence and a lifetime
maximum of three sends. Configuration revisions scope the keys. Repeated calls,
concurrent processes and restarts reuse the same ledger. A clock moving backward
suppresses reminders until the cadence passes. Additional eligible conditions
can be serviced by a later explicit pass; inspect `reminders` for sent history.
Changed revisions create new keys, so these are per-revision bounds, not a global
room quota. Chat and its dedup record commit atomically; failure rolls both back.

Tick only observes state and sends reminders. There is no background scheduler,
automatic bank submission, science approval, outline publication or claim cleanup.
A live agent can invoke it in its ordinary room loop. A stopped steward sends
nothing; any peer can still inspect the same durable state and failure evidence.

## Resume explicit work

For a pending/failed integration, inspect `squad_integration_attempt` /
`squad integration attempt <id>`, then explicitly resume `squad_bank` /
`squad bank <id>`. Keep the same attempt ID; its executor handles runner leases
and retries. Attempts from retired configurations cannot bank under the current
configuration: retain them as history and explicitly submit still-needed work to
the selected current target. Address conflicts/build failures before retrying. Do not create a
replacement attempt merely because the steward restarted.

For newly declared artifacts without an attempt, use `squad_node_submit` /
`squad node submit <id> <expected-revision> <request-key> <config-revision>`,
then bank its returned ID. Persist and reuse the request key recorded in the
attempt. Check whether the node already has a current attempt before submitting.

For a missing review, explicitly open/reuse a directed request with
`squad_node_claim` / `squad node claim <id> <expected-revision> <target>`.
Choose an independent reviewer, preserving existing manually held claims.
The directed reviewer must claim the request first with `squad_review_claim` /
`squad review claim <request-id>`. For an existing request, inspect its node's
review receipts (including the persisted `request_key`) and resume the same
`squad_node_review` / `squad node review <request-id> '<JSON>'` call with the same
`request_key`, `attempt_id`, verdict and rationale. Only the directed independent
reviewer performs that review, after examining the exact revision and evidence.
Do not submit an approval just to clear the queue. Completed idempotent calls
return their durable receipt, including terminal failures. A running lease may
require waiting; recovery of an interrupted lease records a terminal failure.
Inspect and retain that evidence, then deliberately use a fresh key if a new
verification is needed. Never invent a replacement key before inspecting history.

For the outline, inspect publication history first. Explicitly resume
`squad_outline_publish` / `squad outline publish <request-key> [path]` with the
existing key for its archived snapshot. A new snapshot needs a new key. Keep
failure evidence queryable and preserve generated-file ownership checks.

Finish with another status read and report verified changes separately from
reminders, pending work and failures. Never infer a state change from chat.
