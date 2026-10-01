#!/usr/bin/env bash
# write-scope-fixture.sh — test helper: make a suite's fixture checkout a
# repository this installation may write to, so the real #9548 write-scope
# decision admits it. Nothing is stubbed or bypassed: the suite's subject
# still vets its target with `loom_write_repo` (lib/forge-helpers.sh), which
# asks `loom-daemon forge may-write` when a daemon is present and otherwise
# applies the shell fallback. Either way the fixture passes for the reason a
# real workspace does:
#
#   - it is a git checkout whose only remote, `origin`, is the repository the
#     suite's stub `gh` models (so gh resolves the checkout to origin);
#   - Loom is installed in it (`.loom/`), which makes that origin managed; and
#   - the credential can write it: `LOOM_GH_BIN` (the `gh` the daemon's
#     permission probe runs) answers `gh api repos/OWNER/REPO` with `push`.
#     Every other call is handed on to the suite's own stub `gh`.
#
#   source "$TEST_DIR/lib/write-scope-fixture.sh"
#   write_scope_register "$FIXTURE_DIR" owner/repo      # then run from it
#
# A negative control registers with `pull` instead: with a daemon, the real
# gate must then refuse (the shell fallback, used only when no daemon can
# answer, does not probe permissions; see test-write-scope.sh).
#
#   write_scope_register "$FIXTURE_DIR" owner/repo pull
#
# WRITE_SCOPE_FIXTURE_PERMISSION=pull does the same for a whole suite run, the
# quickest way to confirm a suite's writes really pass through the gate: with
# a daemon, the suite's write cases must then fail.
#
# A suite whose `loom-daemon` is itself a stub (for other verbs) hands
# `forge may-write` to "${WRITE_SCOPE_DAEMON:-}" when that names a real
# daemon, and otherwise answers as a binary predating the verb (exit 2), which
# is what sends loom_write_repo to its shell fallback:
#
#   write_scope_stub_verb_snippet >> "$STUB_DIR/loom-daemon"  # after the shebang
#
# Run such a suite with WRITE_SCOPE_DAEMON=<built loom-daemon> to exercise the
# daemon's decision; suites without a stub daemon use LOOM_DAEMON_BIN.
#
# The wrapper and the permission cache (LOOM_WRITE_SCOPE_CACHE_DIR) live in the
# fixture's git dir, so its working tree stays clean,
# and GH_REPO / LOOM_REPO are unset, since either would retarget gh.

write_scope_register() {
  local dir="${1:?write_scope_register: checkout dir required}"
  local nwo="${2:?write_scope_register: OWNER/REPO required}"
  local permission="${3:-${WRITE_SCOPE_FIXTURE_PERMISSION:-push}}"
  local inner="${WRITE_SCOPE_INNER_GH:-${LOOM_GH_BIN:-}}"
  mkdir -p "$dir/.loom"
  if ! git -C "$dir" rev-parse --git-dir >/dev/null 2>&1; then
    git -C "$dir" init -q
  fi
  # Kept in the git dir, so the fixture's working tree stays clean.
  local aux
  aux="$(git -C "$dir" rev-parse --absolute-git-dir)/loom-write-scope"
  mkdir -p "$aux"
  [[ "$inner" != "$aux/gh" ]] || inner=""
  local url="https://github.com/$nwo.git"
  if git -C "$dir" remote get-url origin >/dev/null 2>&1; then
    git -C "$dir" remote set-url origin "$url"
  else
    git -C "$dir" remote add origin "$url"
  fi
  # The wrapper resolves the suite's stub gh at call time (the first `gh` on
  # PATH), unless a LOOM_GH_BIN was already in force when this ran.
  cat > "$aux/gh" <<EOF
#!/usr/bin/env bash
# #9548 write-scope permission probe for the registered fixture $nwo.
if [[ "\${1:-}" == "api" && "\${2:-}" == "repos/$nwo" ]]; then
  echo '{"$permission":true}'
  exit 0
fi
if [[ "\${1:-}" == "api" && "\${2:-}" == "installation/repositories" ]]; then
  echo 'fixture gh: not an App installation token' >&2
  exit 1
fi
inner="$inner"
[[ -n "\$inner" ]] || inner="\$(command -v gh 2>/dev/null)"
[[ -n "\$inner" ]] || { echo "fixture gh: no gh to hand \$* to" >&2; exit 127; }
exec "\$inner" "\$@"
EOF
  chmod +x "$aux/gh"
  export LOOM_GH_BIN="$aux/gh"
  export LOOM_WRITE_SCOPE_CACHE_DIR="$aux/cache"
  unset GH_REPO LOOM_REPO
}

# write_scope_stub_verb_snippet: the `forge may-write` arm for a stub
# loom-daemon (see above), printed for the stub's own file.
write_scope_stub_verb_snippet() {
  cat <<'SNIPPET'
if [[ "${1:-} ${2:-}" == "forge may-write" ]]; then
  [[ -n "${WRITE_SCOPE_DAEMON:-}" ]] && exec "$WRITE_SCOPE_DAEMON" "$@"
  echo "error: unrecognized subcommand 'may-write'" >&2
  exit 2
fi
SNIPPET
}
