#!/usr/bin/env bash
# guard-destructive.sh — PreToolUse guard DISPATCHER (Loom-specific glue, #4041)
#
# The generic destructive-command pattern list this file used to contain has its
# canonical home in Repo Skills (https://github.com/rjwalters/repo), installed
# into consumer repos at .claude/skills/repo/hooks/guard-destructive.sh. That
# canonical guard carries the rjwalters/repo#29 curl-pipe false-positive fix and
# is the general-by-design tool installed in many non-Loom repos, so Loom defers
# to it instead of shipping (and separately maintaining) a second generic guard.
#
# This dispatcher decides at RUNTIME which generic guard to run:
#   1. The canonical Repo Skills guard, IF it is present AND passes BOTH of the
#      probes below. This is the preferred path in a repo that has Repo Skills
#      installed.
#   2. Otherwise the vendored generic guard shipped alongside this file
#      (guard-destructive-generic.sh), so standalone-Loom repos WITHOUT Repo
#      Skills — and any repo whose canonical guard fails either probe — keep
#      full destructive-command coverage.
#
# The two probes (#4894 — both are REQUIRED, not either/or):
#   a. VERSION probe — the canonical guard carries the rjwalters/repo#29 fix
#      (detected by the `repo#29` marker comment; presence/version probe, no
#      semver arithmetic).
#   b. CAPABILITY probe — the canonical guard also implements the Loom-only
#      Bash-tool **write-confinement** category (`>`/`>>` redirection, `tee`,
#      `sed -i`, `cp`/`mv` into a worktree-isolated main checkout, issue
#      #4178), detected by the same stable decision-tag the vendored guard
#      emits for that category ($WRITE_CONFINEMENT_MARKER below).
#
# Probe (a) alone used to be sufficient, but it only proves the canonical guard
# picked up an unrelated upstream fix — it says nothing about whether the
# Loom-only category actually runs. Once a consumer repo's Repo Skills install
# carried the repo#29 marker WITHOUT the write-confinement category (Repo
# Skills 0.7.0), the dispatcher would `exec` it and that category would stop
# running silently, with no warning and no override (#4894). Requiring probe
# (b) too means the dispatcher only ever defers to a canonical guard that
# genuinely offers equal-or-better coverage; a canonical guard that has the
# version fix but not (yet) the write-confinement category still routes to the
# vendored fallback, which always carries it.
#
# Exactly one generic guard runs; never zero. Because the choice is made here at
# runtime rather than by rewriting .claude/settings.json, this file stays the
# `${CLAUDE_PROJECT_DIR}/.loom/hooks/guard-destructive.sh` entry in settings —
# so Loom-ownership detection, the settings.json merge, and uninstall are all
# preserved, and there is never a window with zero generic guard wired even when
# Repo Skills' own coexistence-aware installer defers to Loom's entry.
#
# Loom-specific enforcement (the `gh pr merge` → merge-pr.sh redirect, the
# worktree pip-install block) lives in the separate guard-loom-workflow.sh hook;
# worktree path confinement lives in guard-worktree-paths.sh. Those stay
# Loom-owned and are unaffected by this dispatcher.
#
# Contract (same as any guard): reads the PreToolUse JSON on stdin, MUST never
# exit non-zero, and either `exec`s the resolved guard (which emits the
# deny/ask/allow decision from the same stdin) or exits 0 (allow) when no guard
# is available. Fail-open on any unexpected error.

# Fail-open: any unexpected error resolves to allow (exit 0), never breaks the
# tool call or wedges Claude Code in a retry loop.
trap 'exit 0' ERR

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd 2>/dev/null || echo ".")"

# Resolve the consuming repo's root, so CANONICAL_GUARD (below) points at the
# right `.claude/skills/repo/hooks/...` regardless of WHERE this dispatcher
# itself lives:
#
#   - Legacy/project-level wiring: SCRIPT_DIR is <repo>/.loom/hooks (the
#     settings entry resolves to the main worktree's copy even from a linked
#     worktree), so ../../ is the repo root. This is the historical behavior,
#     preserved byte-for-byte when LOOM_PROJECT_ROOT is unset.
#   - Machine-level wiring (Epic #3835 Phase 5, #4262): this file runs from
#     the shared checkout (SCRIPT_DIR is <checkout>/defaults/hooks), where
#     SCRIPT_DIR-relative resolution would point outside the consuming repo
#     entirely. The user-scope command wrapper (provision-hooks.sh) resolves
#     the worktree-aware repo root BEFORE exec'ing this dispatcher and passes
#     it via LOOM_PROJECT_ROOT, so that root is preferred when set.
#
# VENDORED_GUARD is always a SCRIPT_DIR-relative sibling — correct in both
# layouts, since guard-destructive-generic.sh ships alongside this dispatcher
# either way.
CANONICAL_ROOT="${LOOM_PROJECT_ROOT:-$SCRIPT_DIR/../..}"
CANONICAL_GUARD="$CANONICAL_ROOT/.claude/skills/repo/hooks/guard-destructive.sh"
# Vendored copy of the canonical guard, shipped by Loom for standalone repos.
VENDORED_GUARD="$SCRIPT_DIR/guard-destructive-generic.sh"

# Stable decision-tag the vendored guard's write-confinement category passes to
# its own deny() call (see the "BASH-TOOL WRITE CONFINEMENT" section of
# guard-destructive-generic.sh, issue #4178). Grepping for this exact tag is
# the CAPABILITY probe (#4894): it is present in the vendored file today, and
# Loom's release-time re-vendoring step preserves the whole write-confinement
# section (including this tag) verbatim, so the probe only ever starts passing
# against a canonical Repo Skills guard once that guard genuinely implements
# the same category — never as a side effect of an unrelated upstream change.
WRITE_CONFINEMENT_MARKER='worktree-write-confinement'

# Prefer the canonical guard ONLY when it carries the rjwalters/repo#29 fix
# (VERSION probe) AND independently implements the write-confinement category
# (CAPABILITY probe, #4894 — see the header comment above for why both are
# required). The cheap bash-builtin `[[ -r ]]` test (zero forks) guards both
# greps, so a repo without Repo Skills pays no extra process — preserving the
# guard's #3687 read-only fast path in that common case. In a dual-install repo
# the two marker greps cost at most two forks per command before the canonical
# guard's own fast path runs; each grep -q short-circuits at the first match,
# and the capability grep only runs once the version grep has already matched.
if [[ -r "$CANONICAL_GUARD" ]] \
   && grep -q 'repo#29' "$CANONICAL_GUARD" 2>/dev/null \
   && grep -q "$WRITE_CONFINEMENT_MARKER" "$CANONICAL_GUARD" 2>/dev/null; then
    exec bash "$CANONICAL_GUARD"
fi

# Fall back to the vendored generic guard (standalone-Loom repos, a repo whose
# Repo Skills copy predates the repo#29 fix, or a repo whose Repo Skills copy
# has the fix but not yet the write-confinement category, #4894).
if [[ -r "$VENDORED_GUARD" ]]; then
    exec bash "$VENDORED_GUARD"
fi

# Neither guard is available — allow (fail-open).
exit 0
