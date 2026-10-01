#!/usr/bin/env python3
"""Deterministic half of /repo:optimize-ci — audit GitHub Actions workflows.

Parses .github/workflows/*.y(a)ml, extracts triggers, path filters, caches,
concurrency, and toolchains, cross-references required status checks (classic
branch protection + rulesets), and — when Actions history is readable —
estimates minutes per finding from recent runs. Emits a ranked report (text or
--json) that commands/repo/optimize-ci.md reasons over.

READ-ONLY by construction: every GitHub call is a GET. Writing proposed edits
(`--apply`) is the prompt's job, behind its own confirmation.

Subcommands:
  scan    Offline: analyze a local checkout. Required checks are supplied by
          flag (--required CONTEXT, repeatable, or --required-unknown), so the
          analysis is testable with no network.
  report  Online: scan (a local checkout, or --remote to read the workflows via
          the contents API) plus required-check lookup and run-history
          evidence. Degrades to "not measured" / "unknown" rather than failing.

Python 3.9+ and nothing outside the standard library. Workflow YAML is read by
a small built-in block-YAML reader (below) rather than PyYAML: PyYAML is not
guaranteed on a consumer machine, and under YAML 1.1 it parses the `on:` key as
boolean True — the one key this audit cares most about.
"""

import argparse
import base64
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}

# Files whose change can never affect a build or test run in a typical repo.
# Deliberately NOT `**/*.md`: in a prompt/skill repo (this one included)
# markdown IS the product and is exercised by the test suite. The prompt still
# has to verify no job reads these (README-layout tests, doc linters) before
# recommending an ignore list — see optimize-ci.md.
DOC_ONLY_PATTERNS = [
    "docs/**",
    "**/README*",
    "CHANGELOG*",
    "CONTRIBUTING*",
    "CODE_OF_CONDUCT*",
    "SECURITY*",
    "LICENSE*",
    "AUTHORS*",
    ".github/ISSUE_TEMPLATE/**",
    ".github/PULL_REQUEST_TEMPLATE*",
    ".github/pull_request_template*",
]

# Toolchain -> (root-level input files that affect it, when present).
TOOLCHAIN_INPUTS = {
    "node": ["package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
             "npm-shrinkwrap.json", "pnpm-workspace.yaml", ".nvmrc", ".node-version"],
    "python": ["pyproject.toml", "requirements.txt", "requirements-dev.txt",
               "uv.lock", "poetry.lock", "Pipfile", "Pipfile.lock", "setup.py",
               "setup.cfg", ".python-version"],
    "rust": ["Cargo.toml", "Cargo.lock", "rust-toolchain", "rust-toolchain.toml"],
    "go": ["go.mod", "go.sum"],
    "docker": ["Dockerfile", ".dockerignore"],
}
LOCKFILES = {"package-lock.json", "pnpm-lock.yaml", "yarn.lock", "npm-shrinkwrap.json",
             "uv.lock", "poetry.lock", "Pipfile.lock", "Cargo.lock", "go.sum"}

RECOMMENDED_CONCURRENCY = (
    "concurrency:\n"
    "  group: ${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}\n"
    "  cancel-in-progress: ${{ github.event_name == 'pull_request' }}"
)

JOB_LEVEL_FILTER = (
    "Keep the workflow triggering on every PR and filter at JOB level instead: add a\n"
    "`changes` job running dorny/paths-filter, gate the heavy jobs with\n"
    "`if: needs.changes.outputs.<filter> == 'true'`, and add an always-running\n"
    "aggregator job (`if: always()`, `needs: [<all gated jobs>]`, failing only if a\n"
    "needed job failed or was cancelled) — then make THAT aggregator the required\n"
    "check. A skipped job reports as success; a skipped workflow reports nothing."
)


class YamlError(Exception):
    pass


# ---------------------------------------------------------------------------
# Minimal block-YAML reader: block mappings/sequences, single-line flow
# collections, quoted and plain scalars (incl. multi-line plain), block
# scalars (| and >), comments. Anchors/aliases/tags are kept as plain strings.
# That is the subset GitHub workflow files use in practice.
# ---------------------------------------------------------------------------

def _strip_comment(text):
    out, quote = [], None
    for i, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = None
        elif ch in ("'", '"') and (i == 0 or text[i - 1] in " \t:[{,-"):
            quote = ch
        elif ch == "#" and (i == 0 or text[i - 1] in " \t"):
            break
        out.append(ch)
    return "".join(out).rstrip()


def _split_key(text):
    """Return (key, rest) if text is `key: rest` / `key:`, else None."""
    if text[:1] in ("'", '"'):
        q = text[0]
        end = text.find(q, 1)
        while end != -1 and q == "'" and text[end + 1:end + 2] == "'":
            end = text.find(q, end + 2)
        if end == -1:
            return None
        after = text[end + 1:].lstrip()
        if not after.startswith(":"):
            return None
        return _scalar(text[:end + 1]), after[1:].strip()
    if text[:1] in ("[", "{"):
        return None
    depth = 0
    for i, ch in enumerate(text):
        if ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == ":" and depth == 0 and (i + 1 == len(text) or text[i + 1] in " \t"):
            if text.startswith("${{") and "}}" not in text[:i]:
                continue
            return text[:i].strip(), text[i + 1:].strip()
    return None


def _scalar(text):
    t = text.strip()
    if not t:
        return None
    if t[0] == '"' and t.endswith('"') and len(t) >= 2:
        try:
            return json.loads(t)
        except ValueError:
            return t[1:-1]
    if t[0] == "'" and t.endswith("'") and len(t) >= 2:
        return t[1:-1].replace("''", "'")
    if t[0] in "[{":
        return _flow(t)
    # YAML 1.2 core schema only: `on`/`yes`/`no` stay strings.
    if t in ("true", "True", "TRUE"):
        return True
    if t in ("false", "False", "FALSE"):
        return False
    if t.lower() in ("null", "~"):
        return None
    if re.fullmatch(r"-?\d+", t):
        return int(t)
    if re.fullmatch(r"-?\d+\.\d+", t):
        return float(t)
    return t


def _flow_split(inner):
    items, depth, quote, cur = [], 0, None, []
    for ch in inner:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "'\"":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == "," and depth == 0:
            items.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    if "".join(cur).strip():
        items.append("".join(cur).strip())
    return items


def _flow(t):
    if t[0] == "[":
        if not t.endswith("]"):
            raise YamlError(f"unterminated flow sequence: {t}")
        return [_scalar(x) for x in _flow_split(t[1:-1])]
    if not t.endswith("}"):
        raise YamlError(f"unterminated flow mapping: {t}")
    out = {}
    for item in _flow_split(t[1:-1]):
        kv = _split_key(item)
        if kv is None:
            out[_scalar(item)] = None
        else:
            out[kv[0]] = _scalar(kv[1])
    return out


class _Reader:
    def __init__(self, text):
        self.raw = text.replace("\t", "  ").splitlines()
        self.lines = []  # (indent, content, raw_index)
        for idx, raw in enumerate(self.raw):
            content = _strip_comment(raw)
            if not content.strip() or content.strip() in ("---", "..."):
                continue
            indent = len(content) - len(content.lstrip(" "))
            self.lines.append([indent, content.strip(), idx])
        self.i = 0

    def peek(self):
        return self.lines[self.i] if self.i < len(self.lines) else None

    def parse(self):
        if not self.lines:
            return None
        value = self.block(self.lines[0][0])
        if self.peek() is not None:
            raise YamlError(f"unexpected content at line {self.peek()[2] + 1}")
        return value

    def block(self, indent):
        first = self.peek()
        if first[1] == "-" or first[1].startswith("- "):
            return self.sequence(indent)
        if _split_key(first[1]) is None:
            self.i += 1
            value = _scalar(first[1])
            if isinstance(value, str) and first[1][:1] not in "'\"":
                value = self.continuation(value, indent - 1)
            return value
        return self.mapping(indent)

    def sequence(self, indent):
        out = []
        while True:
            line = self.peek()
            if line is None or line[0] != indent or not (line[1] == "-" or line[1].startswith("- ")):
                return out
            rest = line[1][1:].lstrip(" ")
            if not rest:
                self.i += 1
                nxt = self.peek()
                out.append(self.block(nxt[0]) if nxt and nxt[0] > indent else None)
                continue
            # Re-home the inline item content at its own column and parse it
            # as a block, so `- key: v` followed by sibling keys is a mapping.
            line[0] = indent + (len(line[1]) - len(rest))
            line[1] = rest
            out.append(self.block(line[0]))

    def mapping(self, indent):
        out = {}
        while True:
            line = self.peek()
            if line is None or line[0] != indent or line[1] == "-" or line[1].startswith("- "):
                return out
            kv = _split_key(line[1])
            if kv is None:
                raise YamlError(f"expected 'key:' at line {line[2] + 1}: {line[1]}")
            key, rest = kv
            self.i += 1
            if rest[:1] in ("|", ">") and re.fullmatch(r"[|>][-+]?\d*", rest):
                out[key] = self.block_scalar(indent, line[2], rest)
            elif rest:
                if rest[:1] in "[{" and not self._balanced(rest):
                    rest = self.join_flow(rest)
                val = _scalar(rest)
                if isinstance(val, str) and rest[:1] not in "'\"":
                    val = self.continuation(val, indent)
                out[key] = val
            else:
                nxt = self.peek()
                if nxt and nxt[0] > indent:
                    out[key] = self.block(nxt[0])
                elif nxt and nxt[0] == indent and (nxt[1] == "-" or nxt[1].startswith("- ")):
                    out[key] = self.sequence(indent)
                else:
                    out[key] = None

    @staticmethod
    def _balanced(text):
        return text.count("[") + text.count("{") == text.count("]") + text.count("}")

    def join_flow(self, text):
        while not self._balanced(text) and self.peek() is not None:
            text += " " + self.peek()[1]
            self.i += 1
        return text

    def continuation(self, value, indent):
        # Multi-line plain scalar: deeper lines that are not themselves keys.
        while True:
            nxt = self.peek()
            if nxt is None or nxt[0] <= indent or _split_key(nxt[1]) is not None \
                    or nxt[1].startswith("- "):
                return value
            value = f"{value} {nxt[1]}"
            self.i += 1

    def block_scalar(self, indent, header_idx, header):
        # Consume raw lines (blank ones included) deeper than the key.
        body, idx = [], header_idx + 1
        while idx < len(self.raw):
            raw = self.raw[idx]
            if raw.strip() and (len(raw) - len(raw.lstrip(" "))) <= indent:
                break
            body.append(raw)
            idx += 1
        while self.i < len(self.lines) and self.lines[self.i][2] < idx:
            self.i += 1
        nonblank = [len(b) - len(b.lstrip(" ")) for b in body if b.strip()]
        cut = min(nonblank) if nonblank else 0
        text = [b[cut:] for b in body]
        while text and not text[-1].strip():
            text.pop()
        if header[0] == "|":
            value = "\n".join(text)
        else:
            # Folding: line breaks between plain lines become spaces; blank
            # lines and more-indented lines keep their breaks.
            parts = []
            for t in text:
                if not t.strip():
                    parts.append("\n")
                elif t[0] == " ":
                    if parts and not parts[-1].endswith("\n"):
                        parts.append("\n")
                    parts.append(t + "\n")
                else:
                    if parts and not parts[-1].endswith("\n"):
                        parts.append(" ")
                    parts.append(t)
            value = "".join(parts).rstrip("\n")
        # Chomping: `-` strips the final break, default (clip) keeps one.
        return value if "-" in header or not value else value + "\n"


def parse_yaml(text):
    return _Reader(text).parse()


# ---------------------------------------------------------------------------
# GitHub path-filter globs (paths / paths-ignore): `**` crosses `/`, `*` and
# `?` do not, `!` negates, and the last matching pattern wins.
# ---------------------------------------------------------------------------

def glob_regex(pattern):
    out, i = [], 0
    while i < len(pattern):
        ch = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif ch == "*":
            out.append("[^/]*")
            i += 1
        elif ch == "?":
            out.append("[^/]")
            i += 1
        elif ch == "[":
            end = pattern.find("]", i)
            if end == -1:
                out.append(re.escape(ch))
                i += 1
            else:
                out.append(pattern[i:end + 1])
                i = end + 1
        else:
            out.append(re.escape(ch))
            i += 1
    return re.compile("".join(out) + r"\Z")


def filter_matches(patterns, path):
    """True if `path` is selected by an ordered GitHub pattern list."""
    hit = False
    for pat in patterns or []:
        pat = str(pat)
        neg = pat.startswith("!")
        if glob_regex(pat[1:] if neg else pat).match(path):
            hit = not neg
    return hit


def is_doc_only(path):
    return filter_matches(DOC_ONLY_PATTERNS, path)


# ---------------------------------------------------------------------------
# Workflow model
# ---------------------------------------------------------------------------

def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def normalize_on(on):
    """Return {event: config-dict} for the workflow's triggers."""
    if isinstance(on, str):
        return {on: {}}
    if isinstance(on, list):
        return {str(e): {} for e in on}
    if isinstance(on, dict):
        return {str(k): (v if isinstance(v, dict) else {}) for k, v in on.items()}
    return {}


def _text(value):
    return "" if value is None else str(value)


def detect_toolchains(steps):
    found = set()
    for step in steps:
        uses = _text(step.get("uses")).lower()
        run = _text(step.get("run"))
        if "setup-node" in uses or "pnpm/action-setup" in uses or \
                re.search(r"(^|[\s;&|(])(npm|pnpm|yarn|npx)\s", run):
            found.add("node")
        if "setup-python" in uses or "setup-uv" in uses or "snok/install-poetry" in uses or \
                re.search(r"(^|[\s;&|(])(pip3?|uv|poetry|pytest|tox)\s", run):
            found.add("python")
        if "rust-toolchain" in uses or "rust-cache" in uses or \
                re.search(r"(^|[\s;&|(])cargo\s", run):
            found.add("rust")
        if "setup-go" in uses or re.search(r"(^|[\s;&|(])go (build|test|vet|mod|run)\b", run):
            found.add("go")
        if "docker/build-push-action" in uses or re.search(r"(^|[\s;&|(])docker (buildx )?build\b", run):
            found.add("docker")
    return found


def job_display_name(job_id, job):
    name = job.get("name") if isinstance(job, dict) else None
    return str(name) if name else job_id


def is_required(job_id, job, required):
    """Does this job produce a check-run whose context is a required check?"""
    names = {job_id, job_display_name(job_id, job)}
    for ctx in required:
        for name in names:
            if ctx == name or ctx.startswith(name + " (") or ctx.startswith(name + " / ") \
                    or ctx.endswith(" / " + name):
                return True
            # Names with expressions (`test (${{ matrix.os }})`) — compare the
            # literal prefix before the first expression.
            if "${{" in name:
                prefix = name.split("${{", 1)[0].rstrip(" (")
                if prefix and ctx.startswith(prefix):
                    return True
    return False


class Workflow:
    def __init__(self, path, text):
        self.path = path
        self.error = None
        try:
            doc = parse_yaml(text)
        except (YamlError, ValueError, UnicodeDecodeError) as exc:
            doc, self.error = {}, str(exc)
        if not isinstance(doc, dict):
            doc, self.error = {}, self.error or "workflow is not a mapping"
        self.doc = doc
        self.name = _text(doc.get("name")) or Path(path).name
        self.on = normalize_on(doc.get("on", doc.get(True)))
        jobs = doc.get("jobs")
        self.jobs = {str(k): (v if isinstance(v, dict) else {}) for k, v in (jobs or {}).items()} \
            if isinstance(jobs, dict) else {}

    def steps(self, job):
        return [s for s in _as_list(job.get("steps")) if isinstance(s, dict)]

    def all_steps(self):
        return [s for job in self.jobs.values() for s in self.steps(job)]

    @property
    def pr_events(self):
        return [e for e in ("pull_request", "pull_request_target") if e in self.on]

    def uses_job_level_filter(self):
        for job in self.jobs.values():
            for step in self.steps(job):
                uses = _text(step.get("uses")).lower()
                if "paths-filter" in uses or "changed-files" in uses:
                    return True
            if "needs.changes" in _text(job.get("if")):
                return True
        return False

    def local_uses(self):
        out = set()
        for job in self.jobs.values():
            refs = [_text(job.get("uses"))] + [_text(s.get("uses")) for s in self.steps(job)]
            for ref in refs:
                if ref.startswith("./"):
                    out.add(ref[2:].split("@", 1)[0].rstrip("/"))
        return out


def load_local(root):
    wf_dir = Path(root) / ".github" / "workflows"
    if not wf_dir.is_dir():
        return []
    return [Workflow(f".github/workflows/{p.name}", p.read_text(encoding="utf-8", errors="replace"))
            for p in sorted(wf_dir.iterdir()) if p.suffix in (".yml", ".yaml") and p.is_file()]


def local_root_files(root):
    return {p.name for p in Path(root).iterdir() if p.is_file()} if Path(root).is_dir() else set()


# ---------------------------------------------------------------------------
# Static analysis
# ---------------------------------------------------------------------------

def finding(fid, severity, category, wf, message, recommendation, job=None, measure=None):
    return {"id": fid, "severity": severity, "category": category, "workflow": wf.path,
            "job": job, "message": message, "recommendation": recommendation,
            "measure": measure, "estMinutesSaved": None}


def analyze(workflows, root_files, required, default_branch="main", partial=False):
    """Static findings. `required` is a list of contexts, or None for unknown.

    `partial` means `required` came from only some sources (another was
    unreadable): a workflow matching none of them may still be required.
    """
    findings = []
    required_known = required is not None and not partial
    for wf in workflows:
        if wf.error:
            findings.append(finding("unparseable-workflow", "low", "meta", wf,
                                    f"could not parse ({wf.error}); not audited",
                                    "The built-in reader could not parse it (YAML anchors/aliases "
                                    "are unsupported). Check that GitHub accepts the file (an "
                                    "invalid workflow never runs), then audit it by hand."))
            continue
        findings += _path_findings(wf, root_files, required, required_known)
        findings += _cache_findings(wf)
        findings += _wasted_run_findings(wf, default_branch)
    # One row per distinct problem: a job with three identical build steps
    # should read as one finding (x3), not three.
    merged = {}
    for f in findings:
        k = (f["id"], f["workflow"], f["job"], f["message"])
        if k in merged:
            merged[k]["occurrences"] += 1
        else:
            f["occurrences"] = 1
            merged[k] = f
    return list(merged.values())


def _required_jobs(wf, required):
    if required is None:
        return []
    return [jid for jid, job in wf.jobs.items() if is_required(jid, job, required)]


def _path_findings(wf, root_files, required, required_known):
    out = []
    req_jobs = _required_jobs(wf, required)
    wf_filtered = [e for e in wf.pr_events
                   if wf.on[e].get("paths") is not None or wf.on[e].get("paths-ignore") is not None]

    # Required-check safety (critical): a workflow-level PR path filter skips
    # the whole workflow, which then never reports its required status.
    if wf_filtered and req_jobs:
        names = ", ".join(f"`{job_display_name(j, wf.jobs[j])}`" for j in req_jobs)
        out.append(finding(
            "required-check-workflow-paths", "critical", "required-check", wf,
            f"workflow-level `{wf_filtered[0]}` path filter on a workflow whose job(s) {names} "
            "are REQUIRED status checks — a PR that touches none of the paths never gets that "
            "status and is blocked forever",
            "Remove the workflow-level `paths:`/`paths-ignore:`. " + JOB_LEVEL_FILTER))
    elif wf_filtered and not required_known:
        out.append(finding(
            "unverified-required-check-paths", "medium", "required-check", wf,
            f"workflow-level `{wf_filtered[0]}` path filter, but required checks could not be "
            "(fully) read — if any job here is a required check, a PR that touches none of the "
            "paths never gets that status and is blocked forever",
            "Verify in the branch protection / ruleset settings that no job in this workflow is a "
            "required check. If one is, remove the workflow-level `paths:`/`paths-ignore:`. "
            + JOB_LEVEL_FILTER))

    # Change relevance: PR-triggered, no filter at any level.
    if wf.pr_events and not wf_filtered and not wf.uses_job_level_filter() and wf.jobs:
        if req_jobs or not required_known:
            why = ("contains required check(s) " + ", ".join(f"`{j}`" for j in req_jobs)
                   if req_jobs else "required checks could not be (fully) read, so assume it may be required")
            rec = f"Workflow {why}: do NOT add workflow-level `paths:`. " + JOB_LEVEL_FILTER
        else:
            rec = ("No job here is a required check, so a workflow-level filter is safe. Add "
                   "`paths-ignore:` for files no job reads, e.g. " +
                   ", ".join(f"`{p}`" for p in DOC_ONLY_PATTERNS[:5]) +
                   " — after confirming no job consumes them (README tests, doc linters, "
                   "link checkers). Never ignore lockfiles, the workflow file, or shared dirs.")
        out.append(finding(
            "unfiltered-pr-workflow", "medium", "path-filter", wf,
            "runs every job on every PR, including documentation-only PRs", rec,
            measure="doc-only-pr-runs"))

    # Under-filtering: an existing PR `paths:` list that omits a real input, so
    # a PR changing only that input skips validation and merges green. Push-only
    # filters are not checked: on a release/deploy workflow they are deliberate
    # scoping, not a merge gate.
    inputs = inferred_inputs(wf, root_files)
    for event in wf.pr_events:
        cfg = wf.on[event]
        missing = []
        if cfg.get("paths") is not None:
            missing = [p for p in inputs if not filter_matches(_as_list(cfg["paths"]), p)]
        elif cfg.get("paths-ignore") is not None:
            missing = [p for p in inputs if filter_matches(_as_list(cfg["paths-ignore"]), p)]
        if missing:
            kind = "`paths:` omits" if cfg.get("paths") is not None else "`paths-ignore:` excludes"
            out.append(finding(
                "under-filtered-paths", "high", "path-filter", wf,
                f"`on.{event}` {kind} real input(s) {', '.join(f'`{m}`' for m in missing)} — a "
                "change to them skips this workflow and can merge green while broken",
                f"Add {', '.join(f'`{m}`' for m in missing)} to `on.{event}.paths` "
                "(or drop them from `paths-ignore`)."))
    return out


def inferred_inputs(wf, root_files):
    """Files this workflow demonstrably depends on (that exist at the root)."""
    inputs = [wf.path]
    toolchains = detect_toolchains(wf.all_steps())
    for tc in sorted(toolchains):
        for name in TOOLCHAIN_INPUTS[tc]:
            if name in root_files and name not in inputs:
                inputs.append(name)
    for ref in sorted(wf.local_uses()):
        target = ref if ref.endswith((".yml", ".yaml")) else ref + "/action.yml"
        if target not in inputs:
            inputs.append(target)
    return inputs


def _key_text(value):
    if isinstance(value, list):
        return " ".join(_text(v) for v in value)
    return _text(value)


def _cache_findings(wf):
    out = []
    for jid, job in wf.jobs.items():
        steps = wf.steps(job)
        has_matrix = isinstance(job.get("strategy"), dict) and job["strategy"].get("matrix") is not None
        job_caches = False
        for step in steps:
            uses = _text(step.get("uses")).lower()
            with_ = step.get("with") if isinstance(step.get("with"), dict) else {}
            if uses.startswith("actions/cache") and "/save" not in uses:
                job_caches = True
                key = _key_text(with_.get("key"))
                restore = with_.get("restore-keys")
                if re.search(r"github\.(sha|run_id|run_number|run_attempt)", key):
                    out.append(finding(
                        "cache-key-never-hits", "low" if restore else "high", "cache", wf,
                        f"cache key `{key}` includes a per-run value, so the exact key never hits"
                        + (" (restore-keys still restore a prefix, but every run uploads a new "
                           "cache and evicts useful ones)" if restore else ""),
                        "Key on the inputs instead: `${{ runner.os }}-<tool>-${{ hashFiles('<lockfile>') }}`"
                        " with a `restore-keys:` prefix fallback.", job=jid))
                elif "hashFiles(" not in key:
                    out.append(finding(
                        "cache-key-no-lockfile-hash", "high", "cache", wf,
                        f"cache key `{key}` has no `hashFiles(...)` of a lockfile, so it never "
                        "invalidates — stale dependencies are restored forever",
                        "Append `${{ hashFiles('<lockfile>') }}` (package-lock.json, pnpm-lock.yaml, "
                        "uv.lock, Cargo.lock, go.sum, …) to the key.", job=jid))
                if not restore and "hashFiles(" in key:
                    out.append(finding(
                        "cache-no-restore-keys", "low", "cache", wf,
                        "cache has no `restore-keys:` fallback, so every lockfile change is a full "
                        "cold miss", "Add `restore-keys: ${{ runner.os }}-<tool>-`.", job=jid))
                if has_matrix and "matrix." not in key:
                    out.append(finding(
                        "cache-matrix-key-collision", "medium", "cache", wf,
                        "matrix job caches under a key with no `matrix.*` component — matrix legs "
                        "race to save and restore each other's (wrong) caches",
                        "Include the distinguishing matrix values (e.g. `${{ matrix.os }}`, "
                        "`${{ matrix.node }}`) in the key.", job=jid))
            if "swatinem/rust-cache" in uses:
                job_caches = True
        out += _toolchain_cache_findings(wf, jid, steps, job_caches)
    return out


def _toolchain_cache_findings(wf, jid, steps, job_caches):
    out = []
    runs = "\n".join(_text(s.get("run")) for s in steps)
    uses_all = [(_text(s.get("uses")).lower(), s.get("with") if isinstance(s.get("with"), dict) else {})
                for s in steps]

    def step_with(fragment):
        return [w for u, w in uses_all if fragment in u]

    def miss(tool, message, rec):
        out.append(finding(f"no-cache-{tool}", "medium", "cache", wf, message, rec,
                           job=jid, measure="job-minutes"))

    node = step_with("setup-node")
    if re.search(r"\b(npm (ci|install|i)\b|pnpm (install|i)\b|yarn( install)?\s*($|\n|&&))", runs) \
            and not job_caches and not any(w.get("cache") for w in node):
        miss("node", "installs node dependencies with no dependency cache",
             "Set `cache: npm|pnpm|yarn` on actions/setup-node (keys on the lockfile hash "
             "automatically).")
    py = step_with("setup-python")
    if re.search(r"\b(pip3? install|poetry install|pipenv install)\b", runs) and not job_caches \
            and not any(w.get("cache") for w in py):
        miss("python", "installs python dependencies with no dependency cache",
             "Set `cache: pip|poetry|pipenv` on actions/setup-python (with "
             "`cache-dependency-path:` pointing at the lockfile), or use astral-sh/setup-uv.")
    for w in step_with("setup-uv"):
        if _text(w.get("enable-cache")).lower() == "false":
            miss("python", "astral-sh/setup-uv has `enable-cache: false`",
                 "Drop `enable-cache: false` (or set it true) so the uv cache keys on uv.lock.")
    if re.search(r"(^|[\s;&|(])cargo (build|test|clippy|check|nextest)\b", runs) and not job_caches:
        miss("rust", "builds with cargo with no `~/.cargo` / `target/` cache",
             "Add `Swatinem/rust-cache` after the toolchain step (keys on Cargo.lock and the "
             "toolchain).")
    for w in step_with("setup-go"):
        if _text(w.get("cache")).lower() == "false":
            miss("go", "actions/setup-go has `cache: false`",
                 "Remove `cache: false`; setup-go caches the module/build cache keyed on go.sum "
                 "by default.")
    for w in step_with("docker/build-push-action"):
        if not w.get("cache-from"):
            miss("docker", "docker/build-push-action with no `cache-from` — every layer rebuilds",
                 "Add `cache-from: type=gha` and `cache-to: type=gha,mode=max`.")
    if re.search(r"(^|[\s;&|(])docker build\b", runs) and "--cache-from" not in runs:
        miss("docker", "plain `docker build` with no layer cache — every layer rebuilds each run",
             "Switch to docker/setup-buildx-action + docker/build-push-action with "
             "`cache-from: type=gha` / `cache-to: type=gha,mode=max`.")
    return out


def _cancel_value(conc):
    if isinstance(conc, dict):
        return conc.get("cancel-in-progress")
    return None


def _pushes_default_branch(wf, default_branch):
    if "push" not in wf.on:
        return False
    branches = wf.on["push"].get("branches")
    if branches is None:
        return wf.on["push"].get("tags") is None or wf.on["push"].get("branches-ignore") is not None
    return filter_matches(_as_list(branches), default_branch)


def _wasted_run_findings(wf, default_branch):
    out = []
    if not wf.pr_events or not wf.jobs:
        return out
    wf_conc = wf.doc.get("concurrency")
    job_concs = [job.get("concurrency") for job in wf.jobs.values()]
    if wf_conc is None and not all(c is not None for c in job_concs):
        out.append(finding(
            "missing-concurrency", "medium", "wasted-runs", wf,
            "no `concurrency:` group — every push to a PR leaves the superseded run burning minutes",
            "Add at workflow level (cancels superseded PR runs; never cancels default-branch "
            "pushes):\n" + RECOMMENDED_CONCURRENCY, measure="superseded-pr-runs"))
    elif wf_conc is not None:
        cancel = _cancel_value(wf_conc)
        if cancel is None or cancel is False:
            out.append(finding(
                "concurrency-no-cancel", "medium", "wasted-runs", wf,
                "`concurrency:` group without `cancel-in-progress` — superseded PR runs queue "
                "instead of being cancelled",
                "Set `cancel-in-progress: ${{ github.event_name == 'pull_request' }}`.",
                measure="superseded-pr-runs"))
        elif cancel is True and _pushes_default_branch(wf, default_branch):
            out.append(finding(
                "cancel-on-default-branch", "medium", "wasted-runs", wf,
                f"`cancel-in-progress: true` also cancels `{default_branch}` pushes — a burst of "
                f"merges can leave intermediate `{default_branch}` commits with no completed run",
                "Scope cancellation to PRs: "
                "`cancel-in-progress: ${{ github.event_name == 'pull_request' }}`."))
    push = wf.on.get("push")
    if push is not None and all(push.get(k) is None for k in ("branches", "branches-ignore", "tags")):
        out.append(finding(
            "duplicate-push-pr", "medium", "wasted-runs", wf,
            "triggers on `push` to every branch AND `pull_request` — each commit on a "
            "same-repo PR branch runs twice",
            f"Restrict `on.push.branches` to `[{default_branch}]` (PRs are covered by "
            "`pull_request`).", measure="duplicate-push-runs"))
    return out


# ---------------------------------------------------------------------------
# GitHub reads (all GET). Every failure degrades; none aborts the report.
# ---------------------------------------------------------------------------

class ApiError(Exception):
    def __init__(self, endpoint, message):
        super().__init__(f"{endpoint}: {message}")
        m = re.search(r"HTTP (\d{3})", message)
        self.status = int(m.group(1)) if m else None


class GitHub:
    def api(self, endpoint):
        result = subprocess.run(["gh", "api", "--method", "GET", endpoint],
                                text=True, capture_output=True, check=False)
        if result.returncode:
            raise ApiError(endpoint, (result.stderr or result.stdout).strip())
        return json.loads(result.stdout) if result.stdout.strip() else None


def origin_repo():
    result = subprocess.run(["git", "remote", "get-url", "origin"],
                            text=True, capture_output=True, check=False)
    url = result.stdout.strip()
    m = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$", url)
    return f"{m.group(1)}/{m.group(2)}" if m else None


def _check_contexts(data):
    found = list(data.get("contexts") or []) + [c.get("context") for c in data.get("checks") or []]
    return [c for c in found if c]


def _classic_required(gh, repo, branch):
    """Classic branch-protection required contexts as (source_state, contexts).

    `repos/O/R/branches/<b>` exposes `protected` and
    `protection.required_status_checks` to anyone with read access, so it is the
    primary source. The dedicated protection endpoint answers 404 to every
    non-admin token even when checks ARE required, so its 404 means "none" only
    when the branch itself reports `protected: false`; otherwise it is unknown.
    """
    protected = None
    try:
        info = gh.api(f"repos/{repo}/branches/{branch}") or {}
        protected = info.get("protected")
        if protected is False:
            return "none", []
        rsc = (info.get("protection") or {}).get("required_status_checks")
        if isinstance(rsc, dict) and ("contexts" in rsc or "checks" in rsc):
            found = _check_contexts(rsc)
            return ("read" if found else "none"), found
    except ApiError:
        pass
    try:
        found = _check_contexts(gh.api(f"repos/{repo}/branches/{branch}/protection/required_status_checks") or {})
        return "read", found
    except ApiError as exc:
        why = ("branch is protected but its checks are unreadable" if protected
               else "branch protection status unreadable")
        return f"unknown ({exc.status or 'error'}: {why})", []


def required_checks(gh, repo, branch):
    """Union of classic-protection and ruleset required contexts.

    Returns {"state": known|unknown, "partial": bool, "contexts": [...], "sources": {...}}.
    An unreadable source is never reported as "no required checks": with no
    contexts from any source the state is unknown; with contexts from one source
    but another unreadable, `partial` is set and callers must treat any workflow
    matching none of the known contexts as possibly required.
    """
    sources = {}
    sources["classic"], contexts = _classic_required(gh, repo, branch)
    unknown = sources["classic"].startswith("unknown")
    try:
        for rule in gh.api(f"repos/{repo}/rules/branches/{branch}") or []:
            if rule.get("type") == "required_status_checks":
                for chk in (rule.get("parameters") or {}).get("required_status_checks") or []:
                    if chk.get("context"):
                        contexts.append(chk["context"])
        sources["rulesets"] = "read"
    except ApiError as exc:
        if exc.status == 404:
            sources["rulesets"] = "none"
        else:
            sources["rulesets"] = f"unknown ({exc.status or 'error'})"
            unknown = True
    uniq = sorted(set(contexts))
    return {"state": "unknown" if unknown and not uniq else "known",
            "partial": unknown and bool(uniq), "contexts": uniq, "sources": sources}


def load_remote(gh, repo, ref):
    listing = gh.api(f"repos/{repo}/contents/.github/workflows?ref={ref}") or []
    workflows = []
    for entry in listing:
        name = entry.get("name", "")
        if entry.get("type") != "file" or not name.endswith((".yml", ".yaml")):
            continue
        blob = gh.api(f"repos/{repo}/contents/.github/workflows/{name}?ref={ref}") or {}
        text = base64.b64decode(blob.get("content", "")).decode("utf-8", errors="replace")
        workflows.append(Workflow(f".github/workflows/{name}", text))
    root = gh.api(f"repos/{repo}/contents?ref={ref}") or []
    return workflows, {e.get("name") for e in root if e.get("type") == "file"}


def _ts(value):
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) if value else None


def _minutes(start, end):
    s, e = _ts(start), _ts(end)
    return max((e - s).total_seconds() / 60.0, 0.0) if s and e else 0.0


def gather_history(gh, repo, max_runs=100, days=30, jobs_sample=5, pr_sample=30):
    """Recent runs, per-workflow runner-minutes, and per-PR changed files."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    runs, page = [], 1
    while len(runs) < max_runs:
        data = gh.api(f"repos/{repo}/actions/runs?per_page={min(100, max_runs)}&page={page}"
                      f"&created=%3E%3D{since}") or {}
        batch = data.get("workflow_runs") or []
        runs += batch
        if len(batch) < min(100, max_runs):
            break
        page += 1
    runs = [r for r in runs[:max_runs] if r.get("status") == "completed"]
    by_path = {}
    for run in runs:
        by_path.setdefault(run.get("path", "").split("@", 1)[0], []).append(run)
    per_workflow = {}
    for path, wf_runs in by_path.items():
        sampled, job_minutes = 0, {}
        total_sample = 0.0
        for run in wf_runs[:jobs_sample]:
            try:
                jobs = (gh.api(f"repos/{repo}/actions/runs/{run['id']}/jobs?per_page=100") or {}).get("jobs") or []
            except ApiError:
                continue
            sampled += 1
            for job in jobs:
                mins = _minutes(job.get("started_at"), job.get("completed_at"))
                job_minutes.setdefault(job.get("name", "?"), []).append(mins)
                total_sample += mins
        if sampled:
            per_run = total_sample / sampled
        else:
            walls = [_minutes(r.get("run_started_at"), r.get("updated_at")) for r in wf_runs]
            per_run = sum(walls) / len(walls) if walls else 0.0
        per_workflow[path] = {
            "runs": len(wf_runs),
            "avgRunnerMinutesPerRun": round(per_run, 2),
            "totalRunnerMinutes": round(per_run * len(wf_runs), 1),
            "jobs": {k: round(sum(v) / len(v), 2) for k, v in sorted(job_minutes.items())},
            "sampledRuns": sampled,
        }
    # A run's `pull_requests` array is emptied once its PR merges and the
    # branch is deleted, so fall back to one listing of recent PRs and map the
    # run's head branch to a PR number.
    by_branch = {}
    if any(r.get("event") in ("pull_request", "pull_request_target") and not r.get("pull_requests")
           for r in runs):
        try:
            for pr in gh.api(f"repos/{repo}/pulls?state=all&sort=updated&direction=desc&per_page=100") or []:
                by_branch.setdefault((pr.get("head") or {}).get("ref"), pr.get("number"))
        except ApiError:
            pass
    pr_files, pr_numbers = {}, []
    for run in runs:
        if run.get("event") in ("pull_request", "pull_request_target"):
            nums = [pr.get("number") for pr in run.get("pull_requests") or []]
            if not nums and by_branch.get(run.get("head_branch")):
                nums = [by_branch[run["head_branch"]]]
            run["_prs"] = nums
            for number in nums:
                if number not in pr_numbers:
                    pr_numbers.append(number)
    for number in pr_numbers[:pr_sample]:
        try:
            files = gh.api(f"repos/{repo}/pulls/{number}/files?per_page=100") or []
            pr_files[number] = [f.get("filename", "") for f in files]
        except ApiError:
            continue
    return {"since": since, "runs": runs, "perWorkflow": per_workflow, "prFiles": pr_files,
            "prSampled": len(pr_files), "prSeen": len(pr_numbers)}


def estimate(findings, history):
    """Fill estMinutesSaved (over the sampled window) where a finding is measurable."""
    if history is None:
        return
    for f in findings:
        stats = history["perWorkflow"].get(f["workflow"])
        if not stats:
            if f["measure"]:
                f["estNote"] = "no runs of this workflow in the window"
                if f["measure"] != "job-minutes":
                    f["estMinutesSaved"] = 0.0
            continue
        per_run = stats["avgRunnerMinutesPerRun"]
        runs = [r for r in history["runs"] if r.get("path", "").split("@", 1)[0] == f["workflow"]]
        if f["measure"] == "doc-only-pr-runs":
            pr_runs = [r for r in runs if r.get("event") in ("pull_request", "pull_request_target")]
            doc_only = 0
            known = 0
            for r in pr_runs:
                nums = r.get("_prs") or [p.get("number") for p in r.get("pull_requests") or []]
                files = next((history["prFiles"][n] for n in nums if n in history["prFiles"]), None)
                if files is None:
                    continue
                known += 1
                if files and all(is_doc_only(x) for x in files):
                    doc_only += 1
            f["estMinutesSaved"] = round(doc_only * per_run, 1)
            f["estNote"] = f"{doc_only}/{known} classified PR runs touched only documentation files"
        elif f["measure"] == "superseded-pr-runs":
            pr_runs = sorted([r for r in runs if r.get("event") in ("pull_request", "pull_request_target")],
                             key=lambda r: r.get("created_at", ""))
            saved, count = 0.0, 0
            for i, r in enumerate(pr_runs):
                later = [x for x in pr_runs[i + 1:] if x.get("head_branch") == r.get("head_branch")]
                if not later or r.get("conclusion") == "cancelled":
                    continue
                nxt_created, end = _ts(later[0].get("created_at")), _ts(r.get("updated_at"))
                start = _ts(r.get("run_started_at") or r.get("created_at"))
                if nxt_created and end and start and nxt_created < end:
                    wall = max((end - start).total_seconds(), 1.0)
                    frac = min((end - nxt_created).total_seconds() / wall, 1.0)
                    saved += frac * per_run
                    count += 1
            f["estMinutesSaved"] = round(saved, 1)
            f["estNote"] = f"{count} PR run(s) kept running after a newer push to the same branch"
        elif f["measure"] == "duplicate-push-runs":
            pr_shas = {r.get("head_sha") for r in runs if r.get("event") == "pull_request"}
            dup = [r for r in runs if r.get("event") == "push" and r.get("head_sha") in pr_shas]
            f["estMinutesSaved"] = round(len(dup) * per_run, 1)
            f["estNote"] = f"{len(dup)} push run(s) duplicated a pull_request run of the same commit"
        elif f["measure"] == "job-minutes":
            f["estNote"] = (f"not measured (cache hit/miss needs job logs); workflow averages "
                            f"{per_run} runner-min/run over {stats['runs']} runs")


def rank(findings):
    def key(f):
        est = f.get("estMinutesSaved")
        return (0 if f["severity"] == "critical" else 1,
                -(est or 0.0),
                SEVERITY_ORDER.get(f["severity"], 9), f["workflow"], f.get("job") or "", f["id"])
    return sorted(findings, key=key)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def build_report(workflows, findings, required_info, history_info, repo=None, default_branch="main"):
    return {
        "repo": repo,
        "defaultBranch": default_branch,
        "workflowsFound": len(workflows),
        "requiredChecks": required_info,
        "history": history_info,
        "workflows": [{"file": w.path, "name": w.name, "triggers": sorted(w.on),
                       "jobs": sorted(w.jobs), "parseError": w.error} for w in workflows],
        "findings": rank(findings),
    }


def render_text(report):
    lines = []
    title = report["repo"] or "local checkout"
    lines.append(f"CI OPTIMIZATION REPORT — {title} ({report['workflowsFound']} workflows, "
                 f"default branch {report['defaultBranch']})")
    lines.append("=" * len(lines[0]))
    if report["workflowsFound"] == 0:
        lines.append("No .github/workflows/*.yml found — nothing to audit (GitHub Actions only).")
        return "\n".join(lines) + "\n"
    rc = report["requiredChecks"]
    if rc["state"] == "unknown":
        lines.append("Required checks: UNKNOWN (" + ", ".join(f"{k}: {v}" for k, v in rc.get("sources", {}).items())
                     + ") — treating every PR workflow as possibly required")
    elif rc["contexts"]:
        lines.append("Required checks: " + ", ".join(f"`{c}`" for c in rc["contexts"])
                     + (" (partial — a source was unreadable; workflows matching none of these are "
                        "treated as possibly required)" if rc.get("partial") else ""))
    else:
        lines.append("Required checks: none configured")
    h = report["history"]
    if h["state"] == "measured":
        lines.append(f"Run history: {h['runs']} completed runs since {h['since']}; "
                     f"PR file lists for {h['prSampled']}/{h['prSeen']} PRs (minutes are over this window)")
    else:
        lines.append(f"Run history: not measured ({h.get('reason', 'unavailable')})")
    lines.append("")
    if not report["findings"]:
        lines.append("No findings.")
        return "\n".join(lines) + "\n"
    for n, f in enumerate(report["findings"], 1):
        est = f.get("estMinutesSaved")
        est_s = "not measured" if est is None else f"~{est} min saved"
        where = Path(f["workflow"]).name + (f" / {f['job']}" if f.get("job") else "")
        times = f" x{f['occurrences']}" if f.get("occurrences", 1) > 1 else ""
        lines.append(f"{n:>2}. [{f['severity']}] {where} — {f['id']}{times} ({est_s})")
        lines.append(f"    {f['message']}")
        if f.get("estNote"):
            lines.append(f"    evidence: {f['estNote']}")
        for i, rec in enumerate(f["recommendation"].splitlines()):
            lines.append(("    fix: " if i == 0 else "         ") + rec)
    return "\n".join(lines) + "\n"


def run_scan(args):
    root = Path(args.root)
    workflows = load_local(root)
    required = None if args.required_unknown else list(args.required or [])
    findings = analyze(workflows, local_root_files(root), required, args.default_branch)
    required_info = {"state": "unknown" if required is None else "known",
                     "contexts": sorted(required or []), "sources": {"flag": "supplied"}}
    return build_report(workflows, findings, required_info,
                        {"state": "not measured", "reason": "offline scan"},
                        default_branch=args.default_branch)


def run_report(args, gh=None):
    gh = gh or GitHub()
    repo = args.repo or origin_repo()
    if not repo:
        raise SystemExit("repo-optimize-ci: origin is not a GitHub remote; pass --repo OWNER/REPO "
                         "(GitHub Actions only)")
    try:
        meta = gh.api(f"repos/{repo}") or {}
        branch = args.default_branch or meta.get("default_branch") or "main"
    except ApiError:
        branch = args.default_branch or "main"
    if args.remote:
        try:
            workflows, root_files = load_remote(gh, repo, branch)
        except ApiError as exc:
            if exc.status == 404:
                workflows, root_files = [], set()
            else:
                raise SystemExit(f"repo-optimize-ci: could not check {repo}: {exc}")
    else:
        workflows, root_files = load_local(args.root), local_root_files(args.root)
    req = required_checks(gh, repo, branch)
    required = None if req["state"] == "unknown" else req["contexts"]
    findings = analyze(workflows, root_files, required, branch, partial=bool(req.get("partial")))
    history_info = {"state": "not measured", "reason": "--no-history"}
    if not args.no_history and workflows:
        try:
            hist = gather_history(gh, repo, args.runs, args.days)
            if hist["runs"]:
                estimate(findings, hist)
                history_info = {"state": "measured", "since": hist["since"], "runs": len(hist["runs"]),
                                "prSampled": hist["prSampled"], "prSeen": hist["prSeen"],
                                "perWorkflow": hist["perWorkflow"]}
            else:
                history_info = {"state": "not measured", "reason": f"no completed runs since {hist['since']}"}
        except ApiError as exc:
            history_info = {"state": "not measured",
                            "reason": f"Actions history unreadable (HTTP {exc.status or '?'})"}
    return build_report(workflows, findings, req, history_info, repo=repo, default_branch=branch)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    scan = sub.add_parser("scan", help="offline analysis of a local checkout")
    scan.add_argument("--root", default=".")
    scan.add_argument("--required", action="append", metavar="CONTEXT",
                      help="a required status-check context (repeatable)")
    scan.add_argument("--required-unknown", action="store_true",
                      help="required checks could not be read — recommend only job-level filters")
    scan.add_argument("--default-branch", default="main")
    scan.add_argument("--json", action="store_true")
    rep = sub.add_parser("report", help="scan + required checks + run-history evidence (read-only)")
    rep.add_argument("--repo", help="OWNER/REPO (default: derived from origin)")
    rep.add_argument("--root", default=".")
    rep.add_argument("--remote", action="store_true",
                     help="read workflows via the contents API instead of a local checkout")
    rep.add_argument("--default-branch")
    rep.add_argument("--runs", type=int, default=100, help="max recent runs to sample")
    rep.add_argument("--days", type=int, default=30, help="history window in days")
    rep.add_argument("--no-history", action="store_true")
    rep.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = run_scan(args) if args.command == "scan" else run_report(args)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        sys.stdout.write(render_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
