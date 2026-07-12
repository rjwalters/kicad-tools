---
name: layout-journal
invocation: /kct:layout-journal
suggestedModel: sonnet
description: Keep a LAYOUT_NOTES.md journal for a hand-routing session so the reasoning behind manual copper survives across reboots and hand-offs — decisions, rip-ups, referee results, and open blockers, all tied to the committed artifact.
---

# Layout journal

Keep a `LAYOUT_NOTES.md` journal alongside a board while hand-routing it, so the *reasoning* behind manual copper is not lost when the scratch workspace is (a reboot, a new worktree, a hand-off to another agent or the owner). The committed board artifact is shipping truth; this journal is the *why* behind it. This skill generalizes the hand-router-session convention proven during kicad-tools' own manufacturing hand-routing work — it hardcodes no board and no fixture.

> **The `kct` namespace.** This skill lives in `.claude/commands/kct/` — the kicad-tools-native, harness-agnostic agent-tool namespace, invoked as `/kct:layout-journal`. It runs from inside a **consumer repo** and assumes nothing about the current directory being the kicad-tools repo.

## Why this skill exists

Hand-routing is iterative and the intermediate reasoning is fragile: which nets you ripped and why, which corridor you chose, what the referee said after each pass, what is still blocked and on whom. Scratch state (`/tmp`, un-committed working notes) is routinely lost on reboot. A `LAYOUT_NOTES.md` committed next to the board preserves that journal as durable, reviewable truth — the same reason artifact-first treats the committed board as canonical (Epic #4054 convention #3). Without it, a resumed session re-derives decisions that were already made and paid for.

## Prerequisite

The native router/DRC backend must be built in the active checkout so the referee gate (below) runs at full speed:

```bash
uv run kct build-native --check   # expect: "C++ backend: available"
uv run kct build-native           # if "not installed"
```

## Model selection

`suggestedModel: sonnet`. Maintaining a structured journal and running a referee gate is disciplined record-keeping, not frontier judgment. Model resolves through the harness's normal precedence chain.

## Arguments

**Arguments**: `$ARGUMENTS`

`$ARGUMENTS` is `<board-path> [--entry "<free text>"]`.

| Token | Meaning |
|-------|---------|
| `<board-path>` | **Required.** Path to the board being hand-routed (a `*.kicad_pcb`, or a board directory containing one). The journal is written to `<board-dir>/LAYOUT_NOTES.md`, derived from this token — never a hardcoded board directory. |
| `--entry "<free text>"` | Append a single dated session entry describing what you just did (the nets touched, the decision, the referee result). Optional; if omitted, the skill creates/updates the journal skeleton and prompts for the first entry. |

## The `LAYOUT_NOTES.md` convention

Maintain `<board-dir>/LAYOUT_NOTES.md` with these sections. Keep it terse and factual — it is a journal, not prose.

```markdown
# LAYOUT_NOTES — <board name>

Artifact-first: the committed <board>.kicad_pcb is shipping truth; this file is the WHY.
Referee for every entry: `kicad-cli pcb drc --refill-zones <board.kicad_pcb>` → 0 new errors.

## Standing constraints
- <constraint the hand-route must not violate — e.g. net class widths, keep-outs, Kelvin taps>
- <owner/EE decisions already ratified that bind this board>

## Net status (living)
| Net | Status | Notes |
|-----|--------|-------|
| <NET_NAME> | routed / partial / blocked | <one line> |

## Session journal (newest first)
### YYYY-MM-DD — <one-line summary>
- Touched: <nets ripped / re-dressed / newly routed>
- Decision: <what you chose and WHY — corridor, layer, order>
- Referee: `kicad-cli pcb drc --refill-zones` → <N new errors>; `kct check --mfr <tier>` → <result>
- Open: <what's still blocked, and on whom (owner call / EE review / another net)>
```

### Rules for each session entry

1. **One dated entry per session, newest first.** Never overwrite prior entries — the history is the value.
2. **Every entry names the referee result.** After a hand-route pass, run the cross-gate and record it:
   ```bash
   kicad-cli pcb drc --refill-zones <board.kicad_pcb>
   ```
   `--refill-zones` is load-bearing (it refills pours before checking, so DRC sees actual copper, not a stale fill). Record **new-error count**, not a bare "looks fine". A pass entry is one with **0 new errors**.
3. **Record decisions, not just actions.** "Ripped 5 sense nets and re-dressed as one Kelvin-ordered bundle through the south band *because* the nearest blockers were 0.05-0.35mm" — the *because* is what a resumed session needs.
4. **Surface blockers explicitly.** If a net is stuck on an owner/EE decision, say so in "Open" and in the net-status table — do not bury it. (For an analog/placement-blocked net, `/kct:ee-review <board-path>` produces the decision document the hand-route then executes.)
5. **Tie every claim to the committed artifact.** Measured facts ("nearest blocker 0.05mm") come from `kct net-status <pcb> --why` on the committed board, not memory.

## Referee gate (mandatory before you call a session done)

```bash
kicad-cli pcb drc --refill-zones <board.kicad_pcb>   # 0 new errors
kct check <board.kicad_pcb> --mfr <tier>             # tier resolved per /kct:manufacturing-readiness
```

A hand-route session is not "done" until the referee is clean and the journal entry records it. For full fab sign-off, hand off to `/kct:manufacturing-readiness <board-path>` (adds the export bundle on top of the cross-gate).

## What this skill does NOT do

- It does not route copper for you — it records the reasoning behind copper you route by hand.
- It does not depend on any CI workflow, GitHub Actions context, or repo-internal gate script. It runs identically inside any consumer repo's working tree.
- It hardcodes no board, no fixture, and no fab-tier list (tiers come from `kicad_tools.manufacturers`).

## References

- The `LAYOUT_NOTES.md` journal pattern was proven during kicad-tools' own manufacturing hand-routing work, where a preserved journal survived scratch-state loss across reboots and carried the routing rationale forward.
- `/kct:ee-review` — produces the EE decision document a hand-route executes when a net is analog/placement-blocked.
- `/kct:manufacturing-readiness` — the fab sign-off ritual the finished hand-route hands off to.
