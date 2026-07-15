#!/usr/bin/env bash
# install-kct.sh - Install kicad-tools into a consumer PCB-design repository.
#
# Distribution model (Epic #4054, owner-confirmed): uv-dependency + vendored
# skills (Anvil's model, NOT Loom's full-package vendoring). This installer:
#   1. Adds kicad-tools as a uv dependency in the target's pyproject.toml
#      (git source by default, local path source with --path <dir>).
#   2. Vendors .claude/commands/kct/*.md skills into the target (NEVER touches
#      .claude/commands/loom/ — coexists additively with Loom).
#   3. Vendors the portable CI gate scripts (scripts/ci/check_copper_lvs.py,
#      check_routed_drc.py, net_class_map_resolver.py) into the target's
#      .kct/ci/ (Epic #4054 Child 2, #4056). These are stdlib+yaml only, take
#      the board path as a CLI argument, and gate copper-LVS / routed-DRC in
#      the consumer's own CI. Copied verbatim as a sibling triple so
#      check_routed_drc.py's local `from net_class_map_resolver import ...`
#      (a sys.path insert relative to the script's own dir) still resolves.
#   4. Appends a guarded, idempotent <!-- BEGIN KICAD-TOOLS --> block to the
#      target's CLAUDE.md carrying the three hard-won Epic #4054 conventions.
#   5. Writes .kct/install-metadata.json for a future uninstaller/upgrader.
#   6. --dry-run prints every planned write and writes nothing.
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
CI_GATES_SRC="$KCT_ROOT/scripts/ci"
[[ -d "$CI_GATES_SRC" ]] || error "source root missing scripts/ci/: $KCT_ROOT"
ok "KCT_ROOT=$KCT_ROOT"

# The portable CI gate scripts vendored into the consumer's .kct/ci/ (#4056).
# This list is DELIBERATELY explicit, not a glob: only these three gates are
# consumer-generic (stdlib+yaml, CLI-arg-driven, no boards/ assumption). The
# board-specific / repo-internal gates in scripts/ci/ (check_board_00_e2e.py,
# check_board_05_blocking.py, check_diffpair_coverage.py, etc.) are NOT vendored.
# They are copied verbatim as a sibling triple: check_routed_drc.py imports
# net_class_map_resolver via a sys.path insert relative to its OWN directory,
# so the three must land together in one dir for that import to resolve.
CI_GATE_FILES=(
  "check_copper_lvs.py"
  "check_routed_drc.py"
  "net_class_map_resolver.py"
)

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
    # uv >= 0.5 dropped `--git`; the supported spelling is a git+ URL with
    # the ref appended (works across uv versions).
    ( cd "$TARGET" && uv add "kicad-tools @ git+${KCT_GIT_URL}@${GIT_TAG}" )
  else
    ( cd "$TARGET" && uv add "kicad-tools @ git+${KCT_GIT_URL}@${GIT_REV}" )
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

# ----- Stage 6b: vendor portable CI gate scripts (#4056) --------------------
# Copy the three consumer-generic gates verbatim into .kct/ci/ as a sibling
# triple. Executable (chmod 0755) so a consumer can invoke them directly; also
# recorded in installed_files for the upgrader. Re-running the installer
# overwrites them from the (possibly newer) source checkout — the documented
# `uv lock --upgrade` + re-run refresh path (no separate versioning scheme).
info "Stage 6b: vendor portable CI gate scripts into .kct/ci/"
DST_CI_DIR="$TARGET/.kct/ci"

vendor_ci_gate() {
  local src="$1" dst="$2"
  mkdir -p "$(dirname "$dst")"
  cp "$src" "$dst"
  chmod 0755 "$dst"
}

for gate in "${CI_GATE_FILES[@]}"; do
  [[ -f "$CI_GATES_SRC/$gate" ]] || error "source CI gate missing: $CI_GATES_SRC/$gate"
  do_action "vendor .kct/ci/$gate" \
    vendor_ci_gate "$CI_GATES_SRC/$gate" "$DST_CI_DIR/$gate"
  VENDORED_FILES+=(".kct/ci/$gate")
done
# Ship a short README next to the gates documenting the consumer-side
# invocation (path-as-CLI-arg, --allow N instead of the repo-internal allowlist).
CI_README_DST="$DST_CI_DIR/README.md"
write_ci_readme() {
  mkdir -p "$DST_CI_DIR"
  cat > "$CI_README_DST" <<'CI_README_EOF'
# Portable kicad-tools CI gates (`.kct/ci/`)

These scripts are vendored by `install-kct.sh` (kicad-tools Epic #4054,
Child #4056). They are copies of `scripts/ci/*.py` from the kicad-tools
source repo — **installer-managed**: re-running `install-kct.sh` (the
`uv lock --upgrade` + re-run upgrade path) overwrites them. Edit the
upstream originals, not these copies.

They are stdlib + `yaml` only (no `kicad_tools` import) and take the board
path as a CLI argument, so they run against **your** board path — there is
no `boards/` assumption.

## `check_copper_lvs.py` — copper-LVS gate

Run the checker from the `kicad_tools` package (available via the uv
dependency) to emit a JSON verdict, then gate on it:

```bash
uv run python -m kicad_tools.lvs.copper_lvs \
  hardware/<your-board>/<board>.kicad_sch \
  hardware/<your-board>/output/<board>_routed.kicad_pcb \
  > /tmp/copper-lvs.json
uv run python .kct/ci/check_copper_lvs.py /tmp/copper-lvs.json
```

Exit `0` = clean, `2` = a short/open was found, `1` = usage/tool error.

## `check_routed_drc.py` — routed-DRC blocking-error gate

Pass your routed `*.kicad_pcb` path(s). Use `--allow N` to set the tolerance
explicitly. Do **not** rely on the default `--allowlist`
(`.github/routed-drc-tolerance.yml`) — that is the kicad-tools repo's own
per-board grandfather list and does not exist in a fresh consumer repo.

The gate checks at `--mfr jlcpcb` by default. If your board targets a
different fab tier (e.g. `jlcpcb-tier1` for in-pad-via rescue), name it
explicitly with `--mfr` — there is no need to author an allowlist YAML just
to set the profile in a fresh consumer repo:

```bash
# Fail on any blocking DRC error (fresh repo, no grandfathered history):
uv run python .kct/ci/check_routed_drc.py \
  hardware/<your-board>/output/<board>_routed.kicad_pcb \
  --allow 0

# Same, gating at a non-default fab tier:
uv run python .kct/ci/check_routed_drc.py \
  hardware/<your-board>/output/<board>_routed.kicad_pcb \
  --mfr jlcpcb-tier1 --allow 0
```

Exit `0` = within tolerance, `2` = exceeded (job fails), `1` = tool error.
Passing zero files is a no-op that exits `0` (a first CI run with no changed
`*_routed.kicad_pcb` files passes).

`net_class_map_resolver.py` is a dependency of `check_routed_drc.py`; keep
all three files together in this directory so its sibling import resolves.
CI_README_EOF
}
do_action "write .kct/ci/README.md" write_ci_readme
VENDORED_FILES+=(".kct/ci/README.md")
ok "vendored ${#CI_GATE_FILES[@]} CI gate script(s) + README into .kct/ci/"

# ----- Stage 6c: vendor load-bearing conventions into .kct/CONVENTIONS.md ----
# The three load-bearing Epic #4054 conventions live here verbatim. This file
# is the "guaranteed-present, survives CLAUDE.md divergence" artifact: the
# installer owns it outright and overwrites it on every run (same refresh
# semantics as .kct/ci/README.md and .kct/install-metadata.json). Moving the
# substance out of the CLAUDE.md marker block — a file a consumer may hand-edit
# — into this installer-owned file preserves the divergence guarantee by
# construction while keeping the root CLAUDE.md block a lightweight pointer.
info "Stage 6c: vendor load-bearing conventions into .kct/CONVENTIONS.md"
CONVENTIONS_DST="$TARGET/.kct/CONVENTIONS.md"

write_conventions() {
  mkdir -p "$TARGET/.kct"
  cat > "$CONVENTIONS_DST" <<'CONVENTIONS_EOF'
# kicad-tools load-bearing conventions (`.kct/CONVENTIONS.md`)

These conventions are vendored by `install-kct.sh` (kicad-tools Epic #4054) and
**installer-managed**: re-running the installer overwrites this file. Edit the
upstream originals, not this copy. Read these before routing or manufacturing
sign-off.

1. **Build the C++ router backend after every fresh checkout/worktree.** Run
   `uv run kct build-native` (verify with `--check`). `uv sync` alone does
   NOT build the native extension; a missing backend makes routing 10-100x
   slower (multi-minute-per-net).
2. **Cross-gate DRC for manufacturing sign-off.** `kct check` alone is
   insufficient — always also run `kicad-cli pcb drc --refill-zones`. `kct check`
   can miss marginals (e.g. 0.1000 vs 0.1016 mm) and read stale zone fills.
3. **Artifact-first.** The committed board artifact is shipping truth. A
   `generate_design.py`/regeneration may diverge by design and is NOT
   authoritative over a committed board.
CONVENTIONS_EOF
}
do_action "write .kct/CONVENTIONS.md" write_conventions
VENDORED_FILES+=(".kct/CONVENTIONS.md")
ok "vendored load-bearing conventions into .kct/CONVENTIONS.md"

# ----- Stage 7: guarded CLAUDE.md block -------------------------------------
info "Stage 7: CLAUDE.md guarded block"
CLAUDE_MD="$TARGET/CLAUDE.md"

# The block is a lightweight pointer (matching the Loom/Repo-Skills pattern):
# it references the vendored files rather than inlining their substance. The
# load-bearing conventions live verbatim in .kct/CONVENTIONS.md — a file the
# installer owns outright and rewrites on every run (see Stage 6c) — so they
# survive CLAUDE.md drift by construction, without needing to be inlined into a
# file the consumer may hand-edit.
NEW_BLOCK="$KCT_MARK_BEGIN
## kicad-tools ($KCT_VERSION)

This repo uses [kicad-tools](https://github.com/rjwalters/kicad-tools) (\`kct\`)
for PCB design/routing/DRC. Skills: \`/kct:<name>\` (see
\`.claude/commands/kct/README.md\`). **Load-bearing conventions (native backend
build, cross-gate DRC, artifact-first): \`.kct/CONVENTIONS.md\` — read before
routing or sign-off.** Managed by \`install-kct.sh\` — edit outside the markers
only; re-running the installer replaces it in place.
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
