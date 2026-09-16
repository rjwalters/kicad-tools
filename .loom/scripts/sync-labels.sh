#!/usr/bin/env bash
# Sync workflow labels from .github/labels.yml onto the forge.
#
# Creates (or updates) the Loom coordination labels defined in
# .github/labels.yml. Run this after a Quick / files-only install to create
# the labels the label-based workflow depends on — a Quick install ships
# .github/labels.yml but does NOT create the labels on the forge (see issue
# #3582).
#
# By default this is ADDITIVE ONLY (#5066): it never touches GitHub's default
# labels (bug, documentation, duplicate, enhancement, good first issue, help
# wanted, invalid, question, wontfix). Deleting those is destructive to
# pre-existing repo data — it silently strips the label from every issue/PR
# carrying it — so it requires the explicit --prune-defaults opt-in below.
#
# Supports both GitHub (via the gh CLI) and Gitea (via the forge API).
#
# Usage:
#   .loom/scripts/sync-labels.sh [--] [WORKTREE_PATH]
#   .loom/scripts/sync-labels.sh --repo OWNER/NAME [--dry-run] [--] [WORKTREE_PATH]
#   .loom/scripts/sync-labels.sh --prune-defaults [--force] [--dry-run] [--] [WORKTREE_PATH]
#   .loom/scripts/sync-labels.sh --check [--repo OWNER/NAME] [--] [WORKTREE_PATH]
#
#   WORKTREE_PATH  Directory containing .github/labels.yml and a git remote.
#                  Defaults to the current directory. `--` ends option parsing,
#                  so a path that begins with `-` can still be passed.
#
# --check (#6716) is a REPORT-ONLY drift check: it fetches the target's live
# label set once and compares it against .github/labels.yml, reporting any
# declared label that is MISSING (absent live) or STALE (present, but its
# color/description differs), plus any UNKNOWN EXTRA — a live label whose
# name starts with "loom:" but is not declared in labels.yml at all. It never
# creates, updates, or deletes anything (mutation is left to a normal,
# non-`--check` run, which is exactly the additive/idempotent sync already
# described above — safe to run unconditionally after a `--check` reports
# drift). Exits 0 when the live set fully matches, 3 when any drift was
# found, 1 on a forge/lookup error. Composes with --repo (still GitHub-only)
# but is independent of --prune-defaults/--force/--dry-run, all of which are
# about the mutating path this flag deliberately skips.
#
# --repo OWNER/NAME (#4498) retargets the sync at an arbitrary GitHub repo
# while still reading labels.yml from WORKTREE_PATH. Because every GitHub label
# operation here is a `gh label` API call, no local checkout of the target repo
# is needed — so one workspace can bring a whole fleet of repos online:
#
#   for r in owner/tool-repo owner/analog-a owner/analog-b; do
#     .loom/scripts/sync-labels.sh --repo "$r"
#   done
#
# --repo bypasses repository *resolution* (the target is named, not inferred
# from a git remote) and is GitHub-only — an explicitly configured Gitea forge
# (LOOM_FORGE_TYPE or forge.type in the resolved config) rejects it. Pair it
# with --dry-run first: --repo makes any --prune-defaults deletions land on a
# repo you are not standing in, so a typo'd NWO is worth previewing. A real
# (non dry-run) --repo sync additionally preflights the named target with
# `gh repo view` before mutating anything (#4524), because a bare dry run is
# deliberately forge-free and so cannot tell a typo apart from a real repo.
#
# --prune-defaults (#5066) opts into the old (pre-#5066) unconditional
# behavior of deleting GitHub's default labels — intended for a genuinely
# greenfield repo where clearing those defaults is wanted. Before deleting a
# default label that is still attached to at least one issue/PR, the script
# either warns and skips it (no --force) or deletes it anyway after warning
# (--force) — it never deletes an in-use label silently. Combined with
# --dry-run, --prune-defaults reports which default labels are currently in
# use (a read-only forge query) without deleting anything; a bare --dry-run
# (no --prune-defaults) stays completely forge-free, as before.
#
# This is the installed-tree counterpart of the source-only
# scripts/install/sync-labels.sh. It is self-contained apart from the
# shipped .loom/scripts/lib/forge-helpers.sh helper library.

set -euo pipefail

# --- Argument parsing -------------------------------------------------------

usage() {
  cat <<'EOF'
Usage: sync-labels.sh [--] [WORKTREE_PATH]
       sync-labels.sh --repo OWNER/NAME [--dry-run] [--] [WORKTREE_PATH]
       sync-labels.sh --prune-defaults [--force] [--dry-run] [--] [WORKTREE_PATH]
       sync-labels.sh --check [--repo OWNER/NAME] [--] [WORKTREE_PATH]

Sync Loom workflow labels from .github/labels.yml onto the forge (GitHub or
Gitea). Creates missing labels and updates existing ones to match
labels.yml. Additive by default — GitHub's default labels (bug,
documentation, duplicate, enhancement, good first issue, help wanted,
invalid, question, wontfix) are left untouched unless --prune-defaults is
given.

Arguments:
  WORKTREE_PATH     Directory containing .github/labels.yml (default: .)

Options:
      --repo OWNER/NAME
                    Sync onto this GitHub repo instead of the one detected from
                    WORKTREE_PATH's git remote. labels.yml is still read from
                    WORKTREE_PATH, so no checkout of the target is needed.
                    GitHub-only (uses the gh CLI).
      --check       Report-only drift check: compare the target's live label
                    set against labels.yml and report missing/stale declared
                    labels plus unknown loom:-prefixed extras. Never creates,
                    updates, or deletes anything. Exits 0 (in sync), 3 (drift
                    found), 4 (could not reach the forge -- benign, the check
                    did not run), or 1 (unexpected failure, i.e. a defect in
                    this script). Independent of
                    --prune-defaults/--force/--dry-run.
      --prune-defaults
                    Opt in to deleting GitHub's default labels (the pre-#5066
                    behavior). A default label still attached to any issue/PR
                    is skipped with a warning listing the affected numbers,
                    unless --force is also given.
      --force       Combined with --prune-defaults, delete an in-use default
                    label anyway (after warning). No effect without
                    --prune-defaults.
      --dry-run     Print the label operations that would run and exit without
                    calling the forge. Recommended before a --repo run.
      --            End of options: every remaining argument is positional, so
                    a WORKTREE_PATH beginning with '-' can be passed.
  -h, --help        Show this help and exit.
EOF
}

WORKTREE_PATH="."
REPO_OVERRIDE=""
DRY_RUN=0
PRUNE_DEFAULTS=0
FORCE_PRUNE=0
CHECK_MODE=0
POSITIONAL_SEEN=0
# Set by `--`: from that point on every remaining argument is positional, even
# one that starts with `-`. Without this the `--)` case below was a no-op shift
# and a `-`-leading path still fell into the "Unknown option" branch (#4524).
POSITIONAL_ONLY=0

# Accept the single positional argument (WORKTREE_PATH), rejecting a second one.
# Factored out so the pre-`--` and post-`--` paths cannot drift apart.
take_positional() {
  if [[ "$POSITIONAL_SEEN" -eq 1 ]]; then
    echo "Unexpected extra argument: $1" >&2
    usage >&2
    exit 2
  fi
  WORKTREE_PATH="$1"
  POSITIONAL_SEEN=1
}

while [[ $# -gt 0 ]]; do
  if [[ "$POSITIONAL_ONLY" -eq 1 ]]; then
    take_positional "$1"
    shift
    continue
  fi
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --repo)
      if [[ $# -lt 2 || -z "$2" ]]; then
        echo "Option --repo requires an OWNER/NAME argument" >&2
        usage >&2
        exit 2
      fi
      REPO_OVERRIDE="$2"
      shift 2
      ;;
    --repo=*)
      REPO_OVERRIDE="${1#--repo=}"
      if [[ -z "$REPO_OVERRIDE" ]]; then
        echo "Option --repo requires an OWNER/NAME argument" >&2
        usage >&2
        exit 2
      fi
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --prune-defaults)
      PRUNE_DEFAULTS=1
      shift
      ;;
    --force)
      FORCE_PRUNE=1
      shift
      ;;
    --check)
      CHECK_MODE=1
      shift
      ;;
    --)
      POSITIONAL_ONLY=1
      shift
      ;;
    -*)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
    *)
      take_positional "$1"
      shift
      ;;
  esac
done

# Validate --repo eagerly: an NWO typo would otherwise be discovered only after
# the first default-label deletion had already been attempted somewhere.
#
# Each segment must START with an alphanumeric: that is what rejects
# path-traversal shapes ('../..', 'owner/..', './x') and `-`-leading segments
# ('-foo/bar', which a downstream `gh ... -R -foo/bar` would read as a flag
# bundle rather than a repo). Dots and dashes remain legal *inside* a segment,
# so real names like 'my-org/my-repo.name' still pass.
if [[ -n "$REPO_OVERRIDE" ]]; then
  if [[ ! "$REPO_OVERRIDE" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
    echo "Invalid --repo value: '$REPO_OVERRIDE' (expected OWNER/NAME)" >&2
    usage >&2
    exit 2
  fi
fi

# Source the shipped forge-agnostic helper library. Provides forge_detect,
# forge_get_repo_nwo, forge_split_nwo, and gitea_api.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/forge-helpers.sh
source "${SCRIPT_DIR}/lib/forge-helpers.sh"

# ANSI color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

error() {
  echo -e "${RED}✗ Error: $*${NC}" >&2
  exit 1
}

# check_unavailable: --check could not REACH the forge, as distinct from
# --check having run and found something (#7745).
#
# Exit 4 is the "I could not perform the check" signal. It exists because
# callers previously could not tell an unreachable forge (benign: no `gh`
# auth, no remote, a misconfigured Gitea) apart from this script being
# BROKEN -- a bash incompatibility or a typo also exits non-zero, and
# resync-installed.sh's catch-all treated both as "skip, carry on, exit 0".
# A checker that cannot distinguish "checked, fine" from "could not check"
# reports the second as the first.
#
# 1 therefore keeps its narrower meaning here: an unexpected failure that
# should be treated as a defect, not absorbed.
check_unavailable() {
  echo -e "${RED}✗ Error: $*${NC}" >&2
  exit 4
}

info() {
  echo -e "${BLUE}ℹ $*${NC}" >&2
}

success() {
  echo -e "${GREEN}✓ $*${NC}" >&2
}

warning() {
  echo -e "${YELLOW}⚠ Warning: $*${NC}" >&2
}

# `cd --`: WORKTREE_PATH may legitimately start with '-' now that `--` ends
# option parsing, and a bare `cd -x` would be parsed as a cd flag.
cd -- "$WORKTREE_PATH"

# _configured_forge_type -> echoes the EXPLICITLY configured forge type
# (lowercased), or "auto" when nothing configures one.
#
# Mirrors the first two tiers of forge_detect's precedence (forge-helpers.sh):
# the LOOM_FORGE_TYPE env var, then forge.type from the resolved config-tier
# chain (loom_config_get over defaults/.loom/config.json/.loom-project/
# .loom-local). Deliberately stops short of forge_detect's third tier —
# git-remote autodetection — because --repo names its target explicitly, so
# only an explicit *setting* should veto it, never an inference drawn from the
# checkout the operator happens to be standing in.
_configured_forge_type() {
  local value="${LOOM_FORGE_TYPE:-}"
  if [[ -z "$value" ]]; then
    value="$(loom_config_get "$(_forge_config_root)" "forge.type" "auto" 2>/dev/null || echo "auto")"
  fi
  printf '%s' "$value" | tr '[:upper:]' '[:lower:]'
}

# --repo names a GitHub repo and drives it purely through `gh label`; there is
# no equivalent "point at an arbitrary remote" path for the Gitea helpers (they
# need a resolved base URL + token for that host), so fail loudly rather than
# silently syncing GitHub while the operator expects Gitea. Runs after the cd
# so the config tiers resolve against WORKTREE_PATH's repo, exactly like the
# forge_detect call further below.
if [[ -n "$REPO_OVERRIDE" && "$(_configured_forge_type)" == "gitea" ]]; then
  if [[ -n "${LOOM_FORGE_TYPE:-}" ]]; then
    echo "--repo is GitHub-only, but LOOM_FORGE_TYPE=gitea is set" >&2
  else
    echo "--repo is GitHub-only, but the resolved Loom config sets forge.type=gitea" >&2
  fi
  echo "Run sync-labels.sh from a checkout of the Gitea repo instead." >&2
  exit 2
fi

if [[ -n "$REPO_OVERRIDE" ]]; then
  # Explicit target: skip forge_detect / forge_get_repo_nwo entirely. Both of
  # those read the *current directory's* git remote, which is precisely what
  # --repo exists to avoid needing — WORKTREE_PATH supplies labels.yml, the
  # flag supplies the target, and no checkout of the target is required.
  FORGE_TYPE="github"
  REPO="$REPO_OVERRIDE"
  # --check always contacts the forge (it IS the read), even under --dry-run,
  # so it needs gh regardless of DRY_RUN — unlike the mutating path below,
  # where a bare --dry-run stays forge-free and so doesn't need gh at all.
  if [[ ( "$DRY_RUN" -eq 0 || "$CHECK_MODE" -eq 1 ) ]] && ! command -v gh >/dev/null 2>&1; then
    error "--repo requires the gh CLI, which was not found on PATH"
  fi
else
  # Detect forge type (github/gitea) from env, config, or git remote.
  forge_detect

  # Resolve the target repository NWO (owner/repo).
  REPO="$(forge_get_repo_nwo || true)"
  if [[ -z "$REPO" ]]; then
    error "Could not determine repository from git remote"
  fi
fi
# Populate FORGE_OWNER / FORGE_REPO for the Gitea API paths.
forge_split_nwo "$REPO"

info "Target repository: $REPO (${FORGE_TYPE})"
if [[ "$DRY_RUN" -eq 1 ]]; then
  info "Dry run: no labels will be created, updated, or deleted."
fi

LABELS_FILE=".github/labels.yml"

if [[ ! -f "$LABELS_FILE" ]]; then
  warning "Labels file not found: $LABELS_FILE"
  warning "Skipping label sync"
  exit 0
fi

info "Syncing workflow labels from $LABELS_FILE..."

# ============================================================================
# GitHub label operations
# ============================================================================

github_delete_label() {
  local label="$1"
  if output=$(gh label delete "$label" -R "$REPO" --yes 2>&1); then
    info "Deleted default label: $label"
  elif ! echo "$output" | grep -qi "not found\|404"; then
    warning "Could not delete label '$label': $output"
  fi
}

# Numbers (one per line) of open+closed issues/PRs currently carrying `label`
# on GitHub. GitHub's REST /issues endpoint returns both issues and pull
# requests (PRs are "issues" in that API), so a single query covers both —
# no separate /pulls call needed. Capped at 100 results (the max page size);
# callers only need "is this in use" plus a representative sample, not an
# exhaustive audit. Best-effort: any API failure (bad REPO, no access, rate
# limit) is treated as "no known usage" rather than blocking the caller,
# matching this script's existing warn-don't-block posture on delete
# failures — an in-use check that can't run must never itself become the
# reason a label silently fails to sync.
github_label_usage() {
  local label="$1"
  gh api "repos/${REPO}/issues" \
    -f state=all -f per_page=100 -f "labels=${label}" \
    --jq '.[].number' 2>/dev/null || true
}

# Format a newline-separated list of issue/PR numbers as "#1 #2 #3", noting
# when the list was truncated at github_label_usage's 100-result cap.
join_usage_numbers() {
  local numbers="$1" count list
  count=$(printf '%s\n' "$numbers" | grep -c . || true)
  list=$(printf '%s\n' "$numbers" | sed '/^$/d; s/^/#/' | paste -sd' ' -)
  if [[ "$count" -ge 100 ]]; then
    printf '%s (100+ shown, there may be more)' "$list"
  else
    printf '%s' "$list"
  fi
}

# Delete a default label unless it is currently in use, in which case skip it
# (warn) or delete it anyway (warn, then delete) depending on FORCE_PRUNE.
# Never deletes an in-use label silently.
github_maybe_delete_label() {
  local label="$1"
  local usage_numbers usage_list
  usage_numbers="$(github_label_usage "$label")"
  if [[ -n "$usage_numbers" ]]; then
    usage_list="$(join_usage_numbers "$usage_numbers")"
    if [[ "$FORCE_PRUNE" -eq 1 ]]; then
      warning "Deleting in-use default label '$label' (--force): affects issue/PR $usage_list"
      github_delete_label "$label"
    else
      warning "Refusing to delete in-use default label '$label': affects issue/PR $usage_list (pass --force to delete anyway)"
    fi
  else
    github_delete_label "$label"
  fi
}

github_sync_label() {
  local name="$1" description="$2" color="$3"

  if gh label list -R "$REPO" --json name --jq '.[].name' 2>&1 | grep -q "^${name}$" 2>/dev/null; then
    if output=$(gh label edit "$name" -R "$REPO" --description "$description" --color "$color" 2>&1); then
      info "Updated label: $name"
    else
      warning "Failed to update label: $name"
      echo "$output" >&2
    fi
  else
    if output=$(gh label create "$name" -R "$REPO" --description "$description" --color "$color" 2>&1); then
      info "Created label: $name"
    else
      if echo "$output" | grep -q "already exists"; then
        if update_output=$(gh label edit "$name" -R "$REPO" --description "$description" --color "$color" 2>&1); then
          info "Updated label: $name"
        else
          warning "Failed to update label: $name"
          echo "$update_output" >&2
        fi
      else
        warning "Failed to create label: $name"
        echo "$output" >&2
      fi
    fi
  fi
}

# ============================================================================
# --check (#6716) shared parsing + report helpers
#
# read_declared_labels() populates three parallel global arrays from
# $LABELS_FILE using the exact same line-triplet parser the main sync loop
# below uses (so --check and the mutating sync can never see a different
# label set from the same file). print_label_check_report() then renders the
# missing/stale/extras verdict both forge backends share; only the LIVE
# label fetch itself (gh vs the Gitea API) differs between the two.
# ============================================================================

DECL_NAMES=()
DECL_COLORS=()
DECL_DESCS=()

read_declared_labels() {
  DECL_NAMES=()
  DECL_COLORS=()
  DECL_DESCS=()
  local line name desc_line color_line description color
  while IFS= read -u 3 -r line; do
    if [[ "$line" =~ ^-\ name:\ (.+)$ ]]; then
      name="${BASH_REMATCH[1]}"
      read -u 3 -r desc_line
      read -u 3 -r color_line

      description=""
      color=""

      if [[ "$desc_line" =~ description:\ (.+)$ ]]; then
        description="${BASH_REMATCH[1]}"
        description="${description//\"/}"
      fi

      if [[ "$color_line" =~ color:\ \"?([0-9A-Fa-f]{6})\"?.*$ ]]; then
        color="${BASH_REMATCH[1]}"
      fi

      DECL_NAMES+=("$name")
      DECL_COLORS+=("$color")
      DECL_DESCS+=("$description")
    fi
  done 3< "$LABELS_FILE"
}

# is_declared_label <name> -> 0 if $name is in DECL_NAMES, else 1.
is_declared_label() {
  local target="$1" n
  for n in "${DECL_NAMES[@]}"; do
    [[ "$n" == "$target" ]] && return 0
  done
  return 1
}

# Populated by diff_declared_against_live_tsv, read by print_label_check_report.
# Plain globals rather than local-by-reference (bash namerefs, `local -n`,
# need bash 4.3+ and aren't used anywhere else in this codebase -- this repo
# targets bash 3.2+ for macOS compatibility per .shellcheckrc) — every
# --check run only ever needs one live verdict at a time, so a shared pair of
# globals is simpler than passing arrays around.
MISSING_LABELS=()
STALE_LABELS=()
EXTRA_LABELS=()

# print_label_check_report
#
# Prints the human-readable verdict from MISSING_LABELS/STALE_LABELS/
# EXTRA_LABELS (set by diff_declared_against_live_tsv) and returns the
# --check exit code: 0 (no drift), 3 (drift found). Never called on a
# forge/lookup failure — those are reported directly by the caller via
# error() (exit 1) before this runs.
# Every array expansion below uses the `${arr[@]+"${arr[@]}"}` guard: under
# `set -u`, bash 3.2 (stock macOS /bin/bash) treats an EMPTY indexed array's
# "${arr[@]}" as an unbound variable and aborts. Bash fixed this in 4.4, so
# the bare form works everywhere except the interpreter a large share of
# operators actually run. `${#arr[@]}` is safe on 3.2 and stays as-is. Only
# reachable at all since #7717 -- this function previously crashed before
# these lines ran.
print_label_check_report() {
  local n s

  if [[ "${#MISSING_LABELS[@]}" -eq 0 && "${#STALE_LABELS[@]}" -eq 0 && "${#EXTRA_LABELS[@]}" -eq 0 ]]; then
    success "Label check: ${#DECL_NAMES[@]} declared label(s), all in sync with $REPO. No unknown loom:-prefixed extras."
    return 0
  fi

  warning "Label drift detected on $REPO (${#DECL_NAMES[@]} declared, ${#MISSING_LABELS[@]} missing, ${#STALE_LABELS[@]} stale, ${#EXTRA_LABELS[@]} unknown extra):"
  for n in ${MISSING_LABELS[@]+"${MISSING_LABELS[@]}"}; do
    echo "  MISSING       $n (declared in labels.yml, absent on $REPO)" >&2
  done
  for s in ${STALE_LABELS[@]+"${STALE_LABELS[@]}"}; do
    echo "  STALE         $s" >&2
  done
  for n in ${EXTRA_LABELS[@]+"${EXTRA_LABELS[@]}"}; do
    echo "  UNKNOWN EXTRA $n (present on $REPO, not declared in labels.yml — never deleted automatically)" >&2
  done
  if [[ "${#MISSING_LABELS[@]}" -gt 0 || "${#STALE_LABELS[@]}" -gt 0 ]]; then
    warning "Run without --check (still additive-only, never deletes/renames) to create the missing labels and refresh the stale ones."
  fi
  # 3 (not 1) so callers can distinguish "drift found" from a forge/lookup
  # error, which error() reports separately via exit 1 before this ever runs.
  return 3
}

# diff_declared_against_live_tsv <tsv>
#
# Shared by both forge backends: $1 is the live label set as one
# name<TAB>color<TAB>description line per label (color WITHOUT a leading
# '#', already lowercased by the caller is NOT required — this normalizes
# case itself). Diffs it against the already-populated DECL_NAMES/
# DECL_COLORS/DECL_DESCS (call read_declared_labels first), populates
# MISSING_LABELS/STALE_LABELS/EXTRA_LABELS, and hands the result to
# print_label_check_report. Returns that function's exit code (0 no drift, 3
# drift found).
# Lowercase helper. `${x,,}` is bash 4.0+ and this repo targets bash 3.2+
# (.shellcheckrc: "Target bash for macOS compatibility (bash 3.2+)"), because
# stock macOS ships /bin/bash 3.2 and `#!/usr/bin/env bash` resolves to it.
_lower() {
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]'
}

diff_declared_against_live_tsv() {
  local live_tsv="$1"

  # Parallel indexed arrays + linear search rather than associative arrays:
  # `local -A` is bash 4.0+, same 3.2 constraint as _lower() above (#7717).
  # This mirrors the DECL_NAMES/DECL_COLORS/DECL_DESCS + is_declared_label()
  # pattern already used in this file. A live label set is dozens of entries,
  # so the O(n*m) scan costs nothing next to the forge call that produced it.
  local LIVE_NAMES=() LIVE_COLORS=() LIVE_DESCS=()
  local name color desc
  while IFS=$'\t' read -r name color desc; do
    [[ -z "$name" ]] && continue
    LIVE_NAMES+=("$name")
    LIVE_COLORS+=("$(_lower "$color")")
    LIVE_DESCS+=("$desc")
  done <<<"$live_tsv"

  MISSING_LABELS=()
  STALE_LABELS=()
  EXTRA_LABELS=()
  local i j decl_name decl_color decl_desc found live_color live_desc
  for ((i = 0; i < ${#DECL_NAMES[@]}; i++)); do
    decl_name="${DECL_NAMES[$i]}"
    decl_color="$(_lower "${DECL_COLORS[$i]}")"
    decl_desc="${DECL_DESCS[$i]}"
    found=""
    live_color=""
    live_desc=""
    for ((j = 0; j < ${#LIVE_NAMES[@]}; j++)); do
      if [[ "${LIVE_NAMES[$j]}" == "$decl_name" ]]; then
        found=1
        live_color="${LIVE_COLORS[$j]}"
        live_desc="${LIVE_DESCS[$j]}"
        break
      fi
    done
    if [[ -z "$found" ]]; then
      MISSING_LABELS+=("$decl_name")
    elif [[ "$live_color" != "$decl_color" || "$live_desc" != "$decl_desc" ]]; then
      STALE_LABELS+=("$decl_name (color: live=$live_color vs declared=$decl_color; description: live=\"$live_desc\" vs declared=\"$decl_desc\")")
    fi
  done

  # Iterates the live set in forge order. The associative-array version this
  # replaced iterated "${!LIVE_SEEN[@]}", i.e. bash hash order, so unknown
  # extras are now reported deterministically instead of arbitrarily.
  for ((j = 0; j < ${#LIVE_NAMES[@]}; j++)); do
    name="${LIVE_NAMES[$j]}"
    if [[ "$name" == loom:* ]] && ! is_declared_label "$name"; then
      EXTRA_LABELS+=("$name")
    fi
  done

  print_label_check_report
}

# github_check_labels: fetch $REPO's live label set ONCE, then diff it
# against labels.yml. Read-only — makes exactly one gh call (a `gh label
# list --json ... --jq` list of name/color/description tab-triples), never a
# `gh label create/edit/delete`.
github_check_labels() {
  read_declared_labels

  local live_tsv
  if ! live_tsv=$(gh label list -R "$REPO" --json name,color,description \
        --jq '.[] | [.name, .color, .description] | @tsv' --limit 300 2>&1); then
    check_unavailable "Could not list labels for $REPO: $live_tsv"
  fi

  diff_declared_against_live_tsv "$live_tsv"
}

# ============================================================================
# Gitea label operations
#
# forge-helpers.sh's gitea_api emits the response BODY on stdout and signals
# success via its exit code (0 on 2xx, 1 otherwise) — unlike the source
# install script's forge-detect.sh helper, which appended the HTTP status to
# the body. These functions are wired to that exit-code contract.
# ============================================================================

# Look up a Gitea label ID by name. Echoes the ID (empty if not found).
gitea_label_id() {
  local name="$1"
  local body
  body=$(gitea_api GET "repos/${FORGE_OWNER}/${FORGE_REPO}/labels" 2>/dev/null) || return 0
  echo "$body" | python3 -c "
import json, sys
target = $(python3 -c "import json,sys; print(json.dumps('$name'))")
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for l in data:
    if l.get('name') == target:
        print(l['id'])
        break
" 2>/dev/null || true
}

gitea_delete_label() {
  local label="$1"
  local label_id
  label_id=$(gitea_label_id "$label")

  if [[ -n "$label_id" ]]; then
    if gitea_api DELETE "repos/${FORGE_OWNER}/${FORGE_REPO}/labels/${label_id}" >/dev/null 2>&1; then
      info "Deleted default label: $label"
    else
      warning "Could not delete label '$label'"
    fi
  fi
}

# Gitea counterpart of github_label_usage: numbers (one per line) of
# open+closed issues/PRs currently carrying `label`. Gitea's issues endpoint
# filters by numeric label ID (not name) and, with type=all, includes both
# issues and pull requests. Best-effort, same as the GitHub version: any
# lookup failure (label not found, API error) reports "no known usage"
# rather than blocking the caller.
gitea_label_usage() {
  local label="$1"
  local label_id body
  label_id=$(gitea_label_id "$label")
  [[ -z "$label_id" ]] && return 0
  body=$(gitea_api GET "repos/${FORGE_OWNER}/${FORGE_REPO}/issues?labels=${label_id}&state=all&type=all" 2>/dev/null) || return 0
  echo "$body" | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for item in data:
    n = item.get('number')
    if n is not None:
        print(n)
" 2>/dev/null || true
}

# Delete a default label unless it is currently in use, in which case skip it
# (warn) or delete it anyway (warn, then delete) depending on FORCE_PRUNE.
gitea_maybe_delete_label() {
  local label="$1"
  local usage_numbers usage_list
  usage_numbers="$(gitea_label_usage "$label")"
  if [[ -n "$usage_numbers" ]]; then
    usage_list="$(join_usage_numbers "$usage_numbers")"
    if [[ "$FORCE_PRUNE" -eq 1 ]]; then
      warning "Deleting in-use default label '$label' (--force): affects issue/PR $usage_list"
      gitea_delete_label "$label"
    else
      warning "Refusing to delete in-use default label '$label': affects issue/PR $usage_list (pass --force to delete anyway)"
    fi
  else
    gitea_delete_label "$label"
  fi
}

gitea_sync_label() {
  local name="$1" description="$2" color="$3"
  local label_id
  label_id=$(gitea_label_id "$name")

  local payload
  payload="{\"name\":$(printf '%s' "$name" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read().strip()))'),\"description\":$(printf '%s' "$description" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read().strip()))'),\"color\":\"#${color}\"}"

  if [[ -n "$label_id" ]]; then
    # Update existing label
    if gitea_api PATCH "repos/${FORGE_OWNER}/${FORGE_REPO}/labels/${label_id}" -d "$payload" >/dev/null 2>&1; then
      info "Updated label: $name"
    else
      warning "Failed to update label: $name"
    fi
  else
    # Create new label
    if gitea_api POST "repos/${FORGE_OWNER}/${FORGE_REPO}/labels" -d "$payload" >/dev/null 2>&1; then
      info "Created label: $name"
    else
      warning "Failed to create label: $name"
    fi
  fi
}

# gitea_check_labels: Gitea counterpart of github_check_labels. Read-only —
# a single GET of the repo's label list, parsed with python3 (matching every
# other Gitea JSON handling in this script) and normalized into the same
# name<TAB>color<TAB>description shape diff_declared_against_live_tsv
# expects (color's leading '#', which gitea_sync_label's payload adds on
# create/update, is stripped here so it compares equal to the bare hex in
# labels.yml).
gitea_check_labels() {
  read_declared_labels

  local body
  body=$(gitea_api GET "repos/${FORGE_OWNER}/${FORGE_REPO}/labels" 2>&1) \
    || check_unavailable "Could not list labels for $REPO: $body"

  local live_tsv
  live_tsv=$(echo "$body" | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
except Exception as e:
    sys.exit(1)
for l in data:
    name = l.get('name', '')
    color = (l.get('color') or '').lstrip('#')
    desc = (l.get('description') or '').replace('\t', ' ').replace('\n', ' ')
    print(f'{name}\t{color}\t{desc}')
" 2>/dev/null) || error "Could not parse label list for $REPO"

  diff_declared_against_live_tsv "$live_tsv"
}

# ============================================================================
# Main sync logic
# ============================================================================

# --check (#6716) short-circuits everything below: it never touches default
# labels, never prunes, never creates/updates a Loom label. Dispatched here
# (rather than immediately after the LABELS_FILE existence check above) so
# every github_/gitea_check_labels() helper it calls is already defined.
if [[ "$CHECK_MODE" -eq 1 ]]; then
  # `|| check_rc=$?` (not a bare call) so a drift/error return (3 or 1) does
  # not itself trip `set -e` before this script can report it and exit with
  # that same code deliberately.
  check_rc=0
  if [[ "$FORGE_TYPE" == "github" ]]; then
    github_check_labels || check_rc=$?
  else
    gitea_check_labels || check_rc=$?
  fi
  exit "$check_rc"
fi

# Remove default labels that clutter issue tracking
DEFAULT_LABELS=(
  "bug"
  "documentation"
  "duplicate"
  "enhancement"
  "good first issue"
  "help wanted"
  "invalid"
  "question"
  "wontfix"
)

# --- Deletion preflight for --repo (#4524) ----------------------------------
#
# --dry-run is deliberately forge-free, so it CANNOT distinguish a typo'd NWO
# from the repo you meant — if the typo happens to name a real repo you can
# administer, the preview looks perfectly reasonable and the real run then
# deletes that repo's default labels. The loop below is the first irreversible
# step, so verify the named target actually exists and is writable by the
# current gh identity before entering it.
#
# Scoped to the --repo path on purpose: the no-flag path already resolved REPO
# via `gh repo view` in the current checkout (existence proven), and adding a
# second call there would change its byte-for-byte behavior.
repo_override_preflight() {
  local out permission err_file
  # stderr goes to a file rather than being folded into $out with 2>&1: a gh
  # deprecation notice on stderr would otherwise be mistaken for the permission.
  err_file="$(mktemp)"
  if ! out=$(gh repo view "$REPO" --json nameWithOwner,viewerPermission \
      --jq '.viewerPermission // ""' 2>"$err_file"); then
    cat "$err_file" >&2
    rm -f "$err_file"
    error "--repo target '$REPO' is not reachable (see the gh error above) — verify the OWNER/NAME spelling and that this gh identity can see it; nothing was deleted"
  fi
  rm -f "$err_file"
  permission="$(printf '%s' "$out" | tr -d '[:space:]' | tr '[:lower:]' '[:upper:]')"
  case "$permission" in
    ADMIN | MAINTAIN | WRITE)
      info "Preflight OK: $REPO exists and is writable ($permission)"
      ;;
    "")
      # Some tokens (e.g. fine-grained PATs) omit viewerPermission. Existence
      # is still proven, so warn rather than block.
      warning "Could not determine your permission on $REPO; continuing (repository exists)"
      ;;
    *)
      error "--repo target '$REPO' is not writable by this gh identity (permission: $permission) — label sync would fail partway (nothing was deleted)"
      ;;
  esac
}

if [[ -n "$REPO_OVERRIDE" && "$DRY_RUN" -eq 0 ]]; then
  repo_override_preflight
fi

# Additive by default (#5066): deleting GitHub's default labels is
# destructive to pre-existing repo data (it strips the label from every
# issue/PR carrying it), so it only happens with the explicit --prune-defaults
# opt-in below.
if [[ "$PRUNE_DEFAULTS" -eq 1 ]]; then
  info "Pruning default labels (--prune-defaults)..."
  for label in "${DEFAULT_LABELS[@]}"; do
    if [[ "$DRY_RUN" -eq 1 ]]; then
      # Unlike the bare-dry-run path below, --prune-defaults + --dry-run DOES
      # contact the forge here: a read-only in-use lookup, so the preview can
      # flag exactly which deletions would be skipped/need --force, per #5066's
      # acceptance criteria. A bare --dry-run (no --prune-defaults) never
      # reaches this branch at all, so it stays completely forge-free.
      usage_numbers=""
      if [[ "$FORGE_TYPE" == "github" ]]; then
        usage_numbers="$(github_label_usage "$label")"
      elif [[ "$FORGE_TYPE" == "gitea" ]]; then
        usage_numbers="$(gitea_label_usage "$label")"
      fi
      if [[ -n "$usage_numbers" ]]; then
        info "[dry-run] would delete default label: $label (IN USE: $(join_usage_numbers "$usage_numbers") — would be skipped without --force)"
      else
        info "[dry-run] would delete default label: $label"
      fi
    elif [[ "$FORGE_TYPE" == "github" ]]; then
      github_maybe_delete_label "$label"
    elif [[ "$FORGE_TYPE" == "gitea" ]]; then
      gitea_maybe_delete_label "$label"
    fi
  done
else
  if [[ "$DRY_RUN" -eq 1 ]]; then
    info "[dry-run] would leave default labels untouched (pass --prune-defaults to remove them): ${DEFAULT_LABELS[*]}"
  else
    info "Leaving default labels untouched (pass --prune-defaults to remove them): ${DEFAULT_LABELS[*]}"
  fi
fi

# Sync Loom workflow labels
info "Syncing Loom workflow labels..."

label_count=0
while IFS= read -u 3 -r line; do
  if [[ "$line" =~ ^-\ name:\ (.+)$ ]]; then
    name="${BASH_REMATCH[1]}"
    read -u 3 -r desc_line
    read -u 3 -r color_line

    description=""
    color=""

    if [[ "$desc_line" =~ description:\ (.+)$ ]]; then
      description="${BASH_REMATCH[1]}"
      description="${description//\"/}"
    fi

    if [[ "$color_line" =~ color:\ \"?([0-9A-Fa-f]{6})\"?.*$ ]]; then
      color="${BASH_REMATCH[1]}"
    fi

    if [[ "$DRY_RUN" -eq 1 ]]; then
      # Deliberately forge-free: a dry run must be answerable offline (it is the
      # preview an operator runs before pointing --repo at a repo they are not
      # standing in), so it reports intent from labels.yml alone and never asks
      # the forge which labels already exist.
      info "[dry-run] would create or update label: $name (color=$color)"
    elif [[ "$FORGE_TYPE" == "github" ]]; then
      github_sync_label "$name" "$description" "$color"
    elif [[ "$FORGE_TYPE" == "gitea" ]]; then
      gitea_sync_label "$name" "$description" "$color"
    fi

    ((label_count++)) || true
  fi
done 3< "$LABELS_FILE"

if [ "$label_count" -gt 0 ]; then
  if [[ "$DRY_RUN" -eq 1 ]]; then
    success "Dry run complete: $label_count labels would be synced to $REPO"
  else
    success "Synced $label_count labels"
  fi
else
  warning "No labels found in $LABELS_FILE"
fi
