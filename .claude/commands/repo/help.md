---
name: "help"
description: "Explain the installed /repo:* commands — what each does, how to use them, where to start"
domain: repo
type: command
user-invocable: true
---

# /repo:help — How to Use Repo Skills

Explain the repo skills installed in **this** repository. Introspect the
actual install rather than assuming — installs can be filtered to a subset of
commands.

## Usage

```
/repo:help                     # Overview of all installed commands
/repo:help reset               # Detailed help for one command
```

## Steps — no argument (overview)

### 1. Read the actual install

```bash
ls .claude/commands/repo/*.md
cat .claude/skills/repo/install-metadata.json
cat .claude/skills/repo/.install-local.json 2>/dev/null   # machine-local, gitignored
```

Build the command list from the files present. For each, pull the one-line
`description:` from its frontmatter. Do not list commands that are not
installed here.

`install-metadata.json` (tracked) carries `version`, `commands`, and `dev`. The
install date lives in the gitignored `.install-local.json` sidecar
(`installed_at`), so it is present only on the machine that ran the install;
older installs still keep `installed_at` inline in `install-metadata.json`. Use
whichever is present for the date below, and simply omit "(installed <date>)"
when neither has it (e.g. a fresh clone on another machine) — its absence is
expected, not an error.

### 2. Present the overview

Structure it like this:

```
## Repo Skills v<version> (installed <date>)

General repository hygiene and environment tools. Commands **apply their
safe, reversible fixes by default** and report each change; add `--ask`
to review and confirm first. Irreversible removals (branches, worktrees,
stashes, untracked files) always need an explicit opt-in.

### Everyday
| Command | When to reach for it |
|---------|----------------------|
| /repo:reset | Done with a task — get back on main, synced, stale state reviewed |
| /repo:tidy  | Working tree cluttered with build artifacts and temp files |
| /repo:remote | Need a cloud dev box (GCP/AWS) with this repo ready to go |
| /repo:release | Cut a release — pre-flight, semver, CHANGELOG, version bump, tag, GitHub Release |

### Periodic maintenance
| /repo:audit | Monthly sweep, or after a big refactor/import |
| /repo:update-tools | Keep Loom/Anvil/Repo Skills installs current |

### Focused checks
| /repo:docs | Documentation health — content, README structure, cross-refs |
| /repo:branches | ... |
| /repo:gitignore | ... |
| /repo:links | ... |
| /repo:orphans | ... |
| /repo:readme | ... |
```

(`audit` runs the branch/gitignore/links/orphans/readme checks; `docs` is the
canonical doc command and subsumes `readme` + `links`.)

Group whatever is actually installed into those three buckets (everyday /
periodic / focused); put unrecognized commands in a fourth "Other" group with
their frontmatter descriptions.

### 3. Close with orientation

- Where to start: `/repo:audit` for a first look at repo health, `/repo:reset`
  at the end of a work session
- Most commands take an optional path to limit scope. Safe fixes apply by
  default; add `--ask` to review first, or `--prune` to also remove
  confirmed-safe branches/worktrees (after a permanent-loss check)
- Full details per command: `/repo:help <command>` or the files in
  `.claude/commands/repo/`
- Updating: `/repo:update-tools` (source: https://github.com/rjwalters/repo)

## Steps — with a command argument

Read `.claude/commands/repo/<name>.md` and summarize it for a user (not a
reimplementation): what it does, the usage lines, what it will and won't do
without confirmation, and one concrete example invocation. If the file isn't
installed, say so and list what is.

## Notes

- This is a read-only command: it never runs the other commands, only
  describes them
- Keep the overview short enough to read in one screen — the point is
  orientation, not documentation
