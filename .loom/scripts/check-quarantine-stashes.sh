#!/usr/bin/env bash
# check-quarantine-stashes.sh - Surface outstanding `loom-quarantine:` stashes.
#
# This is a NON-BLOCKING, advisory check, mirroring check-host-sleep.sh (#3350)
# and check-main-freshness.sh (#3770) in structure and contract.
#
# check-main-clean.sh --quarantine (see the Wave Lifecycle "Backstop" step in
# defaults/.claude/commands/loom/sweep.md) rescues contamination it finds in the
# primary clone's working tree into a labeled `git stash` entry rather than
# discarding it — by design, this never loses data. But the quarantine is
# recorded only in a structured JSON log line
# (.loom/logs/main-quarantine.log); nothing surfaces that a rescue stash is
# outstanding. Absent an operator who happens to notice, a labeled
# `loom-quarantine:` stash can sit indefinitely with nobody aware there is
# quarantined work to reconcile (#5185).
#
# refs/stash is shared across every worktree of a repo (not per-worktree —
# see the #4821 note in defaults/.claude/commands/loom/sweep.md), so this
# check is meaningful regardless of which worktree it runs from.
#
# It MUST NOT block — even if git fails, it returns 0. It is strictly
# read-only: it never pops, drops, or applies a stash.
#
# Usage:
#   ./.loom/scripts/check-quarantine-stashes.sh          # print warning (or nothing) and exit 0
#   ./.loom/scripts/check-quarantine-stashes.sh --quiet   # suppress the stdout one-liner
#   ./.loom/scripts/check-quarantine-stashes.sh --help    # show usage
#
# Exit codes:
#   0 - Always. This script is advisory; it never blocks Loom.
#
# See also: check-host-sleep.sh (#3350), check-main-freshness.sh (#3770) — the
# sibling pre-flight advisories this script mirrors in structure and contract.

set -uo pipefail  # NOTE: no -e — this script must never exit non-zero

# ---------- output helpers ----------

# Colors (only when stderr is a tty)
if [[ -t 2 ]]; then
    YELLOW='\033[1;33m'
    GREEN='\033[1;32m'
    BOLD='\033[1m'
    NC='\033[0m'
else
    YELLOW=''
    GREEN=''
    BOLD=''
    NC=''
fi

QUIET=0
for arg in "$@"; do
    case "$arg" in
        --quiet|-q)
            QUIET=1
            ;;
        --help|-h)
            sed -n '2,29p' "$0" | sed 's/^# //; s/^#//'
            exit 0
            ;;
        *)
            # Unknown args are ignored — this script must never fail.
            ;;
    esac
done

warn() {
    # Print a multi-line warning block to stderr. Always returns 0.
    printf '%b\n' "$*" >&2 || true
}

info_oneliner() {
    # Print a single status line to stdout (suppressed by --quiet).
    if [[ "$QUIET" -eq 0 ]]; then
        printf '%b\n' "$*" || true
    fi
}

# ---------- pre-flight: must be inside a git repo ----------

if ! git rev-parse --git-dir >/dev/null 2>&1; then
    info_oneliner "${YELLOW}[quarantine-check] not inside a git repository; skipping.${NC}"
    exit 0
fi

# ---------- collect loom-quarantine: stash entries ----------
#
# `git stash list` never dates its entries; `git log -g` over refs/stash does
# (via the reflog), and %gs (reflog subject) is the same "On <branch>: <msg>"
# text `git stash list` shows. Reading the reflog directly also survives a
# repo with zero stashes (an empty/absent refs/stash) without erroring.
#
# NOTE: do NOT pass --date=relative here — it leaks into %gd itself, turning
# the copy-pasteable "stash@{0}" selector into "stash@{49 minutes ago}" (not
# a valid ref). %cr already gives a relative date independent of --date.

STASH_LINES=""
if git rev-parse --verify --quiet refs/stash >/dev/null 2>&1; then
    STASH_LINES="$(git log -g --format='%gd|%cr|%gs' refs/stash 2>/dev/null || true)"
fi

if [[ -z "$STASH_LINES" ]]; then
    info_oneliner "${GREEN}[quarantine-check] no outstanding loom-quarantine: stashes.${NC}"
    exit 0
fi

QUARANTINE_LINES="$(printf '%s\n' "$STASH_LINES" | grep 'loom-quarantine:' || true)"

if [[ -z "$QUARANTINE_LINES" ]]; then
    info_oneliner "${GREEN}[quarantine-check] no outstanding loom-quarantine: stashes.${NC}"
    exit 0
fi

COUNT="$(printf '%s\n' "$QUARANTINE_LINES" | grep -c '.' || true)"
[[ "$COUNT" =~ ^[0-9]+$ ]] || COUNT=0

# ---------- outstanding quarantines found: warn (non-blocking) ----------

warn ""
warn "${YELLOW}${BOLD}========================================================================${NC}"
warn "${YELLOW}${BOLD}  WARNING: ${COUNT} outstanding loom-quarantine: stash(es) found (#5185)${NC}"
warn "${YELLOW}${BOLD}========================================================================${NC}"
warn "${YELLOW}check-main-clean.sh --quarantine rescued contaminated main-worktree${NC}"
warn "${YELLOW}changes into these stashes rather than discarding them. Nothing was${NC}"
warn "${YELLOW}lost, but nobody has reconciled them back into their owning issue yet.${NC}"
warn ""

while IFS='|' read -r ref age subject; do
    [[ -n "$ref" ]] || continue
    # subject looks like "On main: loom-quarantine: run=... issue=N [...]" —
    # strip the "On <branch>: " prefix so the label reads cleanly.
    label="${subject#On*: }"
    warn "${YELLOW}  ${BOLD}${ref}${NC}${YELLOW} (${age}): ${label}${NC}"
done <<< "$QUARANTINE_LINES"

warn ""
warn "${BOLD}To inspect a quarantine's contents:${NC}"
warn "      ${BOLD}git stash show -p <ref>${NC}"
warn "${BOLD}To reconcile it into the issue worktree it belongs to, then drop it:${NC}"
warn "      ${BOLD}git stash apply <ref>${NC}   # or: git stash pop <ref>"
warn "${BOLD}Structured records (label, paths, stash sha) for every quarantine:${NC}"
warn "      ${BOLD}.loom/logs/main-quarantine.log${NC}"
warn ""
warn "${YELLOW}========================================================================${NC}"
warn ""

info_oneliner "${YELLOW}[quarantine-check] WARNING: ${COUNT} outstanding loom-quarantine: stash(es). See stderr for details.${NC}"

# Always succeed — this script is advisory only.
exit 0
