# Task Credential Discovery: Reference by Name, Never by Value

This convention covers **task credentials** — cloud provider tokens, SSH
identities, service API keys, per-host env files — that an agent needs for
work *outside* Loom's own plumbing (issue tracking, worktrees, the sweep
lifecycle). Loom's own credentials (`forge.*`, `safehouse.*`,
`observability.ingestKeyFile`) are covered by the config-resolution tiers and
by [`credential-storage.md`](credential-storage.md), which states the general
"secrets never live in a repo or worktree" policy this document assumes
throughout. This document is the narrower, per-repo layer on top: how an agent
*finds* a task credential without ever seeing, printing, or being handed its
value.

## The problem this solves

An agent working an issue-driven task often needs a credential that is
already available somewhere in the owner's environment — a session-exported
env var, an owner-only `0600` file under `~/.config/<tool>/`, a machine
keychain entry. Without a documented convention, discovery falls on the agent
either stumbling interactively ("what's the SSH key path?") or, worse, on the
operator pasting a value into chat, an issue, or a PR. Both defeat the "your
only job is to write issues and review PRs" model at exactly the moment an
agent needs a key to make progress.

## The manifest: `./.loom/credentials.md`

A repo may commit an optional `./.loom/credentials.md` — authored by the repo
owner, **names only, never values** — that lists, per credential, where an
agent finds it:

| Purpose | Reference | Usage |
|---|---|---|
| `<what it's for>` | env var `<NAME>` **or** owner-store path `~/.config/<tool>/<file>` **or** "provisioned file: `<path>`" | pull into the process env / read in-session; never print, commit, or quote in issue/PR text; report names only |

Because the file holds only names, paths, and env-var identifiers — never a
secret — it is safe to commit. A template with placeholder rows,
`.loom/credentials.md.example`, ships with every Loom install (mirroring how
this repo already allows `*.env.example` in-tree); copy it to
`./.loom/credentials.md` and fill in the real names for the repo.

This is the task-credential analog of the tiering already applied to Loom's
own config: a raw token belongs host-local and is never committed (see
`forge.gitea.token` in the config-resolution-tiers design doc), while the
*name* of where to find one is exactly the kind of thing that is safe, and
useful, to commit.

## Lookup-before-ask

When a task requires a credential:

1. **Check `./.loom/credentials.md` first**, if the repo has one. Resolve the
   named env var, read the named owner-store file, or use the named
   provisioned file — whichever the manifest specifies.
2. **Only a missing credential may trigger an operator interaction** — never
   a credential the manifest already names. If the repo has no manifest at
   all, or the manifest does not cover the credential you need, that is a
   missing-credential case too.
3. **That interaction asks for the name, shape, and provisioning path —
   never the value.** For example: "This task needs a write-only ingestion
   token for `<service>`. Is one already provisioned? If not, what env var
   name or file path should I read it from once you've added it?" Never ask
   "what is the key" and never accept a value pasted into the conversation,
   an issue, or a PR.

A credential that is missing and cannot be provisioned in-session is a
mechanical blocker, not a judgement call — park it the same way any other
missing-credential case is parked (`loom:operator-only` +
`loom:operator-mechanical`, per the builder role prompt's label taxonomy),
naming the credential by purpose, not by value.

## Standard provisioning flow

The shape that already works in practice: the **owner** provisions a
credential external to the checkout, and the **agent** installs or consumes
it by reference — the secret value never has to be typed, pasted, or read
aloud to the agent, and it never lands in a commit, issue, or transcript.

1. **Owner writes an owner-only file or env export.** Mode `0600` for a file,
   `0700` for its directory, outside every repository and worktree — see
   [`credential-storage.md`](credential-storage.md) for the storage rules
   this step inherits. The file may contain public material only (e.g. SSH
   *public* keys with `label:` lines) or a private value the agent will read
   in-session but never echo.
2. **Owner records the reference** — the env var name, the file path, or
   both — in `./.loom/credentials.md`.
3. **Agent verifies by name**, not by value: confirms the env var is set, the
   file exists with the expected mode, or a checksum/label matches. Installs
   or consumes the credential idempotently (safe to re-run without
   duplicating state).
4. **Agent reports only the outcome** — readiness, permissions, and
   success/failure — never the value, never a full expanded config that
   would reveal it.

## Worked example

**Cloud token**, already provisioned: the owner has exported
`CLOUD_DEPLOY_TOKEN` in the shell that launches the agent, and
`./.loom/credentials.md` lists it:

| Purpose | Reference | Usage |
|---|---|---|
| Deploy to staging | env var `CLOUD_DEPLOY_TOKEN` | read via `$CLOUD_DEPLOY_TOKEN` in-process; never print or log |

The agent reads `$CLOUD_DEPLOY_TOKEN` directly — no lookup, no operator
interaction needed, because the manifest already named it.

**SSH public-key provisioning**, credential missing: a task needs a new
deploy key installed on a remote host, and no key exists yet.

1. Agent checks `./.loom/credentials.md` — no entry for this key — and asks
   the operator only for the *name and shape*: "This task needs a deploy SSH
   key for `<host>`. None is provisioned yet — should I generate a keypair
   and hand you the public half to authorize, or will you provide one?"
2. Owner (or the agent, if asked to generate) writes an owner-only `0600`
   keys file containing the **public** key material plus a `label:` line
   identifying the purpose — never the private half.
3. Agent installs the public key on the target idempotently (matched by key
   blob, not by label — the label itself never has to land on the target
   host) and records the reference (file path, or "installed on `<host>`
   under `<user>`") in `./.loom/credentials.md`.
4. Agent reports installation succeeded, with the fingerprint, not the key.

The private key, if one exists, never touches the agent at any point in this
flow.

## Relationship to Loom's own tiering

This is a task-surface convention layered on top of, not a replacement for,
Loom's own credential handling:

- [`credential-storage.md`](credential-storage.md) states the general "never
  in a repo or worktree" policy and the safe-handoff/verification rules this
  document's provisioning flow follows.
- The config-resolution tiers
  ([`docs/design/config-resolution-tiers.md`](https://github.com/rjwalters/loom/blob/main/docs/design/config-resolution-tiers.md))
  already treat a raw secret as host-local (never committed) while the *name*
  of where to find one can live in tracked config — the same distinction
  `./.loom/credentials.md` draws for task credentials outside Loom's own config
  surface.
