#!/usr/bin/env bash
# post-comment.sh — the single entry for agent-posted issue/PR comments (#9774).
#
# `gh issue comment` / `gh pr comment` run by hand bypass every comment
# chokepoint, so agent comments shipped without the dashboard footer
# (#9772/#9774). This is the single-sourced replacement, in the role prompts:
# a Shape-A stub over `loom-daemon forge comment`, which appends the footer
# (loom:dashboard-link) and posts via the REST comments endpoint. The flag
# surface is `gh comment`-shaped so a prompt's existing invocation switches
# by changing the command name:
#
#   post-comment.sh <NUMBER> (--body TEXT | --body-file PATH) [--pr] [--repo OWNER/REPO]
#
#   --pr   the number names a pull request (the footer's link says /pull/N;
#          the endpoint is shared — a PR IS an issue for comments)
#   --repo defaults to the current checkout's origin remote
#
# Scripts that post comments go through forge_gh_comment_rl_safe
# (lib/forge-helpers.sh) instead, which appends the same footer from its bash
# twin (lib/dashboard-link.sh) and is the binary-absent path.

set -uo pipefail
# requires-daemon: forge >= 0.19.598   #9818 — the stub is a pure exec of `forge comment`, so an older binary fails right here with clap's unrecognized-subcommand error; rolling to 0.19.598+ is the remedy (there is no degraded path: an unfootered comment is what #9774 exists to prevent).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
# shellcheck source=./lib/locate-daemon-bin.sh
source "$SCRIPT_DIR/lib/locate-daemon-bin.sh"
SELF_BIN="$(loom_resolve_self_daemon_bin "$(git rev-parse --show-toplevel 2>/dev/null || pwd)")"
if [[ -z "$SELF_BIN" ]]; then
  echo "post-comment.sh: no loom-daemon binary found — comments need it to append the dashboard footer (#9772). Searched:" >&2
  loom_daemon_bin_search_paths "$(git rev-parse --show-toplevel 2>/dev/null || pwd)" | sed 's/^/  /' >&2
  exit 78
fi
exec "$SELF_BIN" forge comment "$@"
