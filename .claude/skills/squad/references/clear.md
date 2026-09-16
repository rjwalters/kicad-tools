---
description: Wipe the squad room (messages, goals, claims, cursors) for a fresh session
---

Reset the squad room to empty.

This deletes all messages, all goals (open and done), all file claims, every agent's read cursor, and the member list. It is destructive and affects every participant in this room, so confirm with the user first unless they explicitly asked for the reset in this same request (e.g. they invoked `/squad:clear` with clear intent).

Then call `squad_clear` and confirm the room is empty with `squad_goals` / `squad_join`.
