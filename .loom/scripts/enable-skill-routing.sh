#!/usr/bin/env bash
# enable-skill-routing.sh - Enable skill routing for agent suggestions
#
# Copies the default skill-routes.json config into place, activating
# the UserPromptSubmit hook that suggests Loom agents based on prompt content.
#
# Usage:
#   ./.loom/scripts/enable-skill-routing.sh          # Enable with defaults
#   ./.loom/scripts/enable-skill-routing.sh --force   # Overwrite existing config
#   ./.loom/scripts/enable-skill-routing.sh --disable # Remove config (disable routing)

set -euo pipefail

# Determine repo root
MAIN_ROOT="$(cd "$(git rev-parse --git-common-dir 2>/dev/null)/.." 2>/dev/null && pwd)" || {
    echo "Error: Could not determine repository root" >&2
    exit 1
}

CONFIG_DIR="${MAIN_ROOT}/.loom/config"
CONFIG_FILE="${CONFIG_DIR}/skill-routes.json"

# Find the Loom source repo for default config
LOOM_SOURCE=""
if [[ -f "${MAIN_ROOT}/.loom/loom-source-path" ]]; then
    LOOM_SOURCE=$(cat "${MAIN_ROOT}/.loom/loom-source-path" 2>/dev/null)
fi

# Check for default config in Loom source
DEFAULT_CONFIG=""
if [[ -n "$LOOM_SOURCE" ]] && [[ -f "${LOOM_SOURCE}/defaults/config/skill-routes.json" ]]; then
    DEFAULT_CONFIG="${LOOM_SOURCE}/defaults/config/skill-routes.json"
fi

FORCE=false
DISABLE=false

for arg in "$@"; do
    case "$arg" in
        --force|-f) FORCE=true ;;
        --disable) DISABLE=true ;;
        --help|-h)
            echo "Usage: $0 [--force] [--disable]"
            echo ""
            echo "Enable or disable skill routing for agent suggestions."
            echo ""
            echo "Options:"
            echo "  --force    Overwrite existing skill-routes.json"
            echo "  --disable  Remove skill-routes.json (disable routing)"
            echo ""
            echo "The routing hook is always installed but only activates when"
            echo ".loom/config/skill-routes.json exists."
            exit 0
            ;;
    esac
done

if [[ "$DISABLE" == "true" ]]; then
    if [[ -f "$CONFIG_FILE" ]]; then
        rm "$CONFIG_FILE"
        echo "Skill routing disabled (removed $CONFIG_FILE)"
    else
        echo "Skill routing is already disabled (no config file found)"
    fi
    exit 0
fi

if [[ -f "$CONFIG_FILE" ]] && [[ "$FORCE" != "true" ]]; then
    echo "Skill routing is already enabled ($CONFIG_FILE exists)"
    echo "Use --force to overwrite with defaults"
    exit 0
fi

mkdir -p "$CONFIG_DIR"

if [[ -n "$DEFAULT_CONFIG" ]]; then
    cp "$DEFAULT_CONFIG" "$CONFIG_FILE"
    echo "Skill routing enabled (copied default routes from Loom source)"
else
    # Inline minimal default if Loom source not available
    cat > "$CONFIG_FILE" <<'ROUTES'
{
  "version": 1,
  "description": "Skill routing table for Loom agent suggestions.",
  "routes": [
    { "pattern": "shepherd|orchestrate|lifecycle|end.to.end", "agent": "/shepherd", "description": "Full issue lifecycle orchestration" },
    { "pattern": "architect|design|proposal|system design|rfc", "agent": "/architect", "description": "System design and architecture" },
    { "pattern": "review|\\bPR\\b|pull request|code review", "agent": "/judge", "description": "Code review" },
    { "pattern": "fix|bug|broken|changes.requested|merge.conflict", "agent": "/doctor", "description": "Bug fixes and PR feedback" },
    { "pattern": "simplify|refactor|clean.?up|dead.?code|complexity", "agent": "/hermit", "description": "Simplification opportunities" },
    { "pattern": "build|implement|feature|develop|code", "agent": "/builder", "description": "Implement features and fixes" },
    { "pattern": "curate|triage|issue|enhance|enrich", "agent": "/curator", "description": "Issue enrichment" },
    { "pattern": "prioriti[zs]e|backlog|roadmap", "agent": "/guide", "description": "Backlog triage" },
    { "pattern": "audit|validate|check.?build", "agent": "/auditor", "description": "Build validation" },
    { "pattern": "loom|daemon|auto.?build", "agent": "/loom", "description": "System daemon orchestration" }
  ]
}
ROUTES
    echo "Skill routing enabled (created default routes)"
fi

echo "Config: $CONFIG_FILE"
echo ""
echo "To customize routes, edit the config file directly or create"
echo ".loom/config/skill-routes.local.json for untracked overrides."
