#!/usr/bin/env bash
# loom help - Show help for Loom CLI
#
# Usage:
#   loom help              Show main help
#   loom help <command>    Show help for specific command
#   loom --help            Same as 'loom help'

set -euo pipefail

# Find repository root
find_repo_root() {
    local dir="$PWD"
    while [[ "$dir" != "/" ]]; do
        if [[ -d "$dir/.loom" ]]; then
            echo "$dir"
            return 0
        fi
        if [[ -f "$dir/.git" ]]; then
            local gitdir
            gitdir=$(sed 's/^gitdir: //' "$dir/.git")
            local main_repo
            main_repo=$(dirname "$(dirname "$(dirname "$gitdir")")")
            if [[ -d "$main_repo/.loom" ]]; then
                echo "$main_repo"
                return 0
            fi
        fi
        dir="$(dirname "$dir")"
    done
    echo ""
}

REPO_ROOT=$(find_repo_root)
CLI_DIR=""
if [[ -n "$REPO_ROOT" ]]; then
    CLI_DIR="$REPO_ROOT/.loom/scripts/cli"
fi

# ANSI colors
# shellcheck disable=SC2034  # Color palette - not all colors used in every script
if [[ -t 1 ]]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    CYAN='\033[0;36m'
    GRAY='\033[0;90m'
    BOLD='\033[1m'
    NC='\033[0m'
else
    RED=''
    GREEN=''
    YELLOW=''
    CYAN=''
    GRAY=''
    BOLD=''
    NC=''
fi

# Show main help
show_main_help() {
    cat <<EOF
${BOLD}Loom CLI - tmux agent pool management${NC}

Loom is a multi-terminal orchestration system for AI-powered development.
This CLI provides management of tmux-backed agent pools.

${YELLOW}USAGE:${NC}
    ./.loom/bin/loom <command> [options]

${YELLOW}COMMANDS:${NC}
    ${GREEN}start${NC}     Spawn agent pool from .loom/config.json
    ${GREEN}status${NC}    Display agent pool state and work queues
    ${GREEN}health${NC}    Diagnostic daemon health check
    ${GREEN}stop${NC}      Graceful shutdown (or --force to kill immediately)
    ${GREEN}attach${NC}    Open live tmux session for an agent
    ${GREEN}send${NC}      Send command to agent session
    ${GREEN}logs${NC}      Tail agent output
    ${GREEN}help${NC}      Show this help message

${YELLOW}QUICK START:${NC}
    ${GRAY}# Start all configured agents${NC}
    ./.loom/bin/loom start

    ${GRAY}# Check what's running${NC}
    ./.loom/bin/loom status

    ${GRAY}# Run diagnostic health check${NC}
    ./.loom/bin/loom health

    ${GRAY}# View an agent's terminal${NC}
    ./.loom/bin/loom attach shepherd-1

    ${GRAY}# Send a command to an agent${NC}
    ./.loom/bin/loom send builder-1 "/loom:sweep 123"

    ${GRAY}# View logs${NC}
    ./.loom/bin/loom logs builder-1

    ${GRAY}# Stop everything${NC}
    ./.loom/bin/loom stop

${YELLOW}EXAMPLES:${NC}
    ./.loom/bin/loom start                  Start all configured agents
    ./.loom/bin/loom start --only builder   Start only builder agents
    ./.loom/bin/loom status                 Show current state
    ./.loom/bin/loom status --json          Machine-readable status
    ./.loom/bin/loom health                 Run diagnostic health check
    ./.loom/bin/loom health --json          Machine-readable health report
    ./.loom/bin/loom attach builder-1       Connect to agent terminal
    ./.loom/bin/loom send builder-1 "/loom:sweep 123"  Send command to agent
    ./.loom/bin/loom stop                   Graceful shutdown
    ./.loom/bin/loom stop --force           Force kill all sessions
    ./.loom/bin/loom stop builder-1         Stop single agent
    ./.loom/bin/loom logs terminal-1        Tail agent output
    ./.loom/bin/loom logs --all             Tail all agent logs

${YELLOW}SSH AND REMOTE:${NC}
    All commands work over SSH without \$DISPLAY.
    For non-interactive scripts, use --yes flag to skip prompts.

${YELLOW}CONFIGURATION:${NC}
    Agents are configured in .loom/config.json:

    {
      "terminals": [
        {
          "id": "terminal-1",
          "name": "Builder",
          "roleConfig": {
            "roleFile": "builder.md"
          }
        }
      ]
    }

${YELLOW}TMUX SOCKET:${NC}
    Loom uses a dedicated tmux socket named "loom" for isolation.
    Sessions are named "loom-<agent-id>" (e.g., loom-builder-1).

    To manually interact with Loom tmux sessions:
    ${GRAY}tmux -L loom list-sessions${NC}
    ${GRAY}tmux -L loom attach -t loom-builder-1${NC}

${YELLOW}FOR MORE HELP:${NC}
    ./.loom/bin/loom <command> --help    Command-specific help
    ./.loom/bin/loom help <command>      Same as above

${YELLOW}RELATED COMMANDS:${NC}
    /loom:sweep <issue>       Orchestrate a single issue lifecycle (Curator → Merge)
    /builder, /judge, etc.    Assume specialized agent roles
    mcp__loom__dispatch_sweep Multi-issue dispatch via loom-daemon (Tier 2)

${GRAY}Loom CLI v0.1.0${NC}
EOF
}

# Show command-specific help
show_command_help() {
    local command="$1"

    case "$command" in
        start)
            if [[ -n "$CLI_DIR" && -f "$CLI_DIR/loom-start.sh" ]]; then
                exec "$CLI_DIR/loom-start.sh" --help
            else
                echo -e "${RED}Error: start command not installed${NC}"
                exit 1
            fi
            ;;
        status)
            if [[ -n "$CLI_DIR" && -f "$CLI_DIR/loom-status.sh" ]]; then
                exec "$CLI_DIR/loom-status.sh" --help
            else
                echo -e "${RED}Error: status command not installed${NC}"
                exit 1
            fi
            ;;
        health)
            # `loom health` now delegates to native `loom-daemon status` (#4274);
            # surface that command's help.
            local loom_daemon_bin=""
            if command -v loom-daemon &>/dev/null; then
                loom_daemon_bin="$(command -v loom-daemon)"
            elif [[ -n "$REPO_ROOT" ]]; then
                # Reuse lib/locate-daemon-bin.sh's shared repo-local candidate
                # generator (#6208) instead of only probing the two hardcoded
                # loom-daemon/target/{release,debug} paths below -- it also
                # honors a redirected $CARGO_TARGET_DIR / ~/.cargo/config.toml's
                # build.target-dir (via `cargo metadata`). Falls back to the
                # original two-candidate probe if the lib is missing (e.g. an
                # unusually stale/partial checkout) so this help text never
                # hard-fails on a missing dependency.
                local _loom_help_locate_bin_lib _loom_help_repo_candidate
                _loom_help_locate_bin_lib="$(cd "$(dirname "${BASH_SOURCE[0]}")/../lib" 2>/dev/null && pwd)/locate-daemon-bin.sh"
                if [[ -r "$_loom_help_locate_bin_lib" ]]; then
                    # shellcheck source=../lib/locate-daemon-bin.sh
                    source "$_loom_help_locate_bin_lib"
                    while IFS= read -r _loom_help_repo_candidate; do
                        if [[ -n "$_loom_help_repo_candidate" && -x "$_loom_help_repo_candidate" ]]; then
                            loom_daemon_bin="$_loom_help_repo_candidate"
                            break
                        fi
                    done < <(_loom_daemon_repo_candidates "$REPO_ROOT")
                elif [[ -x "$REPO_ROOT/loom-daemon/target/release/loom-daemon" ]]; then
                    loom_daemon_bin="$REPO_ROOT/loom-daemon/target/release/loom-daemon"
                elif [[ -x "$REPO_ROOT/loom-daemon/target/debug/loom-daemon" ]]; then
                    loom_daemon_bin="$REPO_ROOT/loom-daemon/target/debug/loom-daemon"
                fi
            fi
            if [[ -n "$loom_daemon_bin" ]]; then
                exec "$loom_daemon_bin" status --help
            else
                echo -e "${RED}Error: health command requires the loom-daemon binary${NC}"
                exit 1
            fi
            ;;
        stop)
            if [[ -n "$CLI_DIR" && -f "$CLI_DIR/loom-stop.sh" ]]; then
                exec "$CLI_DIR/loom-stop.sh" --help
            else
                echo -e "${RED}Error: stop command not installed${NC}"
                exit 1
            fi
            ;;
        attach)
            if [[ -n "$CLI_DIR" && -f "$CLI_DIR/loom-attach.sh" ]]; then
                exec "$CLI_DIR/loom-attach.sh" --help
            else
                echo -e "${RED}Error: attach command not installed${NC}"
                exit 1
            fi
            ;;
        logs)
            if [[ -n "$CLI_DIR" && -f "$CLI_DIR/loom-logs.sh" ]]; then
                exec "$CLI_DIR/loom-logs.sh" --help
            else
                echo -e "${RED}Error: logs command not installed${NC}"
                exit 1
            fi
            ;;
        send)
            if [[ -n "$CLI_DIR" && -f "$CLI_DIR/loom-send.sh" ]]; then
                exec "$CLI_DIR/loom-send.sh" --help
            else
                echo -e "${RED}Error: send command not installed${NC}"
                exit 1
            fi
            ;;
        help)
            show_main_help
            ;;
        *)
            echo -e "${RED}Error: Unknown command '$command'${NC}"
            echo ""
            echo "Available commands: start, status, health, stop, attach, send, logs, help"
            exit 1
            ;;
    esac
}

# Main
main() {
    case "${1:-}" in
        ""|--help|-h)
            show_main_help
            ;;
        *)
            show_command_help "$1"
            ;;
    esac
}

main "$@"
