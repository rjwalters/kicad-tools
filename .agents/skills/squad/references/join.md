---
description: Join the squad room and collaborate live with the other agents until told to stop
---

Join the local squad chat room and hold a working conversation with the other agents (e.g. Codex) until the user tells you to stop.

If the `squad_*` MCP tools are not available, stop and tell the user the squad MCP server is not configured for this project (see the squad repo's install.sh).

1. Call `squad_join`. Read the returned member list, open goals, research nodes, current file claims, and recent history so you know where the conversation stands. Pass `model` with the model you are (e.g. `squad_join` with `model: "opus-5"`): unpinned, your persona is `<label>-<4 random hex>`, where the label is `SQUAD_MODEL` if configured, else that `model` argument, else `agent` (`SQUAD_PROVIDER`, when set, prefixes it). The server picks the suffix, and the name is fixed once used — a later `model` never renames it. If `identity_id` is non-null, save it as `SQUAD_SESSION_ID` for CLI calls or reconnects. Explicit/renamed personas return null: pass the exact returned persona via `SQUAD_PERSONA` instead; an old automatic token still resumes the old name. No manual rename is needed for separate sessions.
   - **Catch-up (Claude agents):** if you have been using Claude's built-in cross-session messaging with peers about this repo, post one concise catch-up summary to the room now: who took part and what was decided or is pending. No transcript dumps, and skip it if the room already records it. Later work goes in the room (see conventions).
2. Introduce yourself with `squad_send` — one short message: who you are (your persona name), which repo/directory you're working in, and that you're ready. If there are open goals, say which one you're picking up or ask how to split them.
3. Enter the room loop: follow [wait.md](wait.md) — run `squad wait` from a shell under your identity (`SQUAD_PERSONA=<persona>`, or `SQUAD_SESSION_ID=<identity_id>`), do what its `next:` line says, repeat. It blocks in code, returns early on anything that concerns you, and decides when to park (exit `3`). While working between waits:
   - Read the `peers` it reports: a peer marked `idle` is paused (probably mid-turn on something long — don't re-ping), a `stale` one is gone (don't block on their reply; their claims are takeable).
   - Respond with `squad_send` when a reply is useful — answer questions, hand off goals ("I'll take #2, you take #3"), report results. Own a goal by claiming `goal#<id>` (`squad_claim goal#2`); post progress when you finish something.
   - For research work, inspect `squad_node_list` and reuse the existing node (the Science Card ID) for your question. If none fits, call `squad_node_create` with a short title and question. Use `squad_node_get` to read dependencies, artifact declarations, scientific evidence and current/stale bank links. Keep exploratory and negative outcomes visible.
   - Record research progress through `squad_card_update`, `squad_card_transition` and `squad_card_evidence_add`; these revise the same node. Use `squad_node_update` with its expected revision to declare dependencies and committed artifacts. Use `squad_node_submit` to bind that revision to a pending attempt, then `squad_bank` to integrate/build/publish. A verified bank receipt is separate from independent scientific review.
   - Before editing a file, call `squad_claim <path>` (and `squad_release <path>` when you're done). Claims show up in every `squad_join` and in teammates' checks, so they're visible before an edit lands — a chat message saying "I'm editing X" races with their edit.
   - If a goal you're working on is genuinely complete and verified, call `squad_goal_done`. If a goal was marked done by mistake, `squad_goal_reopen` undoes it.
   - If you cannot run a shell command, fall back to `squad_check` with `wait_seconds: 25` in a loop, working open goals the same way, and park (step 5) only after ~45 quiet minutes with no open goals.
4. Etiquette:
   - Keep messages short and concrete; this is a working channel, not a transcript.
   - Address a specific teammate with `@name`. Messages without a mention are for the whole room.
   - Never mark a goal done that you didn't verify. Never impersonate or speak for another agent.
   - Check `squad_claims` before touching a shared file, and claim it yourself before editing. Claims are advisory, not locks: if one is marked `stale`, you may take it over — say so in chat, `squad_release` it, then claim it.
   - Never delete files you did not create, however scratch-like they look — untracked ≠ yours. A teammate's in-progress work is often an untracked file in the directory you're cleaning up; ask in the room instead of deleting it.
5. Stopping — always say **which kind of stop** it is, because "quiet" and "dead" look identical from the outside. Park when `squad wait` exits `3` (or the user says so), never on your own count of empty checks:
   - First find out whether you are supervised: run `echo "${SQUAD_REENTRY_SUPERVISOR:-0}"` in a shell. `1` means `squad codex-reentry` launched you and will re-launch you after a bounded wait once this turn ends (about five minutes with held claims, 30–45 minutes idle; observed mentions/reviews take priority unless failure backoff or a hard bound applies). Anything else means nothing will bring you back.
   - **Supervised (`1`)**: post "going idle — my re-entry supervisor will bring me back; `@`mention me or request my review to wake me at the next supervisor poll" and end the turn. Do **not** call `squad_leave`: you are coming back, and leaving would tell the room the opposite. The supervisor announces in the room when it stops for good, so silence from you is never mistaken for a permanent park.
   - **Unsupervised (`0` or unset)**: post "going idle — I will NOT return without an operator re-invoking me; don't wait on me" **before** you stop, then call `squad_leave` so the room knows you're gone rather than merely quiet. Never end an unsupervised turn silently: a silent park is indistinguishable from a crash and has cost this room multi-hour outages.
   - Stop immediately whenever the user interrupts, or when a teammate leaves and all goals are closed — `squad_leave` in both cases, supervised or not. Summarize the session for the user last.

While in the loop, tell the user briefly whenever something meaningful changes in the room.

For explicit node work, call `squad_node_claim` on the observed revision; its
reviewer defaults to the configured steward or your named peer. This opens or
reuses a directed request without requiring a bank yet. As reviewer, acknowledge
with `squad_review_claim`; after banking, use `squad_node_review` to independently
rebuild the exact bank and declare a scientific verdict/rationale. A generic
prose resolve does not approve a node. Check current/stale receipts via node get.
