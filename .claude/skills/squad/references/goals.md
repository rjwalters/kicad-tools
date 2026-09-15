---
description: Show the squad's shared goal board, or add goals from the arguments
---

Manage the squad's shared goal board. The user’s request may contain new goals.

If the `squad_*` MCP tools are not available, stop and tell the user the squad MCP server is not configured for this project.

- **No arguments:** call `squad_goals` with `include_done: true` and show the board as a compact checklist (id, status, text, who added it). If there are unread messages (`squad_check` with `peek: true`), mention how many and from whom.
- **With arguments:** treat the argument text as one or more goals to add (split on newlines or semicolons if the user listed several). Call `squad_goal_add` for each. Goal additions are automatically announced in the room, so any agent sitting in a the join workflow loop will pick them up on its next check — no extra message needed. If the user instead asks to reopen a goal that was marked done by mistake, call `squad_goal_reopen` with its id.

Report the resulting board when done. Do not start working on the goals yourself unless the user asks — setting goals and joining the room are separate actions.
