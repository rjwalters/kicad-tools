#!/usr/bin/env bash
# repo-scrub-forks.sh — sweep a repo's fork network for a piece of sensitive
# content, separately from any code/search-based sweep (rjwalters/repo#185).
#
# WHY THIS IS A SEPARATE SCRIPT (companion to #174/#186, split out of #174's
# "scope split" comment): a fork is a copy you have already lost control of —
# you cannot rewrite it, you cannot delete it, and the only lever is asking a
# stranger nicely. GitHub's code search AND repository search both exclude
# forks by default (this cannot be turned off for code search), so any tool
# built on the search API is structurally blind to exactly the copies it most
# needs to find. This script never uses search to ENUMERATE forks — only the
# forks API (`GET /repos/{o}/{r}/forks`), walked recursively, because a fork of
# a fork is a distinct copy and depth cannot be assumed to be <= 1.
#
# Real incident this is built from: a sweep across ~73 public repos reported a
# repo clean after remediation, while two public forks still carried the
# original content. `GET .../forks` on the (by-then-private) original returned
# empty because making a repo private DETACHES one fork into a new network
# root and RE-PARENTS the other fork underneath it — the fork count captured
# before the visibility change no longer matched anything queryable after. One
# copy was found only by a description fingerprint (repository search DOES
# index description/README, and detachment had reset `fork=false`); the other
# was found only by listing forks OF THAT FIRST FORK.
#
# ─────────────────────────────────────────────────────────────────────────────
# Subcommands
# ─────────────────────────────────────────────────────────────────────────────
#
#   repo-scrub-forks.sh sweep <owner>/<repo> --path <path> [--path <path> ...]
#                        [--pattern <regex>] [--max-depth N] [--json]
#     Recursively enumerates the fork network via the forks API, adds any
#     description/README-first-line fingerprint matches (the detached-fork
#     fallback), then — for each candidate — fetches the given --path(s) at
#     the candidate's default-branch HEAD and confirms the content is present
#     (optionally matched against --pattern) before reporting it as a finding.
#     A fork that does not carry any --path (or whose content there does not
#     match --pattern) is NOT a finding, per the issue: "a fork predating the
#     content is not a finding." Findings are reported UNREMEDIABLE — outreach
#     is the only lever; the tool never suggests a fix the operator cannot
#     perform on someone else's repository.
#
#   repo-scrub-forks.sh warn-before-private <owner>/<repo> [--json]
#     Captures the current fork list BEFORE any visibility change and persists
#     it to disk, then warns loudly if the repo has forks. Read-only — this
#     script never changes a repo's visibility itself; it exists to be called
#     by whatever workflow is about to do so, because that action scrambles
#     fork/parent relationships and makes the copies harder to find afterwards
#     (the opposite of what someone privatizing a repo for cleanup intends).
#
# Exit codes:
#   0   completed; sweep found no forks, or found candidate fork(s) but none
#       confirmed to carry the checked content (not a finding, per the issue)
#   1   findings: sweep confirmed >=1 fork/fingerprint-match carrying the
#       content (`sweep`), or the repo has >=1 fork (`warn-before-private`,
#       advisory only — this script cannot block a visibility change itself)
#   2   error: usage, missing `gh`/`jq`, `gh` not authenticated, or an API
#       failure (rate limit, auth, network) during the recursive forks-API
#       walk — this is INCONCLUSIVE and must never be reported as "no forks
#       found"
#
# Testability hooks:
#   PATH                        a mocked `gh` is picked up from PATH
#   REPO_SCRUB_FORKS_STATE_DIR  where `warn-before-private` persists its
#                                fork-list snapshot (default: see below)
#
set -uo pipefail

SCRIPT_NAME="repo-scrub-forks"
log()  { printf '%s\n' "${SCRIPT_NAME}: $*" >&2; }
die()  { local code="$1"; shift; printf '%s\n' "${SCRIPT_NAME}: ERROR: $*" >&2; exit "$code"; }

usage() {
  awk 'NR >= 4 { if ($0 !~ /^#/) exit; sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]}"
}

# ── json emission (no external dependency for output; values are controlled) ─
json_escape() {
  local s="$1"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  s="${s//$'\n'/\\n}"
  s="${s//$'\t'/\\t}"
  s="${s//$'\r'/}"
  printf '%s' "$s"
}

# percent-encode for GitHub search `q=` query strings (bash 3.2 compatible —
# this repo's macOS builds ship bash 3.2, no associative arrays / mapfile).
urlencode() {
  local s="$1" out="" c hex i
  for (( i = 0; i < ${#s}; i++ )); do
    c="${s:i:1}"
    case "$c" in
      [a-zA-Z0-9.~_-]) out+="$c" ;;
      ' ') out+='+' ;;
      *) printf -v hex '%%%02X' "'$c"; out+="$hex" ;;
    esac
  done
  printf '%s' "$out"
}

# in_array <needle> <haystack-array-elements...>
in_array() {
  local needle="$1"; shift
  local x
  for x in "$@"; do [[ "$x" == "$needle" ]] && return 0; done
  return 1
}

# ── preflight ────────────────────────────────────────────────────────────────
require_tools() {
  command -v gh  >/dev/null 2>&1 || die 2 "gh CLI is required but not found on PATH"
  command -v jq  >/dev/null 2>&1 || die 2 "jq is required but not found on PATH"
  gh auth status >/dev/null 2>&1 || die 2 "gh CLI is not authenticated (run 'gh auth login' / set GH_TOKEN)"
}

# ── option parsing ───────────────────────────────────────────────────────────
ACTION=""
TARGET=""
PATHS=()
PATTERN=""
MAX_DEPTH=10
JSON_OUT=false

parse_args() {
  [[ $# -gt 0 ]] || die 2 "no action given (expected: sweep | warn-before-private; see --help)"
  case "$1" in
    -h|--help) usage; exit 0 ;;
    sweep|warn-before-private) ACTION="$1"; shift ;;
    *) die 2 "unknown action: $1 (expected: sweep | warn-before-private; see --help)" ;;
  esac

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --path)      [[ -n "${2:-}" ]] || die 2 "--path requires an argument"; PATHS+=("$2"); shift 2 ;;
      --pattern)   [[ -n "${2:-}" ]] || die 2 "--pattern requires an argument"; PATTERN="$2"; shift 2 ;;
      --max-depth) [[ -n "${2:-}" ]] || die 2 "--max-depth requires an argument"; MAX_DEPTH="$2"; shift 2 ;;
      --json)      JSON_OUT=true; shift ;;
      -h|--help)   usage; exit 0 ;;
      -*)          die 2 "unknown option: $1 (see --help)" ;;
      *)
        [[ -z "$TARGET" ]] || die 2 "unexpected extra argument: $1"
        TARGET="$1"; shift ;;
    esac
  done

  [[ -n "$TARGET" ]] || die 2 "missing <owner>/<repo>"
  [[ "$TARGET" == */* ]] || die 2 "expected <owner>/<repo>, got: $TARGET"
  [[ "$MAX_DEPTH" =~ ^[0-9]+$ ]] || die 2 "--max-depth must be a non-negative integer, got: $MAX_DEPTH"

  if [[ "$ACTION" == "sweep" && ${#PATHS[@]} -eq 0 ]]; then
    die 2 "sweep requires at least one --path <path> (the specific content to check for — presence of a fork alone is never a finding)"
  fi
}

# ── the recursive forks-API walk (never search — see header) ───────────────
# Populates the parallel arrays below. Any API failure aborts the WHOLE sweep
# (exit 2) rather than silently under-reporting — an inconclusive walk must
# never be reported as "no forks found" (explicit test-plan requirement).
VISITED=()
FORK_PARENT=()      # full_name of the repo this fork was discovered under
FORK_FULLNAME=()
FORK_OWNER=()
FORK_DEFAULT_BRANCH=()
FORK_DESCRIPTION=()


# NOTE on the pattern below: fetch_forks_page's caller MUST capture its output
# via plain command substitution (`page="$(fetch_forks_page ...)"`) and check
# `$?` directly, NEVER via `done < <(fetch_forks_page ...)`. Process
# substitution (`<(...)`) runs the producer in a subshell whose exit status is
# never surfaced to the consuming `while read` loop — a `die`/`exit` inside it
# only kills that anonymous subshell, so the loop just sees EOF (0 rows) and
# the caller silently treats an API failure as "this fork has no children"
# instead of aborting. That bug was caught by a live smoke test against a real
# repo (a renamed/moved fork 404ing mid-walk kept going and reported partial
# results with exit 0 instead of the required exit 2) — this comment exists so
# it is never reintroduced. `$(...)` command substitution is safe here because
# its exit status IS reflected in `$?` immediately after the assignment,
# unlike `<(...)`.
FETCH_ERR=""
fetch_forks_page() {  # <owner/repo> -> tsv rows on stdout; return 1 + sets FETCH_ERR on API failure
  local fn="$1" out errfile rc
  errfile="$(mktemp)"
  out="$(gh api "repos/${fn}/forks?per_page=100" --paginate 2>"$errfile")"
  rc=$?
  FETCH_ERR="$(cat "$errfile" 2>/dev/null)"
  rm -f "$errfile"
  [[ $rc -eq 0 ]] || return 1
  printf '%s' "$out" | jq -r '.[] | [.full_name, .owner.login, .default_branch, (.description // "")] | @tsv'
}

walk_fork_network() {  # <root owner/repo>
  local root="$1"
  local -a queue=("$root") depth=(0)
  VISITED=("$root")

  while [[ ${#queue[@]} -gt 0 ]]; do
    local cur="${queue[0]}" curdepth="${depth[0]}"
    queue=("${queue[@]:1}"); depth=("${depth[@]:1}")

    if (( curdepth >= MAX_DEPTH )); then
      log "WARNING: max depth (${MAX_DEPTH}) reached at ${cur} — the network may be deeper; raise --max-depth to walk further."
      continue
    fi

    local page
    page="$(fetch_forks_page "$cur")" \
      || die 2 "fork enumeration failed at ${cur} (rate limit / auth / network) — sweep is INCONCLUSIVE, not 'no forks found'. ${FETCH_ERR}"

    local fn owner branch desc
    while IFS=$'\t' read -r fn owner branch desc; do
      [[ -n "$fn" ]] || continue
      FORK_PARENT+=("$cur"); FORK_FULLNAME+=("$fn"); FORK_OWNER+=("$owner")
      FORK_DEFAULT_BRANCH+=("$branch"); FORK_DESCRIPTION+=("$desc")
      if ! in_array "$fn" "${VISITED[@]}"; then
        VISITED+=("$fn")
        queue+=("$fn"); depth+=($((curdepth + 1)))
      fi
    done <<<"$page"
  done
}

# ── fingerprint fallback for detached forks (description / README first line) ─
# Repository search DOES index description and README even though code search
# excludes forks entirely — this is the mechanism that found the real
# incident's detached fork (detachment had reset fork=false). Search failures
# here are NON-FATAL (this is a fallback, not the primary enumeration): a
# fallback that cannot run degrades the sweep, it does not invalidate the
# fork-API results already collected.
FP_FULLNAME=()
FP_OWNER=()
FP_DEFAULT_BRANCH=()
FP_BASIS=()
FP_DEGRADED=false

readme_first_line() {  # <owner/repo> -> first line of README on stdout, or empty
  local fn="$1" json b64
  json="$(gh api "repos/${fn}/readme" 2>/dev/null)" || { printf ''; return; }
  b64="$(printf '%s' "$json" | jq -r '.content // empty' 2>/dev/null)"
  [[ -n "$b64" ]] || { printf ''; return; }
  printf '%s' "$b64" | base64 --decode 2>/dev/null | head -n1
}

fingerprint_search() {  # <exact-phrase> <field: description|readme> <root-owner>
  local phrase="$1" field="$2" root_owner="$3" q json rc errfile
  [[ -n "$phrase" ]] || return 0
  q="$(urlencode "\"${phrase}\" in:${field}")"
  errfile="$(mktemp)"
  json="$(gh api "search/repositories?q=${q}&per_page=50" 2>"$errfile")"
  rc=$?
  if [[ $rc -ne 0 ]]; then
    log "WARNING: ${field} fingerprint search failed (non-fatal, degrades coverage): $(cat "$errfile")"
    FP_DEGRADED=true
    rm -f "$errfile"
    return 0
  fi
  rm -f "$errfile"

  local fn owner branch fdesc
  while IFS=$'\t' read -r fn owner branch fdesc; do
    [[ -n "$fn" ]] || continue
    [[ "$owner" == "$root_owner" ]] && continue
    in_array "$fn" "${VISITED[@]}" && continue
    in_array "$fn" "${FP_FULLNAME[@]}" && continue

    if [[ "$field" == "description" ]]; then
      # Exact-match confirmation (per issue: "flag exact matches").
      [[ "$fdesc" == "$phrase" ]] || continue
    else
      # README search hits are confirmed by re-fetching the candidate's own
      # README and comparing its first line exactly — "in:readme" only proves
      # the phrase appears SOMEWHERE in the README, not that it IS the first
      # line fingerprint.
      local cand_line; cand_line="$(readme_first_line "$fn")"
      [[ "$cand_line" == "$phrase" ]] || continue
    fi

    FP_FULLNAME+=("$fn"); FP_OWNER+=("$owner"); FP_DEFAULT_BRANCH+=("$branch"); FP_BASIS+=("$field")
  done < <(printf '%s' "$json" | jq -r '.items[]? | [.full_name, .owner.login, .default_branch, (.description // "")] | @tsv')
}

# ── per-candidate content check (presence-confirmed, not presence-assumed) ──
# Fetches each --path at the candidate's default-branch HEAD only (explicitly
# scoped — a path reachable only in the fork's history is out of scope for
# this check and this is stated in the output, not silently mis-reported).
path_present_at_head() {  # <owner/repo> <default_branch> <path> -> 0 if content confirmed present (and matches --pattern, if set)
  local fn="$1" branch="$2" path="$3" json rc content
  json="$(gh api "repos/${fn}/contents/${path}?ref=${branch}" 2>/dev/null)"
  rc=$?
  [[ $rc -eq 0 ]] || return 1   # 404 (or any other failure) -> not confirmed present
  content="$(printf '%s' "$json" | jq -r '.content // empty' 2>/dev/null)"
  [[ -n "$content" ]] || return 1   # a directory listing (no .content) -> not a file match

  if [[ -n "$PATTERN" ]]; then
    printf '%s' "$content" | base64 --decode 2>/dev/null | grep -Eq "$PATTERN"
  else
    return 0
  fi
}

# ── sweep ────────────────────────────────────────────────────────────────────
FINDINGS_FULLNAME=()
FINDINGS_OWNER=()
FINDINGS_PARENT=()
FINDINGS_PATH=()
FINDINGS_BASIS=()

do_sweep() {
  local root="$TARGET" root_json root_desc root_owner root_readme_line

  root_json="$(gh api "repos/${root}" 2>/dev/null)" \
    || die 2 "could not fetch root repo metadata for ${root} (rate limit / auth / not found)"
  root_owner="$(printf '%s' "$root_json" | jq -r '.owner.login')"
  root_desc="$(printf '%s' "$root_json" | jq -r '.description // empty')"
  root_readme_line="$(readme_first_line "$root")"

  walk_fork_network "$root"

  fingerprint_search "$root_desc" "description" "$root_owner"
  fingerprint_search "$root_readme_line" "readme" "$root_owner"

  # Union of forks-API candidates and fingerprint candidates (parent is
  # "fingerprint" for the latter — there is no forks-API edge to a detached
  # fork by definition).
  local -a cand_fullname=() cand_owner=() cand_branch=() cand_parent=() cand_basis=()
  local i
  for i in "${!FORK_FULLNAME[@]}"; do
    cand_fullname+=("${FORK_FULLNAME[$i]}"); cand_owner+=("${FORK_OWNER[$i]}")
    cand_branch+=("${FORK_DEFAULT_BRANCH[$i]}"); cand_parent+=("${FORK_PARENT[$i]}")
    cand_basis+=("forks-api")
  done
  for i in "${!FP_FULLNAME[@]}"; do
    cand_fullname+=("${FP_FULLNAME[$i]}"); cand_owner+=("${FP_OWNER[$i]}")
    cand_branch+=("${FP_DEFAULT_BRANCH[$i]}"); cand_parent+=("(fingerprint: detached fork, no forks-API edge)")
    cand_basis+=("fingerprint-${FP_BASIS[$i]}")
  done

  for i in "${!cand_fullname[@]}"; do
    local fn="${cand_fullname[$i]}" branch="${cand_branch[$i]}" p
    for p in "${PATHS[@]}"; do
      if path_present_at_head "$fn" "$branch" "$p"; then
        FINDINGS_FULLNAME+=("$fn"); FINDINGS_OWNER+=("${cand_owner[$i]}")
        FINDINGS_PARENT+=("${cand_parent[$i]}"); FINDINGS_PATH+=("$p")
        FINDINGS_BASIS+=("${cand_basis[$i]}")
      fi
    done
  done

  emit_sweep_result "$root" "${#cand_fullname[@]}"
}

emit_sweep_result() {  # <root> <candidate-count>
  local root="$1" cand_count="$2" nfindings="${#FINDINGS_FULLNAME[@]}"

  if [[ "$JSON_OUT" == true ]]; then
    printf '{'
    printf '"action":"sweep","root":"%s","candidates_checked":%s,"fingerprint_degraded":%s,"findings":[' \
      "$(json_escape "$root")" "$cand_count" "$FP_DEGRADED"
    local i first=true
    for i in "${!FINDINGS_FULLNAME[@]}"; do
      [[ "$first" == true ]] && first=false || printf ','
      printf '{"fork":"%s","owner":"%s","path":"%s","discovered_via":"%s","parent":"%s","remediable":false,"recommended_action":"outreach"}' \
        "$(json_escape "${FINDINGS_FULLNAME[$i]}")" "$(json_escape "${FINDINGS_OWNER[$i]}")" \
        "$(json_escape "${FINDINGS_PATH[$i]}")" "$(json_escape "${FINDINGS_BASIS[$i]}")" \
        "$(json_escape "${FINDINGS_PARENT[$i]}")"
    done
    printf '],"leaf_first_removal_note":"%s"}\n' \
      "$(json_escape "If removal is ever pursued by the fork owners themselves: delete leaf-first. Deleting a fork network's ROOT promotes a child fork to become the new root, so the content survives naive root-first deletion.")"
  else
    if [[ $nfindings -eq 0 ]]; then
      if [[ "$cand_count" -eq 0 ]]; then
        log "no forks found for ${root}. Nothing to report."
      else
        log "${cand_count} candidate fork(s)/fingerprint-match(es) found for ${root}, but none confirmed carrying the checked path(s). Not a finding."
      fi
    else
      log "UNREMEDIABLE: ${nfindings} fork(s) of ${root} confirmed carrying sensitive content. Outreach is the only lever — none of the following can be fixed by the operator of ${root}:"
      local i
      for i in "${!FINDINGS_FULLNAME[@]}"; do
        log "  - ${FINDINGS_FULLNAME[$i]} (owner: ${FINDINGS_OWNER[$i]}, discovered via ${FINDINGS_BASIS[$i]}) carries ${FINDINGS_PATH[$i]} at HEAD."
        log "      recommended action: contact ${FINDINGS_OWNER[$i]} about ${FINDINGS_FULLNAME[$i]} and ask them to remove or privatize it. There is no API to delete a fork you do not own."
      done
      log "If removal is ever pursued by the fork owners themselves: delete LEAF-FIRST. Deleting a fork network's ROOT promotes a child fork to become the new root, so the content survives naive root-first deletion."
    fi
    if [[ "$FP_DEGRADED" == true ]]; then
      log "WARNING: the description/README fingerprint fallback could not fully run (search failure) — coverage for detached forks is degraded on this run."
    fi
  fi
}

# ── warn-before-private ──────────────────────────────────────────────────────
do_warn_before_private() {
  local root="$TARGET"

  walk_fork_network "$root"

  local state_dir ts snap_file
  state_dir="${REPO_SCRUB_FORKS_STATE_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)/.loom/state/repo-scrub-forks}"
  mkdir -p "$state_dir" 2>/dev/null || true
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  snap_file="${state_dir}/$(printf '%s' "$root" | tr '/' '__')-forks-${ts}.json"

  {
    printf '{"root":"%s","captured_at":"%s","forks":[' "$(json_escape "$root")" "$ts"
    local i first=true
    for i in "${!FORK_FULLNAME[@]}"; do
      [[ "$first" == true ]] && first=false || printf ','
      printf '{"full_name":"%s","owner":"%s","default_branch":"%s","parent":"%s"}' \
        "$(json_escape "${FORK_FULLNAME[$i]}")" "$(json_escape "${FORK_OWNER[$i]}")" \
        "$(json_escape "${FORK_DEFAULT_BRANCH[$i]}")" "$(json_escape "${FORK_PARENT[$i]}")"
    done
    printf ']}\n'
  } >"$snap_file"

  local nforks="${#FORK_FULLNAME[@]}"

  if [[ "$JSON_OUT" == true ]]; then
    printf '{"action":"warn-before-private","root":"%s","fork_count":%s,"snapshot_file":"%s","has_forks":%s}\n' \
      "$(json_escape "$root")" "$nforks" "$(json_escape "$snap_file")" "$([[ $nforks -gt 0 ]] && echo true || echo false)"
  else
    log "fork list captured BEFORE any visibility change: ${nforks} fork(s), snapshot written to ${snap_file}"
    if [[ $nforks -gt 0 ]]; then
      log "WARNING: ${root} has ${nforks} fork(s). Making this repo private will DETACH some forks into new network roots and RE-PARENT others underneath them — this scrambles fork/parent relationships and makes any remaining copies HARDER to find afterwards, which is the opposite of what privatizing for cleanup usually intends."
      log "  Review the captured fork list at ${snap_file} and consider a fork sweep ('${SCRIPT_NAME} sweep ${root} --path <path>') BEFORE changing visibility."
    else
      log "no forks found for ${root} — no fork/parent relationships to scramble."
    fi
  fi

  return 0
}

# ── main ─────────────────────────────────────────────────────────────────────
main() {
  parse_args "$@"
  require_tools

  case "$ACTION" in
    sweep)
      do_sweep
      [[ "${#FINDINGS_FULLNAME[@]}" -gt 0 ]] && exit 1
      exit 0
      ;;
    warn-before-private)
      do_warn_before_private
      [[ "${#FORK_FULLNAME[@]}" -gt 0 ]] && exit 1
      exit 0
      ;;
  esac
}

main "$@"
