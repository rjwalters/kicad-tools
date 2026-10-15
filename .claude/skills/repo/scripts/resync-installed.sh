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
# WHAT IT TOUCHES — the pure-copy surface map install.sh writes, plus ONE
# targeted field edit (not a copy — see the CLAUDE.md entry under OUT OF SCOPE
# below for the split) it shares with install.sh:
#   .claude/skills/repo/SKILL.md                        <- skills/repo/SKILL.md
#   .claude/skills/repo/hooks/*.sh                      <- hooks/repo/*.sh
#     (guard-destructive.sh only when install-metadata.json's guardHookInstalled
#     is true/absent — repo#490: a target that deferred to another tool's guard
#     at install time never had this file, and a resync must not start adding it
#     — and never when the operator opted out with `install.sh --no-guard`,
#     recorded as "guardHook": "disabled" and preserved on every re-stamp, repo#557)
#   .claude/skills/repo/scripts/repo-remote.sh          <- scripts/repo/repo-remote.sh
#   .claude/skills/repo/scripts/repo-scrub-forks.sh     <- scripts/repo/repo-scrub-forks.sh
#   .claude/skills/repo/scripts/resync-installed.sh     <- scripts/repo/resync-installed.sh
#   .claude/commands/repo/<cmd>.md                      <- commands/repo/<cmd>.md
#   .agents/skills/repo/SKILL.md                        <- skills/repo/SKILL.md (Codex form)
#   .agents/skills/repo/references/<cmd>.md             <- commands/repo/<cmd>.md
#   CLAUDE.md (REPO-SKILLS block, restamped to $VERSION) <- targeted field edit, not a copy
#
# The Codex half is refreshed ONLY when it is already installed and carries this
# package's ownership marker. A repo installed before Codex packaging existed, or
# one whose operator declined it with `install.sh --no-codex`, is left without
# it: a refresh must not quietly add a surface nobody asked for. Re-run install.sh
# to adopt it (that is what the layout_version warning below is telling you).
#
# EXPLICITLY OUT OF SCOPE (owned by install.sh / uninstall.sh, not by resync),
# WITH ONE NARROW EXCEPTION for CLAUDE.md's version token (repo#407):
#   .claude/settings.json  - hook wiring is a JSON merge into a file the consumer
#                            also owns; re-run install.sh if the wiring is missing
#   CLAUDE.md              - the REPO-SKILLS marker block's BOILERPLATE PROSE
#                            (wording, structure) is install.sh's alone — a full
#                            block rewrite only ever happens there. Resync DOES
#                            own the "v<version>" token inside that block, the
#                            same way it already owns install-metadata.json's
#                            version field: every run restamps the whole block
#                            to the source's current $VERSION, so the two can
#                            never disagree (the bug this exception fixes: a
#                            resync from N->M used to leave metadata at M while
#                            this block kept reading vN). A block that was never
#                            installed, or whose destination is gitignored, is
#                            left alone either way — resync never adds or
#                            un-hides one, mirroring install.sh's own opt-outs.
#   .gitignore             - the sidecar-ignore entry, written by install.sh
#   config.json            - consumer-owned guard toggles; never generated here
#
# REPO-OWNED PINS (requirement C10, repo#511). A consumer declares an installed
# path theirs by listing it in `.claude/skills/repo/resync-ignore`, one
# target-relative path per line (`#` comments and blank lines ignored; a trailing
# `/` pins a whole subtree). A pinned destination is reported as `pinned` and its
# content is never written — by this script OR by install.sh, which reads the
# same list through the same lib/resync-ignore.sh, because a pin honored by only
# one of the two writers is undone by the other on its next run.
#
# Before this list existed, the only edit to a vendored file that survived a
# reinstall was no edit at all. One consumer lost the same `/repo:scrub`
# customization four times over a few months (2am#463 -> #593 -> #594 ->
# v0.11.12 -> #775/#776 -> v0.12.2/v0.14.0 -> #1596) for exactly that reason. A
# pin that matches nothing this run is reported as a dead pin rather than left
# silent — see lib/resync-ignore.sh for the full format and the rationale on
# what the list deliberately does NOT cover (metadata, settings.json,
# .gitignore, CLAUDE.md's marker block).
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

# Same markers install.sh/uninstall.sh use for the CLAUDE.md REPO-SKILLS block
# (install.sh:68-69, uninstall.sh:21-22) — not hoisted into a shared lib, so
# redefined identically here, same as those two already do independently.
MARKER_BEGIN='<!-- BEGIN REPO-SKILLS -->'
MARKER_END='<!-- END REPO-SKILLS -->'

# Same gate install.sh's dest_is_gitignored() uses (install.sh:777-779): if the
# commands destination is gitignored, install.sh never wrote a CLAUDE.md
# pointer block in the first place (a committed pointer to uncommitted command
# files would be a lie), so resync must not restamp — or otherwise touch — one
# either, even if a block happens to be present from before that gitignore
# rule existed.
CLAUDE_MD_DEST_GITIGNORED=false
if git -C "$TARGET" check-ignore -q .claude/commands/repo/help.md 2>/dev/null; then
  CLAUDE_MD_DEST_GITIGNORED=true
fi

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

# The Codex surface emitter. Soft-sourced, unlike the two above: an older source
# clone that predates Codex packaging can still refresh every Claude-side file it
# does know about, which is strictly better than refusing the whole run.
CODEX_EMITTER=false
if [[ -f "$SOURCE_ROOT/lib/codex-skill.sh" ]]; then
  # shellcheck source=../../lib/codex-skill.sh
  source "$SOURCE_ROOT/lib/codex-skill.sh"
  CODEX_EMITTER=true
fi

# The post-refresh gitignore sweep — requirement C9 of INSTALLER-CONTRACT.md,
# which C7 (this script) SHOULD also run since a consumer editing .gitignore
# after install can introduce the condition without a fresh install.sh run.
# Soft-sourced like the Codex emitter above: an older source clone that
# predates C9 can still resync everything it knows how to.
GITIGNORE_CHECK_AVAILABLE=false
if [[ -f "$SOURCE_ROOT/lib/gitignore-check.sh" ]]; then
  # shellcheck source=../../lib/gitignore-check.sh
  source "$SOURCE_ROOT/lib/gitignore-check.sh"
  GITIGNORE_CHECK_AVAILABLE=true
fi

# The repo-owned pin list — requirement C10, shared with install.sh so the two
# writers cannot disagree about which paths are the consumer's.
#
# Soft-sourced like the two above, with ONE difference that matters: if this
# source clone predates the lib but the target HAS a non-empty pin list, we
# refuse the run instead of degrading. Every other soft-source degrades toward
# "refresh what we can", which is strictly better than refusing; this one would
# degrade toward silently overwriting files the consumer declared theirs, which
# is worse than doing nothing at all and is precisely the failure repo#511 fixed.
if [[ -f "$SOURCE_ROOT/lib/resync-ignore.sh" ]]; then
  # shellcheck source=../../lib/resync-ignore.sh
  source "$SOURCE_ROOT/lib/resync-ignore.sh"
  resync_ignore_load "$TARGET"
elif [[ -s "$TARGET/.claude/skills/repo/resync-ignore" ]]; then
  die "$TARGET/.claude/skills/repo/resync-ignore declares repo-owned paths, but $SOURCE_ROOT/lib/resync-ignore.sh
       is missing — that clone predates the pin list and would overwrite every pinned file.
       Pull that clone (or pass --source /path/to/a/current/repo-skills) and retry. Nothing was written."
else
  # No lib and no pins: define the two predicates as constant-false so the call
  # sites below need no availability branch of their own.
  RESYNC_IGNORE_REL=".claude/skills/repo/resync-ignore"
  resync_ignore_is_pinned() { return 1; }
  resync_ignore_warn_dead_pins() { return 0; }
fi

# The CLAUDE.md marker-block surgery primitive (repo#38) — the only sanctioned
# way to touch the REPO-SKILLS block, shared with install.sh/uninstall.sh.
# Soft-sourced like the two above: an older source clone that predates this
# primitive can still resync every other surface; it just leaves the version
# token in CLAUDE.md's block for install.sh to catch up on the next full run.
CLAUDE_MD_BLOCK_AVAILABLE=false
if [[ -f "$SOURCE_ROOT/lib/claude-md-block.sh" ]]; then
  # shellcheck source=../../lib/claude-md-block.sh
  source "$SOURCE_ROOT/lib/claude-md-block.sh"
  CLAUDE_MD_BLOCK_AVAILABLE=true
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

# guardHookInstalled (repo#490): whether install.sh actually copied
# hooks/repo/guard-destructive.sh, or deferred to a guard some OTHER tool
# already wired into .claude/settings.json. A metadata file written before this
# field existed has none — default to true, matching what every install before
# repo#490 actually did, so an old install's guard keeps being refreshed rather
# than silently vanishing on the next resync.
GUARD_HOOK_INSTALLED="$(json_bool "$METADATA" guardHookInstalled)"
[[ -n "$GUARD_HOOK_INSTALLED" ]] || GUARD_HOOK_INSTALLED=true

# guardHook (repo#557): the operator's explicit guard POLICY, distinct from the
# outcome above. "disabled" (from `install.sh --no-guard`) means the guard is
# never refreshed or re-added here, whatever guardHookInstalled says, and the
# policy is carried through both metadata re-stamps below so a resync can never
# silently drop the opt-out. Absent (every install without the opt-out, and all
# legacy metadata) means no policy: fall back to guardHookInstalled exactly as
# before. Only "disabled" is meaningful; any other value is ignored.
GUARD_POLICY="$(json_string "$METADATA" guardHook)"
if [[ "$GUARD_POLICY" == disabled ]]; then
  GUARD_HOOK_INSTALLED=false
else
  GUARD_POLICY=""
fi

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
PLAN_SRC=(); PLAN_DST=(); PLAN_EXEC=(); PLAN_XFORM=()

plan() {  # <source-rel> <dest-rel> <exec:0|1> [transform: render|codex-skill]
  PLAN_SRC+=("$1"); PLAN_DST+=("$2"); PLAN_EXEC+=("$3"); PLAN_XFORM+=("${4:-render}")
}

plan "skills/repo/SKILL.md"                 ".claude/skills/repo/SKILL.md"                    0
# Only planned when the recorded install decision says the guard was actually
# installed (repo#490) — a target that deferred to another tool's guard at
# install time (GUARD_HOOK_INSTALLED=false) never had this file, and a resync
# must not start adding it: nothing in that target's settings.json would ever
# run a second copy, and a hand-deleted or never-installed file must stay
# absent across resyncs, same as any other deliberate install-time decision
# (compare the FILTERED handling for `commands`, right below).
if [[ "$GUARD_HOOK_INSTALLED" == true ]]; then
  plan "hooks/repo/guard-destructive.sh"      ".claude/skills/repo/hooks/guard-destructive.sh"  1
fi
plan "hooks/repo/session-start-handoff.sh"  ".claude/skills/repo/hooks/session-start-handoff.sh" 1
plan "scripts/repo/repo-remote.sh"          ".claude/skills/repo/scripts/repo-remote.sh"      1
plan "scripts/repo/repo-scrub-forks.sh"     ".claude/skills/repo/scripts/repo-scrub-forks.sh" 1
plan "scripts/repo/repo-org-policy.py"      ".claude/skills/repo/scripts/repo-org-policy.py" 1
plan "scripts/repo/repo-optimize-ci.py"     ".claude/skills/repo/scripts/repo-optimize-ci.py" 1

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

# The Codex surface, when this install actually has one (see the header). The
# SKILL.md is emitted by the same lib/codex-skill.sh function install.sh used, so
# an untouched install reports "unchanged" rather than phantom drift; the
# references/ files are ordinary rendered copies of the command procedures.
CODEX_ROOT=""
if [[ "$CODEX_EMITTER" == true ]] && codex_skill_is_managed "$TARGET/$CODEX_SKILL_REL/SKILL.md"; then
  CODEX_ROOT="$TARGET/$CODEX_SKILL_REL"
  plan "skills/repo/SKILL.md" "$CODEX_SKILL_REL/SKILL.md" 0 codex-skill
  while IFS= read -r cmd; do
    [[ -n "$cmd" ]] || continue
    plan "commands/repo/$cmd.md" "$CODEX_REFERENCES_REL/$cmd.md" 0
  done <<<"$COMMANDS"
fi

# Deferred self-update: this script is one of the files it refreshes, so it goes
# last (see the header). rename(2) already makes the swap safe; ordering makes it
# obviously safe.
plan "scripts/repo/resync-installed.sh" ".claude/skills/repo/scripts/resync-installed.sh" 1

# ---------------------------------------------------------------------------
# Apply (or, under --dry-run, evaluate) the plan.
# ---------------------------------------------------------------------------
N_CREATED=0; N_UPDATED=0; N_UNCHANGED=0; N_SKIPPED=0; N_PINNED=0; N_FAILED=0
FAILED_PATHS=()
SCRATCH=""
cleanup() { [[ -n "$SCRATCH" && -d "$SCRATCH" ]] && rm -rf "$SCRATCH"; }
trap cleanup EXIT

report() {  # <verb> <colour> <dest-rel> [detail]
  say "  $(printf '%b%-9s%b %s%s' "$2" "$1" "$NC" "$3" "${4:+  ($4)}")"
}

# emit <source-abs> <transform> — write the candidate file to stdout. The only
# place a destination's rendering differs, so the two call sites below (dry-run
# candidate, real write) can never disagree about how a file is produced.
emit() {
  case "$2" in
    codex-skill) codex_skill_render "$1" "$COMMANDS" ;;
    *)           render <"$1" ;;
  esac
}

sync_one() {  # <source-rel> <dest-rel> <exec:0|1> <transform>
  local src="$SOURCE_ROOT/$1" dst="$TARGET/$2" is_exec="$3" xform="$4" tmp dstdir

  # The C10 pin check runs FIRST, ahead of even the missing-source check: a pin
  # is a statement about the destination, so it must hold whether or not the
  # source still exists. That also makes "a pinned path upstream has since
  # retired" a clean `pinned` line rather than a confusing `skipped (no
  # counterpart in source)` one.
  if resync_ignore_is_pinned "$2"; then
    N_PINNED=$((N_PINNED + 1)); report "pinned" "$YELLOW" "$2" "repo-owned via $RESYNC_IGNORE_REL"; return
  fi

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
    if ! emit "$src" "$xform" >"$tmp" 2>/dev/null; then
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
  if ! emit "$src" "$xform" >"$tmp" 2>/dev/null; then
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

# sync_claude_md_block — the ONE targeted field edit this script makes (repo#407):
# restamp the REPO-SKILLS block's "v<version>" token in CLAUDE.md to $VERSION.
# Not a sync_one() candidate: the destination isn't a copy of a source file, it's
# a marker span inside a consumer-owned file, so this reuses
# claude_md_block_rewrite() (repo#38) directly instead.
#
# Silently does nothing when:
#   - CLAUDE.md has no REPO-SKILLS block at all (never installed, or removed) —
#     resync must not add one, same as the Codex surface's opt-in-only gate.
#   - the commands destination is gitignored — mirrors install.sh's own skip.
#   - the source clone predates lib/claude-md-block.sh (CLAUDE_MD_BLOCK_AVAILABLE).
# A present-but-unresolvable marker layout (repo#38's failure mode: another
# tool's block glued onto this one with no intervening newline) is reported as
# a FAILURE, exactly like install.sh's own refusal-and-warn — never guessed at.
sync_claude_md_block() {
  local dst="$TARGET/CLAUDE.md" rel="CLAUDE.md" block_file scratch dstdir

  [[ "$CLAUDE_MD_BLOCK_AVAILABLE" == true ]] || return
  [[ -f "$dst" ]] && grep -qF "$MARKER_BEGIN" "$dst" 2>/dev/null || return

  if [[ "$CLAUDE_MD_DEST_GITIGNORED" == true ]]; then
    N_SKIPPED=$((N_SKIPPED + 1))
    report "skipped" "$YELLOW" "$rel" "commands destination is gitignored"
    return
  fi

  if ! block_file="$(mktemp)"; then
    N_FAILED=$((N_FAILED + 1)); FAILED_PATHS+=("$rel (staging failed)")
    report "FAILED" "$RED" "$rel" "cannot stage block content"; return
  fi
  # Mirrors install.sh's BLOCK_FILE verbatim (install.sh:806-816) so a resynced
  # block is byte-identical to a freshly-installed one, boilerplate included —
  # only $VERSION is expected to actually differ run to run.
  {
    echo "$MARKER_BEGIN"
    echo "This repository has [Repo Skills](https://github.com/rjwalters/repo) v$VERSION installed —"
    echo "general repository hygiene and environment commands invoked as \`/repo:<command>\`. Run"
    echo "\`/repo:help\` for the command list, or see \`.claude/skills/repo/SKILL.md\` for the full"
    echo "guide. Hygiene commands apply safe, reversible fixes by default and report each"
    echo "change; run with \`--ask\` to review first, and \`--prune\` to allow irreversible"
    echo "removals. Managed by \`install.sh\` — edit outside the markers only."
    echo "$MARKER_END"
  } >"$block_file"

  if [[ "$DRY_RUN" == true ]]; then
    # Candidate lands in the shared off-to-the-side $SCRATCH dir, same as
    # sync_one()'s dry-run candidates — never inside $TARGET, so "--dry-run
    # writes nothing" holds for this surface too.
    [[ -n "$SCRATCH" ]] || SCRATCH="$(mktemp -d)"
    scratch="$SCRATCH/CLAUDE.md.candidate"
    cp "$dst" "$scratch"
    if ! claude_md_block_rewrite "$scratch" "$MARKER_BEGIN" "$MARKER_END" "$block_file"; then
      N_FAILED=$((N_FAILED + 1)); FAILED_PATHS+=("$rel (${CLAUDE_MD_BLOCK_ERROR:-marker layout unresolved})")
      report "FAILED" "$RED" "$rel" "${CLAUDE_MD_BLOCK_ERROR:-marker layout unresolved}"
      rm -f "$block_file" "$CLAUDE_MD_BLOCK_BACKUP"; return
    fi
    rm -f "$CLAUDE_MD_BLOCK_BACKUP"
    if cmp -s "$scratch" "$dst"; then
      N_UNCHANGED=$((N_UNCHANGED + 1)); report "unchanged" "" "$rel"
    else
      N_UPDATED=$((N_UPDATED + 1)); report "would sync" "$GREEN" "$rel"
    fi
    rm -f "$block_file"
    return
  fi

  dstdir="$(dirname "$dst")"
  if ! scratch="$(mktemp "$dstdir/.resync-installed.XXXXXX" 2>/dev/null)"; then
    rm -f "$block_file"
    N_FAILED=$((N_FAILED + 1)); FAILED_PATHS+=("$rel (staging failed)")
    report "FAILED" "$RED" "$rel" "cannot stage in $dstdir"; return
  fi
  cp "$dst" "$scratch"

  if ! claude_md_block_rewrite "$scratch" "$MARKER_BEGIN" "$MARKER_END" "$block_file"; then
    N_FAILED=$((N_FAILED + 1)); FAILED_PATHS+=("$rel (${CLAUDE_MD_BLOCK_ERROR:-marker layout unresolved})")
    report "FAILED" "$RED" "$rel" "${CLAUDE_MD_BLOCK_ERROR:-marker layout unresolved}"
    rm -f "$block_file" "$scratch" "$CLAUDE_MD_BLOCK_BACKUP"; return
  fi
  rm -f "$CLAUDE_MD_BLOCK_BACKUP"

  if cmp -s "$scratch" "$dst"; then
    N_UNCHANGED=$((N_UNCHANGED + 1)); report "unchanged" "" "$rel"
    rm -f "$block_file" "$scratch"; return
  fi

  # $scratch already validated + rewritten, and lives next to $dst — the same
  # same-filesystem atomic rename sync_one() uses, applied to a file this
  # function staged rather than one sync_one() rendered.
  if ! mv -f "$scratch" "$dst" 2>/dev/null; then
    N_FAILED=$((N_FAILED + 1)); FAILED_PATHS+=("$rel (rename failed)")
    report "FAILED" "$RED" "$rel" "cannot replace"
    rm -f "$block_file" "$scratch"; return
  fi
  rm -f "$block_file"
  N_UPDATED=$((N_UPDATED + 1)); report "synced" "$GREEN" "$rel"
}

info "Repo Skills resync: $SOURCE_ROOT ($VERSION @ $COMMIT) → $TARGET"
say "  installed: ${INSTALLED_VERSION:-unknown}   source resolved from: $SOURCE_ORIGIN"
[[ "$DRY_RUN" == true ]] && info "Dry run — nothing in $TARGET will be written."
if [[ "$GUARD_POLICY" == disabled ]]; then
  info "Destructive-command guard disabled (\"guardHook\": \"disabled\" in install-metadata.json) — guard-destructive.sh is not refreshed."
fi
if [[ "$DEV_INSTALL" == "true" ]]; then
  info "This is a --dev install: the surfaces are symlinks into the source clone, so edits are already live."
fi
say ""

# CLAUDE.md's version-token restamp runs before the copy plan below (which ends
# with this script's own deferred self-update) so a test — or a human — reading
# "the last line synced is the plan's last entry" is not surprised by a report
# line for a file that was never part of PLAN_SRC/PLAN_DST in the first place.
sync_claude_md_block

i=0
while [[ $i -lt ${#PLAN_SRC[@]} ]]; do
  sync_one "${PLAN_SRC[$i]}" "${PLAN_DST[$i]}" "${PLAN_EXEC[$i]}" "${PLAN_XFORM[$i]}"
  i=$((i + 1))
done

# ---------------------------------------------------------------------------
# Report what was LEFT ALONE. This is the visible half of "never uninstalls":
# an installed file with no source counterpart is named, not removed.
# ---------------------------------------------------------------------------
ORPHANS=()
ORPHAN_DIRS=("$TARGET/.claude/commands/repo" "$SKILL_ROOT" "$SKILL_ROOT/hooks" "$SKILL_ROOT/scripts")
[[ -n "$CODEX_ROOT" ]] && ORPHAN_DIRS+=("$CODEX_ROOT" "$CODEX_ROOT/references")
for d in "${ORPHAN_DIRS[@]}"; do
  [[ -d "$d" ]] || continue
  for f in "$d"/*; do
    [[ -f "$f" ]] || continue
    rel="${f#"$TARGET"/}"
    case "$rel" in
      .claude/skills/repo/install-metadata.json|.claude/skills/repo/.install-local.json|.claude/skills/repo/config.json) continue ;;
      # The C10 pin list is consumer-authored bookkeeping that lives inside the
      # tool root by design (same as Loom's `.loom/resync-ignore`). It has no
      # source counterpart and never will, so reporting it as "left alone" every
      # run would train the reader to ignore that section.
      .claude/skills/repo/resync-ignore) continue ;;
      .agents/skills/repo/install-metadata.json) continue ;;
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

# Every planned destination has now been offered to the C10 pin list, so any
# entry that matched nothing is dead and is named rather than left silent (the
# loom#6515 failure mode: the pin LOOKS installed, does nothing, and the file it
# was meant to protect is clobbered anyway).
resync_ignore_warn_dead_pins

# ---------------------------------------------------------------------------
# C9 sweep: warn (never fail) about any installed file now hidden by the
# consumer's .gitignore. Runs regardless of --dry-run — the files it checks
# already exist on disk from a prior install, so this also catches drift (a
# .gitignore edited after install) that --dry-run's "nothing written" framing
# would otherwise mask.
# ---------------------------------------------------------------------------
if [[ "$GITIGNORE_CHECK_AVAILABLE" == true ]]; then
  GITIGNORE_SWEEP_DIRS=("$SKILL_ROOT" "$TARGET/.claude/commands/repo")
  [[ -n "$CODEX_ROOT" ]] && GITIGNORE_SWEEP_DIRS+=("$CODEX_ROOT")
  warn_gitignored_payload "$TARGET" "${GITIGNORE_SWEEP_DIRS[@]}"
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
# policy_kept <staged-file> — false when the operator's guard opt-out (repo#557)
# would be lost by this re-stamp. Only possible with a source clone whose
# lib/metadata.sh predates the `guardHook` field and silently ignores the extra
# argument; keeping the old metadata is strictly better than quietly turning a
# deliberate opt-out back into the default on the next install.sh run.
policy_kept() {
  [[ "$GUARD_POLICY" != disabled ]] || grep -q '"guardHook"' "$1"
}

stamp_metadata() {
  local tmp
  tmp="$(mktemp "$SKILL_ROOT/.install-metadata.XXXXXX")" || { warn "Could not stage install-metadata.json — version stamp skipped"; return; }
  metadata_tracked_json "$VERSION" "$COMMIT" "${DEV_INSTALL:-false}" "${FILTERED:-false}" "$COMMANDS" "$GUARD_HOOK_INSTALLED" "$GUARD_POLICY" >"$tmp"
  if ! policy_kept "$tmp"; then
    rm -f "$tmp"
    warn "$SOURCE_ROOT/lib/metadata.sh cannot record \"guardHook\": \"disabled\" — metadata left as is so the"
    warn "guard opt-out is not lost. Pull that source clone and resync again."
    return
  fi
  mv -f "$tmp" "$METADATA" 2>/dev/null || { rm -f "$tmp"; warn "Could not update install-metadata.json — version stamp skipped"; }

  # The Codex surface carries its own copy of the same tracked metadata (same
  # emitter, same C5 guarantees), so it must be re-stamped alongside or it would
  # keep claiming the version it was installed at.
  [[ -n "$CODEX_ROOT" && -f "$CODEX_ROOT/install-metadata.json" ]] || return 0
  tmp="$(mktemp "$CODEX_ROOT/.install-metadata.XXXXXX")" || { warn "Could not stage $CODEX_SKILL_REL/install-metadata.json — version stamp skipped"; return; }
  metadata_tracked_json "$VERSION" "$COMMIT" "${DEV_INSTALL:-false}" "${FILTERED:-false}" "$COMMANDS" "$GUARD_HOOK_INSTALLED" "$GUARD_POLICY" >"$tmp"
  policy_kept "$tmp" || { rm -f "$tmp"; return 0; }
  mv -f "$tmp" "$CODEX_ROOT/install-metadata.json" 2>/dev/null \
    || { rm -f "$tmp"; warn "Could not update $CODEX_SKILL_REL/install-metadata.json — version stamp skipped"; }
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
# Only when there are pins: a `0 pinned` on every run in every repo that has no
# pin list is noise, and --quiet exists precisely because this one line is all a
# fleet-wide driver reads.
[[ "$N_PINNED" -gt 0 ]] && SUMMARY="$SUMMARY, $N_PINNED pinned"
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
