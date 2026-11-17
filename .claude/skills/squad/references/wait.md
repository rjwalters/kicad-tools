---
description: Stay in the squad room loop — run squad wait, do what its next line says, repeat
---

The room loop lives in `squad wait`; this workflow just runs it. Use it after `/squad:join`, or to resume after a compaction or a drift without re-joining.

1. In a shell, with your identity exported (`SQUAD_PERSONA=<your persona>`, or `SQUAD_SESSION_ID=<identity_id>`), run `squad wait`. It blocks up to 540s by default; if your shell times out sooner, pass a smaller `--max <seconds>` (e.g. `--max 110` for a 2-minute cap). Never run it as `human`.
2. Read what it printed: the new messages (already consumed), the board, and one `next:` line.
3. Do what `next:` says — reply, act on the review or request, continue or claim a goal (`squad_claim goal#<id>`), take over a stale one, or propose goals — then post progress in the room as usual (see [join.md](join.md) for etiquette and claims).
4. Exit code `0` (woke) or `2` (quiet, with a suggestion): go back to step 1.
5. Exit code `3` (park recommended): run `echo "${SQUAD_REENTRY_SUPERVISOR:-0}"`.
   - `1` (supervised): post "going idle — my re-entry supervisor will bring me back; `@`mention me or request my review to wake me" and end the turn. Do **not** call `squad_leave`.
   - Anything else: post "going idle — I will NOT return without an operator re-invoking me; don't wait on me", then call `squad_leave`. Never park silently.

Stop at once if the user interrupts (`squad_leave`, then summarize). Tell the user briefly when something meaningful changes in the room.
