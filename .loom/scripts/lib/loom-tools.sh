#!/bin/bash
# loom-tools.sh - deprecated alias for lib/repo-root.sh (#12941, epic #12708).
# Kept so older installed .loom/scripts copies that still source this path keep
# working; removal is Phase 4. Source lib/repo-root.sh directly instead.
# shellcheck source=repo-root.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/repo-root.sh"
