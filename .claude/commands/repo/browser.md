---
name: "browser"
description: "Check the environment's browser-automation stack (Browser Use CLI, official agent skill, auth, cloud credits) and offer confirm-gated installs; report-first, like remote/sudo"
domain: repo
type: command
user-invocable: true
---

# /repo:browser — Browser-Automation Environment Health

Report whether this environment can drive a browser the way the adopting
org's policy expects, and offer to install what is missing. The stack is
[Browser Use](https://browser-use.com): the `browser-use` CLI (local Chrome,
cloud stealth browsers, or any CDP endpoint), its first-party agent skills,
and — when a key is present — the Browser Use Cloud account behind them.

This command **provisions and reports; it never drives a browser**. Task
execution, URL policy, and what may enter a browser session belong to the
adopting org's browser-automation policy (wherever that org keeps it),
not to this command.

Shaped like [[remote]] and [[sudo]]: the report is read-only and safe to run
anywhere; every install is an environment change and is confirmed before it
runs.

## Usage

```
/repo:browser              # Report, then offer confirm-gated installs for what's missing
/repo:browser --check      # Report only, never writes (also: any unknown argument)
```

## Scope — and the neighboring commands it is not

This is environment provisioning, the `remote`/`sudo`/`host-optimize` family.
It is deliberately **not** [[update-tools]] business: that command scopes
itself to *installer-managed tool packages with a local source clone to diff
against* and routes pip/uv tools to [[deps]] — a globally installed uv CLI
has no local clone, so neither comparison model applies. If `browser-use`
was installed by an installer that records `install-metadata.json`, that
install's currency is [[update-tools]]' to report; this command only checks
presence, version, auth, and cloud health.

## Steps

### 1. CLI presence and version

```bash
command -v browser-use && browser-use --version
```

- Missing → report the official install path (offer in step 4):
  `uv tool install browser-use`, or `uvx browser-use` for a one-off without
  a permanent install.
- Present → record the version. Checking it against the latest published
  release is [[deps]]-shaped currency tracking and is out of scope here;
  `uv tool upgrade browser-use` is the upgrade path (offered in step 4).

### 2. Official skill presence

Browser Use ships first-party SKILL.md bundles and maintains them upstream;
**vendor nothing**. Check where agents look:

```bash
ls .claude/skills/browser-use/SKILL.md 2>/dev/null
command -v browser-use >/dev/null && browser-use skill list 2>/dev/null
```

Missing is a normal state, not an error — the official install is
`npx skills add browser-use/browser-use --skill browser-use` (other bundles:
`cloud`, `qa`, `remote-browser`, `open-source`, `x402`), or
`browser-use skill install` from the CLI. If a *vendored copy* exists that
is not a plain upstream install (edited prose, a fork URL in its
frontmatter), say so: it will drift, and the fix is deleting it and
installing upstream.

### 3. Auth and cloud health

```bash
browser-use auth status        # never prints the key
```

If a key is configured **and already present in the environment**
(`BROWSER_USE_API_KEY`), read the account the way the docs recommend:

```bash
printf 'X-Browser-Use-API-Key: %s\n' "$BROWSER_USE_API_KEY" \
  | curl -sS -H @- https://api.browser-use.com/api/v2/billing/account
```

The header goes to `curl` on stdin (`-H @-`, curl 7.55+), never as an
argument: `curl -H "X-Browser-Use-API-Key: $BROWSER_USE_API_KEY"` expands
the key into curl's argv, where `ps` on a shared host shows it. `printf` is
a shell builtin, so the key never lands in any process's argv. Do not run
this with `set -x` / `bash -x` (or any other shell tracing) on — tracing
prints the expanded `printf` line, key included.

Report the fields that matter operationally: credit balance,
`concurrentSessionLimit`, `activeSessionCount`, and the key's `projectId`.
Keys in one project share capacity and credits — a low balance or a
saturated concurrency limit is a *team* condition, not just this machine's.
If the key lives only inside the CLI's auth store, `auth status` output is
the report; do not fish for the key. If no key is configured anywhere, say
so and stop — obtaining one is an operator action (the org policy doc's
rollout checklist), never this command's.

Worth surfacing verbatim from the docs, because each has bitten someone:
a completed run or closed CDP connection does **not** stop the cloud
browser — it bills until `PATCH /api/v4/browsers/{id}` with
`{"action":"stop"}`; API-key spending caps are soft limits, not a prepaid
wallet; and auto-recharge can charge immediately when enabled below its
threshold.

### 4. Confirm-gated installs

Offered one at a time, each with its own confirmation, in this order. A yes
to the report is not a yes to any of these; a yes to one is not a yes to
the next.

1. **Install/upgrade the CLI** — `uv tool install browser-use`, or
   `uv tool install --upgrade browser-use` when present. Executes code from
   the package; that is why it is confirmed, not auto-applied.
2. **Install the official skill** —
   `npx skills add browser-use/browser-use --skill browser-use` (offer the
   other bundle names if the user's task suggests them). Writes into
   `.claude/skills/`; report where it landed.
3. **Auth handoff** — point the user at `browser-use auth login` and let the
   official flow run interactively. This command never handles the key
   itself: it does not read it from a prompt, store it, write it to a file,
   or pass it on the command line.

Re-run the step-1–3 report after any install so the final state is in the
transcript.

## Safety Rules

1. **Report-first.** The default run writes nothing outside the terminal.
   `--check` and any unrecognized argument are report-only.
2. **Each install is confirmed separately**; never bundle confirmations,
   never infer consent from the report having been read.
3. **The API key is never printed, stored, or written by this command.** If
   a value must be referenced at all, it is masked to its last four
   characters. The REST call in step 3 uses only a key already exported in
   the environment, feeds the header to `curl` on stdin rather than argv,
   runs with shell tracing off, and never echoes the header.
4. **No browsing.** This command does not open pages, run tasks, or start
   sessions. The nearest it comes to the cloud is the read-only billing
   endpoint in step 3.
5. **No key acquisition.** Missing auth is reported, never solved here —
   signup, purchase, and key minting are operator actions under the org's
   policy doc.
6. **Pointer, not copy.** CLI flags, skill bundle lists, and API shapes
   drift; when the report's command names disagree with
   [the docs](https://docs.browser-use.com/llms.txt), the docs win and this
   command file is the thing to fix.
