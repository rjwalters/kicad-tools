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
#   1. The canonical Repo Skills guard, IF it is present AND passes ALL FOUR
#      probes below. This is the preferred path in a repo that has Repo Skills
#      installed.
#   2. Otherwise the vendored generic guard shipped alongside this file
#      (guard-destructive-generic.sh), so standalone-Loom repos WITHOUT Repo
#      Skills — and any repo whose canonical guard fails any probe — keep full
#      destructive-command coverage.
#
# The four probes (#4894, #5916, #5974 — ALL are REQUIRED, not either/or):
#   a. VERSION probe — the canonical guard carries the rjwalters/repo#29 fix
#      (detected by the `repo#29` marker comment; presence/version probe, no
#      semver arithmetic).
#   b. CAPABILITY probe — the canonical guard also implements the Loom-only
#      Bash-tool **write-confinement** category (`>`/`>>` redirection, `tee`,
#      `sed -i`, `cp`/`mv` into a worktree-isolated main checkout, issue
#      #4178), detected by the same stable decision-tag the vendored guard
#      emits for that category ($WRITE_CONFINEMENT_MARKER below).
#   c. CAPABILITY probe (#5916) — the canonical guard also masks `gh --search`
#      and `jq --arg`/`--argjson` quoted values before the catastrophic/ask
#      substring scans (the #5797/#5803/#5809 fix already in
#      guard-destructive-generic.sh's strip_literal_text()). Detected by
#      $SEARCH_MASK_MARKER / $ARGJSON_MASK_MARKER below. Without this, a
#      canonical guard that passes probes (a) and (b) can still false-DENY an
#      idiomatic command like `gh issue list --search "docker system prune"
#      --jq '.[] | .number'` (a `|` inside a trailing single-quoted `--jq` arg,
#      after an already-masked `--search` value, gets misread as a real shell
#      pipe boundary during segment scanning).
#   d. CAPABILITY probe (#5974) — the canonical guard also carries the
#      ungated hard deny on `gh pr comment`/`gh issue comment --body @path`
#      (issue #4523: this shape posts the literal string `@path` instead of
#      expanding the file). Detected by $BODY_LITERAL_AT_MARKER below — the
#      same stable decision-tag guard-destructive-generic.sh passes to its
#      own deny() call for that rule. Without this, a canonical guard that
#      passes probes (a)-(c) can still silently drop the `--body @path`
#      protection entirely, with no warning and no override.
#
# Probe (a) alone used to be sufficient, but it only proves the canonical guard
# picked up an unrelated upstream fix — it says nothing about whether the
# Loom-only category actually runs. Once a consumer repo's Repo Skills install
# carried the repo#29 marker WITHOUT the write-confinement category (Repo
# Skills 0.7.0), the dispatcher would `exec` it and that category would stop
# running silently, with no warning and no override (#4894). Requiring probes
# (b), (c), and (d) too means the dispatcher only ever defers to a canonical
# guard that genuinely offers equal-or-better coverage; a canonical guard
# missing any one of the four still routes to the vendored fallback, which
# always carries all four.
#
# Probe (c) is INERT today (never yet passing): as of 2026-08-10,
# rjwalters/repo has not ported an equivalent search/jq masking fix upstream,
# so every real-world canonical guard fails this probe and the dispatcher
# falls back to the vendored guard (which already carries the fix) for every
# repo with Repo Skills installed. That is the intended behavior — the
# dispatcher must not exec a canonical guard that still has this false-DENY —
# and probe (c) starts actually discriminating only once rjwalters/repo ports
# a matching fix, at which point its marker text may need revisiting (see the
# probe's own comment below).
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

# CAPABILITY probe (c) markers (#5916). Unlike probe (b) above, the #5797/
# #5803/#5809 search/jq-value masking fix has no dedicated decision-tag
# literal to grep for — it lives entirely inside strip_literal_text()'s
# flag-value redaction regex, which never calls deny() with a stable tag —
# and rjwalters/repo has not (yet) ported an equivalent fix upstream, so
# there is no established canonical marker text either. The cheapest
# reliable proxy is two independent grep -qF substring checks for the
# adjacent flag pairs that appear ONLY inside this fix's regex alternation
# (see guard-destructive-generic.sh's strip_literal_text(), the `re =`
# assignment): `--comment|--search` (the `gh --search` alternative, #5797)
# AND `--arg|--argjson` (the `jq --arg`/`--argjson` alternative, #5797/
# #5803). BOTH must match — a canonical guard that masks only one of the two
# flag shapes still leaves the other false-DENYing, so partial coverage does
# not count. -F (fixed-string) matching is used because both markers contain
# a literal `|`, which BRE `grep` would otherwise NOT treat as alternation
# (so plain `grep -q` would technically still be correct here), but -F makes
# the intent — literal substring match, not a regex — unambiguous.
#
# False-positive risk: near-zero — a guard would need to independently
# reproduce this exact regex-alternation shape (not just mention
# --search/--arg/--argjson individually) to accidentally satisfy both greps.
# False-negative risk: an upstream port that fixes the same false-DENY with a
# differently-shaped regex (different flag ordering or a restructured
# alternation) would NOT be detected, and the dispatcher would keep
# preferring the vendored fallback even though the canonical guard is fixed.
# That is the safe failure direction (never worse than today; always at
# least the vendored guard's coverage) — this marker text is expected to
# need revisiting once rjwalters/repo actually ports an equivalent fix.
SEARCH_MASK_MARKER='--comment|--search'
ARGJSON_MASK_MARKER='--arg|--argjson'

# CAPABILITY probe (d) marker (#5974). Stable decision-tag the vendored
# guard's `gh pr comment`/`gh issue comment --body @path` hard deny passes to
# its own deny() call (issue #4523 — see the "gh-comment-body-literal-at"
# argument in guard-destructive-generic.sh's --body @path check). Grepping
# for this exact tag proves the canonical guard actually ported that rule,
# not just the unrelated version/write-confinement/search-mask fixes.
#
# Scope decision (issue #5974, item 3): this dispatcher gates on
# `gh-comment-body-literal-at` ALONE — it does NOT add separate probes for
# the three companion decision-tags guard-destructive-generic.sh also emits
# for the same underlying hazard (`gh-edit-body-literal-at` for `gh pr/issue
# edit --body @path`, and the `-var` suffixed tags for the same two rules
# reached through a shell-variable indirection, #4601/PR #4600). All four
# tags are added, changed, and removed together in guard-destructive-
# generic.sh's history — they are one feature (issue #4523's --body @path
# protection) implemented as four deny() call sites, never edited
# independently — so a single marker is an adequate, low-maintenance proxy
# for "did this canonical guard port issue #4523's fix at all", with the same
# safe failure direction as probe (c): a guard that somehow ported only a
# subset would fail this probe and route to the vendored fallback, never the
# reverse.
BODY_LITERAL_AT_MARKER='gh-comment-body-literal-at'

# Prefer the canonical guard ONLY when it carries the rjwalters/repo#29 fix
# (VERSION probe) AND independently implements the write-confinement
# category (CAPABILITY probe (b), #4894), the search/jq masking fix
# (CAPABILITY probe (c), #5916), AND the --body @path literal-string hard
# deny (CAPABILITY probe (d), #5974 — see the header comment above for why
# all four are required). The cheap bash-builtin `[[ -r ]]` test (zero
# forks) guards all five greps, so a repo without Repo Skills pays no extra
# process — preserving the guard's #3687 read-only fast path in that common
# case. In a dual-install repo the marker greps cost at most five forks per
# command before the canonical guard's own fast path runs; each grep -q/-qF
# short-circuits at the first match, and each later probe only runs once the
# earlier one has already matched.
if [[ -r "$CANONICAL_GUARD" ]] \
   && grep -q 'repo#29' "$CANONICAL_GUARD" 2>/dev/null \
   && grep -q "$WRITE_CONFINEMENT_MARKER" "$CANONICAL_GUARD" 2>/dev/null \
   && grep -qF -- "$SEARCH_MASK_MARKER" "$CANONICAL_GUARD" 2>/dev/null \
   && grep -qF -- "$ARGJSON_MASK_MARKER" "$CANONICAL_GUARD" 2>/dev/null \
   && grep -q -- "$BODY_LITERAL_AT_MARKER" "$CANONICAL_GUARD" 2>/dev/null; then
    exec bash "$CANONICAL_GUARD"
fi

# Fall back to the vendored generic guard (standalone-Loom repos, a repo whose
# Repo Skills copy predates the repo#29 fix, or a repo whose Repo Skills copy
# has the fix but not yet the write-confinement, search/jq masking, and/or
# --body @path literal-string categories, #4894, #5916, #5974).
if [[ -r "$VENDORED_GUARD" ]]; then
    exec bash "$VENDORED_GUARD"
fi

# Neither guard is available — allow (fail-open).
exit 0
