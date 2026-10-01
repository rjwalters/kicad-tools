---
name: "tidy"
description: "Tidy up the repository — build artifacts, caches, temp files, empty dirs"
domain: repo
type: command
user-invocable: true
---

# /repo:tidy — Tidy Up

Sweep the working tree for clutter and clean it up. Inventory everything,
categorize by confidence, then by default delete the SAFE category (pure junk
that holds no unique work) and report what was freed. Regenerable **caches**
(compilation/tool/build output) are kept by default and only cleared when you
pass `--caches` — deleting them is harmless but forces a costly rebuild, so it
is opt-in. Items that could be real work (ASK) are always presented for a human
call, never auto-deleted.

## Usage

```
/repo:tidy                    # Inventory, delete SAFE junk (caches kept), report; ASK items presented
/repo:tidy --caches           # Also clear regenerable caches (__pycache__/, dist/, .mypy_cache/, …)
/repo:tidy --ask              # Walk every category interactively before deleting anything
/repo:tidy --sizes            # Also measure worktree root sizes (slow: du has no prune)
/repo:tidy --fast             # Skip the reference scans entirely; unscanned candidates land in ASK, not SAFE
/repo:tidy --deep             # One reference scan per candidate instead of one batched pass (audit only)
/repo:tidy packages/core      # Scope to one subtree
```

(`--apply` is accepted as a synonym for the default, for muscle memory. `--caches`
composes with `--ask`: `--ask` still walks every category, `--caches` just moves
the cache tier into the auto-delete set for the non-interactive default.
`--sizes` only adds the per-worktree size column described in step 1 — it never
changes what is deleted.)

**`--fast` / `--deep` are the two ends of the reference-scan cost dial, and
neither is the default.** The default is **one batched reference pass** over the
whole candidate set, described in step 2 — that is what every run does unless
one of these flags is passed:

- **`--fast`** skips the reference scans entirely. Candidates that would have
  been scanned are still inventoried and reported, but they are routed to
  **ASK** rather than SAFE/CACHE, because a check that was not run is not a
  check that passed (safety rule 11).
- **`--deep`** restores the pre-batching behaviour: one scan per candidate.
  It exists to **audit** the batched pass, not to get a different answer —
  batching is a traversal optimization, so if `--deep` and the default ever
  disagree, that is a bug in the batched pass and not a feature of `--deep`.
- They are mutually exclusive. If both are passed, `--fast` wins: skipping is
  both the cheaper option and the one that cannot delete more than the default
  would.

## Steps

### 1. Inventory

Gather candidates without deleting anything:

```bash
# Ignored files that exist on disk (usually build output/caches).
#
# STREAM this and bucket as you go — never collect the raw output. Every line
# whose path is at or under one of the CACHE-tier directory names below folds
# into a SINGLE entry for that directory; the per-file lines are discarded
# without being categorized individually. On a repo whose multi-gigabyte
# regenerable tree is ignored file-by-file (rather than by a directory-level
# rule git can collapse), that is the difference between one CACHE line and
# hundreds of thousands of them. Bound it like the `--sizes` du below: a walk
# that exceeds the budget must degrade the ignored-file inventory to partial
# (say so in the report) rather than stall the whole step.
timeout 120 git clean -ndX   # bucket-as-you-stream; see the CACHE-tree note below

# The CACHE-tier trees, at DIRECTORY granularity — the cheap path that makes the
# bucketing above complete without anyone enumerating their contents. `-prune`
# stops the walk AT the match, so a 3 GB dist/ costs one stat, not a descent.
find . \( -name .git -o -name node_modules -o -name .venv \) -prune \
     -o -type d \( -name dist -o -name .turbo -o -name .astro \
                   -o -name __pycache__ -o -name .pytest_cache \
                   -o -name .mypy_cache -o -name .ruff_cache \
                   -o -name htmlcov \) -prune -print

# Untracked files (may include work-in-progress — treat carefully)
git clean -nd

# Empty directories
find . \( -name .git -o -name node_modules -o -name target \
          -o -name dist -o -name .venv \) -prune \
     -o -type d -empty -print

# Large files in the working tree (>10 MB, tracked or not)
find . \( -name .git -o -name node_modules -o -name target \
          -o -name dist -o -name .venv \) -prune \
     -o -type f -size +10M -print

# Orphaned environment content — packages still on disk that the current
# lockfile no longer references. Detect the lockfile and its package manager;
# the prune itself is an ASK item (see Categorize), never run from here.
[ -f pnpm-lock.yaml ]     && echo "prunable: pnpm"   # pnpm prune
[ -f package-lock.json ]  && echo "prunable: npm"    # npm prune

# Git worktree roots — authoritative and tool-agnostic. RETAIN this output as a
# path set for step 2's denylist; do not just print it. `--porcelain` gives one
# `worktree <path>` line per entry, including worktrees that live outside this
# repo root (`../repo-wt-fix123`, `/private/tmp/…`, `/private/var/folders/…`),
# which `git clean` never sees.
git worktree list --porcelain

# The worktree paths alone, for step 3's WORKTREES block. Extract them with
# `sed`, not `awk '{print $2}'`: awk splits on whitespace and would truncate
# `/repos/my checkout/wt` to `/repos/my`. The `worktree ` prefix is fixed, so
# stripping it with sed and reading whole lines keeps paths with spaces intact
# (paths containing newlines need `git worktree list --porcelain -z`).
git worktree list --porcelain | sed -n 's/^worktree //p'

# Worktree SIZES ARE NOT COLLECTED BY DEFAULT — only when `--sizes` is passed.
# See below for why. When it is passed, bound each root so a slow one degrades
# the size column instead of stalling the step:
git worktree list --porcelain | sed -n 's/^worktree //p' \
  | while IFS= read -r wt; do
      timeout 20 du -sh "$wt" 2>/dev/null \
        || printf '%s\t%s\n' 'size unavailable' "$wt"
    done
```

**The CACHE-tier trees are excluded from the ignored-file listing, because the
directory-level `find` above already reports them.** `git clean -ndX` is the
expensive half of this step: it visits every ignored tree, and a multi-gigabyte
regenerable tree that is ignored file-by-file (rather than by a directory-level
rule git can collapse to one line) is enumerated in full — the walk that blew
the five-minute command timeout in the reported case. The seven CACHE-tier trees
step 2 names — `dist`, `.turbo`, `.astro`, `__pycache__`, `.pytest_cache`,
`.mypy_cache`, `.ruff_cache` (plus `htmlcov`) — are the ones worth excluding,
because they are re-entered for no new information: the CACHE tier already
treats each of them as a **whole directory** in both the report
(`dist/ (gitignored, 380 MB)`) and the apply step (`git clean -fdX -- dist/`),
so their contents never appear individually in any output. The exclusion
therefore changes **how** a CACHE tree is discovered, never **whether** it is
reported or what tier it lands in — the same invariant the prune list below
states for the `find` walks.

The mechanism is **bucket-as-you-stream plus the directory-level `find`**, and
it is that way because the obvious alternatives are verified not to work:

- **Pathspec exclude magic does not apply.** `git clean -ndX -- .
  ':(exclude)dist'` (and `':!dist/'`, and `':(exclude,glob)**/dist/**'`) still
  prints `Would remove dist/` — checked on git 2.55.0. `git clean` collapses a
  wholly-ignored directory to a single entry and matches it by leading
  directory, which exclude magic does not suppress. Do not "fix" this step by
  adding one; it reads as an exclusion and silently is not one.
- **`-e <pattern>` is the wrong direction.** `git clean -e` *adds* a pattern to
  the ignore rules in effect, so under `-X` it makes **more** paths eligible for
  removal, not fewer.

So: pipe `git clean -ndX` instead of collecting it, fold every line at or under
a CACHE-tier directory name into one entry for that directory, and take the
authoritative CACHE list from the pruned directory-level `find` — which costs
one `stat` per tree instead of a descent. Bound the `git clean` walk with
`timeout` the way `--sizes` bounds `du`, and if it trips, report the
ignored-file inventory as **partial** in the report rather than silently
dropping it or stalling the step.

The `find` walks `-prune` the heavy trees rather than filtering them out with
`-not -path`. `-not -path` only suppresses *printing* — `find` still descends
into `.git/`, `node_modules/`, and every other excluded directory, which is why
the inventory stalls on a repo with a multi-GB build tree. Pruning is a
**traversal optimization only and must never change what is reported**: junk
outside the pruned trees (an empty `build/`, an 11 MB file under `src/`) is
still listed exactly as before. When editing the prune list:

- **Keep the empty-directory and large-file walks' lists identical.** If they
  drift, one command silently reintroduces the stall. The CACHE-tier walk is
  deliberately **not** in that pair: it must *match* `dist/` and the other cache
  dirs to report them, so it prunes only `.git`, `node_modules`, and `.venv` and
  then prunes at each match instead.
- **Draw entries from the denylist and CACHE categories already named in step 2**
  (`node_modules/`, `.venv/`, `dist/`, plus `target/` for Rust builds) instead
  of growing a second, inconsistent list.
- **Match by `-name`, not `-path`.** `-name` prunes at any depth, so nested
  copies like `packages/foo/node_modules/` and a vendored `vendor/foo/.git/`
  are covered — the old `-not -path './node_modules/*'` only ever matched the
  top-level one.
- **Coordination roots (`.loom/`, `.anvil/`, `.wrangler/`) are deliberately not
  pruned.** They are small, and step 2 needs to see their empty directories in
  order to route them to ASK.

**Worktree sizes are opt-in (`--sizes`); the default inventory reports count and
paths only.** `du` has no `-prune`: sizing a worktree root re-enters the very
`node_modules/`, `target/`, `dist/`, and `.venv/` trees the two `find` walks
above were rewritten to skip, once per root — and the reported case that
motivated all of this is 66 GB of worktrees inside a 94 GB tree, so sizing the
roots is most of a `du` over the whole repo. The cost is bounded by inode count
under those roots, not by the number of worktrees, so "there are only a handful
of them" is not a bound at all. That makes an eager `du` the same unbounded walk
that blew a 120-second timeout and stalled this step before.

Making it a flag is the same call the command already makes for `--caches`:
work whose *result* is useful but whose *cost* is high is presented, not
performed, until asked for. It also keeps the fix for the failure this block
exists to prevent — a report of "nothing to tidy" on a tree with tens of
gigabytes in worktrees — because the count and the paths are what carry that
signal, and they are free (`git worktree list` reads
`.git/worktrees/`, it does not walk the trees). The size column is the
refinement, not the point.

**Expected cost of this step, and where the reference scans fit.** The three
pieces that scale with repo size, in the order they bite:

| Piece | Cost | Bound |
|---|---|---|
| `git clean -ndX` | one pass over every ignored tree | `timeout`, minus the CACHE-tier trees excluded above; degrades to a partial ignored-file inventory |
| the `find` walks | one pass over the tree, pruned at the heavy roots | the prune list |
| step 2's reference scans | **one** batched pass over tracked files, whatever the candidate count | `--fast` skips them; `--deep` opts back into one pass per candidate |
| `du` over worktree roots | one unpruned pass per root | opt-in behind `--sizes`, `timeout 20` per root |

The reference scans are the piece that used to be unbounded in the *number of
candidates* rather than in repo size: written as "for each candidate, scan the
tree", six empty directories meant six full-tree scans (twelve, when each was
scanned for both its path and its basename) and blew a further two-minute
budget on top of the inventory. Step 2 now specifies **one** pass over the whole
candidate set instead, which is the same call `--sizes` and `--caches` already
make — bound the cost first, then offer the expensive shape as an explicit flag
(`--deep`) rather than as the default. `--fast` is the other end of the same
dial: skip the scans, and route what they would have checked to ASK.

When `--sizes` is passed, the sizes are **best-effort**: wrap each root in
`timeout 20` and print `size unavailable` for any root that exceeds it rather
than letting one enormous worktree hang the inventory. (`timeout` is GNU
coreutils — on macOS it is `gtimeout` from `brew install coreutils`. If neither
is available, report sizes as unavailable rather than running the walk
unbounded.) Do not prune inside the `du`: an "excluding regenerable trees"
number would understate exactly the footprint the operator is looking for. The
honest choices are a bounded full number or none.

The **first** `worktree` entry is the **main** working tree — always, regardless
of where the command runs from. It is *not* necessarily
`git rev-parse --show-toplevel`: from inside a linked worktree (which is where
Loom agents run, e.g. `.loom/worktrees/issue-42`), `--show-toplevel` reports the
linked worktree's own root while the first porcelain entry is still the main
repo. Do not treat the two as the same path.

What to drop from the list is **the tree being tidied** — the entry whose path
equals `git rev-parse --show-toplevel` — since that is the repo this run is
sweeping, not a worktree it is protecting from itself. Every remaining entry,
including the main working tree when tidy is invoked from a linked worktree, is
a live checkout to report and protect.

`git worktree list --porcelain` is **authoritative**. A directory whose `.git`
is a **file** (not a directory) containing `gitdir: …/.git/worktrees/…` is also
a worktree root, and that check catches worktrees belonging to *other*
checkouts of the same project; but where the two disagree — e.g. a stale `.git`
pointer whose target is gone — trust `git worktree list --porcelain` and do not
let a speculative `.git`-file scan fail the step.

Also look for junk by pattern, wherever it lives:
- OS/editor droppings: `.DS_Store`, `Thumbs.db`, `*~`, `*.swp`, `.#*`
- Python: `__pycache__/`, `*.pyc`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`
- JS: `node_modules/` outside package roots, stale `dist/`, `.turbo/`, coverage output
- Logs and temp files: `*.log`, `*.tmp`, `tmp/` contents older than a week
- Merge/patch leftovers: `*.orig`, `*.rej`, `*.BACKUP.*`

### 2. Categorize

**Gitignored ≠ safe to delete.** `git clean -ndX` lists *every* gitignored file
on disk — including secrets (`.env`) and expensive-to-rebuild trees (`.venv/`),
which are gitignored *precisely because* they're precious and local. Do not
treat "gitignored" as a synonym for "regenerable." SAFE and CACHE are
**allowlists** of recognized clutter (SAFE = pure junk, auto-deleted; CACHE =
regenerable build output, kept unless `--caches`); a **never-delete denylist**
overrides both; everything else gitignored falls through to ASK.

Apply these tests in order — **denylist first, then the SAFE and CACHE
allowlists, then fall through to ASK**. For empty directories and for
CACHE-tier build-output directories, a **reference scan runs after the
allowlist match, as an additional net, not a replacement for it** — see the
SAFE empty-directory bullet and the CACHE bullet below; the denylist/allowlist
check still runs first and still wins:

**Never-delete denylist (always ASK, never SAFE or CACHE — checked first,
overrides everything below, regardless of gitignore status):**
- Secrets / credentials: `.env`, `.env.*` (but **not** `.env.example` /
  `.env.sample`, which are templates safe to keep), `*.pem`, `*.key`,
  `*.keystore`, `*.p12`, `*.pfx`, `id_rsa*`
- Expensive-to-rebuild environments: `.venv/`, `venv/`, `env/`, and
  `node_modules/` — reinstalling them costs time and network, so they are never
  auto-deleted and `--caches` does **not** reach them (they are environments, not
  caches). Surface them under ASK for an explicit human call.
- Tool scaffolding / coordination roots: runtime-state directories a tool
  expects to exist and manages itself — anything under `.loom/` (`locks/`,
  `worktrees/`, `sweep-run/`, `sweep-checkpoint/`, …), `.anvil/`, `.wrangler/`
  (e.g. `.wrangler/tmp/`), and the equivalent runtime dirs under any other
  tool's dot-directory. **Match by parent tool-directory prefix, not by an
  enumerated leaf list**, so coordination subdirectories added later are covered
  without editing this file. For these, **empty is the normal, healthy state** —
  an empty `.loom/locks/` means no lock is currently held, not that the
  directory is abandoned — so emptiness is never evidence of junk here (see the
  empty-directory rule under SAFE). (The cache dot-directories already named
  under CACHE — `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, `.turbo/`,
  `.astro/` — are **not** coordination roots: they are regenerable output and
  stay in CACHE, so `--caches` still clears them. The discriminator is that a
  coordination root is one where *empty is the normal state*; a cache directory
  is one whose entire contents can be rebuilt by re-running the tool.)
- Git worktree roots — **any** path listed by `git worktree list --porcelain`
  in step 1, **or** any directory whose `.git` is a **file** (not a directory)
  whose contents match `gitdir: .*/\.git/worktrees/.*`. A worktree root is a
  live checkout that can hold uncommitted, unpushed, one-of-a-kind work; it is
  never SAFE and never CACHE, regardless of gitignore status. This test is
  **tool-agnostic and independent of the tool-scaffolding prefixes above** — it
  is what covers `.claude/worktrees/`, `.codex/worktrees/`, and any other
  tool's worktree cache dir that is not named in the `.loom/` / `.anvil/` /
  `.wrangler/` list, as well as ad hoc worktrees under no dot-directory at all
  (`git worktree add ../repo-wt-fix123`, `/private/tmp/…`,
  `/private/var/folders/…`). Do not rely on the prefix match to reach these.
  Emptiness never promotes a worktree root to SAFE either (safety rule 8) — a
  checked-out worktree is not legitimately empty, and a parent directory that
  *contains* worktrees still routes through the existing empty-directory rule.
  A nested worktree shows up in `git clean -ndX` as `Would skip repository
  <path>` — plain `git clean -fdX` leaves it alone, but `-ffdX` (double force)
  deletes it outright, so never widen the force flag to make a listing "go
  away". Report these (with their size when `--sizes` is passed — see below);
  deciding which worktrees are *reclaimable* needs merge state and agent
  liveness that `/repo:tidy` does not have, so it stays a report, never a
  deletion.
- Anything else that looks credential-like or holds unique local state
  (local SQLite DBs, local-only config, sample-data caches)

A denylist match routes to **ASK** (never auto-deleted) — not KEEP, which is
reserved for tracked files.

- **SAFE** — pure junk, regenerable with certainty and holding no unique work,
  matched by an explicit **allowlist** (never "everything `git clean -ndX` lists
  minus a couple of exclusions"). Auto-deleted by default. A file is SAFE only if
  it does **not** match the denylist above **and** matches one of:
  - OS/editor droppings: `.DS_Store`, `Thumbs.db`, `*~`, `*.swp`, `.#*`
  - Merge/patch leftovers: `*.orig`, `*.rej`, `*.BACKUP.*`
  - Empty directories **whose path matches no never-delete denylist entry** —
    the denylist is checked first here exactly as it is for the file patterns
    above. An empty directory under a tool-scaffolding / coordination root
    (`.loom/`, `.anvil/`, `.wrangler/`, …) routes to **ASK**, not SAFE:
    emptiness is that tool's normal operating state, not evidence of junk. Every
    other empty directory (an empty `build/`, say) is SAFE. Note that the
    `find . -type d -empty` inventory in step 1 is a raw path scan — it consults
    neither gitignore nor the denylist — so this check must be applied to its
    output before anything is deleted.

    **Reference scan (additional net, after the denylist check, not instead of
    it) — ONE batched pass over every candidate, never one scan per directory.**
    The denylist above is an enumerated/prefix-matched allowlist of known
    tools (`.loom/`, `.anvil/`, `.wrangler/`, git worktree roots); it does not
    cover a tool that is not on that list — a custom app's own state/spool dir,
    or a future tool's coordination root not yet added here. So every empty
    directory that clears the denylist check gets cross-referenced against
    tracked files before SAFE is final. **Collect the candidates first, then
    scan once** — the scan is one pass over the tree whatever the candidate
    count, not a pass per candidate:

    1. **Collect.** Build the candidate list: every empty directory from step
       1 that cleared the denylist check. Nothing is scanned yet.
    2. **Short-circuit on empty.** If the candidate list is empty, the scan is
       a **no-op** — skip it entirely. Never invoke the scan with an empty
       pattern file: an empty `-f` file is not portably "matches nothing", and
       an empty pattern is "matches everything", so either shape would report
       every candidate as referenced or error out.
    3. **Build one pattern file.** One fixed string per line: each candidate's
       repo-relative path, plus its bare basename when the path is generic
       enough that a config is likely to name only the leaf. This is the same
       pair of patterns the per-directory form used to search for; it is now
       two *lines*, not two *scans*.
    4. **Scan once.**
       `git grep -I -n -F -f "$PATTERNS" -- .` — a single pass over tracked
       files for all candidates at once. `-F` keeps every pattern a fixed
       string (a path with `.` in it must not be read as a regex), and `-n`
       gives `path:line:text` so the next step can attribute hits.
    5. **Attribute.** For each hit line, the candidate is whichever pattern the
       matched text contains; count distinct referencing files per candidate.
       A candidate with at least one hit is demoted; a candidate with none is
       not. If the candidate set is large enough that one pattern file is
       unwieldy, **chunk it** — the bound is one pass per chunk, never one pass
       per candidate.

    **Batching is a traversal optimization only and must never change what is
    reported** — the same invariant step 1's prune list states for the `find`
    walks. Any hit — a script, config, or source file that names the directory —
    demotes it from SAFE to ASK, reported with its reason (e.g. "referenced by N
    files"), regardless of whether the directory matched a named
    tool-scaffolding prefix, exactly as before. A directory with no reference
    hit and no denylist match remains SAFE. This scan never *promotes* anything
    the denylist already routed to ASK — it only ever demotes a would-be SAFE
    empty directory, and only when the allowlist match already let it through.

    `--deep` runs the pre-batching shape instead: one scan per candidate, for
    both its path and its basename. It is an **audit** of the batched pass, and
    the two must agree on every candidate; a disagreement is a bug in the
    batched pass. `--fast` skips the scan entirely, in which case every
    denylist-cleared empty directory is reported under **ASK** with the reason
    `reference scan skipped (--fast)` — never auto-deleted as SAFE on the
    strength of a check that did not run (safety rule 11).

  Nothing in this category may be tracked by git or match a source-code
  extension.
- **CACHE** — regenerable compilation/tool/build output. Same certainty as SAFE
  (definitely regenerable, no unique work), but deleting it forces a potentially
  slow rebuild, so it is **kept by default** and cleared **only** when `--caches`
  is passed (see Apply). A file is CACHE if it does **not** match the denylist
  and matches one of:
  - Python caches: `__pycache__/`, `*.pyc`, `.pytest_cache/`, `.mypy_cache/`,
    `.ruff_cache/`
  - Build output: stale `dist/`, `.turbo/`, `.astro/`, `htmlcov/`, `.coverage`,
    coverage output, `site/dist`

  **Reference scan (additional net, after the allowlist match, not instead of
  it) — the same mechanism as the empty-directory reference scan above,
  applied to a different question.** "Regenerable" and "harmless to delete
  right now" are different properties: a `dist/` can be truly rebuildable by
  re-running the tool and still be the exact bytes a live, registered process
  is reading from disk this second — clearing it would not lose source, but it
  would break that process until the next build. Every path that clears the
  CACHE allowlist match is cross-referenced against registered MCP server
  configs before CACHE is final — and, like the empty-directory scan,
  **collect the candidates first, then read each config source exactly once
  per run, never once per candidate.** The four sources below are a handful of
  small JSON files: read them once into a single set of referenced paths, then
  match every CACHE candidate against that in-memory set. The sources are:

  1. `.mcp.json` in the repo root, if present —
     `jq -r '.mcpServers[]?.args[]?' .mcp.json`
  2. `~/.claude.json` `.mcpServers[]?.args[]?` (top-level, user-scope servers)
  3. `~/.claude.json` `.projects["<repo-root-abs-path>"].mcpServers[]?.args[]?`
     (project-scoped servers) — resolve `<repo-root-abs-path>` the same way
     this command already resolves the repo root elsewhere (step 1's `git
     rev-parse --show-toplevel`), never hardcoded
  4. Best-effort: any installed tool's tracked `install-metadata.json` whose
     `installed_files` (or equivalent) names a path under the candidate
     directory

  This is a **path-prefix** match, not an exact one — a candidate directory
  demotes when any scanned path starts with it (`mcp-loom/dist/` demotes on a
  hit for `mcp-loom/dist/index.js`). A hit on any source demotes the path from
  CACHE to **ASK**, reported with the reason, mirroring the empty-directory
  wording: `← live MCP bundle (referenced by <config-path>)`. Like the
  empty-directory scan, this is purely a **demotion** — it never promotes
  anything the denylist already routed to ASK, and a CACHE entry with no
  reference hit stays CACHE exactly as before, so `--caches` still clears
  every regenerable dir that nothing is currently loading from.

  The same cost dial applies here as to the empty-directory scan, with the same
  safety asymmetry. `--deep` re-reads the config sources per candidate (an audit
  of the batched read — the two must agree). `--fast` skips the scan, and every
  unscanned CACHE candidate is reported under **ASK** with the reason
  `reference scan skipped (--fast)` instead of being cleared by `--caches`:
  under `--fast`, `--caches` clears strictly less than it would by default,
  never more (safety rule 11).

  Like SAFE, nothing here may be tracked by git or match a source-code extension.
  (`node_modules/` and virtualenvs are **not** CACHE — they are denylisted
  environments and stay in ASK even with `--caches`.)
- **ASK** — probably junk but needs a human call. This covers:
  - Untracked files that aren't gitignored (could be unsaved work!), large
    files, stale-looking logs, old `tmp/` contents.
  - **Any gitignored file that matches the never-delete denylist** (secrets,
    virtualenvs, tool-scaffolding roots) — surfaced here, never auto-deleted.
  - **Any empty directory whose path matches the denylist** (a `.loom/`,
    `.anvil/`, or `.wrangler/` coordination or runtime-state dir) — emptiness
    is that tool's normal state, so it lands here rather than in SAFE.
  - **Any empty directory demoted by the reference scan** (see the SAFE
    empty-directory bullet above) — a tracked file references its path even
    though it matched no denylist entry. Report it with the reason, e.g.
    "referenced by N files", rather than silently skipping it.
  - **Any CACHE-tier directory demoted by the MCP-config reference scan**
    (see the CACHE bullet above) — its path is referenced by `.mcp.json`,
    `~/.claude.json` `mcpServers`, or an installed tool's
    `install-metadata.json`, so a registered MCP server loads from it right
    now. Report it with the reason, e.g. "referenced by ~/.claude.json"; it is
    regenerable but not currently harmless to delete, so `--caches` must not
    reach it.
  - **Any candidate whose reference scan was skipped because `--fast` was
    passed** — an empty directory that cleared the denylist, or a CACHE-tier
    directory that cleared the allowlist, but that was never cross-referenced.
    Report it with the reason `reference scan skipped (--fast)`. A check that
    was not run is not a check that passed, so these land here rather than in
    SAFE or CACHE (safety rule 11).
  - **Any git worktree root** detected in step 1 — surfaced on its own
    `worktree:` inventory line (see Report), never auto-deleted, whether or not
    it is gitignored and whether or not it sits under a recognized tool
    dot-directory.
  - **Any gitignored file that does not match the SAFE or CACHE allowlist** (a
    novel/unrecognized cache dir, unrecognized local state) — when in doubt, it
    lands here, not in SAFE or CACHE.
  - **An orphaned-environment prune offer**, when step 1 detected a lockfile
    and its package manager. This is the one ASK entry that is a **verb, not a
    path** — see below.

  **The prune offer (ASK-tier, and why).** Dependency churn leaves packages in
  `node_modules/` that the current lockfile no longer references. After a round
  of major bumps, one real repo's `node_modules/` went from 60 MB to 106 MB
  because `node_modules/.pnpm` retained both the old and new TypeScript side by
  side — ~46 MB unreferenced by the lockfile. That is exactly the dead weight
  from dependency churn that tidy exists to catch, and until now there was no
  tier that could reach it: `node_modules/` is denylisted as an *environment*,
  so the only options were keep-the-whole-tree or delete-the-whole-tree.

  The prune is the missing middle. It removes **only** packages the lockfile
  does not reference, so it cannot break the working install and forces no
  reinstall of anything in use. It nonetheless lands in **ASK, not CACHE**:
  it mutates an environment in place, and ASK is the tier whose contract is
  *never automatic under any flag* (Apply, below). `--caches` deliberately does
  not reach it. This **adds a tier and relaxes nothing** — `node_modules/`
  full-tree deletion stays denylisted and ASK exactly as before.

  Two things not to overstate:

  - **pnpm is the real case; npm is usually a near-no-op.** `pnpm prune` clears
    the `.pnpm` link farm, where the duplication actually accumulates. `npm
    install` already reconciles extraneous packages as part of its own run, so
    `npm prune` typically reclaims little. Offer it, but do not present the two
    as equivalent wins. In a pnpm workspace the prune must be run recursively
    (`-r`) or it only touches the root package.
  - **A pre-run size estimate is optional.** Diffing `node_modules/.pnpm`
    entries against lockfile references to predict the reclaim is real parsing
    work on every run, for a number that can be wrong. Reporting the current
    `node_modules/` size and the **actual** bytes freed afterwards — what every
    other category effectively does — is enough. Compute the estimate only if
    it is cheap and reliable in the repo at hand; never block the offer on it.

  Deciding which worktrees, branches, and stashes are *stale* remains
  [[reset]]'s job — point there instead of pruning them here. `/repo:tidy`'s
  job with worktrees is only **visibility and protection**: report every root
  (and its size under `--sizes`) so the operator can see the footprint, and
  never delete one.
- **KEEP** — flagged only as information: tracked files that look like they
  don't belong. Nothing here is ever deleted — safety rule 1 is absolute — but
  "looks like it doesn't belong" covers two shapes whose remedies are
  **opposite**, so KEEP is reported as two named sub-cases and never as one
  collapsed list:
  - **KEEP (generated)** — a tracked file that looks like build output or some
    other regenerable artifact that got committed by accident
    (`assets/build.min.js`). It is inert: nothing reads it as source, it is
    just noise in the history and the diff. The remedy is to stop tracking it
    and ignore it going forward, which is [[gitignore]]'s job — point there.
    This is what KEEP has always meant, and this sub-case is unchanged.
  - **KEEP (name collision)** — a tracked file whose **trailing extension is a
    live source extension in this repo**, so every tool that walks the project
    by extension (compilers, EDA tools, linters, IDEs) opens it as real source.
    This one is not inert, and [[gitignore]] is the **wrong** pointer for it:
    the file is already tracked, already on disk, and already being parsed, so
    adding an ignore rule changes nothing about the harm. The only remedy is a
    deliberate `git rm` — see **The printed recipe** below.

    Detection is a **naming heuristic, not a content check**: no file is opened
    or parsed to make this call. It runs over **tracked files only**, since an
    untracked or gitignored file is another tier's problem:

    ```bash
    # The candidate set and the sibling set are both drawn from here.
    git ls-files
    ```

    A tracked file is a name collision when **all three** conditions hold:

    1. Its basename carries a backup/copy marker **in the stem — before the
       final `.`**: the substring `backup`, `copy`, or `orig`, matched
       **case-insensitively** (`Connectors_BACKUP_20260427.kicad_sch`,
       `schematic copy.kicad_sch`, `parser.orig.rs`).
    2. Its trailing extension has **real siblings**: at least one *other*
       tracked file ends in the same `.<ext>` and carries **no** such marker.
       That sibling test is what makes `<ext>` a live source extension *for
       this repo*. Never match against a hardcoded global extension list — a
       `.kicad_sch` collision matters only in a repo that has real `.kicad_sch`
       files, and in a repo with no such siblings the same filename is just a
       file with an unusual name. **An extension that itself carries a marker
       is never live**, however many files share it: `.backup-20260427_163100`
       and `.orig` are provenance suffixes, nothing parses them as source, and
       a `*.orig` merge leftover is the SAFE tier's leftover rule, not this
       one — that exclusion is what keeps the two rules from overlapping in a
       repo whose `*.rs.orig` leftovers would otherwise make `orig` look live.
    3. The marker reads as a **provenance stamp on an existing file**, not as
       the file's subject. Strip the **marker run** off the *end* of the stem
       — the marker word, the separator run (space, `_`, `-`, `.`) in front of
       it, and any timestamp or copy index (digits and separators) behind it —
       and what remains must be a non-empty stem that names a **base sibling**:
       another tracked file `<base>.<ext>`, same extension, **same directory**,
       matched **case-insensitively** (same rationale as condition 1 — this
       preserves real-world filesystem behavior on macOS/Windows, where
       `Connectors.kicad_sch` and `connectors.kicad_sch` name the same file).
       `connectors_backup_20260427_163100.kicad_sch` → `connectors.kicad_sch`,
       `schematic copy.kicad_sch` → `schematic.kicad_sch`, `sheet - Copy
       2.kicad_sch` → `sheet.kicad_sch`, `parser.orig.rs` → `parser.rs`. The
       base sibling is the file this one is a copy *of*; if it cannot be
       named, this is not a collision.

    Condition 1 is deliberately about the **stem**, and that is what keeps the
    inert shape out. `connectors_backup_20260427_163100.kicad_sch` collides:
    its trailing extension really is `.kicad_sch`, so KiCad loads it as a
    schematic sheet and its contents are counted a second time.
    `connectors.kicad_sch.backup-20260427_163100` does **not** collide: its
    trailing extension is `.backup-20260427_163100`, the marker *is* the
    extension rather than part of the stem, no other tracked file shares that
    extension, and no extension-walking tool will ever open it. Flagging the
    second shape alongside the first would bury the signal, which is the whole
    point of the sub-case: separate the backups that are actively being parsed
    from the ones that are harmlessly sitting there.

    Condition 3 is what tells a **stamp** from a **topic**, and without it
    conditions 1 and 2 flag ordinary source in any repo that merely discusses
    backups or copying. `src/backup.py` and `src/copy.py` strip to nothing.
    `copyright.py`, `BackupManager.ts`, `useCopyToClipboard.ts` and
    `deepcopy_helpers.py` have no marker *run* at all — the marker is glued
    into a longer word, with no separator in front of it and no
    separator-or-digits behind it. `copy_utils.ts` carries the marker at the
    **front**, with no base in front of it to be a copy of.
    `docs/backup-strategy.md` has both problems. None of these are backups of
    anything and none may appear in this sub-case: it prints `git rm`, and a
    pasted false positive here is the one recipe in `/repo:tidy` that costs a
    source file.

    The trade is deliberate — precision bought with recall. A genuine backup
    whose base file was since renamed or deleted, or that was moved into a
    `backups/` directory away from its original, has no base sibling and is
    **not** reported here even though a tool would still parse it. It is not
    lost: like the inert shapes, it stays in the **generated** sub-case with
    the [[gitignore]] pointer. Only the alarm and the `git rm` recipe are
    withheld, and they are withheld exactly when tidy cannot say truthfully
    what the file is a backup of — which is the same circumstance in which the
    `why:` line below could not be written honestly.

    **The printed recipe.** For each collision, print a literal,
    copy-pasteable `git rm <path>` line plus a one-line reason naming the tool
    class that parses the file and what that costs. The reason is only ever
    written from what conditions 2 and 3 established — the sibling count that
    made the extension live, and the base sibling the file is a copy of. **If
    that sentence cannot be written truthfully, the file is not a collision
    and must not be reported here**; never assert that a file is a backup of
    something tidy could not name. `/repo:tidy` **prints this string and
    nothing else** — it never runs `git rm`, never stages it, and never
    offers to run it, not under `--ask`, `--apply`, or any other flag.
    Removing a tracked file is a commit the user makes deliberately (safety
    rule 1); the recipe exists so that decision is one paste away instead of a
    research task, exactly as the [[gitignore]] pointer is for the generated
    sub-case.

  Print each sub-header **only when it has entries**. A repo with nothing
  colliding must not grow an empty `name collision` heading — an empty tier
  reads as a finding, and this one is meant to read as an alarm.

### 3. Report

```
## Repo Clean — inventory

SAFE (would free 32 MB — deleted by default):
  .DS_Store × 14
  3 *.orig merge leftovers
  6 empty directories

CACHE (would free 402 MB — kept by default; pass --caches to clear):
  __pycache__/ × 22 dirs
  .mypy_cache/ (gitignored, 22 MB)
  dist/ (gitignored, 380 MB)

ASK:
  .env                     gitignored, 1 KB  ← credentials, never auto-deleted
  .venv/                   gitignored, 240 MB  ← virtualenv, expensive to rebuild
  node_modules/            gitignored, 310 MB  ← environment, reinstall via npm; not a --caches target
  prune node_modules       pnpm-lock.yaml detected  ← run `pnpm prune` to drop lockfile-unreferenced packages
  mcp-loom/dist/           gitignored, 796K  ← live MCP bundle (referenced by ~/.claude.json); --caches will not clear it
  .loom/locks/             gitignored, empty  ← coordination root, empty is normal state
  .wrangler/tmp/           gitignored, empty  ← tool runtime dir, empty between builds
  notes-scratch.md         untracked, 3 KB, modified today  ← might be real work
  sim-output-old/          untracked, 1.2 GB, untouched 60 days

WORKTREES (4 roots — never auto-deleted, listed for visibility; --sizes to measure):
  worktree: .loom/worktrees/issue-42      ← live git worktree
  worktree: .claude/worktrees/scratch-wt  ← live git worktree
  worktree: ../repo-wt-fix123             ← live git worktree (outside repo root)
  worktree: /private/tmp/wt-bisect        ← live git worktree (outside repo root)
  Pruning stale worktrees is /repo:reset's call, not tidy's.

KEEP (informational) — tracked files, never deleted by tidy (safety rule 1):

  generated — committed build output; stop tracking it going forward:
    assets/build.min.js      tracked but looks generated — see /repo:gitignore

  name collision — tracked AND parsed as real source; gitignoring fixes nothing:
    connectors_backup_20260427_163100.kicad_sch
      why: backup of connectors.kicad_sch, and 14 real .kicad_sch files make
           that a live extension here — KiCad opens this backup as a schematic
           sheet too, so its contents are counted twice
      run deliberately (tidy will not run this for you):
        git rm connectors_backup_20260427_163100.kicad_sch
```

The two KEEP sub-blocks are formatted differently on purpose: the generated
sub-case is a one-line pointer at another command, while the name-collision
sub-case gets its own indented `why:` line and a `git rm` recipe on a line of
its own, because the operator has to read the reason before running it. Print
whichever sub-blocks have entries and omit the others entirely — on the common
repo where nothing collides, the output is the single `generated` block and is
byte-for-byte what it has always been. The `git rm` line above is **report
text**: it is printed for the operator to run, never executed by `/repo:tidy`.

With `--sizes`, the same block gains a right-aligned size column and a total
(`size unavailable` for any root that hit the `timeout`):

```
WORKTREES (66 GB across 4 roots — never auto-deleted, listed for visibility):
  worktree: .loom/worktrees/issue-42       32 GB  ← live git worktree
  worktree: .claude/worktrees/scratch-wt  3.1 GB  ← live git worktree
  worktree: ../repo-wt-fix123              12 GB  ← live git worktree (outside repo root)
  worktree: /private/tmp/wt-bisect         19 GB  ← live git worktree (outside repo root)
  Pruning stale worktrees is /repo:reset's call, not tidy's.
```

With `--fast`, the tiers are the same shape but the SAFE and CACHE tiers shrink
and ASK grows by exactly the candidates whose reference scan did not run, each
carrying the skip as its reason:

```
ASK:
  build/                   empty  ← reference scan skipped (--fast)
  packages/ui/dist/        gitignored, 88 MB  ← reference scan skipped (--fast)
```

Print that reason literally rather than folding these into a generic "needs a
human call" line: the operator has to be able to tell a candidate tidy *checked
and could not clear* from one it *never checked*, and the remedy for the second
is simply to re-run without `--fast`.

The `WORKTREES` block is a **distinct inventory section**, not folded into the
generic denylist ASK lines, and under `--sizes` its bytes are summed
**separately** from the SAFE/CACHE/ASK totals (a worktree's contents are neither
freed nor freeable by this command); roots whose size is unavailable are
excluded from the total and the total is marked as a lower bound. Print the
block whenever at least one worktree root exists — including on an
otherwise-clean run and including without `--sizes`, so `/repo:tidy` never
reports "nothing to tidy" on a tree where tens of gigabytes are sitting in
worktrees with no hint of where the space went. The count and the paths are what
carry that signal; the sizes only quantify it.

If the repo documents its **own** worktree-management tooling — a script,
`package.json` command, `Makefile` target, or binary the repo's own docs point
at for this purpose — mention it after the block as a soft pointer (e.g.
`Reclaimable worktrees: try 'loom-daemon clean --safe --dry-run'.`). Only
whichever the repo actually documents; `/repo:tidy` has no reliable way to
discover arbitrary third-party tooling, so **omit the line entirely rather than
guessing** at a command that may not exist.

### 4. Apply

- Default: delete the SAFE category immediately, report the CACHE tier as kept
  (with the bytes `--caches` would free), then present ASK items for a decision.
  Never auto-delete anything in ASK, no matter the flags.
- With `--caches`: the CACHE tier joins the auto-delete set — delete SAFE **and**
  CACHE immediately, then present ASK. `--caches` never widens what counts as
  deletable beyond the CACHE allowlist; denylisted paths (secrets, virtualenvs,
  `node_modules/`) stay in ASK regardless.
- With `--ask`: walk through every category with the user, including SAFE and
  CACHE; delete only what they approve. (`--ask` already surfaces caches for a
  decision, so `--caches` is redundant with it — the flag only affects the
  non-interactive default.)
- With `--fast`: the tiers are unchanged, but the reference scans in step 2 do
  not run, so every candidate that would have been scanned sits in ASK with the
  reason `reference scan skipped (--fast)`. The auto-delete set is therefore a
  **subset** of what the default run would delete — `--fast` can only ever
  delete less, never more or something different. `--deep` is the reverse trade
  (same set, more scans) and changes nothing about this step.
- **KEEP is never part of the apply step, under any flag.** Both sub-cases are
  tracked files, so both are report-only: `/repo:tidy` never runs `git rm`,
  never stages a tracked-file deletion, and never prompts to do either — not
  even for a name collision, and not even under `--ask`. The `git rm <path>`
  line in the report is a string printed for the operator, in exactly the same
  sense as the `see /repo:gitignore` pointer beside it. If a future change ever
  needs tidy to *perform* it, that is a different command with a different
  safety story, not a flag on this one.

**The prune offer, if accepted, runs the package manager's own verb and
nothing else** — `pnpm prune` (add `-r` in a workspace) or `npm prune`, from
the repo root. Never `git clean`, never `rm` inside `node_modules/`, and never
a hand-rolled walk of `.pnpm`: the whole safety argument for this tier is that
the package manager decides what is unreferenced, so bypassing it forfeits the
guarantee. Report the actual bytes freed by measuring `node_modules/` before
and after. If the prune command fails, report the failure and leave the tree
alone — a partially pruned `node_modules/` is the package manager's to
reconcile on the next install, not tidy's to repair.

The default auto-delete is scoped to **SAFE-allowlisted paths only** (plus the
CACHE allowlist when `--caches` is passed). Never pass a denylisted path
(secrets, virtualenvs, `node_modules/`, tool-scaffolding roots, git worktree
roots — **including when it is empty**) or an unrecognized gitignored path to
`git clean -fdX` — those are ASK items and require an explicit human call. Build
the explicit `<paths>` list from the SAFE category (and CACHE under `--caches`)
and nothing else; do **not** run a blanket `git clean -fdX` that would sweep
whatever `git clean -ndX` lists.

Use `git clean -fdX -- <paths>` for gitignored artifacts and plain `rm` only
for pattern-matched junk you listed in the report. After deleting, re-run the
inventory to confirm and report bytes freed.

## Safety Rules

1. **Never delete tracked files** — that's a git operation the user does deliberately
2. **Never touch `.git/`** internals
3. **Untracked ≠ junk** — an untracked file modified recently is presumed to be
   unsaved work and always lands in ASK
4. **Everything deleted must have appeared in the report first**
5. When scoped to a subtree, do not delete anything outside it
6. **Gitignored ≠ safe to delete** — the never-delete denylist (secrets like
   `.env`/`*.pem`/`*.key`, environments like `.venv/`/`venv/`/`env/` and
   `node_modules/`, tool-scaffolding roots like `.loom/`/`.anvil/`/
   `.wrangler/`, and git worktree roots) always overrides SAFE and CACHE and
   routes to ASK, regardless of what `git clean -ndX` lists. Unrecognized
   gitignored files fall through to ASK, never SAFE or CACHE.
7. **Caches are opt-in** — the CACHE tier (`__pycache__/`, `dist/`, `.mypy_cache/`,
   and the other compilation/tool/build patterns) is never auto-deleted by
   default; it is cleared only when `--caches` is passed (or approved item-by-item
   under `--ask`). Deleting a cache is safe but forces a rebuild, so the default
   keeps it.
8. **Empty ≠ abandoned** — for lock, coordination, and runtime-state
   directories, empty is the *normal operating state*, not clutter: an empty
   `.loom/locks/` means nothing is currently held, and `.loom/worktrees/`,
   `.loom/sweep-run/`, `.loom/sweep-checkpoint/`, or `.wrangler/tmp/` are empty
   whenever no run is in flight. Deleting them reads a tool working correctly as
   evidence of junk. So the ordering in rule 6 applies to **directories exactly
   as it does to files**: check the denylist before the empty-directory rule in
   SAFE. Emptiness never promotes a denylisted path into SAFE, and never
   bypasses the fall-through to ASK for unrecognized gitignored paths.
9. **Never delete a git worktree root** — a worktree is a live checkout that
   may hold uncommitted, unpushed work, and it is denylisted on the strength of
   `git worktree list --porcelain` (or a `.git` **file** pointing at
   `…/.git/worktrees/…`), *not* on where it happens to live. Do not infer
   protection from a dot-directory prefix: `.claude/worktrees/`, a sibling
   `../repo-wt-fix123`, and a worktree under `/private/tmp/` are all protected
   by this rule and none of them are matched by rule 6's prefix list. Report
   every root (see Report) even when nothing is deleted — the one thing worse
   than deleting a worktree is telling the operator their 94 GB tree is clean.
   Sizing those roots is a `du` with no `-prune`, so it is opt-in behind
   `--sizes` and bounded per root; the report itself never is.
10. **Prune only through the package manager** — the orphaned-environment
   offer runs `pnpm prune` / `npm prune` and nothing else. Never `rm` inside
   `node_modules/`, never `git clean` against it, never a hand-rolled walk of
   `node_modules/.pnpm`. The tier is defensible *because* the package manager
   decides what the lockfile no longer references; deciding that ourselves
   forfeits the only guarantee that makes it safe. And the offer is ASK-tier,
   so rule 7's `--caches` opt-in does not reach it and no flag makes it
   automatic — `node_modules/` full-tree deletion remains denylisted and ASK
   exactly as under rule 6.
11. **A skipped check never widens what is deleted** — `--fast` skips step 2's
   reference scans, so the candidates they would have cross-referenced are
   reported under ASK (`reference scan skipped (--fast)`), never auto-deleted as
   SAFE or cleared by `--caches`. A check that was not run is not a check that
   passed. The same asymmetry governs any future cost flag: making tidy cheaper
   may shrink the auto-delete set, never grow it. Its companion is the
   invariant on the other side of the dial — **batching a scan is a traversal
   optimization only and must never change what is reported** (step 1 states it
   for the `find` prune list, step 2 for the batched reference pass), which is
   why `--deep` exists as an audit of the default rather than as a second
   answer.
