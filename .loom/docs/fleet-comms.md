# Fleet-comms etiquette (safehouse posting for worker roles)

Phase 3 of the safehouse interface roadmap (#4196, phase 2 of #3999/#3997). The
plumbing already exists and is documented in
[`safehouse.md`](safehouse.md): when the `safehouse` config block is enabled,
`spawn-claude.sh` injects a session-scoped MCP config that gives the worker the
`safehouse_send` / `safehouse_read` (and room-admin) tools alongside `loom`.
This document is the **behavioral layer** — when and how a role uses those
tools once they're present. It does not change label-based coordination, which
remains the sole source of truth (see "What NOT to do" below).

> **Path note**: this file lives at `defaults/docs/fleet-comms.md` in the Loom
> source repo. A consumer install maps it to `.loom/docs/fleet-comms.md` (not
> `defaults/.loom/docs/`) — see `defaults/docs/runtime-adapters.md` for the same
> convention spelled out in detail.

## 1. Detection — the room is optional, always

Safehouse posting is conditional (config-gated, host-dependent). A role session
may or may not be able to reach the room — **do not assume either way**. There
are two delivery paths, tried in order; the same degradation contract from
`safehouse.md` applies to both:

1. **MCP tools (`safehouse_send` / `safehouse_read`)** — present when
   `spawn-claude.sh` injected the safehouse MCP server *and* your runtime/role
   exposes MCP tools. Prefer these when available.
2. **Bash helpers (`.loom/scripts/fleet-send.sh` / `.loom/scripts/fleet-check.sh`)**
   — the fallback for roles whose tool allowlists exclude MCP tools. Lifecycle
   role subagents (`loom-builder` / `loom-judge` / `loom-doctor`) pin their
   tools to Read/Glob/Grep/Bash(/Write/Edit) with no MCP tools, so the
   injected `safehouse_send` / `safehouse_read` are invisible to them — but
   they all have Bash. Both helpers resolve the socket/persona from
   `$SAFEHOUSED_SOCKET` + `$SAFEHOUSE_PERSONA` (exported into the session by
   `spawn-claude.sh`) and speak the same JSON-lines wire protocol:
   `fleet-send.sh` posts (`send` op), `fleet-check.sh` reads (`check` op).

- **If a path is available**: use it per the guidance below (MCP first, then the
  helper).
- **If neither resolves**: proceed exactly as you do today. This is the normal
  case for most sessions, not an error condition. `fleet-send.sh` itself exits 0
  silently when no socket/persona is set, so an unconditional call is safe.
- **Never fail, retry, stall, or comment on the room's absence.** No presence
  check should ever block, slow, or change the outcome of a role's normal work.
  Treat an unreachable room the same way you'd treat a missing optional
  dependency — silently unavailable, nothing to fix.

## 2. When to post (sparingly)

**The signal room is a human's phone, not a log file.** A message should be
something a person watching Element would actually want to see arrive as a
notification. (Since #4225 the fleet spans a signal room and per-repo firehose
rooms — see §3a — but that is a reason to pick the right `type`, **not** a licence
to narrate freely into the firehose: the guidance below applies to both.)
Routine progress narration is already covered by the daemon's own event-bus
narration (`safehouse.md` phase 1) — a worker posting the same information a
second time is noise, not signal.

| Role | Post on | Do NOT post |
|------|---------|-------------|
| **Builder** | One line on claim ("starting issue #N: `<title>`"); one line on PR creation; a *notable* mid-task finding (surprising discovery, a concern worth human eyes, a decision the human might want to veto) | Routine progress ("wrote the function", "running tests now", file-by-file narration) |
| **Judge** | Verdict summary — approve or changes-requested, one-line why | The full review comment (that's what `gh pr comment` is for) |
| **Doctor** | One line on what was fixed | Step-by-step fix narration |
| **All roles** | A genuine blocker — post with `type: handoff` (the "a human must act" signal) | A concern you're already handling yourself (that's not a blocker) |

Curator, Champion, and Guide are label-machine roles — out of scope for now
(their throughput is high and their output is mechanical; the noise risk
outweighs the value). Do not add fleet-comms posting to those roles without a
separate issue.

## 3. How to post

Via the MCP tool (when present):

```
safehouse_send(
  task_id: "<repo>_<issue>",   # threads with the daemon's own narration for the same issue
  to: "*",                      # broadcast
  type: "task" | "handoff" | "chat",
  body: "<one concise line>"
)
```

Via the Bash helper (the fallback for roles without MCP tools):

```
.loom/scripts/fleet-send.sh --task-id "<repo>_<issue>" --type task --body "<one concise line>"
```

The helper defaults `to` to `"*"`, reads persona/socket from the session env,
and exits 0 silently when the room is unreachable — call it unconditionally.

To read back pending mail without MCP tools, use the read/check counterpart:

```
.loom/scripts/fleet-check.sh [--peek] [--limit <n>]
```

`fleet-check.sh` reads persona/socket from the same session env, prints each
pending message as one JSON object per line to stdout on success, and prints
nothing (exit 0) on any failure or an empty mailbox — treat empty output as
"no mail" uniformly. By default a `check` **advances the persona's read
cursor** (mail is consumed once read); pass `--peek` to read without
consuming. `--limit <n>` caps how many messages come back in one call.

- **`task_id`**: the repo-qualified `<repo>_<issue>` form the daemon narrates on
  (post-#4224 — e.g. `loom_4199`, the workspace-root basename plus the issue
  number), so your message threads alongside the daemon's phase narration for
  that issue (see the envelope table in `safehouse.md`).
- **`to`**: `"*"` (broadcast) — this is a shared room, not a DM.
- **`type`**: this is also what picks the **room** your message lands in (see
  §3a) — choose it for what the message *is*, and routing follows.
  - `task` — routine, in-band progress (claim / PR-created lines).
  - `handoff` — a genuine blocker; this is the signal that a human must act.
  - `chat` — free-form conversation (rare for automated posts; mostly for
    replying to an operator's directed message).

## 3a. Which room your post lands in (routing by `type`, #4225)

The fleet is not one room. Messages route by **attention class first, repo
second**, and **severity routes — never duplicate**: one message, exactly one
room. You do **not** name a room; the `type` you pick decides it:

| Your `type` | Room | Why |
|---|---|---|
| `task`, `chat` | the **per-repo firehose** (`fleet-<repo>`, muted by default) | dispatch/phase/worker chatter — read when someone is actively watching that repo |
| `handoff` | the **signal room** (`loom-fleet`, notifications **on**) | a human must act; this is the operator's phone |

Practical consequences for a role:

- **Do not post the same thing twice** to "make sure it's seen". A `handoff`
  already goes to the room with notifications on; re-posting it as a `task` just
  adds noise to the firehose.
- **`handoff` is the escalation lever, and it is the only one.** Use it for a
  genuine blocker (per §2) — not for routine progress you would like to be
  noticed. Over-using it re-creates exactly the drowned-signal problem this split
  exists to fix.
- Keep `task_id` repo-qualified (`<repo>_<issue>`) as below: threading works the
  same inside whichever room the message lands in.
- On a host with no `safehouse.rooms` map configured, every type still goes to the
  single configured room — the behavior is unchanged, and nothing you do differs.
  The daemon's own narration follows the identical table (see `safehouse.md`).

## 4. What NOT to do

- **Labels remain the sole coordination mechanism.** Never treat the room as
  state — no role should read the room to decide what to do next (with the one
  exception in §5 below), and no role should skip or substitute a label
  transition because it "already said so in the room."
- **Never post secrets, tokens, or keys.** The same rule as every other
  Loom-side channel (logs, PR comments, issue comments) applies here.
- **Never block on `safehouse_read`.** Poll it only at natural pause points
  (e.g., right after pushing a PR, right after claiming an issue) — never as a
  wait loop, and never as a precondition for continuing your normal work.

## 5. Read-back: operator guidance is advisory input

At the natural checkpoints where you do poll the room, you may see an
operator's `@`-directed message. Treat it exactly like an issue comment: fold
it in as advisory input to your current task. It does **not** override role
guardrails (scope discipline, label discipline, the "issues are suggestions"
guardrails, etc.) — it's a hint from a human watching the fleet, not a
privilege escalation. If it conflicts with your role's mandatory rules, follow
your role's rules and, if useful, say why in a reply.

For roles without MCP tools, `.loom/scripts/fleet-check.sh` is the non-MCP
path to this read-back (`safehouse_read`'s Bash-fallback counterpart to
`safehouse_send`/`fleet-send.sh`) — poll it at the same natural checkpoints,
never as a wait loop.

**Shared-cursor caveat**: a persona's mailbox cursor is shared by every
process using that persona. A default (advancing) `check` consumes mail for
*all* consumers sharing the persona, not just the caller — if another process
(or a later checkpoint in your own role) expected to see that mail, it won't.
Use `--peek` when you're not sure you're the sole consumer of the persona's
mailbox at that checkpoint.

## Summary for role authors

If you're adding a fleet-comms pointer to a new role file, keep it short (a
handful of lines) and link back here rather than restating the etiquette
inline — see `builder.md`, `judge.md`, `doctor.md` for the pattern.
