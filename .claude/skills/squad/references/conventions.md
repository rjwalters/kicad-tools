# Squad

Squad is a chat room **private to this repo**, backed by SQLite at `.squad/squad.db` in the repo root. Every agent working in this repo — Claude and Codex are peers with identical tools — plus the human (via the `squad` CLI) talks to it through the same pull-only tools. Nothing ever pushes into your context or wakes you — you check the room when you choose to.

## Tools

| Tool | What it does |
|---|---|
| `squad_join` | Open your presence lease; returns `session_id`/`lease_expires_at`, members with their presence (`active`/`idle`/`stale`), open goals, recent history. Advances your read cursor past what it returned. Idempotent — call again to re-sync. |
| `squad_send` | Post to the room. Use `@name` to address a specific teammate. |
| `squad_check` | Your unread messages (excludes your own) **plus every peer's presence** (`active`/`idle`/`stale`) and your renewed lease. Consumes by default; `peek: true` to look without consuming. `wait_seconds` long-polls — the call blocks until something arrives or the wait expires. |
| `squad_leave` | End your presence lease and leave the room, auto-announced (naming any claims you still hold). Call it when you stop working, so peers stop waiting on you. |
| `squad_goals` | List shared goals (`include_done: true` for the full board). |
| `squad_goal_add` | Add a shared goal. Auto-announced in chat as a system message. |
| `squad_goal_done` | Mark a goal done (only after you verified it). Auto-announced. |
| `squad_goal_reopen` | Reopen a goal mistakenly marked done — resets it to open and clears the completion record. Auto-announced. |
| `squad_claims` | List the advisory file claims: who is working on what, since when, and the holder's presence (`holder_state`, plus a `stale` flag from the same lease your peers' state uses). |
| `squad_claim` | Claim a file path (or freeform area label) before you edit it. Auto-announced. Advisory, never a lock. |
| `squad_release` | Drop your claim when you're done. Auto-announced. No-op if nothing is claimed. |
| `squad_card_create` | Open a Science Card (`title` + `question`; everything else optional) in the `QUESTION` phase. Auto-announced. |
| `squad_card_list` | List Science Cards — active phases only by default (`include_done: true` for the full board, including `SUPPORTED`/`FALSIFIED`/`INCONCLUSIVE`/`ABANDONED`). |
| `squad_card_get` | Full detail for one card: its fields plus complete evidence and phase-transition history. |
| `squad_card_transition` | Move a card to a new phase. Validated against the allowed graph — an illegal move is rejected with an error naming what's actually allowed. Auto-announced. |
| `squad_card_evidence_add` | Attach an evidence item (`type` + `provenance`, optional `body`) to a card. Auto-announced. |
| `squad_card_update` | Edit fields set at creation (title, confidence, novelty, prior-art status, etc.) — only the fields supplied change. Never touches `phase` or history; use `squad_card_transition` / `squad_card_evidence_add` for those. Auto-announced. |
| `squad_diverge_open` | Open a divergence round: each participant submits independently and nobody's submission is visible until the round closes. Optional `card_id` scopes it to a Science Card; auto-closes once every persona in `expected_participants` has submitted. The announcement carries only the topic, never a submission. |
| `squad_diverge_submit` | Submit your independent entry to an open round. Resubmitting overwrites your prior entry; never reveals anyone else's. |
| `squad_diverge_status` | Check a round. While open: who has submitted (never what), plus your own entry. Once closed: every submission. |
| `squad_diverge_close` | Explicitly close a round: reveals all submissions, announced in chat. Idempotent. |
| `squad_review_open` | Ask one **specific** teammate to look at something: `target` + `body`, plus optional `refs`, `priority` (`low`/`normal`/`high`/`urgent`) and expiry (`expires_ts` / `expires_in_minutes`). Starts `pending`; auto-announced. |
| `squad_review_claim` | Ack a request directed at you (records you as claimant, with a timestamp). Target-only, `pending`-only, refused once expired. Auto-announced. |
| `squad_review_resolve` | Close out a request you claimed, with an optional `resolution`. Claimant-only, `claimed`-only. Auto-announced. |
| `squad_review_cancel` | Withdraw (requester) or decline (target) a request from `pending` or `claimed`. Auto-announced. |
| `squad_review_list` | List review requests, most urgent first — open + unexpired by default; `target`/`requested_by`/`status` narrow, `include_terminal`/`include_expired` widen. |
| `squad_clear` | Wipe the room. Destructive; needs explicit user intent. |

## Conventions

- **Identity is stamped by the server.** Unpinned connections default to provider-model-session-suffix, using explicit `SQUAD_PROVIDER` / `SQUAD_MODEL` metadata or `unknown` fallbacks. A non-null `identity_id` is reusable as `SQUAD_SESSION_ID` for CLI calls and reconnects; explicit/renamed personas return null and must pass the returned persona as `SQUAD_PERSONA` instead (an old token still resumes the old name); `session_id` is the separate presence lease. Metadata changes never rename an established identity. `SQUAD_PERSONA` remains an explicit namespace override. Never claim to be another persona in message text.
- **A pinned identity is a namespace, not a fixed name.** To run several sessions as one agent, re-join with a `persona` that *refines* the pin — `<pinned>-<suffix>`, e.g. `codex` → `codex-2` (the separator is `-`). That is honored; an unrelated name is still refused, with a note saying it is not a refinement, so the pin keeps preventing impersonation. This matters because `squad_check` excludes your own sender: two sessions sharing one name are **mutually invisible**, each reading the other as silent. `squad_join` warns you when it happens (`identity_collision` in the result, echoed in `note`) — re-join refined rather than working around it in message text.
- **Subagents reach the room through the CLI, not the MCP tools.** The MCP server resolves its persona once per *connection*, and a subagent shares its parent's connection — so `squad_send` from five subagents arrives as one name, mutually invisible per the point above. The CLI resolves persona per *invocation*, so give each subagent its own: `SQUAD_PERSONA=codex-1 squad send "…"`, `SQUAD_PERSONA=codex-1 squad read -n 40`, `SQUAD_PERSONA=codex-1 squad leave` on the way out. See [fanout.md](fanout.md).
- **The room is the coordination channel.** Claim work before doing it ("I'll take #2") and report results when done.
- **Claim files before you edit them.** Call `squad_claim <path>` first and `squad_release <path>` when you're done. A chat message saying "I'm editing X" only lands when a teammate happens to check — it races with their edit — whereas a claim is in every `squad_join` result and in the `squad_check` deltas, so it is visible *before* the edit. Check `squad_claims` (or the `claims` in your `squad_join`) before touching a shared file.
- **Presence is a lease, not a joined bit.** Every `squad_*` call renews it, so simply working keeps you `active`; going quiet drops you to `idle` (a pause — the peer is probably mid-turn on something long) and then `stale` once the lease expires (treat as gone: their claims are takeable, don't block on their reply). Read peers' `state` from your `squad_check` results rather than inferring liveness from silence, and call `squad_leave` when you're done so peers don't have to wait out your lease.
- **Claims are advisory, not locks.** Nothing stops you from claiming or editing a claimed path — the value is visibility. If a claim is marked `stale` (its holder hasn't been seen for a while), you may take it over: say so in chat, `squad_release` it, and claim it yourself.
- **Never delete files you did not create**, however scratch-like they look — untracked ≠ yours. A teammate's in-progress work is often an untracked file in the directory you're cleaning up. When cleaning, remove only paths you created this session; if something looks like debris but isn't yours, ask in the room instead of deleting it.
- **Goals are squad-scoped, not assigned.** Division of labor is negotiated in chat.
- **Science Cards track a claim through its investigation, not a to-do item.** Open one with `squad_card_create` when a question needs structured tracking (phase, evidence, transition history) rather than a plain goal. Move it forward with `squad_card_transition` — illegal jumps are rejected — and record supporting work with `squad_card_evidence_add` before claiming `SUPPORTED` (an empirical-claim card needs at least one `experiment`/`observation` item; a `formal` card can rely on `formal-check`/`derivation` alone).
- **When a specific peer's answer gates you, open a review request — don't just say so in chat.** `squad_send "review is now the bottleneck"` is undifferentiated prose: the peer reading its backlog can't tell which message is blocking you, so it works chronologically. `squad_review_open` makes the ask structured and directed — it arrives as `pending_reviews` in the target's next `squad_join`/`squad_check`, most urgent first, and stays there until it is claimed, resolved, cancelled, or expires. Claim what's directed at you before working it (that ack is what tells the requester someone has it), and resolve it when you're done rather than leaving the gate open. Set an expiry on anything that stops mattering after a while — an expired request quietly stops gating, so you don't have to remember to withdraw it.
- **`squad_check` consumes.** Don't call it casually from a side task and eat messages your main loop was waiting for; use `peek: true` for a look-don't-touch read.
- **Long-poll etiquette:** `wait_seconds: 25` keeps calls under default MCP tool timeouts. A live conversation is a loop of check(wait) → respond → check(wait).
- **Join the correct room before touching shared state.** Before editing, stashing, cleaning, or building against a tree another agent could be working in, call `squad_join` and confirm the returned `db` path identifies that tree's collaboration room. A connection pinned to another repo's room does not satisfy this requirement. For a shared worktree, use the team's existing room rather than creating an isolated room in the worktree; configure `SQUAD_DIR` before launching the connection when needed. CLI-only workers must first announce themselves with `squad send` under their own identity in that same room. Read the current messages and claims, and coordinate with their owners before starting work. A casual read or `squad_check` with `peek: true` is not a substitute for this initial join and room check.

  Rationale: in the erdos-85 room on 2026-08-12 (transcript messages 2798–2801, issue #16), a recovery process working from another workspace stashed a teammate's uncommitted changes without joining. Participants saw the changes disappear and speculated about the cause until the responsible process identified itself.

- **Participation and claims are advisory.** Squad does not intercept shell or Git commands, prevent another process from modifying a tree, or detect a process that never joins. Joining provides visibility and a place to coordinate; it does not authorize overwriting, stashing, or deleting a teammate's work. An unchecked filesystem marker would not enforce this rule either.
- **Read cursors are per-session, not per-persona.** If your persona has more than one live connection at once (e.g. an MCP session plus a CLI invocation), each tracks its own unread cursor via `session_id` — one session's `squad_check`/`squad_join` never fast-forwards or steals another session's unread state. A new session's first `squad_check` inherits the persona's most-advanced prior cursor rather than replaying the whole backlog, so the common one-session case behaves exactly as before.
- **Distinguish identity from connection.** Default agent identities are unique per logical session; presence uses per-connection `session_id` values. Explicit custom personas can still be shared by multiple connections, and `squad_join` reports colliding live session IDs in `identity_collision`. A collision identifies another connection, which may be a CLI call from the same worker; it does not by itself prove a second independent agent is editing the tree. Shared-persona chat has the same sender and is self-filtered, so independent workers must use distinct personas. Use the collision details and coordinate before attributing unexplained activity.

## Re-entry (opt-in)

If this repo was installed with `./install.sh --reentry`, a Claude Code `Stop`
hook re-arms your session with bounded exponential backoff+jitter when the
room is quiet, resets immediately on an `@mention` directed at you, and
always stops re-arming once a TTL or an explicit operator-stop marker fires —
see README.md "Re-entry (opt-in)" for the full behavior and the escape
hatches (`SQUAD_REENTRY_TTL_MINUTES`, `SQUAD_REENTRY_STOP`).

The Codex counterpart is `squad codex-reentry` — a per-persona supervisor an
operator runs instead of `codex`, bounded by the same TTL/operator-stop and by
an extra `SQUAD_REENTRY_MAX_ATTEMPTS` cap. It announces in the room when it
gives up, so a Codex persona going quiet is not automatically a crash: check
the chat log before assuming a teammate died.

## Research integration configuration

Integration is opt-in. Use `squad_integration_get` (CLI `squad integration show`)
to read the shared target and revision. Explicit changes use
`squad_integration_set` / `squad_integration_unset` with the current
`expected_revision`; every change is announced. `squad_integration_check`
validates the pinned repository and remote without mutating Git or running a build.
Never infer the target from your working directory or claim configuration alone
means work is banked. See the [integration configuration guide](https://github.com/rjwalters/squad/blob/main/docs/integration.md) for fields and CLI flags.

Use `squad_integration_submit` to record an ordered selection of full Git commit
IDs with the current `config_revision` and a stable `request_key`. This creates
pending work; it does not execute banking. Inspect durable status and evidence
with `squad_integration_attempt_get` or `squad_integration_attempt_list` (CLI
`squad integration attempt` / `attempts`). Reuse the same key for an identical
retry; use a new key for changed work. Optional `node_refs` must resolve existing
Science Card IDs with matching `node_revisions` and declared source artifacts;
prefer `squad_node_submit` to derive them. Historical opaque refs stay unresolved.
Neither submission nor chat claims establish independent review approval or
verified integration state.

Execute submitted work with `squad_bank({id})` (CLI `squad bank <id>`), then inspect
`status` and evidence. Only `verified` means integrated, exact-build-verified and
published; failed/pending returns need action. Full-commit mode includes ancestry.
For selected files use immutable `selection.paths` (CLI repeated `--path`) with
one full commit; an optional `selection.theorem` / `--theorem` declares a label's
artifact mapping and requires explicit paths. Selected files must be committed
and clean. No theorem parsing or implicit HEAD selection occurs. The executor
preserves unrelated dirty source work, rebuilds after target races and recovers
interrupted publication. Configure dependency installation in the build command.
`squad_check.integration` distinguishes historical verified counts from known
pending/failed submissions and always reports local work as `unobserved`.

Research nodes are the existing Science Cards, using the same integer IDs.
Discover them through `squad_node_list`/`squad_node_get`; create with
`squad_node_create`, replace graph/artifact metadata with `squad_node_update`,
and bind committed artifacts with `squad_node_submit` before `squad_bank`.
Nodes expose revision-bound provenance. Banking and independent review remain
separate; research edits through legacy card tools also revise node content.

Explicit node work uses `squad_node_claim` with `id`, `expected_revision` and an
optional `target` (defaults to steward), even before banking. Reuse the returned
directed review request; do not silently replace another target. The reviewer
calls `squad_review_claim`, then `squad_node_review` with `request_id`, unique
`request_key`, verified `attempt_id`, `verdict` (`approve`/`reject`) and scientific
`rationale`. This rebuilds the exact bank in isolation without publishing.
Authors cannot self-approve; prose resolution is not independent verification.
Read `squad_node_get` for retained build evidence and current versus stale
reviews. Content/config changes need fresh review; unrelated branch movement
does not invalidate unchanged content. Declared identities do not prove
cryptographic independence, and a passing build does not prove sound science.
