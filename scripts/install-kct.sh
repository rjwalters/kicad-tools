#!/usr/bin/env bash
# install-kct.sh - Install kicad-tools into a consumer PCB-design repository.
#
# Distribution model (Epic #4054, owner-confirmed): uv-dependency + vendored
# skills (Anvil's model, NOT Loom's full-package vendoring). This installer:
#   1. Adds kicad-tools as a uv dependency in the target's pyproject.toml
#      (git source by default, local path source with --path <dir>).
#   2. Vendors .claude/commands/kct/*.md skills into the target (NEVER touches
#      .claude/commands/loom/ — coexists additively with Loom).
#   3. Appends a guarded, idempotent <!-- BEGIN KICAD-TOOLS --> block to the
#      target's CLAUDE.md carrying the three hard-won Epic #4054 conventions.
#   4. Writes .kct/install-metadata.json for a future uninstaller/upgrader.
#   5. --dry-run prints every planned write and writes nothing.
#
# Usage:
#   ./scripts/install-kct.sh [OPTIONS] <target-repo>
#
# Options:
#   --path <dir>       Install kicad-tools as a LOCAL PATH dependency pointing
#                      at <dir> (a kicad-tools checkout), for sibling-repo
#                      development (e.g. --path ../kicad-tools). Network-free.
#                      Without --path, a git source is used (default).
#   --tag <tag>        Git-source pin: use this tag (git mode only). Default:
#                      the source checkout's kct version tag (v<version>).
#   --rev <rev>        Git-source pin: use this rev/sha (git mode only).
#   --skills=<a,b,c>   Vendor only the listed skills (default: all *.md under
#                      the source .claude/commands/kct/). README is always
#                      vendored (it documents the namespace).
#   --dry-run          Print planned actions, write nothing.
#   -y, --yes          Non-interactive (skip confirmation; auto-enabled when
#                      stdin is not a TTY, e.g. CI/agent shells).
#   -h, --help         Show this help and exit.
#
# Examples:
#   ./scripts/install-kct.sh /path/to/board-repo
#   ./scripts/install-kct.sh --path ../kicad-tools /path/to/board-repo
#   ./scripts/install-kct.sh --dry-run /path/to/board-repo
#   ./scripts/install-kct.sh --skills=ee-review /path/to/board-repo
#
# Prerequisites in the target repo:
#   - A pyproject.toml at the target root. This MVP does NOT `uv init` a
#     non-uv repo; it fails with a clear error naming the missing file
#     (Epic #4054 "least-invasive mechanism" — the happy path is a
#     uv-managed consumer repo).
#
# Re-running the installer is the upgrade/idempotency path: a second run with
# the same args adds no duplicate CLAUDE.md block and no duplicate dependency.

set -euo pipefail

# ----- ANSI colors -----------------------------------------------------------
if [[ -t 1 ]]; then
  RED=$'\033[0;31m'
  GREEN=$'\033[0;32m'
  BLUE=$'\033[0;34m'
  YELLOW=$'\033[1;33m'
  CYAN=$'\033[0;36m'
  NC=$'\033[0m'
else
  RED=""; GREEN=""; BLUE=""; YELLOW=""; CYAN=""; NC=""
fi

error() { echo "${RED}error: $*${NC}" >&2; exit 1; }
info()  { echo "${BLUE}> $*${NC}"; }
ok()    { echo "${GREEN}  ok: $*${NC}"; }
warn()  { echo "${YELLOW}  warn: $*${NC}"; }
note()  { echo "${CYAN}  note: $*${NC}"; }

usage() {
  # The Usage / Options / Examples block lives in the header comment.
  sed -n '4,45p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
}

# ----- CLAUDE.md marker constants -------------------------------------------
KCT_MARK_BEGIN='<!-- BEGIN KICAD-TOOLS -->'
KCT_MARK_END='<!-- END KICAD-TOOLS -->'

# ----- Argument parsing ------------------------------------------------------
SKILLS_FILTER=""
DRY_RUN=false
NON_INTERACTIVE=false
PATH_MODE=false
PATH_DIR=""
GIT_TAG=""
GIT_REV=""
TARGET=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skills=*) SKILLS_FILTER="${1#--skills=}"; [[ -z "$SKILLS_FILTER" ]] && error "--skills requires a comma-separated list"; shift ;;
    --skills)   shift; SKILLS_FILTER="${1:-}"; [[ -z "$SKILLS_FILTER" ]] && error "--skills requires a comma-separated list"; shift ;;
    --path)     shift; PATH_DIR="${1:-}"; [[ -z "$PATH_DIR" ]] && error "--path requires a directory argument"; PATH_MODE=true; shift ;;
    --path=*)   PATH_DIR="${1#--path=}"; [[ -z "$PATH_DIR" ]] && error "--path requires a directory argument"; PATH_MODE=true; shift ;;
    --tag)      shift; GIT_TAG="${1:-}"; [[ -z "$GIT_TAG" ]] && error "--tag requires a value"; shift ;;
    --tag=*)    GIT_TAG="${1#--tag=}"; shift ;;
    --rev)      shift; GIT_REV="${1:-}"; [[ -z "$GIT_REV" ]] && error "--rev requires a value"; shift ;;
    --rev=*)    GIT_REV="${1#--rev=}"; shift ;;
    --dry-run)  DRY_RUN=true; shift ;;
    -y|--yes)   NON_INTERACTIVE=true; shift ;;
    -h|--help)  usage ;;
    --*)        error "unknown option: $1 (run with --help to see usage)" ;;
    *)
      if [[ -n "$TARGET" ]]; then
        error "unexpected extra argument: $1 (target already set to: $TARGET)"
      fi
      TARGET="$1"
      shift
      ;;
  esac
done

if [[ "$PATH_MODE" == true ]] && { [[ -n "$GIT_TAG" ]] || [[ -n "$GIT_REV" ]]; }; then
  error "--path is a local-path source; --tag/--rev only apply to git mode"
fi

# Auto-detect non-interactive mode when stdin is not a TTY (agent/CI shells).
if [[ "$NON_INTERACTIVE" != true ]] && [[ ! -t 0 ]]; then
  NON_INTERACTIVE=true
fi

[[ -z "$TARGET" ]] && error "target repository path required (run with --help to see usage)"

# ----- Stage 1: resolve KCT_ROOT (this installer's source checkout) ---------
info "Stage 1: resolve kicad-tools source root"
KCT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ -f "$KCT_ROOT/pyproject.toml" ]] || error "source root missing pyproject.toml: $KCT_ROOT"
SKILLS_SRC="$KCT_ROOT/.claude/commands/kct"
[[ -d "$SKILLS_SRC" ]] || error "source root missing .claude/commands/kct/: $KCT_ROOT"
ok "KCT_ROOT=$KCT_ROOT"

# Extract kicad-tools version from the source pyproject.toml (first version =).
KCT_VERSION="$(grep -m1 -E '^version[[:space:]]*=' "$KCT_ROOT/pyproject.toml" \
  | sed -E 's/^version[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/')"
[[ -n "$KCT_VERSION" ]] || error "could not extract version from $KCT_ROOT/pyproject.toml"
ok "KCT_VERSION=$KCT_VERSION"

# Source checkout short SHA (empty if not a git checkout — tolerated).
KCT_COMMIT="$(git -C "$KCT_ROOT" rev-parse --short HEAD 2>/dev/null || echo "")"
INSTALL_DATE="$(date +%Y-%m-%d)"
KCT_GIT_URL="https://github.com/rjwalters/kicad-tools"

# ----- Stage 2: resolve and validate TARGET ---------------------------------
info "Stage 2: resolve and validate target"
TARGET="${TARGET/#\~/$HOME}"
[[ -d "$TARGET" ]] || error "target directory does not exist: $TARGET"
TARGET="$(cd "$TARGET" && pwd)"
ok "TARGET=$TARGET"

if [[ "$TARGET" == "$KCT_ROOT" ]]; then
  error "refusing to install kicad-tools into its own source checkout ($KCT_ROOT)"
fi

TARGET_PYPROJECT="$TARGET/pyproject.toml"
if [[ ! -f "$TARGET_PYPROJECT" ]]; then
  error "target has no pyproject.toml: $TARGET
       kicad-tools installs as a uv dependency and requires a uv-managed
       consumer repo. Run 'uv init' in the target first, then re-run."
fi

# ----- Stage 3: resolve source mode + ref -----------------------------------
info "Stage 3: resolve source mode"
if [[ "$PATH_MODE" == true ]]; then
  PATH_DIR="${PATH_DIR/#\~/$HOME}"
  [[ -d "$PATH_DIR" ]] || error "--path directory does not exist: $PATH_DIR"
  RESOLVED_PATH="$(cd "$PATH_DIR" && pwd)"
  [[ -f "$RESOLVED_PATH/pyproject.toml" ]] || error "--path is not a kicad-tools checkout (no pyproject.toml): $RESOLVED_PATH"
  SOURCE_MODE="path"
  SOURCE_REF="$RESOLVED_PATH"
  # Record the path source's own short SHA when available.
  PATH_COMMIT="$(git -C "$RESOLVED_PATH" rev-parse --short HEAD 2>/dev/null || echo "")"
  [[ -n "$PATH_COMMIT" ]] && KCT_COMMIT="$PATH_COMMIT"
  note "path mode: local source at $RESOLVED_PATH (network-free)"
else
  SOURCE_MODE="git"
  # Default the git pin to the source version tag when neither --tag nor --rev
  # was given, so a bare `install-kct.sh <target>` pins reproducibly.
  if [[ -z "$GIT_TAG" && -z "$GIT_REV" ]]; then
    GIT_TAG="v$KCT_VERSION"
  fi
  if [[ -n "$GIT_TAG" ]]; then
    SOURCE_REF="$KCT_GIT_URL@$GIT_TAG"
  else
    SOURCE_REF="$KCT_GIT_URL@$GIT_REV"
  fi
  note "git mode: $SOURCE_REF"
fi
ok "source_mode=$SOURCE_MODE"

# ----- Stage 4: enumerate + filter skills -----------------------------------
info "Stage 4: enumerate source skills"
# Glob *.md under the source skills dir at install time (Curator: never
# hardcode the file list — Child 3 additions must be picked up automatically).
# README.md documents the namespace and is always vendored; the remaining *.md
# files are the selectable skills.
ALL_SKILLS=()
while IFS= read -r -d '' md; do
  base="$(basename "$md" .md)"
  [[ "$base" == "README" ]] && continue
  ALL_SKILLS+=("$base")
done < <(find "$SKILLS_SRC" -maxdepth 1 -name '*.md' -type f -print0 | LC_ALL=C sort -z)

[[ ${#ALL_SKILLS[@]} -gt 0 ]] || error "no skills found under $SKILLS_SRC/*.md"
note "available skills: ${ALL_SKILLS[*]}"

SELECTED_SKILLS=()
if [[ -n "$SKILLS_FILTER" ]]; then
  IFS=',' read -r -a REQUESTED <<< "$SKILLS_FILTER"
  for s in "${REQUESTED[@]}"; do
    s="$(echo "$s" | tr -d '[:space:]')"
    [[ -z "$s" ]] && continue
    found=false
    for avail in "${ALL_SKILLS[@]}"; do
      [[ "$avail" == "$s" ]] && { found=true; break; }
    done
    $found || error "unknown skill: $s; available: ${ALL_SKILLS[*]}"
    SELECTED_SKILLS+=("$s")
  done
  [[ ${#SELECTED_SKILLS[@]} -gt 0 ]] || error "--skills= was empty after filtering"
else
  SELECTED_SKILLS=("${ALL_SKILLS[@]}")
fi
ok "selected: ${SELECTED_SKILLS[*]}"

# ----- Confirmation prompt --------------------------------------------------
if [[ "$NON_INTERACTIVE" != true ]] && [[ "$DRY_RUN" != true ]]; then
  echo ""
  echo "About to install kicad-tools v$KCT_VERSION into: $TARGET"
  echo "Source: $SOURCE_MODE ($SOURCE_REF)"
  echo "Skills: ${SELECTED_SKILLS[*]}"
  echo ""
  read -r -p "Proceed? [y/N] " -n 1 reply
  echo ""
  [[ "$reply" =~ ^[Yy]$ ]] || { info "cancelled"; exit 0; }
fi

# ----- Helpers --------------------------------------------------------------
# Run a write action, or print it under --dry-run.
do_action() {
  local desc="$1"; shift
  if [[ "$DRY_RUN" == true ]]; then
    echo "  [dry-run] $desc"
  else
    "$@"
  fi
}

# Does the target pyproject.toml already declare a whole-word `kicad-tools`
# dependency in [project.dependencies]? Matches the bare name at a value
# boundary so `kicad-tools-extra` does not false-positive.
target_declares_kct_dep() {
  # A PEP 621 dependency entry looks like `"kicad-tools"` or
  # `"kicad-tools @ ..."` or `"kicad-tools>=1.0"`; the name is always
  # immediately after an opening quote and ends at a quote, space, or a
  # version/marker operator. Anchor on the opening quote + name + a
  # terminator that is NOT `-` (which would be part of a longer name).
  grep -Eq '"kicad-tools([[:space:]]|"|[<>=!~@]|;)' "$TARGET_PYPROJECT"
}

# Does [tool.uv.sources] already carry a kicad-tools entry, and does it match
# the requested source mode + ref? Echoes "match", "mismatch", or "absent".
uv_source_state() {
  local line
  line="$(grep -E '^[[:space:]]*kicad-tools[[:space:]]*=' "$TARGET_PYPROJECT" | head -n1 || true)"
  if [[ -z "$line" ]]; then
    echo "absent"; return
  fi
  if [[ "$SOURCE_MODE" == "path" ]]; then
    # uv records the path relative to the target's pyproject.toml, so compare
    # by resolving whatever path uv wrote back to an absolute directory rather
    # than string-matching the (possibly relative) literal.
    local recorded
    recorded="$(printf '%s' "$line" | sed -nE 's/.*path[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/p')"
    if [[ -n "$recorded" ]]; then
      local abs
      abs="$(cd "$TARGET" && cd "$recorded" 2>/dev/null && pwd || echo "")"
      if [[ "$line" == *"path"* && "$abs" == "$RESOLVED_PATH" ]]; then
        echo "match"; else echo "mismatch"; fi
    else
      echo "mismatch"
    fi
  else
    # git mode: match on the URL and the specific tag/rev value.
    local pin="${GIT_TAG:-$GIT_REV}"
    if [[ "$line" == *"git"* && "$line" == *"$KCT_GIT_URL"* && "$line" == *"$pin"* ]]; then
      echo "match"; else echo "mismatch"; fi
  fi
}

# Build the uv add invocation for the resolved source mode.
run_uv_add() {
  if ! command -v uv >/dev/null 2>&1; then
    error "uv not found on PATH but required to add the kicad-tools dependency"
  fi
  if [[ "$SOURCE_MODE" == "path" ]]; then
    # Idiomatic uv path-dependency form: pass the directory itself as the
    # package argument; uv detects the local pyproject.toml and records a
    # path source under [tool.uv.sources]. --editable makes it an editable
    # path install (sibling-repo dev workflow).
    # --frozen writes the pyproject/[tool.uv.sources] entry WITHOUT resolving
    # or syncing the environment — keeps path mode network-free (Epic #4054
    # CI-no-network constraint). The operator runs `uv sync` afterwards.
    ( cd "$TARGET" && uv add "$RESOLVED_PATH" --editable --frozen )
  elif [[ -n "$GIT_TAG" ]]; then
    ( cd "$TARGET" && uv add kicad-tools --git "$KCT_GIT_URL" --tag "$GIT_TAG" )
  else
    ( cd "$TARGET" && uv add kicad-tools --git "$KCT_GIT_URL" --rev "$GIT_REV" )
  fi
}

# ----- Stage 5: add the kicad-tools uv dependency ---------------------------
# Idempotency (Curator's 4-step algorithm): only call `uv add` when the dep is
# absent OR present-but-pointing-at-a-different-source. If present and the
# source entry already matches the requested mode/ref, no-op.
info "Stage 5: kicad-tools uv dependency"
if target_declares_kct_dep; then
  case "$(uv_source_state)" in
    match)
      ok "kicad-tools dependency already present and up to date (no-op)"
      ;;
    mismatch)
      warn "kicad-tools present but points at a different source; updating to $SOURCE_MODE"
      do_action "uv add kicad-tools ($SOURCE_MODE: $SOURCE_REF) [update]" run_uv_add
      ;;
    absent)
      note "kicad-tools listed in dependencies but no [tool.uv.sources] entry; adding source"
      do_action "uv add kicad-tools ($SOURCE_MODE: $SOURCE_REF)" run_uv_add
      ;;
  esac
else
  do_action "uv add kicad-tools ($SOURCE_MODE: $SOURCE_REF)" run_uv_add
fi

# ----- Stage 6: vendor .claude/commands/kct/ skills -------------------------
# Copy README.md (always) plus each selected skill's <name>.md. NEVER touch
# .claude/commands/loom/ (coexist additively with Loom).
info "Stage 6: vendor .claude/commands/kct/ skills"
DST_SKILLS_DIR="$TARGET/.claude/commands/kct"
VENDORED_FILES=()  # target-relative paths, for the metadata manifest.

vendor_file() {
  local src="$1" dst="$2"
  mkdir -p "$(dirname "$dst")"
  cp "$src" "$dst"
  chmod 0644 "$dst"
}

# README.md is always vendored (documents the namespace convention).
do_action "vendor .claude/commands/kct/README.md" \
  vendor_file "$SKILLS_SRC/README.md" "$DST_SKILLS_DIR/README.md"
VENDORED_FILES+=(".claude/commands/kct/README.md")

for s in "${SELECTED_SKILLS[@]}"; do
  do_action "vendor .claude/commands/kct/$s.md" \
    vendor_file "$SKILLS_SRC/$s.md" "$DST_SKILLS_DIR/$s.md"
  VENDORED_FILES+=(".claude/commands/kct/$s.md")
done
ok "vendored ${#VENDORED_FILES[@]} skill file(s)"

# ----- Stage 7: guarded CLAUDE.md block -------------------------------------
info "Stage 7: CLAUDE.md guarded block"
CLAUDE_MD="$TARGET/CLAUDE.md"

# The block literally carries the three load-bearing Epic #4054 conventions.
# It must survive even if the target's CLAUDE.md diverges from ours, so the
# substance is inlined, not linked.
NEW_BLOCK="$KCT_MARK_BEGIN
## kicad-tools ($KCT_VERSION)

This repo uses [kicad-tools](https://github.com/rjwalters/kicad-tools) (\`kct\`)
for PCB design/routing/DRC. Skills live under \`.claude/commands/kct/\` and are
invoked as \`/kct:<name>\`. This block is managed by \`install-kct.sh\` — edit
outside the markers only; re-running the installer replaces it in place.

### Conventions (load-bearing — do not drop)
1. **Build the C++ router backend after every fresh checkout/worktree.** Run
   \`uv run kct build-native\` (verify with \`--check\`). \`uv sync\` alone does
   NOT build the native extension; a missing backend makes routing 10-100x
   slower (multi-minute-per-net).
2. **Cross-gate DRC for manufacturing sign-off.** \`kct check\` alone is
   insufficient — always also run \`kicad-cli pcb drc --refill-zones\`. \`kct check\`
   can miss marginals (e.g. 0.1000 vs 0.1016 mm) and read stale zone fills.
3. **Artifact-first.** The committed board artifact is shipping truth. A
   \`generate_design.py\`/regeneration may diverge by design and is NOT
   authoritative over a committed board.
$KCT_MARK_END"

# Validate the kicad-tools marker structure of a CLAUDE.md. Echoes a
# human-readable reason and returns non-zero when the markers are malformed
# (unterminated BEGIN, or an END that appears before its BEGIN). Shared by the
# real merge and the --dry-run preview so both agree on what will happen.
#
# A malformed file MUST NOT be edited: an unterminated BEGIN would cause every
# line after it to be silently dropped, then clobber the original on `mv`.
claude_md_marker_error() {
  local file="$1"
  local line depth=0
  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" == *"$KCT_MARK_BEGIN"* ]]; then
      depth=$((depth + 1))
    elif [[ "$line" == *"$KCT_MARK_END"* ]]; then
      if [[ "$depth" -eq 0 ]]; then
        echo "CLAUDE.md has an $KCT_MARK_END before any $KCT_MARK_BEGIN; refusing to edit — fix the markers and re-run"
        return 1
      fi
      depth=$((depth - 1))
    fi
  done < "$file"
  if [[ "$depth" -ne 0 ]]; then
    echo "CLAUDE.md has an unterminated $KCT_MARK_BEGIN block (no $KCT_MARK_END marker); refusing to edit — fix the markers and re-run"
    return 1
  fi
  return 0
}

merge_claude_md() {
  if [[ ! -f "$CLAUDE_MD" ]]; then
    printf '%s\n' "$NEW_BLOCK" > "$CLAUDE_MD"
    return
  fi

  if grep -qF "$KCT_MARK_BEGIN" "$CLAUDE_MD"; then
    # Abort on a malformed target rather than risk silent data loss.
    local marker_err
    if ! marker_err="$(claude_md_marker_error "$CLAUDE_MD")"; then
      error "$marker_err"
    fi

    # Replace the marked block in place. Pure-bash line rebuild (BSD-sed-safe;
    # no GNU `sed -i`). Preserves everything outside the markers, including any
    # Loom or Anvil block.
    local tmp in_block=0 replaced=0
    tmp="$(mktemp)"
    while IFS= read -r line || [[ -n "$line" ]]; do
      if [[ "$in_block" -eq 0 ]]; then
        if [[ "$line" == *"$KCT_MARK_BEGIN"* ]]; then
          printf '%s\n' "$NEW_BLOCK" >> "$tmp"
          in_block=1
          replaced=1
        else
          printf '%s\n' "$line" >> "$tmp"
        fi
      else
        if [[ "$line" == *"$KCT_MARK_END"* ]]; then
          in_block=0
        fi
      fi
    done < "$CLAUDE_MD"
    if [[ "$replaced" -eq 0 ]]; then
      rm -f "$tmp"
      return 1
    fi
    mv "$tmp" "$CLAUDE_MD"
    return
  fi

  # Existing CLAUDE.md, no kicad-tools markers: append after a blank line,
  # preserving all existing content untouched.
  local existing
  existing="$(cat "$CLAUDE_MD")"
  printf '%s\n\n%s\n' "${existing%$'\n'}" "$NEW_BLOCK" > "$CLAUDE_MD"
}

if [[ "$DRY_RUN" == true ]]; then
  if [[ ! -f "$CLAUDE_MD" ]]; then
    echo "  [dry-run] create CLAUDE.md with kicad-tools marker block"
  elif grep -qF "$KCT_MARK_BEGIN" "$CLAUDE_MD"; then
    marker_err="$(claude_md_marker_error "$CLAUDE_MD")" || error "$marker_err"
    echo "  [dry-run] replace existing kicad-tools block in CLAUDE.md (in place)"
  else
    echo "  [dry-run] append kicad-tools marker block to CLAUDE.md (preserves existing content)"
  fi
else
  merge_claude_md
  ok "CLAUDE.md updated"
fi

# ----- Stage 8: install metadata --------------------------------------------
info "Stage 8: install metadata"
METADATA_DIR="$TARGET/.kct"
METADATA="$METADATA_DIR/install-metadata.json"

write_metadata() {
  mkdir -p "$METADATA_DIR"
  # Emit skills_selected and installed_files as JSON arrays.
  local skills_json files_json
  skills_json="$(printf '%s\n' "${SELECTED_SKILLS[@]}" \
    | awk 'BEGIN{printf "["} {printf "%s\"%s\"", (NR>1?", ":""), $0} END{printf "]"}')"
  files_json="$(printf '%s\n' "${VENDORED_FILES[@]}" \
    | awk 'BEGIN{printf "["} {printf "%s\"%s\"", (NR>1?", ":""), $0} END{printf "]"}')"
  cat > "$METADATA" <<META_EOF
{
  "kct_version": "$KCT_VERSION",
  "kct_commit": "$KCT_COMMIT",
  "install_date": "$INSTALL_DATE",
  "source_mode": "$SOURCE_MODE",
  "source_ref": "$SOURCE_REF",
  "skills_selected": $skills_json,
  "installed_files": $files_json
}
META_EOF
}

do_action "write .kct/install-metadata.json" write_metadata
[[ "$DRY_RUN" == true ]] || ok "wrote $METADATA"

# ----- Summary --------------------------------------------------------------
echo ""
if [[ "$DRY_RUN" == true ]]; then
  info "dry-run complete — no files written to $TARGET"
else
  info "kicad-tools v$KCT_VERSION installed into $TARGET"
  note "source: $SOURCE_MODE ($SOURCE_REF)"
  note "skills: ${SELECTED_SKILLS[*]}"
  note "next: run 'uv sync' in the target, then 'uv run kct build-native' (see the CLAUDE.md block)"
fi
