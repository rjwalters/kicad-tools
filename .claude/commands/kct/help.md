---
name: help
invocation: /kct:help
suggestedModel: sonnet
description: Explain the installed kct skills — what each does, how to invoke it, and the load-bearing conventions — by reading the files actually vendored in this repo. Introspective and strictly read-only.
---

# kct help

Orient a user inside a consumer PCB-design repo that has kicad-tools installed:
**which `{{skill:*}}` skills are present, what each one is for, the load-bearing
conventions, and where to start.** This is a *meta-skill* — it describes the
other skills by reading the files actually vendored under
`{{skills-dir}}`, so it never drifts from a version-skewed or
`--skills=`-filtered install.

> **The `kct` namespace.** This skill lives in `{{skills-dir}}` — the
> kicad-tools-native, harness-agnostic agent-tool namespace, invoked as
> `{{skill:help}}`. It runs from inside a **consumer repo** and assumes nothing
> about the current directory being the kicad-tools source checkout.

> **Strictly read-only.** This skill only *reads* `.md` files and (optionally)
> `.kct/install-metadata.json` to describe what is installed. It **never**
> invokes another `{{skill:*}}` skill, never opens/modifies/routes/checks/exports a
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
| *(none)* | **Overview mode.** Render a one-screen orientation: every installed `{{skill:*}}` skill (one line each from its frontmatter), the load-bearing conventions, and a "start here" pointer. |
| `<command>` | **Detail mode.** Summarize exactly one installed skill (`<command>.md`): its usage/arguments, what it does, one concrete example invocation, and whether it is read-only or writes to the board. If `<command>.md` does not exist, say so and fall back to the overview's skill list. |

## Overview mode (no argument) — introspect, never hardcode

Do **not** hardcode the skill list in this document. Build it at run time from
the files actually present:

1. **List the vendored skills.** Read the directory of this namespace:

   ```bash
   ls {{skill-file:*}}
   ```

   For each skill file other than the namespace README and this help skill,
   read its YAML `name` and `description`. A skill can be stored as `<name>.md`
   or as `kct-<name>/SKILL.md`; identify it from frontmatter, not the basename.
   Render one row per installed skill with its name, purpose, and invocation,
   written as `{{skill:<name>}}` where `<name>` is the skill name without any
   `kct-` prefix. Include a suggested model only when that optional metadata
   is present; harnesses without per-skill model dispatch ignore it. Do not
   infer the installed skill list from the namespace README's full catalog.

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

   - **`.kct/CONVENTIONS.md`** — the four load-bearing conventions verbatim
     (build the native router backend, cross-gate DRC with
     `kicad-cli pcb drc --refill-zones`, artifact-first, and per-rule
     manufacturing warning baselines). Read this before
     routing or manufacturing sign-off. If `.kct/CONVENTIONS.md` is absent
     (an install predating it), fall back to the guarded kicad-tools block in
     the repo's `{{agent-guide}}`, which carries the same pointer.
   - **`{{skills-readme}}`** — why the `kct` namespace exists and
     the canonical skills table.

4. **Start here.** Close with a short pointer: read `.kct/CONVENTIONS.md`
   first, then run `{{skill:help}} <command>` for details on any single skill, or
   read `{{skills-readme}}` for the namespace overview. For the `kct` workflow,
   the `--format json` contract and the sign-off rule, run `kct agent-guide`
   (the primer shipped with kicad-tools, the same in every harness).

## Detail mode (`{{skill:help}} <command>`)

Read `{{skill-file:<command>}}` and **summarize** it — do not
reproduce it:

- **Usage / arguments** — the `## Arguments` table (what `$ARGUMENTS` accepts).
- **What it does** — two or three sentences from the body, not a copy of it.
- **One concrete example** — a single realistic invocation, e.g.
  `{{skill:<command>}} <its-argument>`.
- **Read-only or writes?** — state plainly whether the skill only reads, or
  writes an artifact / edits a `.kicad_pcb`.

This is a *summary*, not a reimplementation — do not execute the skill.

If `{{skill-file:<command>}}` does not exist, say so plainly and list
what **is** installed (the same introspective list as overview mode) so the
user can pick a real one.

## Notes

- **Read-only, always.** This skill reads `.md` files and optionally
  `.kct/install-metadata.json`. It never invokes another skill, never runs
  `kct check` / `kct export` / routing, never opens or modifies a
  `.kicad_pcb`, and writes nothing.
- **No board argument.** Unlike the per-board skills, `{{skill:help}}` takes no
  board path — it describes tools, it does not operate on a board.
- **Introspective, never hardcoded.** The skill list always comes from the
  files present under `{{skills-dir}}`, so adding or removing a skill
  file changes the overview automatically — there is no second list to keep in
  sync.
