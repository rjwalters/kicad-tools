# Forge Authentication Guide

Loom supports multiple forge platforms for issue tracking, PR management, and label coordination. This guide covers authentication setup for each supported forge.

## Supported Forges

| Forge | Detection | CLI Tool | Auth Method |
|-------|-----------|----------|-------------|
| GitHub | `github.com` in remote URL | `gh` CLI | `gh auth login` or `GH_TOKEN` |
| Gitea | Auto-detected via API probe | Loom scripts (direct API) | `GITEA_TOKEN` or `FORGE_TOKEN` |

Forge type is auto-detected from your git remote URL. GitHub is detected by hostname; any non-GitHub remote is probed for the Gitea API version endpoint.

## GitHub Authentication

See [github-authentication.md](github-authentication.md) for the full GitHub authentication guide, including:
- Fine-grained PAT creation
- Required token permissions per role
- Verification and troubleshooting

**Quick start:**
```bash
export GH_TOKEN=github_pat_xxx
gh auth status
```

## Gitea Authentication

### Quick Start

```bash
# 1. Create an API token on your Gitea instance
#    Go to: <your-gitea-instance>/user/settings/applications
#    Create a token with repository read/write permissions

# 2. Export it before running Loom
export GITEA_TOKEN=your_gitea_api_token

# 3. Verify (optional)
curl -s -H "Authorization: token $GITEA_TOKEN" \
  https://your-gitea-instance/api/v1/user | jq .login
```

### Required Token Permissions

Gitea API tokens need the following scopes (if your Gitea version supports scoped tokens):

| Scope | Used By | Purpose |
|-------|---------|---------|
| `repo` | All roles | Repository access (issues, PRs, labels, contents) |
| `issue` | Builder, Curator, Champion, Shepherd | Issue creation, editing, label management |
| `package` | (optional) | Not required by Loom |

For Gitea instances without scoped tokens, a standard API token grants full access to repositories the user can reach.

### Creating an API Token

1. Log in to your Gitea instance
2. Go to **Settings** > **Applications** (URL: `<instance>/user/settings/applications`)
3. Under **Manage Access Tokens**, enter a token name (e.g., `loom-orchestration`)
4. Select appropriate scopes if available (at minimum: repository read/write)
5. Click **Generate Token**
6. Copy the token immediately -- it will not be shown again

### Using the Token

Set the token as an environment variable before running Loom:

```bash
# Option A: Export in current session
export GITEA_TOKEN=your_token_here

# Option B: Use FORGE_TOKEN (generic, works for any forge)
export FORGE_TOKEN=your_token_here

# Option C: Add to shell profile (~/.zshrc, ~/.bashrc)
export GITEA_TOKEN=your_token_here

# Option D: Use a .env file (not committed)
source .env  # where .env contains: export GITEA_TOKEN=your_token_here
```

When using Daemon Mode, set the variable before launching the daemon so all spawned terminals inherit it.

### Verifying Authentication

```bash
# Check user identity
curl -s -H "Authorization: token $GITEA_TOKEN" \
  https://your-instance/api/v1/user | jq '.login'

# Check repository access
curl -s -H "Authorization: token $GITEA_TOKEN" \
  https://your-instance/api/v1/repos/owner/repo | jq '.full_name'

# Check issue access
curl -s -H "Authorization: token $GITEA_TOKEN" \
  https://your-instance/api/v1/repos/owner/repo/issues?limit=1 | jq '.[0].title'
```

### Basic Auth Mode (token-less Gitea instances)

Some self-hosted Gitea deployments (cleanroom mirrors, air-gapped environments, locked-down corporate instances) disable the personal-access-token UI and only expose username/password authentication. For those instances, Loom supports HTTP Basic Auth.

Set `GITEA_USERNAME` to switch into Basic Auth mode. The password is taken from the existing `GITEA_TOKEN` (or `FORGE_TOKEN`) variable -- the field is reused so existing config schemas keep working.

```bash
export GITEA_USERNAME=example-user
export GITEA_TOKEN='<your-gitea-password>'    # password goes here in Basic mode
```

Or in `.loom/config.json`:

```json
{
  "forge": {
    "type": "gitea",
    "gitea": {
      "url": "https://cleanroom-gitea.example.com",
      "username": "example-user",
      "token": "<password>"
    }
  }
}
```

**Precedence**:
1. If `GITEA_USERNAME` (env) or `forge.gitea.username` (config) is set, Loom uses HTTP Basic Auth (`Authorization: Basic base64(user:pass)`).
2. Otherwise, Loom uses token auth (`Authorization: token <T>`).
3. Env vars take precedence over `.loom/config.json` in both modes.

**HTTPS requirement (security guard)**: Loom refuses to start in Basic Auth mode against an `http://` URL because HTTP Basic Auth only base64-encodes the password -- sending it in plaintext leaks the credential on the wire. The error message is:

> Gitea Basic Auth requires HTTPS to avoid leaking credentials. Set forge.gitea.url to an https:// URL, or set LOOM_ALLOW_INSECURE_BASIC_AUTH=1 to override (not recommended).

For air-gapped LAN deployments where TLS is genuinely unavailable, you can override the guard:

```bash
export LOOM_ALLOW_INSECURE_BASIC_AUTH=1
```

This is **not** recommended outside isolated networks. Anyone with access to the network segment can read the password.

**Restrictions**:
- The username cannot contain a colon (`:`). RFC 7617 disallows it because the colon is the user/password separator.
- The password (taken from `token`) may contain any characters -- it is properly handled by `curl -u` and by the Python `requests` library.

### Gitea Instance URL

Loom auto-detects the Gitea API URL from your git remote. For HTTPS remotes like `https://gitea.example.com/owner/repo.git`, the API base URL is `https://gitea.example.com/api/v1`.

If auto-detection fails (e.g., non-standard ports or paths), you can configure the API URL in `.loom/config.json`:

```json
{
  "forge": {
    "type": "gitea",
    "gitea": {
      "api_url": "https://gitea.example.com/api/v1",
      "known_hosts": ["gitea.example.com"]
    }
  }
}
```

## Troubleshooting

### Forge not detected

- Ensure your git remote URL is set: `git remote -v`
- For Gitea, ensure the instance is reachable (Loom probes the `/api/v1/version` endpoint)
- Try setting the forge type explicitly in `.loom/config.json`

### Gitea token not being picked up

- Confirm `echo $GITEA_TOKEN` shows the token value
- The variable must be **exported**, not just set: `export GITEA_TOKEN=...`
- `FORGE_TOKEN` is checked as a fallback if `GITEA_TOKEN` is not set

### Permission errors (401 / 403)

- Verify the token has not expired
- Check that the token has sufficient scopes for the operation
- Ensure the token belongs to a user with access to the target repository

## Security Notes

- **Never commit tokens** to the repository. Add `.env` to `.gitignore` if using an env file.
- Use the minimum permissions required.
- Rotate tokens periodically.
- For Gitea self-hosted instances, ensure HTTPS is configured for API communication.
