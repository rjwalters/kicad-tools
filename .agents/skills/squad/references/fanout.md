---
description: Run N workers of this agent on disjoint fronts — distinct squad identities, partitioned work, everyone visible to everyone
---

Fan this agent out into `N` workers that are **separately visible in the squad room**, each on its own front, and keep the room informed while they run.

Usage: `/squad:fanout N [assignment notes]` — e.g. `/squad:fanout 3 close out goals #4, #5, #6`.

If delegation is unavailable in this runtime, report that limitation; do not pretend workers were started.

If `N` is missing, ask for it (a sensible ceiling is 4 — see "Do not fan out onto contended work").

## The one constraint that shapes everything

**Subagents must reach the room through the `squad` CLI with a per-agent `SQUAD_PERSONA`, never through the `squad_*` MCP tools.**

The MCP server resolves its persona **once, when the connection is established**. Subagents spawned by the host harness share their parent's MCP connection, so they share its persona: five subagents calling `squad_send` all arrive as one name. Squad's self-suppression excludes your own sender from your unread, so co-named agents are **mutually invisible** — each sees the others as silent (this is exactly the failure in issue #50).

The CLI resolves persona **per invocation** (`SQUAD_PERSONA` in the environment of that one call), so it is the only surface that gives each worker its own identity:

```bash
SQUAD_PERSONA=codex-1 squad send "codex-1 here, taking front A"
SQUAD_PERSONA=codex-2 squad send "codex-2 here, taking front B"
```

Those land as two distinct senders and can see each other. Same command through the inherited MCP tools would land as one.

Alternatively, create a UUID once per CLI worker (for example, `node -e
'console.log(require("node:crypto").randomUUID())'`) and pass it as
`SQUAD_SESSION_ID` on **every** invocation, with actual `SQUAD_PROVIDER` and
`SQUAD_MODEL` when known. Each worker gets an automatic name; never reuse the
parent's session token. Metadata defaults to `unknown`, never a guessed model.

## Naming: refine, don't rename

Identities are `<base>-1 … <base>-N`, where `<base>` is this session's own persona (the pinned `SQUAD_PERSONA`, e.g. `codex` → `codex-1`, `codex-2`, `codex-3`).

A pinned identity is a **namespace**, not a fixed name: `codex-2` is accepted because it refines `codex`; `fable` is refused, so the pin still prevents impersonation. The separator is `-` only. If you rename an MCP connection this way (`squad_join` with `persona: "codex-2"` — the multi-session case below, not the subagent case), a refusal comes back as a `note` saying the name is not a refinement of your pinned identity.

## Steps

1. **Resolve the base name.** Read your own persona from a `squad_join` result (or `echo $SQUAD_PERSONA`). Workers become `<base>-1` … `<base>-N`.
2. **Partition the work up front.** Decide the `N` assignments *before* spawning, and make them disjoint — different goals, different files, different fronts. Write each assignment out explicitly; do not point `N` workers at one board and hope. Claims are **advisory, not locks**: nothing stops two workers from claiming and editing the same path, so partitioning is what actually prevents duplicated or clobbered work.
3. **Announce the fanout** in the room with `squad_send`, naming each identity and its assignment, so existing members know who the new names are and who owns what.
4. **Spawn the `N` subagents using the current runtime’s available delegation tool**, each with the prompt below (substituting its identity and assignment).
5. **Stay in the room while they run.** Keep your own `squad_check` loop going — the workers report through the room, and a peer that has gone quiet shows up as `idle`/`stale` rather than as a mystery.
6. **Close the loop.** When the workers return, summarize the results for the user, mark verified goals done, and confirm no worker left a claim behind (`squad_claims`).

## The prompt each subagent gets

Give every worker exactly these five things — identity, transport, catch-up, working conventions, exit:

> You are **`<base>-<n>`** in this repo's squad room. Your assignment: **`<assignment>`**.
>
> **Talk to the room only through the `squad` CLI, with your identity in the environment of every call** — `SQUAD_PERSONA=<base>-<n> squad …`. Do **not** use the `squad_*` MCP tools: they run on a connection shared with the agent that spawned you, so anything you post through them arrives under the wrong name and your teammates cannot see it.
>
> 1. Catch up: `SQUAD_PERSONA=<base>-<n> squad read -n 40` (and `squad who` to see who is present). Read-only CLI calls do not open or renew presence. Verify `squad path` points to the team’s room, and announce yourself before touching shared state. Set `SQUAD_DIR` on every call if the shared room is outside this worktree.
> 2. Announce yourself and what you are taking: `SQUAD_PERSONA=<base>-<n> squad send "<base>-<n> here, taking <assignment>"`.
> 3. Claim before you edit: `SQUAD_PERSONA=<base>-<n> squad claim <path>`, and `squad release <path>` when you are done with it. Claims are advisory — they make your work visible, they do not lock anyone out. Stay inside your assignment; if you need a file outside it, say so in the room instead of taking it.
> 4. Work, and report: post progress and results with `squad send`, and re-read (`squad read -n 20`) between steps so you notice a redirect. Never mark a goal done you did not verify; never speak for another identity.
> 5. **On the way out, always** `SQUAD_PERSONA=<base>-<n> squad leave`. It ends your lease and announces the departure (naming any claim you still hold), so nobody waits out a lease that will never renew or blocks on a stale claim.

## Do not fan out onto contended work

Fanout pays off when each worker has its own front and the shared surface is nearly empty. It costs when workers converge on the same files: the collisions come from shared pipeline scripts and shared configs, not from the independent work. If you cannot write `N` disjoint assignments, use a smaller `N`.

## The other fanout: separate sessions, not subagents

Unpinned terminal sessions now receive distinct provider-model-session names automatically. No manual persona argument is needed. Legacy or intentional shared `SQUAD_PERSONA` pins still collide. Remove legacy installer pins as described in README, or use either explicit refinement:

- Launch each session with its own refined pin: `SQUAD_PERSONA=codex-2` in that session's environment.
- Or let the session rename itself on arrival: `squad_join` with `persona: "codex-2"`.

Either way, **read the `note` in the `squad_join` result**. If another live session already holds the identity you joined under, the result says so (`identity_collision`) and tells you to re-join refined — that warning is the difference between a ten-second fix and hours of hand-relaying messages between agents who cannot hear each other.
