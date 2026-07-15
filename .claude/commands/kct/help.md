---
name: help
invocation: /kct:help
suggestedModel: sonnet
description: Explain the installed /kct:* skills — what each does, how to invoke it, and the load-bearing conventions — by reading the files actually vendored in this repo. Introspective and strictly read-only.
---

# kct help

Orient a user inside a consumer PCB-design repo that has kicad-tools installed:
**which `/kct:*` skills are present, what each one is for, the load-bearing
conventions, and where to start.** This is a *meta-skill* — it describes the
other skills by reading the files actually vendored under
`.claude/commands/kct/`, so it never drifts from a version-skewed or
`--skills=`-filtered install.

> **The `kct` namespace.** This skill lives in `.claude/commands/kct/` — the
> kicad-tools-native, harness-agnostic agent-tool namespace, invoked as
> `/kct:help`. It runs from inside a **consumer repo** and assumes nothing
> about the current directory being the kicad-tools source checkout.

> **Strictly read-only.** This skill only *reads* `.md` files and (optionally)
> `.kct/install-metadata.json` to describe what is installed. It **never**
> invokes another `/kct:*` skill, never opens/modifies/routes/checks/exports a
> `.kicad_pcb`, and **never writes any file**. It takes no board argument
> because it never touches a board. See the Notes section.

## Model selection

`suggestedModel: sonnet`. Reading frontmatter and rendering an orientation
table is summarization, not frontier judgment. Model resolves through the
harness's normal precedence chain (explicit dispatch param → harness role
config → this doc's frontmatter `suggestedModel` → session default).

## Arguments

**Arguments**: `$ARGUMENTS`

`$ARGUMENTS` is `[<command>]` — an *optional* skill name (e.g. `ee-review`).
It is **not** a board path: this skill takes no board argument and touches no
board.

| Token | Meaning |
|-------|---------|
| *(none)* | **Overview mode.** Render a one-screen orientation: every installed `/kct:*` skill (one line each from its frontmatter), the load-bearing conventions, and a "start here" pointer. |
| `<command>` | **Detail mode.** Summarize exactly one installed skill (`<command>.md`): its usage/arguments, what it does, one concrete example invocation, and whether it is read-only or writes to the board. If `<command>.md` does not exist, say so and fall back to the overview's skill list. |

## Overview mode (no argument) — introspect, never hardcode

Do **not** hardcode the skill list in this document. Build it at run time from
the files actually present:

1. **List the vendored skills.** Read the directory of this namespace:

   ```bash
   ls .claude/commands/kct/*.md
   ```

   For each `<name>.md` **other than** `README.md` and `help.md` itself, read
   its YAML frontmatter block (the `name`, `invocation`, `suggestedModel`,
   `description` keys between the leading `---` fences). Render one row per
   skill — mirroring the table in `.claude/commands/kct/README.md` rather than
   inventing a new format:

   | Skill | Purpose (from `description`) | Model |
   |-------|------------------------------|-------|
   | `/kct:<invocation>` | one-line `description` | `suggestedModel` |

   Skip `README.md` (it is the namespace index, not a skill) and `help.md`
   (this file — describing yourself in the table is noise).

2. **Caption with install metadata, if present.** If
   `.kct/install-metadata.json` exists, read it and caption the overview with
   its `kct_version`, `install_date`, and `skills_selected` fields, e.g.
   *"kicad-tools v0.15.1, installed 2026-07-11, skills: ee-review,
   manufacturing-readiness, …"*.

   ```bash
   cat .kct/install-metadata.json   # optional — absent on some installs
   ```

   **Fallback (metadata absent):** a `--path` dev-mode install predating this
   file, or reading from the kicad-tools *source* checkout itself, may have no
   `.kct/install-metadata.json`. Do **not** error — just omit the version/date
   caption and list whatever `.md` files are present.

3. **Surface the load-bearing conventions.** Point the user at the vendored
   convention files rather than restating them here (they are owned by the
   installer and must not be duplicated a third time):

   - **`.kct/CONVENTIONS.md`** — the three load-bearing Epic #4054 conventions
     verbatim (build the native router backend, cross-gate DRC with
     `kicad-cli pcb drc --refill-zones`, artifact-first). Read this before
     routing or manufacturing sign-off. If `.kct/CONVENTIONS.md` is absent
     (an install predating it), fall back to the guarded kicad-tools block in
     the repo's `CLAUDE.md`, which carries the same pointer.
   - **`.claude/commands/kct/README.md`** — why the `kct` namespace exists and
     the canonical skills table.

4. **Start here.** Close with a short pointer: read `.kct/CONVENTIONS.md`
   first, then run `/kct:help <command>` for details on any single skill, or
   read `.claude/commands/kct/README.md` for the namespace overview.

## Detail mode (`/kct:help <command>`)

Read `.claude/commands/kct/<command>.md` and **summarize** it — do not
reproduce it:

- **Usage / arguments** — the `## Arguments` table (what `$ARGUMENTS` accepts).
- **What it does** — two or three sentences from the body, not a copy of it.
- **One concrete example** — a single realistic invocation, e.g.
  `/kct:<command> <its-argument>`.
- **Read-only or writes?** — state plainly whether the skill only reads, or
  writes an artifact / edits a `.kicad_pcb`.

This is a *summary*, not a reimplementation — do not execute the skill.

If `.claude/commands/kct/<command>.md` does not exist, say so plainly and list
what **is** installed (the same introspective list as overview mode) so the
user can pick a real one.

## Notes

- **Read-only, always.** This skill reads `.md` files and optionally
  `.kct/install-metadata.json`. It never invokes another skill, never runs
  `kct check` / `kct export` / routing, never opens or modifies a
  `.kicad_pcb`, and writes nothing.
- **No board argument.** Unlike the per-board skills, `/kct:help` takes no
  board path — it describes tools, it does not operate on a board.
- **Introspective, never hardcoded.** The skill list always comes from the
  files present under `.claude/commands/kct/`, so adding or removing a skill
  file changes the overview automatically — there is no second list to keep in
  sync.
