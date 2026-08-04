# macOS TCC + a stable codesign identity (#4244)

Ad-hoc signing (the `loom-daemon` default, #4016) pins a legible
`--identifier`, but TCC (Transparency, Consent, and Control — the Privacy &
Security prompts) anchors a grant to the binary's **designated requirement**,
which for an ad-hoc signature is a **cdhash-only** DR. Every
`loom-daemon-update.sh` self-update roll rebuilds the binary (build.rs embeds
the git commit and build time), producing a new cdhash — so any TCC grant
made to the previous build silently evaporates and the operator is
re-prompted. See [`daemon-reference.md` → "Why Full Disk Access is never the
right answer"](daemon-reference.md) for the full measured writeup.

`LOOM_CODESIGN_IDENTITY` (env, or the `codesign.identity` config key — the
repo's standard env > config > default precedence) is the opt-in fix: point
it at a certificate already in the keychain and `provision-daemon.sh` signs
the daemon binary with THAT certificate's chain instead of ad-hoc. A
certificate-anchored DR (`identifier "X" and certificate leaf = H"…"`)
survives a rebuild — the identity, not the per-build hash, is what's pinned.
Unset (or an identity `security find-identity -v -p codesigning` doesn't
list) falls back to the ad-hoc path unchanged — this is entirely opt-in and
every non-Darwin / no-`codesign` host is unaffected.

## One-time setup: a self-signed "Code Signing" certificate

You only need a certificate that satisfies the macOS `codeSign` policy — a
paid Developer ID is not required for this local, single-machine use case.

### Option A — Certificate Assistant (GUI)

1. **Keychain Access → Certificate Assistant → Create a Certificate…**
2. Name it something recognizable (e.g. `Loom Local Signing`).
3. **Identity Type**: Self Signed Root. **Certificate Type**: **Code Signing**.
4. Let it install into the login keychain.
5. Verify it resolves: `security find-identity -v -p codesigning` should list it.

### Option B — openssl + `security import` (scriptable)

```bash
# 1. Generate a self-signed cert + key.
openssl req -x509 -newkey rsa:2048 -keyout loom-signing.key \
  -out loom-signing.crt -days 3650 -nodes \
  -subj "/CN=Loom Local Signing" \
  -addext "extendedKeyUsage=codeSigning"

# 2. Package as PKCS#12. OpenSSL 3's default export format fails
#    `security import`'s MAC verification ("MAC verification failed") --
#    the `-legacy` flag is required for a keychain-compatible export.
openssl pkcs12 -export -legacy -in loom-signing.crt -inkey loom-signing.key \
  -out loom-signing.p12 -passout pass:changeit

# 3. Import into the login keychain, trusting codesign(1) as an anchor so
#    later signing is NON-INTERACTIVE (no GUI trust prompt) -- required for
#    the #4055 self-update loop to sign unattended.
security import loom-signing.p12 -k ~/Library/Keychains/login.keychain-db \
  -P changeit -T /usr/bin/codesign

# 4. Trust the cert for the codeSign policy specifically (also avoids an
#    interactive prompt on first use).
security add-trusted-cert -p codeSign -k ~/Library/Keychains/login.keychain-db \
  loom-signing.crt

rm -f loom-signing.p12 loom-signing.key   # keep the .crt if you want a record
```

Both quirks above were hit and confirmed on a real host (2026-07-28):
`openssl pkcs12 -export` without `-legacy` fails `security import` with "MAC
verification failed", and `-T /usr/bin/codesign` at import time is what lets
`codesign` sign later without prompting — provided the login keychain is
unlocked in the user session (true for any interactive login; a headless/CI
context should keep using the ad-hoc default instead).

## Using it

```bash
export LOOM_CODESIGN_IDENTITY="Loom Local Signing"
./.loom/scripts/cli/loom-daemon-update.sh     # or any provision-daemon.sh caller
codesign -dvv ~/.local/bin/loom-daemon        # Authority=Loom Local Signing, no adhoc flag
```

Or persist it in `.loom/config.json` (or `.loom-local/local.json` for a
machine-local, ungitted override):

```json
{
  "codesign": { "identity": "Loom Local Signing" }
}
```

## Provisioning additional Macs

The certificate lives in **one** Mac's login keychain — nothing about it is
committed, so a second fleet Mac invoked with the *same*
`LOOM_CODESIGN_IDENTITY` finds no matching identity and falls back to ad-hoc
signing. `provision-daemon.sh` says so explicitly on stderr before the
ad-hoc line:

```
  [loom-daemon] WARNING: LOOM_CODESIGN_IDENTITY 'Loom Local Signing' not found via 'security find-identity -v -p codesigning'; falling back to ad-hoc signing (see defaults/docs/macos-tcc-codesign.md)
  [loom-daemon] ad-hoc signed /Users/you/.local/bin/loom-daemon (identifier=com.rjwalters.loom-daemon)
```

That host still runs fine — the only consequence is the deferred one this
doc exists for: its TCC grants reset on every rebuild. To fix it, give the
new Mac its own stable identity, either by copying the existing certificate
(Option 1) or by minting a fresh one there (Option 2).

**Prefer Option 2, and reuse the same Common Name on every host.** A TCC
grant is per-host, so nothing requires the *certificate* to be shared —
only that the identity *name* matches what `codesign.identity` /
`LOOM_CODESIGN_IDENTITY` asks for. Minting a same-named cert per host gets
you one committed config value with no private key ever leaving the first
machine. Option 1 exists for when you specifically want one certificate
across the fleet (e.g. so `codesign -dvv` reports an identical
`Authority=` leaf everywhere).

### Option 1 — export the cert + key and import it on the second Mac

On the Mac that already has the identity, export both halves. Keychain
Access is the reliable single-identity path: select the **certificate and
its private key** together → **File → Export Items…** → `Personal
Information Exchange (.p12)`. The CLI equivalent exports *every* identity
in the keychain, so only reach for it on a keychain you know is clean:

```bash
# Exports ALL codesigning identities in the keychain, not just this one.
security export -k ~/Library/Keychains/login.keychain-db \
  -t identities -f pkcs12 -P changeit -o loom-signing.p12

# The public certificate alone, needed for add-trusted-cert on the far side.
security find-certificate -c "Loom Local Signing" -p \
  ~/Library/Keychains/login.keychain-db > loom-signing.crt
```

The `.p12` contains the **private key** — move it over a channel you trust
(`scp`, AirDrop), use a real passphrase rather than `changeit`, and delete
it from both hosts once imported.

On the new Mac, import with the same flags the one-time setup above uses
(`-T /usr/bin/codesign` so later signing is non-interactive; the trust step
is what keeps the self-signed root from prompting on first use):

```bash
security import loom-signing.p12 -k ~/Library/Keychains/login.keychain-db \
  -P changeit -T /usr/bin/codesign

security add-trusted-cert -p codeSign -k ~/Library/Keychains/login.keychain-db \
  loom-signing.crt

rm -f loom-signing.p12          # the private key does not need to persist on disk
security find-identity -v -p codesigning   # should now list "Loom Local Signing"
```

If the import fails with "MAC verification failed", the `.p12` was written
in OpenSSL 3's default format — re-export it with `-legacy` (same quirk as
step 2 of Option B above).

### Option 2 — mint an independent cert per host

Just run the one-time setup (Option A or B) again on the new Mac. No key
material is transferred, and each host's certificate is independent.

- **Same CN on every host** (recommended): use `/CN=Loom Local Signing`
  again, and the committed `codesign.identity` keeps working unchanged.
  The certificates differ per host, but each host only ever validates its
  own signature, so the shared name is all the config needs.
- **Different CN per host** (e.g. `Loom Local Signing (studio)`): then a
  single committed value can't cover the fleet. Set the identity per host
  in `.loom-local/local.json` — the highest-precedence config tier, meant to
  stay untracked (`install-loom.sh --local` gitignores `/.loom-local/` for
  you):

  ```json
  {
    "codesign": { "identity": "Loom Local Signing (studio)" }
  }
  ```

  Exporting `LOOM_CODESIGN_IDENTITY` from the host's shell profile works
  too and takes precedence over both config tiers.

### Verifying the new host

```bash
security find-identity -v -p codesigning        # the identity is listed
./.loom/scripts/cli/loom-daemon-update.sh       # re-provision + re-sign
codesign -dvv ~/.local/bin/loom-daemon 2>&1 | grep -E 'Authority|adhoc'
# want: Authority=Loom Local Signing   (and NO 'adhoc' in the flags line)
```

If `adhoc` is still reported, re-read the provisioning output: the
`WARNING:` line above pinpoints whether the identity was requested but
missing from *this* keychain, versus `codesign` itself failing.

## Grant the daemon identity, not Terminal

Work spawned from an interactive terminal shell — in-session
`spawn-claude.sh`, a hand-run `nohup loom-daemon …`, a debug daemon started
from a worktree — attributes its TCC requests to the **terminal app**, not to
`loom-daemon`. Granting broad file access there extends that grant to
*everything ever run in that terminal*, forever, which is both a bigger
attack surface than intended and does nothing to fix the actual rebuild
churn.

Prefer dispatching through the daemon itself — `loom-daemon dispatch` /
`mcp__loom__dispatch_sweep` — so any TCC prompt a sweep child triggers is
attributed to the `loom-daemon` binary (launchd already attributes children
of a supervised job to the parent binary; no plist change is needed for
this). If a grant is ever genuinely needed, add it to the `loom-daemon` row
specifically (`~/.local/bin/loom-daemon`) rather than to Terminal — and, per
the daemon-reference writeup, first double check the access really needs to
be there at all, since the daemon's legitimate working set is scoped and
FDA/broad grants are rarely the right fix.

If you re-sign the binary with a stable identity after previously granting
Terminal (or a stale ad-hoc `loom-daemon` row), remove the stale row from the
relevant Privacy & Security pane and re-add the current binary path — the
grant will then persist across rolls instead of silently evaporating.
