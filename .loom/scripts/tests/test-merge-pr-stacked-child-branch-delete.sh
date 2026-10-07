#!/usr/bin/env bash
# test-merge-pr-stacked-child-branch-delete.sh — #9372: merge-pr.sh must not
# delete a merged parent's remote branch while open PRs still target it (GitHub
# closes such children unrecoverably). The delete block is extracted from the
# script by its anchor lines and eval'd with a stubbed loom-daemon / forge.
# shellcheck disable=SC2034
set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$(cd "$TEST_DIR/.." && pwd)/merge-pr.sh"
FAILED=0
ok()  { echo "  PASS: $1"; }
bad() { echo "  FAIL: $1"; FAILED=$((FAILED + 1)); }

BLOCK="$(awk '/^DELETE_BRANCH_ON_MERGE=\$\(forge_check_auto_delete/{f=1} /^# Cleanup worktree if requested\./{f=0} f' "$SRC")"
[[ -n "$BLOCK" ]] || { echo "FAIL: could not extract delete block"; exit 1; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
# Stub loom-daemon: behaviour chosen by $STUB_MODE.
cat > "$TMP/loom-daemon" <<'S'
#!/usr/bin/env bash
echo "daemon $*" >> "$STUB_LOG"
case "$STUB_MODE" in
  none)    exit 0 ;;
  retarget) printf 'INFO\tRetargeted child #7\n'; exit 0 ;;
  fail)    printf 'WARNING\tKeeping remote branch\n'; exit 1 ;;
  query)   printf 'WARNING\tcould not list\n'; exit 1 ;;
  old)     exit 2 ;;
esac
S
chmod +x "$TMP/loom-daemon"

run_case() { # mode auto_delete forge_type -> prints log
  local mode="$1" auto="${2:-false}" forge="${3:-github}"
  : > "$TMP/log"
  (
    export STUB_MODE="$mode" STUB_LOG="$TMP/log" LOOM_DAEMON_BIN="$TMP/loom-daemon"
    FORGE_TYPE="$forge"
    info() { :; }; warning() { :; }; success() { :; }
    forge_check_auto_delete() { echo "$auto"; }
    forge_delete_branch() { echo "DELETE $2 forge=$FORGE_TYPE" >> "$STUB_LOG"; }
    REPO_NWO=o/r; GH=gh; PR_BRANCH=feature/parent
    PR_JSON='{"base":{"ref":"main"}}'
    eval "$BLOCK"
  ) >/dev/null 2>&1
  cat "$TMP/log"
}

log="$(run_case none)"
grep -q 'retarget-children --repo o/r --parent-branch feature/parent --base main' <<<"$log" && ok "verb called with parent base" || bad "verb args: $log"
grep -q '^DELETE feature/parent' <<<"$log" && ok "no children: branch deleted" || bad "no children should delete"

log="$(run_case retarget)"
grep -q '^DELETE' <<<"$log" && ok "children retargeted: delete proceeds" || bad "retargeted should delete"

for m in fail query old; do
  log="$(run_case "$m")"
  ! grep -q '^DELETE' <<<"$log" && ok "mode $m: delete skipped" || bad "mode $m must not delete"
done

log="$(run_case fail true)"
[[ -z "$log" ]] && ok "auto-delete repo: verb not consulted, no delete" || bad "auto-delete: $log"

# Judge P1 (#10406): the verb drives gh, so a Gitea merge must never call it;
# the delete runs as before #9372, on the Gitea forge. `fail` would keep the
# branch if the verb were consulted.
log="$(run_case fail false gitea)"
! grep -q '^daemon' <<<"$log" && ok "gitea: retarget-children never invoked" || bad "gitea must not call the GitHub verb: $log"
grep -q '^DELETE feature/parent forge=gitea' <<<"$log" && ok "gitea: delete runs as before" || bad "gitea should delete: $log"

[[ $FAILED -eq 0 ]] && { echo "All passed"; exit 0; } || { echo "$FAILED failed"; exit 1; }
