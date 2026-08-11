#!/usr/bin/env bash
# resync-installed.sh — refresh this repo's installed Repo Skills surfaces from
# the source clone they were installed from.
#
# This is requirement **C7** of INSTALLER-CONTRACT.md (the tool-package installer
# contract this repo owns): a consumer-side, non-destructive refresh the consumer
# can run itself, rather than a full installer invocation driven from outside.
#
#   Usage: resync-installed.sh [--dry-run] [--quiet] [--source <path>] [--target <path>]
#
#   --dry-run, -n     Report the drift and change nothing. Exit 2 if any file
#                     would be created/updated, 0 if already in sync.
#   --quiet, -q       Print only warnings, errors, and the one-line summary.
#   --source <path>   Source clone to resync from. Default: resolved from the
#                     machine-local sidecar, then the legacy inline field (the
#                     C6 order — see "Resolving the source" below).
#   --target <path>   Consumer repo to refresh. Default: the git top level of the
#                     current working directory.
#   --help, -h        This text.
#
# Exit status: 0 = in sync (or successfully applied); 2 = --dry-run found drift;
# 1 = error (nothing installed here, source unresolvable, or a write failed).
# These are the same three codes /repo:update-tools already documents for Loom's
# resync, so a caller can drive either tool with one branch.
#
# WHAT IT TOUCHES — the pure-copy surface map install.sh writes, and nothing else:
#   .claude/skills/repo/SKILL.md                        <- skills/repo/SKILL.md
#   .claude/skills/repo/hooks/*.sh                      <- hooks/repo/*.sh
#   .claude/skills/repo/scripts/repo-remote.sh          <- scripts/repo/repo-remote.sh
#   .claude/skills/repo/scripts/resync-installed.sh     <- scripts/repo/resync-installed.sh
#   .claude/commands/repo/<cmd>.md                      <- commands/repo/<cmd>.md
#
# EXPLICITLY OUT OF SCOPE (owned by install.sh / uninstall.sh, not by resync):
#   .claude/settings.json  - hook wiring is a JSON merge into a file the consumer
#                            also owns; re-run install.sh if the wiring is missing
#   CLAUDE.md              - marker-bounded block, rewritten by install.sh
#   .gitignore             - the sidecar-ignore entry, written by install.sh
#   config.json            - consumer-owned guard toggles; never generated here
#
# IT NEVER UNINSTALLS. There is no code path in this file that removes a file in
# the target: the only `rm` calls target temp files this script itself created.
# A file under the installed surfaces with no counterpart in the source (a
# consumer-local command, a command dropped upstream, a stale copy) is reported
# and LEFT ALONE. Removing installed surfaces is uninstall.sh's job, and it must
# stay a separate, explicit action — a refresh that can delete is not a refresh.
#
# RESOLVING THE SOURCE (contract C6 order, implemented rather than restated):
#   1. --source <path>, when given.
#   2. .claude/skills/repo/.install-local.json -> "source" (the gitignored,
#      machine-local sidecar).
#   3. install-metadata.json -> "source" (pre-split installs embedded it inline).
#   4. Otherwise: error. A source clone is required — this script refreshes FROM
#      something; it cannot invent the new bytes.
#
# ATOMIC WRITES + DEFERRED SELF-UPDATE: every file is written by rendering into a
# temp file NEXT TO its destination and rename(2)-ing it into place, never by
# truncating the destination. That matters most for this script, which is itself
# one of the files it refreshes: a rename swaps the directory entry and leaves
# the inode the running bash is still reading from intact. Belt-and-braces, the
# self-copy is also applied LAST, after every other surface has settled.
#
# NOT Loom's resync-installed.sh. Loom ships a same-named script under
# .loom/scripts/ that always resolves its target against the PRIMARY worktree,
# which is why Loom's Builder role forbids running it from an issue worktree.
# This one has no such hazard: `.claude/` is an ordinary per-worktree directory,
# so the target is simply the git top level of the current working directory and
# writes land where you are standing. There is no --allow-worktree escape hatch
# because there is nothing to escape.
#
# WORKS WITHOUT jq. jq is used when present; otherwise a flat-JSON fallback reads
# the same fields, because install.sh generates these files in a fixed shape and
# a consumer-side script must not hard-depend on a tool the consumer may lack.

set -uo pipefail

RED=''; GREEN=''; BLUE=''; YELLOW=''; NC=''
if [[ -t 1 ]]; then
  RED='\033[0;31m'; GREEN='\033[0;32m'; BLUE='\033[0;34m'; YELLOW='\033[1;33m'; NC='\033[0m'
fi

QUIET=false
DRY_RUN=false
SOURCE_OPT=""
TARGET_OPT=""

die()     { echo -e "${RED}✗ Error: $*${NC}" >&2; exit 1; }
warn()    { echo -e "${YELLOW}⚠ $*${NC}" >&2; }
info()    { [[ "$QUIET" == true ]] || echo -e "${BLUE}ℹ $*${NC}"; }
say()     { [[ "$QUIET" == true ]] || echo -e "$*"; }
success() { [[ "$QUIET" == true ]] || echo -e "${GREEN}✓ $*${NC}"; }

usage() { sed -n '/^#   Usage:/,/^#   --help/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    -n|--dry-run)  DRY_RUN=true ;;
    -q|--quiet)    QUIET=true ;;
    --source)      shift; [[ $# -gt 0 ]] || die "--source requires a path"; SOURCE_OPT="$1" ;;
    --source=*)    SOURCE_OPT="${1#--source=}" ;;
    --target)      shift; [[ $# -gt 0 ]] || die "--target requires a path"; TARGET_OPT="$1" ;;
    --target=*)    TARGET_OPT="${1#--target=}" ;;
    -h|--help)     usage; exit 0 ;;
    -*)            die "Unknown option: $1 (see --help)" ;;
    *)             die "Unexpected argument '$1' — the target is --target <path>, not a positional (see --help)" ;;
  esac
  shift
done

# ---------------------------------------------------------------------------
# Flat-JSON readers. jq when available; otherwise a sed fallback over the fixed
# shape install.sh emits. Both return empty (never an error) for a missing key
# so callers can use `:-` defaults.
# ---------------------------------------------------------------------------
have_jq() { command -v jq >/dev/null 2>&1; }

json_string() {  # <file> <key> -> string value, or empty
  [[ -f "$1" ]] || return 0
  if have_jq; then
    jq -r --arg k "$2" 'if (.[$k]? | type) == "string" then .[$k] else empty end' "$1" 2>/dev/null
  else
    sed -n "s/.*\"$2\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p" "$1" | head -n1
  fi
}

json_bool() {  # <file> <key> -> true|false, or empty
  [[ -f "$1" ]] || return 0
  if have_jq; then
    jq -r --arg k "$2" 'if (.[$k]? | type) == "boolean" then (.[$k] | tostring) else empty end' "$1" 2>/dev/null
  else
    sed -n "s/.*\"$2\"[[:space:]]*:[[:space:]]*\(true\|false\).*/\1/p" "$1" | head -n1
  fi
}

json_number() {  # <file> <key> -> numeric value, or empty
  [[ -f "$1" ]] || return 0
  if have_jq; then
    jq -r --arg k "$2" 'if (.[$k]? | type) == "number" then (.[$k] | tostring) else empty end' "$1" 2>/dev/null
  else
    sed -n "s/.*\"$2\"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p" "$1" | head -n1
  fi
}

json_string_array() {  # <file> <key> -> one element per line
  [[ -f "$1" ]] || return 0
  if have_jq; then
    jq -r --arg k "$2" 'if (.[$k]? | type) == "array" then .[$k][] else empty end' "$1" 2>/dev/null
  else
    sed -n "s/.*\"$2\"[[:space:]]*:[[:space:]]*\[\([^]]*\)\].*/\1/p" "$1" \
      | head -n1 | tr ',' '\n' | sed 's/^[[:space:]]*"//; s/"[[:space:]]*$//' | sed '/^$/d'
  fi
}

# ---------------------------------------------------------------------------
# Resolve the target repo, and prove Repo Skills was actually installed into it.
# ---------------------------------------------------------------------------
if [[ -n "$TARGET_OPT" ]]; then
  TARGET="$(cd "$TARGET_OPT" 2>/dev/null && pwd -P)" || die "--target directory does not exist: $TARGET_OPT"
else
  TARGET="$(git rev-parse --show-toplevel 2>/dev/null)" || TARGET=""
  [[ -n "$TARGET" ]] || TARGET="$PWD"
  TARGET="$(cd "$TARGET" && pwd -P)"
fi

SKILL_ROOT="$TARGET/.claude/skills/repo"
METADATA="$SKILL_ROOT/install-metadata.json"
SIDECAR="$SKILL_ROOT/.install-local.json"

# Fail loudly rather than silently creating a partial install. Resync REFRESHES
# an existing install; it is not a second, quieter installer. Bootstrapping a
# repo is install.sh's job, and conflating the two would let a typo'd --target
# scatter half an install across an unrelated directory.
if [[ ! -f "$METADATA" ]]; then
  die "No Repo Skills install found in $TARGET (expected .claude/skills/repo/install-metadata.json).
       resync refreshes an existing install; run <source>/install.sh '$TARGET' to install it first."
fi

# ---------------------------------------------------------------------------
# Resolve the source clone (contract C6 order: sidecar -> legacy inline).
# ---------------------------------------------------------------------------
SOURCE_ORIGIN=""
if [[ -n "$SOURCE_OPT" ]]; then
  SOURCE_ROOT="$SOURCE_OPT"; SOURCE_ORIGIN="--source"
else
  SOURCE_ROOT="$(json_string "$SIDECAR" source)"
  if [[ -n "$SOURCE_ROOT" ]]; then
    SOURCE_ORIGIN="sidecar (.install-local.json)"
  else
    SOURCE_ROOT="$(json_string "$METADATA" source)"
    [[ -n "$SOURCE_ROOT" ]] && SOURCE_ORIGIN="legacy inline field (install-metadata.json)"
  fi
fi

if [[ -z "$SOURCE_ROOT" ]]; then
  # This is exactly the repo#96 signature /repo:update-tools reports: installed
  # here once, but the machine-local pointer is gone (typically deleted by
  # pulling a commit that untracked it).
  die "Source clone unknown: no --source given, no $SIDECAR, and no legacy inline 'source' in install-metadata.json.
       Pass --source /path/to/repo-skills, or re-run that clone's install.sh once to regenerate the sidecar."
fi

SOURCE_ROOT="$(cd "$SOURCE_ROOT" 2>/dev/null && pwd -P)" \
  || die "Recorded source clone no longer exists on disk (from $SOURCE_ORIGIN). Pass --source /path/to/repo-skills."
[[ -f "$SOURCE_ROOT/install.sh" && -f "$SOURCE_ROOT/skills/repo/SKILL.md" ]] \
  || die "$SOURCE_ROOT does not look like a Repo Skills clone (no install.sh + skills/repo/SKILL.md)."
[[ "$SOURCE_ROOT" != "$TARGET" ]] \
  || die "Source and target are the same directory ($TARGET) — nothing to resync from."

# ---------------------------------------------------------------------------
# Rendering. Shared with install.sh so a resynced file is byte-identical to a
# freshly-installed one (lib/render.sh exists precisely so these cannot drift).
# ---------------------------------------------------------------------------
if [[ -f "$SOURCE_ROOT/lib/render.sh" && -f "$SOURCE_ROOT/lib/metadata.sh" ]]; then
  # shellcheck source=../../lib/render.sh
  source "$SOURCE_ROOT/lib/render.sh"
  # shellcheck source=../../lib/metadata.sh
  source "$SOURCE_ROOT/lib/metadata.sh"
else
  die "$SOURCE_ROOT/lib/ is missing render.sh and/or metadata.sh — that clone predates the shared emitters.
       Pull that clone and retry, or re-run its install.sh."
fi

VERSION="$(cat "$SOURCE_ROOT/VERSION" 2>/dev/null || echo unknown)"
COMMIT="$(git -C "$SOURCE_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
# The install-date token is stamped once, at install time. Re-deriving it as
# "today" would make every rendered file differ on every calendar day, turning a
# no-op resync into permanent phantom drift — so reuse the recorded install date
# and only fall back to today when there is none to reuse. (This file is itself
# rendered on the way into a consumer repo, which is why the token is described
# rather than spelled out here: a literal one in a comment would be substituted.)
INSTALL_DATE="$(json_string "$SIDECAR" installed_at)"
INSTALL_DATE="${INSTALL_DATE%%T*}"
[[ -n "$INSTALL_DATE" ]] || INSTALL_DATE="$(json_string "$METADATA" installed_at)"
INSTALL_DATE="${INSTALL_DATE%%T*}"
[[ -n "$INSTALL_DATE" ]] || INSTALL_DATE="$(date -u +%Y-%m-%d)"
render_repo_identity "$TARGET"

INSTALLED_VERSION="$(json_string "$METADATA" version)"
DEV_INSTALL="$(json_bool "$METADATA" dev)"
FILTERED="$(json_bool "$METADATA" filtered)"

# A layout bump means destinations moved or a metadata field changed meaning —
# things a pure file refresh cannot fix. Warn loudly and keep going (the refresh
# is still an improvement over stale files) rather than refusing outright.
INSTALLED_LAYOUT="$(json_number "$METADATA" layout_version)"
if [[ -n "$INSTALLED_LAYOUT" && "$INSTALLED_LAYOUT" != "$REPO_SKILLS_LAYOUT_VERSION" ]]; then
  warn "Layout version differs (installed $INSTALLED_LAYOUT, source $REPO_SKILLS_LAYOUT_VERSION):"
  warn "a resync only refreshes file contents. Re-run '$SOURCE_ROOT/install.sh $TARGET' to pick up"
  warn "moved destinations or changed wiring."
fi

# ---------------------------------------------------------------------------
# Build the plan. Parallel arrays (not associative) so this runs on bash 3.2,
# which is still what macOS ships.
# ---------------------------------------------------------------------------
PLAN_SRC=(); PLAN_DST=(); PLAN_EXEC=()

plan() {  # <source-rel> <dest-rel> <exec:0|1>
  PLAN_SRC+=("$1"); PLAN_DST+=("$2"); PLAN_EXEC+=("$3")
}

plan "skills/repo/SKILL.md"                 ".claude/skills/repo/SKILL.md"                    0
plan "hooks/repo/guard-destructive.sh"      ".claude/skills/repo/hooks/guard-destructive.sh"  1
plan "hooks/repo/session-start-handoff.sh"  ".claude/skills/repo/hooks/session-start-handoff.sh" 1
plan "scripts/repo/repo-remote.sh"          ".claude/skills/repo/scripts/repo-remote.sh"      1

# Which commands belong to this install. A `--skills=` install is a deliberate
# subset, so widening it here would install commands the operator declined; an
# unfiltered install should pick up commands added upstream since, so restricting
# it to the recorded list would make resync unable to deliver new commands at
# all. `filtered` (written by install.sh) is what distinguishes the two. Older
# metadata predates the field: treat it as unfiltered, which is the common case
# and errs toward delivering rather than withholding.
COMMANDS=""
if [[ "$FILTERED" == "true" ]]; then
  COMMANDS="$(json_string_array "$METADATA" commands)"
else
  for f in "$SOURCE_ROOT"/commands/repo/*.md; do
    [[ -f "$f" ]] || continue
    COMMANDS+="$(basename "$f" .md)"$'\n'
  done
fi
COMMANDS="$(printf '%s' "$COMMANDS" | sed '/^$/d' | sort -u)"
[[ -n "$COMMANDS" ]] || die "No commands resolved for this install (metadata 'commands' empty and no commands/repo/*.md in $SOURCE_ROOT)."

while IFS= read -r cmd; do
  [[ -n "$cmd" ]] || continue
  plan "commands/repo/$cmd.md" ".claude/commands/repo/$cmd.md" 0
done <<<"$COMMANDS"

# Deferred self-update: this script is one of the files it refreshes, so it goes
# last (see the header). rename(2) already makes the swap safe; ordering makes it
# obviously safe.
plan "scripts/repo/resync-installed.sh" ".claude/skills/repo/scripts/resync-installed.sh" 1

# ---------------------------------------------------------------------------
# Apply (or, under --dry-run, evaluate) the plan.
# ---------------------------------------------------------------------------
N_CREATED=0; N_UPDATED=0; N_UNCHANGED=0; N_SKIPPED=0; N_FAILED=0
FAILED_PATHS=()
SCRATCH=""
cleanup() { [[ -n "$SCRATCH" && -d "$SCRATCH" ]] && rm -rf "$SCRATCH"; }
trap cleanup EXIT

report() {  # <verb> <colour> <dest-rel> [detail]
  say "  $(printf '%b%-9s%b %s%s' "$2" "$1" "$NC" "$3" "${4:+  ($4)}")"
}

sync_one() {  # <source-rel> <dest-rel> <exec:0|1>
  local src="$SOURCE_ROOT/$1" dst="$TARGET/$2" is_exec="$3" tmp dstdir

  if [[ ! -f "$src" ]]; then
    N_SKIPPED=$((N_SKIPPED + 1)); report "skipped" "$YELLOW" "$2" "no counterpart in source"; return
  fi
  # A symlinked destination is a --dev install: the file already IS the source,
  # so replacing it with a rendered copy would silently break the live-edit
  # contract dogfooding depends on.
  if [[ -L "$dst" ]]; then
    N_SKIPPED=$((N_SKIPPED + 1)); report "skipped" "$YELLOW" "$2" "symlinked (dev-mode install)"; return
  fi

  if [[ "$DRY_RUN" == true ]]; then
    [[ -n "$SCRATCH" ]] || SCRATCH="$(mktemp -d)"
    tmp="$SCRATCH/candidate"
    if ! render <"$src" >"$tmp" 2>/dev/null; then
      N_FAILED=$((N_FAILED + 1)); FAILED_PATHS+=("$2 (render failed)"); report "FAILED" "$RED" "$2" "render failed"; return
    fi
    if ! render_assert_no_placeholders "$tmp"; then
      N_FAILED=$((N_FAILED + 1)); FAILED_PATHS+=("$2 (unsubstituted ${RENDER_LEAKED[*]})")
      report "FAILED" "$RED" "$2" "unsubstituted ${RENDER_LEAKED[*]}"; return
    fi
    if [[ ! -e "$dst" ]]; then
      N_CREATED=$((N_CREATED + 1)); report "would add" "$GREEN" "$2"
    elif cmp -s "$tmp" "$dst"; then
      N_UNCHANGED=$((N_UNCHANGED + 1)); report "unchanged" "" "$2"
    else
      N_UPDATED=$((N_UPDATED + 1)); report "would sync" "$GREEN" "$2"
    fi
    return
  fi

  dstdir="$(dirname "$dst")"
  if ! mkdir -p "$dstdir" 2>/dev/null; then
    N_FAILED=$((N_FAILED + 1)); FAILED_PATHS+=("$2 (mkdir failed)"); report "FAILED" "$RED" "$2" "cannot create $dstdir"; return
  fi
  # Stage NEXT TO the destination so the rename below is same-filesystem (and
  # therefore atomic), then rename. The destination is never truncated in place.
  if ! tmp="$(mktemp "$dstdir/.resync-installed.XXXXXX" 2>/dev/null)"; then
    N_FAILED=$((N_FAILED + 1)); FAILED_PATHS+=("$2 (staging failed)"); report "FAILED" "$RED" "$2" "cannot stage in $dstdir"; return
  fi
  if ! render <"$src" >"$tmp" 2>/dev/null; then
    rm -f "$tmp"
    N_FAILED=$((N_FAILED + 1)); FAILED_PATHS+=("$2 (render failed)"); report "FAILED" "$RED" "$2" "render failed"; return
  fi
  if ! render_assert_no_placeholders "$tmp"; then
    rm -f "$tmp"
    N_FAILED=$((N_FAILED + 1)); FAILED_PATHS+=("$2 (unsubstituted ${RENDER_LEAKED[*]})")
    report "FAILED" "$RED" "$2" "unsubstituted ${RENDER_LEAKED[*]}"; return
  fi

  local existed=false
  [[ -e "$dst" ]] && existed=true
  if [[ "$existed" == true ]] && cmp -s "$tmp" "$dst"; then
    rm -f "$tmp"
    N_UNCHANGED=$((N_UNCHANGED + 1)); report "unchanged" "" "$2"
    return
  fi

  [[ "$is_exec" == 1 ]] && chmod +x "$tmp" 2>/dev/null
  if ! mv -f "$tmp" "$dst" 2>/dev/null; then
    rm -f "$tmp"
    N_FAILED=$((N_FAILED + 1)); FAILED_PATHS+=("$2 (rename failed)"); report "FAILED" "$RED" "$2" "cannot replace"; return
  fi
  if [[ "$existed" == true ]]; then
    N_UPDATED=$((N_UPDATED + 1)); report "synced" "$GREEN" "$2"
  else
    N_CREATED=$((N_CREATED + 1)); report "added" "$GREEN" "$2"
  fi
}

info "Repo Skills resync: $SOURCE_ROOT ($VERSION @ $COMMIT) → $TARGET"
say "  installed: ${INSTALLED_VERSION:-unknown}   source resolved from: $SOURCE_ORIGIN"
[[ "$DRY_RUN" == true ]] && info "Dry run — nothing in $TARGET will be written."
if [[ "$DEV_INSTALL" == "true" ]]; then
  info "This is a --dev install: the surfaces are symlinks into the source clone, so edits are already live."
fi
say ""

i=0
while [[ $i -lt ${#PLAN_SRC[@]} ]]; do
  sync_one "${PLAN_SRC[$i]}" "${PLAN_DST[$i]}" "${PLAN_EXEC[$i]}"
  i=$((i + 1))
done

# ---------------------------------------------------------------------------
# Report what was LEFT ALONE. This is the visible half of "never uninstalls":
# an installed file with no source counterpart is named, not removed.
# ---------------------------------------------------------------------------
ORPHANS=()
for d in "$TARGET/.claude/commands/repo" "$SKILL_ROOT" "$SKILL_ROOT/hooks" "$SKILL_ROOT/scripts"; do
  [[ -d "$d" ]] || continue
  for f in "$d"/*; do
    [[ -f "$f" ]] || continue
    rel="${f#"$TARGET"/}"
    case "$rel" in
      .claude/skills/repo/install-metadata.json|.claude/skills/repo/.install-local.json|.claude/skills/repo/config.json) continue ;;
    esac
    known=false
    j=0
    while [[ $j -lt ${#PLAN_DST[@]} ]]; do
      [[ "${PLAN_DST[$j]}" == "$rel" ]] && { known=true; break; }
      j=$((j + 1))
    done
    [[ "$known" == true ]] || ORPHANS+=("$rel")
  done
done
if [[ ${#ORPHANS[@]} -gt 0 ]]; then
  say ""
  say "  left alone (no source counterpart — resync never removes files):"
  for o in "${ORPHANS[@]}"; do say "    $o"; done
fi

# ---------------------------------------------------------------------------
# Re-stamp metadata. Only on a clean, applied run: a partial run must not claim
# the install is at the source's version.
#
# Contract split (C5/C6): version + commit are identical on every machine that
# resynced to the same source commit, so they belong in the TRACKED metadata.
# `last_resync` is a per-machine timestamp, so it belongs in the gitignored
# sidecar — putting it in the tracked file is exactly the C5 violation this
# contract exists to prevent.
# ---------------------------------------------------------------------------
stamp_metadata() {
  local tmp
  tmp="$(mktemp "$SKILL_ROOT/.install-metadata.XXXXXX")" || { warn "Could not stage install-metadata.json — version stamp skipped"; return; }
  metadata_tracked_json "$VERSION" "$COMMIT" "${DEV_INSTALL:-false}" "${FILTERED:-false}" "$COMMANDS" >"$tmp"
  mv -f "$tmp" "$METADATA" 2>/dev/null || { rm -f "$tmp"; warn "Could not update install-metadata.json — version stamp skipped"; }
}

stamp_sidecar() {
  local tmp installed_at
  installed_at="$(json_string "$SIDECAR" installed_at)"
  [[ -n "$installed_at" ]] || installed_at="$(json_string "$METADATA" installed_at)"
  [[ -n "$installed_at" ]] || installed_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  tmp="$(mktemp "$SKILL_ROOT/.install-local.XXXXXX")" || { warn "Could not stage the sidecar — last_resync not recorded"; return; }
  metadata_sidecar_json "$SOURCE_ROOT" "$installed_at" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$tmp"
  mv -f "$tmp" "$SIDECAR" 2>/dev/null || { rm -f "$tmp"; warn "Could not update the sidecar — last_resync not recorded"; }
}

CHANGED=$((N_CREATED + N_UPDATED))

if [[ "$DRY_RUN" != true && "$N_FAILED" -eq 0 ]]; then
  stamp_metadata
  stamp_sidecar
fi

SUMMARY="$(printf '%s synced, %s added, %s unchanged, %s skipped' "$N_UPDATED" "$N_CREATED" "$N_UNCHANGED" "$N_SKIPPED")"
[[ "$N_FAILED" -gt 0 ]] && SUMMARY="$SUMMARY, $N_FAILED FAILED"
say ""

if [[ "$N_FAILED" -gt 0 ]]; then
  echo -e "${RED}✗ PARTIAL resync: $SUMMARY${NC}" >&2
  for p in "${FAILED_PATHS[@]}"; do echo "    $p" >&2; done
  echo "  No file was left half-written (staging happens off to the side); fix the cause and re-run." >&2
  exit 1
fi

if [[ "$DRY_RUN" == true ]]; then
  if [[ "$CHANGED" -gt 0 ]]; then
    echo -e "${YELLOW}⚠ Drift found: $SUMMARY (dry run — nothing written)${NC}"
    exit 2
  fi
  echo -e "${GREEN}✓ Already in sync: $SUMMARY (dry run)${NC}"
  exit 0
fi

if [[ "$CHANGED" -gt 0 ]]; then
  echo -e "${GREEN}✓ Resynced to $VERSION ($COMMIT): $SUMMARY${NC}"
else
  echo -e "${GREEN}✓ Already in sync at $VERSION ($COMMIT): $SUMMARY${NC}"
fi
exit 0
