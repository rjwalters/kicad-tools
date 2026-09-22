# Credential storage policy

Real credentials must never be stored inside a repository or worktree, even
when a file is ignored. This applies to provider keys, ingestion tokens,
database passwords, session signing secrets, browser sessions, generated
harness configuration, logs, screenshots, fixtures and temporary artifacts.
Do not place secrets in `.env`, `.loom/config.json` or `.loom-local/local.json`.
Do not add a symlink inside a checkout that exposes an external secret file.

Use an operating-system credential store or owner-only files under the user's
home directory outside all checkouts. On Unix, secret directories must have
mode `0700` and secret files `0600`. Confirm the resolved path is outside
repositories; a symlink or an unusual home-directory checkout can defeat a
textual path check. Containers receive read-only secret-file mounts from that
external directory. Keep credential-bearing database volumes outside checkouts.

## What belongs in version control

Commit schemas, variable names, external-file references and unmistakably fake
test credentials. A public default password becomes a real credential when
used by a deployment: require an externally supplied value for actual services.
Never include live values in examples, command arguments, issue/PR bodies,
test snapshots or screenshots. Gitignore and secret scanners are backstops,
not permission to keep credential files in a checkout.

Host-specific paths belong in machine configuration. The existing machine
defaults file is `~/.local/share/loom/config/defaults.json`; it can hold paths
and non-secret settings. Repository and environment overrides can still take
precedence, so verify effective configuration rather than promising that a
machine default overrides every project. An ignored repository override may
reference an external credential, but must never contain the credential itself.

## Persistent telemetry credentials

Keep cloud credentials separate from the local Collector ingress credential.
For the dual-backend setup, configure Loom to send to the local fanout
Collector; only that Collector needs the cloud ingestion credentials. An
example external layout is:

```text
~/.loom/observability/ingest.key
~/.loom/observability/cloud/signoz-ingest.key
~/.loom/observability/cloud/clickstack-ingest.key
```

Loom already defaults `observability.ingestKeyFile` to the first path. Configure
the machine's service and Collector to read external files explicitly; do not
depend on `.zshrc`, interactive shell exports, or the checkout used to launch
the service. Persist the Collector deployment and its queues outside worktrees
so merging a PR cannot remove them. Restart or reload after rotating keys and
verify actual ingestion; storing a file alone does not enable telemetry.

SigNoz Cloud uses a write-only ingestion key from Settings → Ingestion Settings
and the region-specific OTLP endpoint. Its management/query API key is a
different credential. ClickStack Cloud with managed OTLP ingestion uses the
endpoint and authorization value from its setup instructions; a ClickHouse
database-only service instead needs a suitable Collector and database account.
Do not substitute a ClickHouse organization-management API key for ingestion.
See the official [SigNoz key guide](https://signoz.io/docs/ingestion/signoz-cloud/keys/)
and [ClickStack ingestion guide](https://clickhouse.com/docs/clickstack/ingesting-data/overview).

## Safe handoff and verification

The operator enters keys directly into an external file or credential store
using a local editor or concealed prompt. Ask for file paths and non-secret
endpoints, never for keys in chat. Read secrets only where needed to authenticate;
report readiness, permissions, endpoint and success/failure without values.
Do not print environments, authenticated URLs, headers, browser storage or a
fully expanded Collector/Compose configuration.

If a secret is found inside a checkout, stop copying or staging it, report only
its location, and coordinate relocation without destroying the only copy. If
it was exposed in Git history, logs or a public artifact, revoke/rotate it;
deleting the current file does not undo exposure. This is an agent policy, not
a claim that every runtime API already rejects unsafe storage paths.
