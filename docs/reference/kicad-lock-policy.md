# KiCad lock-marker advisories

The core `save_pcb`, `save_schematic`, `save_project`, `save_footprint`, and
`save_design_rules` functions check for KiCad's exact sibling marker before
writing: `~<full filename>.lck`.
For `my board.rev2.kicad_pcb`, this is `~my board.rev2.kicad_pcb.lck`.
The CLI routing output and interrupted routing snapshot writers also check their actual destination,
including the `_partial` filename when applicable. Other write paths are not
covered by this policy. In particular, a caller that saves to a staging filename
must check its eventual destination separately before replacing it.

The default policy prints a warning to **stderr** and proceeds with the write.
This preserves batch workflows when KiCad has left an abandoned marker. It also
keeps JSON stdout clean. Set `KCT_KICAD_LOCK_POLICY` to select another policy:

| Value | Behavior |
| --- | --- |
| `warn` | Default: warn on marker presence, then write normally. |
| `error` | Raise `PermissionError` before creating temporary output or replacing the destination. Interrupted routing returns a failed-save result. |
| `ignore` | Skip the advisory check and write normally. |

Values are lowercase and exact; invalid values raise `ValueError`, even when
no marker exists. Python callers of `core.kicad_lock.check_kicad_lock` may pass
`policy=` explicitly, which takes precedence over the environment. Public save
functions and routing writers use the shared environment setting.

`probe_kicad_lock(path)` returns the marker path, presence, and optional
`username` and `hostname` strings. These are unverified ownership metadata.
Empty, malformed, oversized, unreadable, and nonregular markers are treated as
present with unknown ownership. If marker existence cannot be inspected due to
an operating-system error, the check conservatively treats it as present.
Neither the probe nor the policy changes, deletes, or reclaims a marker.

**Presence does not prove a live KiCad session.** KiCad's marker contains no PID;
matching the current user or hostname does not exempt a marker. This check is
advisory and has a race window: another process can open the destination after
the check. It does not acquire an exclusive lock or add multi-file transaction
protection. The existing atomic file writes remain unchanged.

The filename and ownership-field conventions were checked against KiCad's
[LOCKFILE source documentation](https://docs.kicad.org/doxygen/lockfile_8h_source.html)
and [file-extension constants](https://docs.kicad.org/doxygen/wildcards__and__files__ext_8cpp_source.html).
The implementation is independent; these sources establish the file convention.
