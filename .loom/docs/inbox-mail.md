# Inbox mail: send and resolve a keyed human-task mail (#10000)

An agent that needs a human to **do** something (not decide) sends one mail to
the loom-ui inbox. Rule: [`label-state-machine.md`](label-state-machine.md)
§"Two ways to reach a human". This file is the inbox-only helper the roles
source; the full both-legs send (inbox + Matrix) stays in
`defaults/.claude/commands/loom/mail-send.md`.

- **One mail per ask.** `inbox_mail key KIND N` prints `mail-<repo>-<KIND>-<N>`
  (e.g. `mail-loom-crithold-pr-123`); `<repo>` comes from the `origin` remote, so
  every checkout and worktree of a repo derives the same key. Send and resolve
  both call it. `POST /api/inbox` is idempotent on `key`, so re-sending is safe.
- **Resolve** with the same key: `POST /api/inbox` `{key, resolve: true}`.
- **Resolve on a merge nobody announced**: `inbox_mail resolve-merged KIND MARKER`
  resolves KIND mail for every PR merged in the last 48h that carries a
  `MARKER` comment, whoever merged it (a human, the GitHub UI, `merge-pr.sh`).
  It costs one `gh pr list` (a `merged:>=` search, not creation order) and
  re-resolving is idempotent. A forged marker can only resolve a mail for a PR
  that is already merged.
- **Config resolves like the daemon** (#10137): `LOOM_UI_INBOX_URL` /
  `LOOM_UI_INGEST_KEY` win when set; otherwise `loom-daemon forge inbox-config`
  supplies the URL -- the origin of the observability endpoint, but **only** an
  `https` endpoint whose path is `/ingest` (a daemon exporting straight to the
  dashboard); an `http`, loopback, collector-port, bare-origin or other-path
  endpoint leaves it unresolved -- and the ingest key *file*, first of:
  `$LOOM_UI_INGEST_KEY_FILE`, `~/.config/loom-ui/ingest.key` (preferred: the
  per-host dashboard key; loom-ui `docs/operator-mail-onboarding.md`), then the
  telemetry tiers (`$LOOM_OBSERVABILITY_INGEST_KEY_FILE`,
  `observability.ingestKeyFile`, `~/.loom/observability/ingest.key`) -- but
  only when the endpoint is that direct `https://.../ingest` dashboard (and
  `LOOM_UI_INBOX_URL`, if set, is the same origin); a collector host's
  telemetry key is never borrowed. An older daemon without the subcommand
  gives the env-only behavior. `loom-daemon health` reports an unresolved
  mail-meant host on every run (`inbox_mail` section); a placeholder endpoint
  or `enabled: false` observability does not make a host mail-meant.
- **No-op when unconfigured**: neither the env vars nor the daemon resolve a URL and key
  prints one note and returns 0, with no forge read. `inbox_mail on` is the same
  test (status only), for gating a caller's own reads. A failed POST warns and
  returns 0; a mail problem never breaks a role's tick.
- No secrets in `BODY`; the ingest key never goes on argv.

Loading it (a missing doc leaves a no-op whose `on` is false):

```bash
_im=$(awk '/^```bash inbox-mail/{f=1;next} /^```/{f=0} f' .loom/docs/inbox-mail.md 2>/dev/null)
[ -n "$_im" ] && eval "$_im"
type inbox_mail >/dev/null 2>&1 || inbox_mail() { [ "$1" != on ]; }
```

```bash inbox-mail
# _inbox_resolve: sets _im_url and _im_keyfile (empty = key comes from the env). Env wins;
# otherwise ask the daemon (paths only, never the key). Old daemon: prints nothing.
_inbox_resolve() {
  local o; _im_url=${LOOM_UI_INBOX_URL:-}; _im_keyfile=
  if [ -z "$_im_url" ] || [ -z "${LOOM_UI_INGEST_KEY:-}" ]; then
    o=$(loom-daemon forge inbox-config 2>/dev/null)
    [ -n "$_im_url" ] || _im_url=$(sed -n 's/^url=//p' <<<"$o" | head -1)
    [ -n "${LOOM_UI_INGEST_KEY:-}" ] || _im_keyfile=$(sed -n 's/^key_file=//p' <<<"$o" | head -1)
  fi
  [ -n "$_im_url" ] && { [ -n "${LOOM_UI_INGEST_KEY:-}" ] || [ -n "$_im_keyfile" ]; }
}
# inbox_mail send|resolve KEY [BODY] | key KIND N | on | resolve-merged KIND MARKER
#   (BODY required for send; optional TITLE, TO)
inbox_mail() {
  local mode="${1:-}" key="${2:-}" body="${3:-}" pf out rc code r n k=
  case "$mode" in
    key) r=$(git remote get-url origin 2>/dev/null); r=${r%/}; r=${r%.git}; r=${r##*/}; r=${r##*:}
      echo "mail-${r:-repo}-$key-$body"; return 0 ;;
    on) _inbox_resolve; return ;;
  esac
  if ! inbox_mail on; then
    echo "inbox not configured — mail $mode skipped (key $key)"; return 0
  fi
  if [ "$mode" = resolve-merged ]; then
    # Filter on merge date in the query: plain --limit orders by creation, and a
    # held PR is usually days old when it merges. GNU date, then BSD date.
    r=$(date -u -d '-2 days' +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -v-2d +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)
    [ -n "$r" ] || { echo "inbox mail: no date, resolve-merged skipped"; return 0; }
    out=$(gh pr list --state merged --limit 100 --search "merged:>=$r" --json number,comments 2>/dev/null)
    [ "$(jq length <<<"$out" 2>/dev/null)" = 100 ] && echo "inbox mail: 100 merges in 48h, window truncated"
    jq -r --arg m "$body" '.[] | select(any(.comments[]?; .body | contains($m))) | .number' <<<"$out" 2>/dev/null |
      while read -r n; do inbox_mail resolve "$(inbox_mail key "$key" "$n")"; done
    return 0
  fi
  pf=$(mktemp) || return 0
  case "$mode" in
    send) [ -n "$body" ] || { echo "inbox mail: empty body, not sent"; rm -f "$pf"; return 0; }
      jq -n --arg key "$key" --arg body "$(printf '%s' "$body" | head -c 20000)" \
            --arg who "${TO:-${LOOM_SENDER_IDENTITY:-$(hostname -s)}}" --arg title "${TITLE:-}" \
        '{key: $key, body: $body, who: $who, severity: "normal"}
          + (if $title == "" then {} else {title: $title} end)' >"$pf" ;;
    resolve) jq -n --arg key "$key" '{key: $key, resolve: true}' >"$pf" ;;
    *) echo "inbox mail: unknown mode $mode"; rm -f "$pf"; return 0 ;;
  esac
  [ -n "${LOOM_UI_INGEST_KEY:-}" ] || k=$(tr -d '\r\n' <"$_im_keyfile" 2>/dev/null)
  out=$(printf 'header = "Authorization: Bearer %s"\n' "${LOOM_UI_INGEST_KEY:-$k}" |
    curl -sS --max-time 30 --config - -X POST -H 'Content-Type: application/json' \
      --data-binary @"$pf" -w '\n%{http_code}' "${_im_url%/}/api/inbox" 2>&1); rc=$?
  k=; rm -f "$pf"; code=${out##*$'\n'}
  case "$rc:$code" in
    0:2??) echo "inbox mail $mode ok (key $key)" ;;
    *) echo "inbox mail $mode FAILED (curl exit $rc, HTTP ${code:-none}, key $key) — continuing" ;;
  esac
  return 0
}
```

Test: `defaults/scripts/tests/test-inbox-mail.sh` (extracts the fence above and
drives the loader with the doc missing).
