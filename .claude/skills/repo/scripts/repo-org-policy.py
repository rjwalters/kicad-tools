#!/usr/bin/env python3
"""Preview and publish organization policy from rjwalters/repo via GitHub PRs.

Python 3.9+ and authenticated gh are required. All API requests use github.com;
all writes require an explicit apply of a saved plan. No client files, settings,
or default branches are modified. Only two organization files are managed.
"""

import argparse
import base64
import copy
import difflib
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse

SOURCE_REPO = "rjwalters/repo"
MANAGED_FILES = {"repo-policy.json", "renovate-config.json"}
RENOVATE_APP_SLUG = "renovate"
RENOVATE_INSTALL_URL = "https://github.com/apps/renovate"


class PolicyError(Exception):
    pass


class GitHub:
    def api(self, endpoint, method="GET", payload=None, missing_ok=False):
        args = ["gh", "api", "--hostname", "github.com", "--method", method,
                endpoint]
        if payload is not None:
            args += ["--input", "-"]
        result = subprocess.run(args, input=json.dumps(payload) if payload is not None else None,
                                text=True, capture_output=True, check=False)
        if result.returncode:
            # Only an actual 404 is eligible for missing-file/repo handling.
            # Permission failures, rate limits, and network errors remain errors.
            if missing_ok and re.search(r"HTTP 404\b", result.stderr):
                return None
            raise PolicyError(f"GitHub {method} {endpoint} failed: {result.stderr.strip()}")
        return json.loads(result.stdout) if result.stdout.strip() else None


def dump(value):
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def repo_name(value):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", value):
        raise PolicyError("Expected a GitHub OWNER/REPO name")
    owner, name = value.split("/", 1)
    if name in (".", ".."):
        raise PolicyError("Expected a GitHub repository name, not a path component")
    return f"{owner.lower()}/{name}"


def client_repo(explicit=None):
    if explicit:
        return repo_name(explicit)
    result = subprocess.run(["git", "remote", "get-url", "origin"],
                            text=True, capture_output=True, check=False)
    if result.returncode:
        raise PolicyError("Run inside a client repository with origin, or pass --repo OWNER/REPO")
    remote = result.stdout.strip()
    if remote.startswith("git@github.com:"):
        value = remote[len("git@github.com:"):]
    else:
        parsed = urlparse(remote)
        if parsed.scheme not in ("https", "ssh") or parsed.hostname != "github.com":
            raise PolicyError("Only github.com origins are supported; use --repo for an explicit target")
        value = parsed.path.lstrip("/")
    return repo_name(value.removesuffix(".git"))


def merge(base, override):
    """Objects merge recursively; arrays/scalars replace. No silent array union."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def validate(policy):
    if (not isinstance(policy, dict) or type(policy.get("schemaVersion")) is not int
            or policy["schemaVersion"] != 1):
        raise PolicyError("Policy must be an object with schemaVersion: 1")
    if set(policy) != {"schemaVersion", "dependencies"}:
        raise PolicyError("Unknown policy fields; supported fields: schemaVersion, dependencies")
    deps = policy["dependencies"]
    if not isinstance(deps, dict) or set(deps) != {
        "provider", "dependabotAlerts", "dependabotSecurityUpdates", "renovate"
    }:
        raise PolicyError("Invalid dependencies policy fields")
    if deps["provider"] != "renovate":
        raise PolicyError("Organization policy installation currently supports provider: renovate")
    for key in ("dependabotAlerts", "dependabotSecurityUpdates"):
        if type(deps[key]) is not bool:
            raise PolicyError(f"dependencies.{key} must be a boolean")
    renovate = deps["renovate"]
    if not isinstance(renovate, dict):
        raise PolicyError("dependencies.renovate must be a Renovate config object")
    if "vulnerabilityAlerts" in renovate and not isinstance(renovate["vulnerabilityAlerts"], dict):
        raise PolicyError("renovate.vulnerabilityAlerts must be an object")
    # Full native option validation belongs to renovate-config-validator in CI.
    for config in (renovate, renovate.get("vulnerabilityAlerts", {})):
        age = config.get("minimumReleaseAge")
        if age is not None and (not isinstance(age, str) or not re.fullmatch(r"\d+ days?", age)):
            raise PolicyError("Policy minimumReleaseAge must be null or a whole number of days")


def read_file(api, repository, filename, revision):
    value = api.api(f"repos/{repository}/contents/{filename}?{urlencode({'ref': revision})}",
                    missing_ok=True)
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("type") != "file" or value.get("encoding") != "base64":
        raise PolicyError(f"Expected an ordinary file: {repository}/{filename}")
    return base64.b64decode(value["content"]).decode("utf-8")


def load_policy(api, owner, source_dir=None):
    filenames = ["policies/default.json", f"policies/organizations/{owner.lower()}.json"]
    if source_dir:
        def read(filename):
            path = Path(source_dir) / filename
            return path.read_text() if path.is_file() else None
        source = {"repository": SOURCE_REPO, "kind": "local-preview"}
    else:
        revision = api.api(f"repos/{SOURCE_REPO}/commits/HEAD")["sha"]
        source = {"repository": SOURCE_REPO, "kind": "github", "revision": revision}

        def read(filename):
            return read_file(api, SOURCE_REPO, filename, revision)
    texts = [read(filename) for filename in filenames]
    if texts[0] is None:
        raise PolicyError("Canonical policies/default.json is unavailable; publish the policy source first")
    values = []
    for filename, content in zip(filenames, texts):
        if content is not None:
            parsed = json.loads(content)
            if not isinstance(parsed, dict):
                raise PolicyError(f"{filename} must contain a JSON object")
            values.append(parsed)
    policy = merge(values[0], values[1]) if len(values) == 2 else values[0]
    validate(policy)
    source["files"] = {name: digest(content) for name, content in zip(filenames, texts)
                       if content is not None}
    return policy, source


def render(policy, source, owner):
    deps = copy.deepcopy(policy["dependencies"])
    renovate = deps.pop("renovate")
    deps["renovatePreset"] = f"github>{owner}/.github:renovate-config"
    return {
        "renovate-config.json": dump(renovate),
        "repo-policy.json": dump({"schemaVersion": 1, "source": source, "dependencies": deps}),
    }


def check_app_installed(api, owner, app_slug=RENOVATE_APP_SLUG):
    """Report whether a GitHub App is installed on an organization/user account.

    Published policy and an authorized client config prove nothing about
    whether the App that is supposed to act on them is actually installed —
    that is a separate, org-owner-gated step this helper does not perform.
    Returns True/False when the installations endpoint answers definitively,
    or None when it cannot be determined from here (insufficient permissions,
    or a personal account other than the caller's own — `user/installations`
    only lists the authenticated user's own installations, and there is no
    equivalent endpoint for an arbitrary third-party personal account).
    """
    try:
        account = api.api(f"users/{owner}")
    except PolicyError:
        return None
    account_type = account.get("type") if account else None
    try:
        if account_type == "Organization":
            installations = api.api(f"orgs/{owner}/installations")
        elif account_type == "User":
            viewer = api.api("user")
            if not viewer or viewer.get("login", "").lower() != owner.lower():
                return None
            installations = api.api("user/installations")
        else:
            return None
    except PolicyError:
        return None
    entries = (installations or {}).get("installations", [])
    return any(entry.get("app_slug") == app_slug for entry in entries)


def app_status_line(installed, app_slug=RENOVATE_APP_SLUG, install_url=RENOVATE_INSTALL_URL):
    name = app_slug.capitalize()
    if installed is True:
        return f"{name} GitHub App: installed."
    if installed is False:
        return (f"{name} GitHub App: NOT installed — policy published, but no PRs will be "
                f"raised until it is. Install: {install_url} (requires an organization "
                "owner/admin, who may not be the person running this command).")
    return (f"{name} GitHub App: UNKNOWN — could not verify installation from here "
            f"(insufficient permissions, or a personal account other than the caller's own). "
            f"Install (if missing): {install_url}")


def snapshot(api, target):
    metadata = api.api(f"repos/{target}", missing_ok=True)
    if metadata is None:
        return None
    branch = metadata["default_branch"]
    # A 409 for an existing empty repository is an error, not an absent repo.
    commit = api.api(f"repos/{target}/commits/{quote(branch, safe='')}")
    return {"branch": branch, "head": commit["sha"], "tree": commit["commit"]["tree"]["sha"]}


def classify_absence(api, owner, client, target):
    """Best-effort disambiguation of a 404 on the target policy repository.

    GitHub answers 404 both for a repository that does not exist and for one
    the caller cannot see, so certainty is not available from here. What is
    available is evidence: the client repository is a known sibling in the same
    account, so admin on it — plus an organization owner, or the caller's own
    personal account — means a private target would normally have been visible.
    Returns (verdict, evidence) where verdict is "absent" (evidence favors a
    genuinely missing repository), "restricted" (a hidden repository cannot be
    ruled out), or "unknown" (the probe itself could not answer).
    """
    try:
        account = api.api(f"users/{owner}", missing_ok=True)
    except PolicyError:
        return "unknown", f"the {owner} account could not be read to establish access"
    if not isinstance(account, dict):
        return "unknown", f"no GitHub account named {owner} is visible from here"
    try:
        sibling = api.api(f"repos/{client}", missing_ok=True)
    except PolicyError:
        return "unknown", f"{client} could not be read to establish access"
    if not isinstance(sibling, dict):
        return "restricted", (f"the sibling repository {client} is not visible from here either, "
                              f"so this token's view of {owner} is incomplete")
    if not (sibling.get("permissions") or {}).get("admin"):
        return "restricted", (f"you do not have admin on the sibling repository {client}, so a "
                              f"private {target} would answer the same 404")
    account_type = account.get("type")
    if account_type == "Organization":
        return "absent", (f"{owner} is an organization and you have admin on the sibling "
                          f"repository {client}, so a private {target} would normally be visible")
    if account_type == "User":
        try:
            viewer = api.api("user")
        except PolicyError:
            viewer = None
        if isinstance(viewer, dict) and viewer.get("login", "").lower() == owner.lower():
            return "absent", (f"{owner} is your own account, whose repositories are all visible "
                              "to this token")
        return "restricted", (f"{owner} is a personal account other than your own, so its private "
                              "repositories are invisible from here")
    return "unknown", f"the {owner} account type ({account_type or 'unreported'}) is unrecognized"


ABSENCE_SUMMARY = {
    "absent": "appears absent rather than hidden",
    "restricted": "is absent or inaccessible, and this token cannot tell which",
    "unknown": "is absent or inaccessible, and the access check was inconclusive",
}


def absence_message(verdict, evidence, target):
    summary = f"{target} {ABSENCE_SUMMARY[verdict]}: {evidence}."
    if verdict == "absent":
        return (f"{summary} GitHub answers 404 for both an absent and a hidden repository, so this "
                "is evidence, not proof. To create it, plan again with --create-repository public "
                "(or private)")
    return (f"{summary} Verify access first; to create it, plan again with --create-repository "
            "public (or private)")


def client_is_private(api, client):
    """The client repository's own visibility, or None when it cannot be read."""
    try:
        metadata = api.api(f"repos/{client}", missing_ok=True)
    except PolicyError:
        return None
    if not isinstance(metadata, dict) or "private" not in metadata:
        return None
    return bool(metadata["private"])


def check_preset_visibility(api, client, target, create_repository):
    """Refuse a private preset a public client could never resolve.

    A public repository cannot consume a preset from a private `.github`, so
    `--create-repository private` for a public client produces a valid-looking
    `extends` reference that fails at Renovate runtime. Refuse that mismatch;
    warn (never silently proceed) when the client's visibility cannot be read.
    """
    if create_repository != "private":
        return
    private = client_is_private(api, client)
    if private is False:
        raise PolicyError(
            f"{client} is public, but --create-repository private would create a private "
            f"{target}, whose preset a public client cannot resolve: "
            f"`extends: github>{target}:renovate-config` fails at Renovate runtime. Plan again "
            "with --create-repository public, or make the client private first")
    if private is None:
        print(f"repo-org-policy: warning: could not verify whether {client} is public; a private "
              f"{target} cannot be consumed from a public client.", file=sys.stderr)


def deps_handoff(client, pending_pr):
    """Name the next command for the invoking client, and what must happen first."""
    reconcile = (f"run /repo:deps --install in {client} to reconcile that client (its "
                 "renovate.json, Dependabot state, ignorePaths for installer-owned roots, and "
                 "security-PR ownership)")
    if pending_pr:
        return (f"Next: merge the organization PR above, then {reconcile}. A hand-written client "
                "renovate.json is not that reconciliation, and the preset does not resolve until "
                "the PR is merged.")
    return (f"Next: {reconcile}. No organization PR is pending, so nothing has to be merged "
            "first.")


def make_plan(api, client, source_dir=None, create_repository=None):
    client = repo_name(client)
    owner = client.split("/")[0]
    target = f"{owner}/.github"
    policy, source = load_policy(api, owner, source_dir)
    current = snapshot(api, target)
    if current is None:
        # Both extra checks stay behind this branch: the common "target already
        # exists" path must not pay for them.
        if create_repository is None:
            verdict, evidence = classify_absence(api, owner, client, target)
            raise PolicyError(absence_message(verdict, evidence, target))
        check_preset_visibility(api, client, target, create_repository)
    before = {name: read_file(api, target, name, current["head"]) if current else None
              for name in sorted(MANAGED_FILES)}
    after = render(policy, source, owner)
    # Unrelated changes in the canonical repo do not force policy republishing.
    # A changed effective policy or source-file digest still produces a diff.
    if before["repo-policy.json"]:
        try:
            old = json.loads(before["repo-policy.json"])
            new = json.loads(after["repo-policy.json"])
            old_source = old.get("source", {})
            if (old_source.get("kind") == source["kind"] == "github"
                    and old_source.get("repository") == source["repository"]
                    and old_source.get("files") == source["files"]
                    and old.get("dependencies") == new["dependencies"]
                    and old.get("schemaVersion") == new["schemaVersion"]):
                new["source"] = old_source
                after["repo-policy.json"] = dump(new)
        except (ValueError, AttributeError):
            pass  # Existing hand-written/invalid data appears in the diff.
    plan = {"planVersion": 1, "client": client, "target": target, "source": source,
            "createRepository": create_repository if current is None else None,
            "base": current, "before": before, "after": after}
    plan["digest"] = digest(dump(plan))
    return plan


def changed(plan):
    return plan["before"] != plan["after"]


def show_plan(api, plan):
    print(f"Source: {SOURCE_REPO} ({plan['source'].get('revision', 'local preview')})")
    print(f"Client: {plan['client']}  Target: {plan['target']}")
    if plan["createRepository"]:
        print(f"Create repository: {plan['createRepository']} {plan['target']}")
    for name in sorted(MANAGED_FILES):
        print("".join(difflib.unified_diff(
            (plan["before"][name] or "").splitlines(True), plan["after"][name].splitlines(True),
            fromfile=f"{plan['target']}/{name} (current)",
            tofile=f"{plan['target']}/{name} (proposed)")), end="")
    print("Policy PR required." if changed(plan) else "Organization policy is current.")
    # Config presence never proves the App that acts on it is installed —
    # report that separately so "policy published" and "policy published but
    # inert" cannot read the same in this output.
    print(app_status_line(check_app_installed(api, plan["client"].split("/")[0])))


def apply_plan(api, plan):
    supplied_digest = plan.get("digest")
    unsigned = {k: v for k, v in plan.items() if k != "digest"}
    if supplied_digest != digest(dump(unsigned)) or plan.get("planVersion") != 1:
        raise PolicyError("Invalid or edited plan; regenerate and review it")
    client = repo_name(plan["client"])
    owner = client.split("/")[0]
    target = plan["target"]
    if target != f"{owner}/.github" or set(plan["after"]) != MANAGED_FILES:
        raise PolicyError("Plan must only manage the client's owner/.github policy files")
    if plan["source"].get("kind") != "github":
        raise PolicyError("Local source is preview-only. Publish to rjwalters/repo, then generate a fresh plan")
    if plan["createRepository"] not in (None, "public", "private"):
        raise PolicyError("Invalid repository visibility")
    # Rebuild from current source and target before any mutation. This detects
    # source changes, local plan edits, target drift, and repository creation races.
    fresh = make_plan(api, client, create_repository=plan["createRepository"])
    if fresh != plan:
        raise PolicyError("Source or organization changed after preview; regenerate and review the plan")
    if not changed(plan):
        return ("Organization policy is current; nothing to publish.\n"
                + deps_handoff(client, pending_pr=False))
    prefix = f"repos/{target}"
    # Publishing the same policy from two clients must reuse the same org PR.
    publication = {key: plan[key] for key in ("target", "base", "after")}
    branch = f"repo-policy/{digest(dump(publication))[:16]}"
    current = plan["base"]
    if current is None:
        account = api.api(f"users/{owner}")
        if account["type"] == "Organization":
            endpoint = f"orgs/{owner}/repos"
        elif account["type"] == "User" and api.api("user")["login"].lower() == owner.lower():
            endpoint = "user/repos"
        else:
            raise PolicyError("Cannot create a repository for this GitHub account")
        api.api(endpoint, "POST", {"name": ".github", "private": plan["createRepository"] == "private",
                                  "auto_init": True, "description": "Organization repository policy"})
        print(f"Created {target} ({plan['createRepository']}); publishing the policy PR next.", file=sys.stderr)
        current = snapshot(api, target)
        if current is None:
            raise PolicyError("Repository created, but its initial commit is not ready. Generate a new plan")
    # Reuse the exact branch on a retry; never force-update an existing branch.
    existing = api.api(f"{prefix}/git/ref/heads/{branch}", missing_ok=True)
    if existing:
        existing_head = existing["object"]["sha"]
        commit = api.api(f"{prefix}/git/commits/{existing_head}")
        if [p["sha"] for p in commit["parents"]] != [current["head"]]:
            raise PolicyError(f"Existing branch {branch} has changed; inspect it before retrying")
        comparison = api.api(f"{prefix}/compare/{current['head']}...{existing_head}")
        if any(f["filename"] not in MANAGED_FILES
               or f.get("previous_filename", f["filename"]) not in MANAGED_FILES
               for f in comparison["files"]):
            raise PolicyError(f"Existing branch {branch} contains unrelated changes")
        for name, content in plan["after"].items():
            if read_file(api, target, name, existing_head) != content:
                raise PolicyError(f"Existing branch {branch} differs from the reviewed plan")
    else:
        tree = api.api(f"{prefix}/git/trees", "POST", {
            "base_tree": current["tree"], "tree": [
                {"path": name, "mode": "100644", "type": "blob", "content": content}
                for name, content in sorted(plan["after"].items())]})
        commit = api.api(f"{prefix}/git/commits", "POST", {
            "message": "chore: reconcile organization dependency policy",
            "tree": tree["sha"], "parents": [current["head"]]})
        api.api(f"{prefix}/git/refs", "POST", {"ref": f"refs/heads/{branch}", "sha": commit["sha"]})
    query = urlencode({"state": "open", "head": f"{owner}:{branch}", "base": current["branch"]})
    prs = api.api(f"{prefix}/pulls?{query}")
    if prs:
        return f"{prs[0]['html_url']}\n" + deps_handoff(client, pending_pr=True)
    body = (f"Install organization policy from {SOURCE_REPO}@{plan['source']['revision']}.\n\n"
            "The canonical preferences remain in rjwalters/repo/policies/. "
            "Edit them there and rerun /repo:org-policy to update this copy.\n\n"
            "This PR updates only repo-policy.json and renovate-config.json. "
            "Merging activates the preset for repositories that extend it. "
            "GitHub App installation, client onboarding, and repository settings are separate steps.")
    pr = api.api(f"{prefix}/pulls", "POST", {"title": "chore: reconcile organization dependency policy",
                 "head": branch, "base": current["branch"], "body": body})
    return f"{pr['html_url']}\n" + deps_handoff(client, pending_pr=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    preview = subs.add_parser("plan", help="read-only diff; optionally save an apply plan")
    preview.add_argument("--repo", help="client OWNER/REPO; defaults to origin")
    preview.add_argument("--source-dir", type=Path, help="local rjwalters/repo checkout; preview only")
    preview.add_argument("--create-repository", choices=["public", "private"], help="explicit creation if absent")
    preview.add_argument("--output", type=Path, help="save the reviewed plan")
    apply = subs.add_parser("apply", help="publish the saved plan as an organization PR")
    apply.add_argument("--plan", type=Path, required=True)
    apply.add_argument("--yes", action="store_true", help="authorize this reviewed plan")
    check_app = subs.add_parser(
        "check-app", help="report whether a GitHub App (default: renovate) is installed for an owner")
    check_app.add_argument("--repo", help="client OWNER/REPO to derive the owner from; defaults to origin")
    check_app.add_argument("--owner", help="explicit organization/user login; overrides --repo")
    check_app.add_argument("--app-slug", default=RENOVATE_APP_SLUG,
                           help="GitHub App slug to check (default: renovate)")
    args = parser.parse_args()
    try:
        api = GitHub()
        if args.command == "plan":
            plan = make_plan(api, client_repo(args.repo), args.source_dir, args.create_repository)
            show_plan(api, plan)
            if args.output:
                # A fresh path prevents overwriting an unrelated user file.
                with args.output.open("x") as out:
                    out.write(dump(plan))
                print(f"Saved plan: {args.output}")
        elif args.command == "apply":
            if not args.yes:
                raise PolicyError("Review the saved plan, then pass --yes to publish its changes")
            plan = json.loads(args.plan.read_text())
            print(apply_plan(api, plan))
            print(app_status_line(check_app_installed(api, plan["client"].split("/")[0])))
        else:
            owner = args.owner or client_repo(args.repo).split("/")[0]
            # Absence is a report, never an error: this call is purely
            # informational and must not make an otherwise-healthy plan/apply
            # run look like it failed.
            print(app_status_line(check_app_installed(api, owner, args.app_slug), args.app_slug))
        return 0
    except (PolicyError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"repo-org-policy: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
