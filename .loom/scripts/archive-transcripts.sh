#!/usr/bin/env bash
# archive-transcripts.sh - Opt-in, idempotent archival of Claude Code session
# transcripts (and their per-subagent transcripts + meta sidecars) to a durable
# local filesystem location.  Issue #3726.
#
# WHY: session JSONL transcripts under ${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects/
# are the ground-truth record of what every agent did and what it cost, but they
# live only on the local box and are subject to Claude Code's own pruning.  This
# copier snapshots them to a long-term location so a multi-day canary run can be
# audited / cost-harvested after the fact (serves #3725).
#
# OPT-IN.  Does nothing unless archival is enabled via env or config:
#   - env  LOOM_TRANSCRIPT_ARCHIVE=<dir>   (a path enables; ""/off/0/no disables)
#   - config .loom/config.json ->
#       { "loom": { "transcriptArchive": { "enabled": true, "dir": "<dir>" } } }
#   Precedence: env  >  config  >  default(disabled).   (matches guards.rmScope)
#
# LAYOUT AT DESTINATION:
#   <dest>/<repo>/<date>/<session-uuid>/
#       <session-uuid>.jsonl              the session's own transcript
#       <session-uuid>/subagents/agent-*.jsonl        per-subagent transcripts
#       <session-uuid>/subagents/agent-*.meta.json    role+issue sidecars
#       <session-uuid>/tool-results/...   large tool outputs
#       index.json                        agent-id-keyed join key (for #3725)
#
# GUARDRAILS (load-bearing — transcripts can contain secrets):
#   - off by default
#   - destination dir mode 0700, files 0600
#   - if the destination is inside a git repo it MUST be gitignored, else refuse
#   - loud startup banner naming the destination when enabled
#
# Usage:
#   ./.loom/scripts/archive-transcripts.sh                 # sync (env/config-gated)
#   ./.loom/scripts/archive-transcripts.sh --dry-run       # preview, no writes
#   ./.loom/scripts/archive-transcripts.sh --source-cwd DIR # slug from DIR (default $PWD)
#   ./.loom/scripts/archive-transcripts.sh --issue N        # tag index with sweep issue
#   ./.loom/scripts/archive-transcripts.sh --dest DIR       # override destination
#   ./.loom/scripts/archive-transcripts.sh --all-repo-sessions  # also worktree cwds
#
# Cron example (durability backstop every 15 min while a canary runs):
#   */15 * * * * cd /path/to/repo && ./.loom/scripts/archive-transcripts.sh >> .loom/logs/archive-transcripts.log 2>&1
#
# Exit codes:
#   0 - archival disabled (no-op) OR sync completed (possibly copying nothing new)
#   1 - a hard error (e.g. destination inside a repo but not gitignored)

set -uo pipefail

# ------------------------------------------------------------------ output ---
RED='\033[0;31m'; GREEN='\033[0;32m'; BLUE='\033[0;34m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; NC='\033[0m'
error()   { echo -e "${RED}Error: $*${NC}" >&2; exit 1; }
info()    { echo -e "${BLUE}$*${NC}"; }
success() { echo -e "${GREEN}$*${NC}"; }
warning() { echo -e "${YELLOW}$*${NC}" >&2; }
header()  { echo -e "${CYAN}$*${NC}"; }

# ---------------------------------------------------------------- defaults ---
DRY_RUN=false
SOURCE_CWD="$PWD"
OPT_DEST=""
SWEEP_ISSUE=""
ARM=""
ATTEMPT=""
ALL_REPO_SESSIONS=false

# ------------------------------------------------------------------- args ---
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)           DRY_RUN=true; shift ;;
    --source-cwd)        SOURCE_CWD="${2:?--source-cwd needs a path}"; shift 2 ;;
    --dest)              OPT_DEST="${2:?--dest needs a path}"; shift 2 ;;
    --issue)             SWEEP_ISSUE="${2:?--issue needs a number}"; shift 2 ;;
    --arm)               ARM="${2:?--arm needs a value}"; shift 2 ;;
    --attempt)           ATTEMPT="${2:?--attempt needs a value}"; shift 2 ;;
    --all-repo-sessions) ALL_REPO_SESSIONS=true; shift ;;
    -h|--help)
      sed -n '2,50p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) error "Unknown option: $1 (use --help)" ;;
  esac
done

lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }

# slugify a path the way Claude Code names its projects/ subdirs: '/' -> '-'.
slugify() { printf '%s' "$1" | sed 's#/#-#g'; }

# portable mtime epoch (BSD stat then GNU stat).
file_mtime_epoch() { stat -f %m "$1" 2>/dev/null || stat -c %Y "$1" 2>/dev/null || echo 0; }
file_size()        { stat -f %z "$1" 2>/dev/null || stat -c %s "$1" 2>/dev/null || echo 0; }
epoch_to_date()    { date -r "$1" +%Y-%m-%d 2>/dev/null || date -d "@$1" +%Y-%m-%d 2>/dev/null || date +%Y-%m-%d; }
iso_now()          { date -u +%Y-%m-%dT%H:%M:%SZ; }

# ---------------------------------------------- resolve enable + destination ---
# CORRECTION 2: base is CLAUDE_CONFIG_DIR-aware, never hard-coded ~/.claude.
CLAUDE_BASE="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
PROJECTS_DIR="$CLAUDE_BASE/projects"

# Repo root of the thing being archived (best-effort; used for the <repo> label
# and for the config.json read).  A worktree resolves to the worktree root.
REPO_ROOT="$(git -C "$SOURCE_CWD" rev-parse --show-toplevel 2>/dev/null || true)"
REPO_LABEL="$(basename "${REPO_ROOT:-$SOURCE_CWD}")"
CONFIG_JSON="${REPO_ROOT:+$REPO_ROOT/.loom/config.json}"

CFG_ENABLED=false
CFG_DIR=""
if [[ -n "${CONFIG_JSON:-}" && -f "$CONFIG_JSON" ]] && command -v jq >/dev/null 2>&1; then
  # Best-effort: malformed JSON -> jq fails -> disabled default preserved.
  CFG_ENABLED="$(jq -r '.loom.transcriptArchive.enabled // false' "$CONFIG_JSON" 2>/dev/null || echo false)"
  CFG_DIR="$(jq -r '.loom.transcriptArchive.dir // ""' "$CONFIG_JSON" 2>/dev/null || echo "")"
fi

ENABLED=false
DEST=""
if [[ -n "${LOOM_TRANSCRIPT_ARCHIVE+set}" ]]; then
  # env is present (even if empty) -> env wins over config.
  case "$(lower "${LOOM_TRANSCRIPT_ARCHIVE}")" in
    ""|off|0|no|false|disabled) ENABLED=false ;;
    *) ENABLED=true; DEST="$LOOM_TRANSCRIPT_ARCHIVE" ;;
  esac
elif [[ "$CFG_ENABLED" == "true" && -n "$CFG_DIR" ]]; then
  ENABLED=true; DEST="$CFG_DIR"
fi

# Explicit --dest always forces on.
if [[ -n "$OPT_DEST" ]]; then ENABLED=true; DEST="$OPT_DEST"; fi

# Disabled -> silent no-op, zero side effects, zero behaviour change.
if [[ "$ENABLED" != "true" ]]; then
  exit 0
fi

# Enabled but no usable destination -> fail safe to off with a warning.
DEST="${DEST/#\~/$HOME}"
if [[ -z "$DEST" ]]; then
  warning "transcriptArchive enabled but no destination configured — skipping (fail-safe off)."
  exit 0
fi

# ------------------------------------------------------------- guardrails ---
# gitignore-or-refuse: if DEST lives inside a git repo it must be gitignored,
# else we would leak secret-bearing transcripts into a tracked tree.
guard_dest_not_tracked() {
  local dest="$1" probe="$1" top
  # DEST may not exist yet — walk up to the nearest existing ancestor.
  while [[ ! -e "$probe" && "$probe" != "/" && -n "$probe" ]]; do
    probe="$(dirname "$probe")"
  done
  top="$(git -C "$probe" rev-parse --show-toplevel 2>/dev/null || true)"
  [[ -z "$top" ]] && return 0  # not inside any repo -> fine
  # Probe WITH a trailing slash so git treats DEST as a directory: this matches
  # directory-only patterns (e.g. `arch/`) even before the dir is created.
  if git -C "$top" check-ignore -q "${dest%/}/" 2>/dev/null; then
    return 0  # ignored -> fine
  fi
  error "Destination '$dest' is inside git repo '$top' but is NOT gitignored.
  Transcripts may contain secrets (full tool I/O, scrolled .env/token values).
  Refusing to copy them into a tracked tree. Add the path to .gitignore, or
  choose a destination outside the repository."
}
guard_dest_not_tracked "$DEST"

# 0700 dirs.
ensure_dir() { mkdir -p "$1" && chmod 700 "$1"; }

# Loud one-line banner: an operator must always know transcripts are leaving box.
warning "════════════════════════════════════════════════════════════════════"
warning "  TRANSCRIPT ARCHIVAL ENABLED — transcripts (which may contain"
warning "  SECRETS) are being copied off-box to: $DEST"
warning "  You (operator) own the security of this location. See CLAUDE.md."
warning "════════════════════════════════════════════════════════════════════"

# --------------------------------------------------------- copy primitives ---
COPIED=0
SKIPPED=0

# copy_file <src> <dst>: idempotent (skip when same size and dst not older).
copy_file() {
  local src="$1" dst="$2"
  if [[ -f "$dst" ]]; then
    local ss ds sm dm
    ss="$(file_size "$src")"; ds="$(file_size "$dst")"
    sm="$(file_mtime_epoch "$src")"; dm="$(file_mtime_epoch "$dst")"
    if [[ "$ss" == "$ds" && "$sm" -le "$dm" ]]; then
      SKIPPED=$((SKIPPED + 1)); return 0
    fi
  fi
  if [[ "$DRY_RUN" == true ]]; then
    info "  would copy: ${src#"$PROJECTS_DIR"/} -> ${dst#"$DEST"/}"
    COPIED=$((COPIED + 1)); return 0
  fi
  cp -p "$src" "$dst" && chmod 600 "$dst"
  COPIED=$((COPIED + 1))
}

# copy_tree <src_dir> <dst_dir>: mirror every file, 0700 dirs / 0600 files.
copy_tree() {
  local src="$1" dst="$2" rel f target
  [[ -d "$src" ]] || return 0
  while IFS= read -r f; do
    rel="${f#"$src"/}"
    target="$dst/$rel"
    [[ "$DRY_RUN" == true ]] || ensure_dir "$(dirname "$target")"
    copy_file "$f" "$target"
  done < <(find "$src" -type f 2>/dev/null)
}

# --------------------------------------------------------------- index.json ---
# CORRECTION 3: role + issue already exist on disk as agent-*.meta.json sidecars.
# Emit an agent-id-keyed session index adding only the loom-side context the
# sidecar lacks (repo, sweep issue, model, start/end ts, arm/attempt). Schema is
# the #3725 join key — keep it agent-id keyed, one row per subagent.
jsonl_first_ts() { jq -r 'select(.timestamp) | .timestamp' "$1" 2>/dev/null | head -1; }
jsonl_last_ts()  { jq -r 'select(.timestamp) | .timestamp' "$1" 2>/dev/null | tail -1; }
jsonl_model()    { jq -r 'select(.message.model) | .message.model' "$1" 2>/dev/null | grep -v '^null$' | head -1; }

write_index() {
  local sess_dir="$1" uuid="$2" date="$3"
  command -v jq >/dev/null 2>&1 || { warning "jq not found — skipping index.json for $uuid"; return 0; }

  local subdir="$sess_dir/$uuid/subagents"
  local agents_json="[]"
  if [[ -d "$subdir" ]]; then
    local meta agent_id role desc issue jsonl model start end obj
    local acc=""
    while IFS= read -r meta; do
      [[ -e "$meta" ]] || continue
      agent_id="$(basename "$meta" .meta.json)"
      role="$(jq -r '.agentType // ""' "$meta" 2>/dev/null || echo "")"
      desc="$(jq -r '.description // ""' "$meta" 2>/dev/null || echo "")"
      issue="$(printf '%s' "$desc" | grep -oE '#?[0-9]+' | head -1 | tr -d '#' || true)"
      jsonl="$subdir/$agent_id.jsonl"
      model=""; start=""; end=""
      if [[ -f "$jsonl" ]]; then
        model="$(jsonl_model "$jsonl")"
        start="$(jsonl_first_ts "$jsonl")"
        end="$(jsonl_last_ts "$jsonl")"
      fi
      obj="$(jq -n \
        --arg id "$agent_id" --arg role "$role" --arg desc "$desc" \
        --arg issue "$issue" --arg model "$model" \
        --arg start "$start" --arg end "$end" \
        --arg arm "$ARM" --arg attempt "$ATTEMPT" \
        --arg tr "subagents/$agent_id.jsonl" --arg meta "subagents/$agent_id.meta.json" \
        '{agent_id:$id,
          role:(($role|select(.!="")) // null),
          issue:(($issue|select(.!="")|tonumber?) // null),
          description:(($desc|select(.!="")) // null),
          model:(($model|select(.!="")) // null),
          arm:(($arm|select(.!="")) // null),
          attempt:(($attempt|select(.!="")|tonumber?) // null),
          start_ts:(($start|select(.!="")) // null),
          end_ts:(($end|select(.!="")) // null),
          transcript:$tr, meta:$meta}')"
      acc="$acc$obj"$'\n'
    done < <(find "$subdir" -maxdepth 1 -name '*.meta.json' 2>/dev/null | sort)
    if [[ -n "$acc" ]]; then
      agents_json="$(printf '%s' "$acc" | jq -s '.')"
    fi
  fi

  local index
  index="$(jq -n \
    --arg schema "loom.transcript-index/v1" \
    --arg uuid "$uuid" --arg repo "$REPO_LABEL" \
    --arg slug "$SLUG" --arg date "$date" \
    --arg issue "$SWEEP_ISSUE" --arg archived "$(iso_now)" \
    --arg sess "$uuid.jsonl" \
    --argjson agents "$agents_json" \
    '{schema:$schema, session_uuid:$uuid, repo:$repo, source_cwd_slug:$slug,
      date:$date, sweep_issue:(($issue|select(.!="")|tonumber?) // null),
      archived_at:$archived, session_transcript:$sess, agents:$agents}')"

  if [[ "$DRY_RUN" == true ]]; then
    info "  would write index.json for session $uuid ($(printf '%s' "$agents_json" | jq 'length') agent row(s))"
    return 0
  fi
  printf '%s\n' "$index" > "$sess_dir/index.json" && chmod 600 "$sess_dir/index.json"
}

# --------------------------------------------------------------- main sync ---
header "════════════════════════════════════════════"
header "  Loom Transcript Archiver"
[[ "$DRY_RUN" == true ]] && header "  (DRY RUN)"
header "════════════════════════════════════════════"

# Claude Code names its projects/ subdir from the *raw* cwd it was launched in
# (not a symlink-canonicalized path), so slug from SOURCE_CWD — not the
# git-canonicalized REPO_ROOT — or a symlinked checkout (e.g. /var -> /private/var
# on macOS) would miss.  REPO_ROOT is used only for the <repo> label + config read.
SLUG="$(slugify "$SOURCE_CWD")"
REPO_SLUG="$(slugify "${REPO_ROOT:-$SOURCE_CWD}")"

# Which project dirs to sync.  Primary: the exact cwd slug (the sweep session's
# own project dir, which also holds all its subagents/).  --all-repo-sessions
# additionally globs sibling slugs (worktree cwds) sharing the repo prefix.
declare -a PROJ_DIRS=()
if [[ -d "$PROJECTS_DIR/$SLUG" ]]; then PROJ_DIRS+=("$PROJECTS_DIR/$SLUG"); fi
if [[ "$ALL_REPO_SESSIONS" == true ]]; then
  for d in "$PROJECTS_DIR/$REPO_SLUG"*/; do
    [[ -d "$d" ]] || continue
    d="${d%/}"
    [[ "$d" == "$PROJECTS_DIR/$SLUG" ]] && continue
    PROJ_DIRS+=("$d")
  done
fi

if [[ ${#PROJ_DIRS[@]} -eq 0 ]]; then
  info "No transcripts found under $PROJECTS_DIR/$SLUG — nothing to archive."
  exit 0
fi

info "Source : $PROJECTS_DIR/$SLUG"
info "Dest   : $DEST/$REPO_LABEL/<date>/<uuid>/"
echo ""

SESSIONS=0
for proj in "${PROJ_DIRS[@]}"; do
  # Union of session uuids: top-level <uuid>.jsonl stems + companion <uuid>/ dirs.
  uuids="$( { \
      for f in "$proj"/*.jsonl; do [[ -e "$f" ]] && basename "$f" .jsonl; done; \
      for d in "$proj"/*/; do [[ -d "$d" ]] && basename "$d"; done; \
    } 2>/dev/null | sort -u )"

  [[ -z "$uuids" ]] && continue

  while IFS= read -r uuid; do
    [[ -z "$uuid" ]] && continue
    local_jsonl="$proj/$uuid.jsonl"
    local_dir="$proj/$uuid"

    # Date bucket: from the session jsonl mtime, else the companion dir mtime.
    if [[ -f "$local_jsonl" ]]; then
      sdate="$(epoch_to_date "$(file_mtime_epoch "$local_jsonl")")"
    elif [[ -d "$local_dir" ]]; then
      sdate="$(epoch_to_date "$(file_mtime_epoch "$local_dir")")"
    else
      continue
    fi

    sess_dir="$DEST/$REPO_LABEL/$sdate/$uuid"
    [[ "$DRY_RUN" == true ]] || ensure_dir "$sess_dir"

    # 1) the session's own transcript (a FILE at the slug top level).
    [[ -f "$local_jsonl" ]] && copy_file "$local_jsonl" "$sess_dir/$uuid.jsonl"
    # 2) the sibling <uuid>/ companion dir (subagents/ + meta sidecars + tool-results/).
    [[ -d "$local_dir" ]] && copy_tree "$local_dir" "$sess_dir/$uuid"
    # 3) the agent-id-keyed join index.
    write_index "$sess_dir" "$uuid" "$sdate"

    SESSIONS=$((SESSIONS + 1))
  done <<< "$uuids"
done

echo ""
if [[ "$DRY_RUN" == true ]]; then
  success "Dry run: $SESSIONS session(s), would copy $COPIED file(s), skip $SKIPPED unchanged."
else
  success "Archived $SESSIONS session(s): $COPIED file(s) copied, $SKIPPED unchanged (idempotent skip)."
fi
exit 0
