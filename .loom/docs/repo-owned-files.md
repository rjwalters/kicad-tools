# Repo-owned files inside `.loom/`

`.loom/` is a Loom-managed tree, but it is not *exclusively* Loom's. Some of it
is an **extension point**: `.loom/hooks/post-worktree.sh`, for example, is a
file Loom never ships and always invokes (`worktree.sh` runs it after creating
a worktree), so a repo that wants per-worktree setup is *expected* to add its
own file there.

This page states the ownership rule the installer applies, and how a repo
declares "this file is mine" so an upgrade cannot delete it.

## The rule

On reinstall (`install.sh --confirm-reinstall`, `loom update`, a direct
`loom-daemon init --force`), Loom cleans each managed `.loom/` directory —
`roles/`, `scripts/`, `hooks/`, `docs/`, `runtimes/`, `bin/` — before copying
the new version in. A file already sitting in one of those directories is
**deleted only when Loom can attribute it to itself**:

| Evidence | Outcome |
|---|---|
| The current `defaults/` tree ships a file at that path | **Removed**, then immediately re-copied (net effect: refreshed) |
| The path is pinned in `.loom/resync-ignore` | **Preserved** — declared repo-owned |
| The path is listed in `.loom/install-metadata.json`'s `installed_files` | **Removed** — Loom installed it, so Loom may retire it |
| None of the above | **Preserved** and reported as unmanaged |

The last row is the important one: **no evidence means no deletion.** A stale
Loom file that survives is cosmetic drift, and the manifest-driven sweeps in
`scripts/install-loom.sh` and `scripts/uninstall-loom.sh` still retire it. A
deleted repo file is unrecoverable, and — for a hook — silently stops firing.
The two failure modes are not symmetric, so the installer errs toward keeping.

Every file the installer removes, and every file it preserves under this rule,
is **named in the installer's output**. Nothing in `.loom/` is deleted silently.

### The one place the rule is looser: an explicit `--clean` uninstall

`uninstall-loom.sh --clean` is an operator saying "wipe the managed
directories, *including* files the manifest never recorded". That request is
honored — with two exceptions:

- a path pinned in `.loom/resync-ignore` is never removed (the declaration is
  the opt-out), and
- inside `.loom/hooks/`, a file the current `defaults/` does not ship is
  preserved even without a pin. Hook scripts can never appear in
  `installed_files` (#4262), so "unrecognized" there carries no information.

Everything preserved is named in the output, same as above.

## Declaring a file repo-owned

List its `.loom/`-relative path, one per line, in `.loom/resync-ignore`:

```
# .loom/resync-ignore — paths Loom must not overwrite or delete
hooks/post-worktree.sh
scripts/project-local-helper.sh
```

Blank lines and `#` comments are ignored; matching is exact (no globs), the
same semantics `resync-installed.sh` already uses.

This is the same file that pins a customization against being *overwritten* by
`./.loom/scripts/resync-installed.sh`; it now also pins it against being
*deleted* by the installer's clean sweep. One list, one meaning: **this path is
the repo's, not Loom's.**

Since #7995 a `PreToolUse` guard reads this list too. In a repo that is not
Loom's own source tree, an agent writing to an **unpinned** path under
`.loom/hooks|scripts|roles|docs|bin/` or `.claude/commands/loom/` is denied —
through the `Edit`/`Write` tools and through the Bash write idioms alike — with
a message naming the two dispositions above. Adding the path here is what turns
that denial off for that one file, because the pin is the repo declaring the
file its own. See [`guard-hooks.md`](guard-hooks.md) §"Installed-File Write
Guard" for the discriminator that decides "consumer repo vs. Loom's own tree"
and for the category toggle.

Commit `.loom/resync-ignore` — it is repo configuration, and the installer
never removes it.

### When you do and do not need it

- **A file Loom never ships** (`hooks/post-worktree.sh`, a project helper
  script) is already preserved on the reinstall path by the "no evidence" rule
  above. Pinning it is still worth doing: it is the only thing that protects a
  non-`hooks/` file from an explicit `--clean`, it turns an implicit outcome
  into a declared one, it moves the file out of the "unmanaged, review me"
  section of the installer's output, and it protects the file from a legacy
  over-broad `installed_files` manifest that wrongly claims Loom wrote it.
- **A file Loom *does* ship**, customized in place (e.g. your own edit of
  `hooks/guard-destructive.sh`), cannot be protected from the installer by a
  pin — the reinstall re-copies the shipped version over it by design. Pinning
  it does stop `resync-installed.sh` from overwriting it between installs. If
  you need a durable fork, give it a name Loom does not ship and wire that name
  up instead.

## Why not just delete anything unrecognized?

That is what the sweep used to do, and it deleted a consumer repo's own
`.loom/hooks/post-worktree.sh` during a routine version upgrade (issue #5971).
Nothing in `.loom/hooks/` is even eligible to appear in `installed_files` —
hook scripts are deliberately excluded from the install manifest (#4262) — so
"absent from the manifest" can never mean "not yours" for that directory.
