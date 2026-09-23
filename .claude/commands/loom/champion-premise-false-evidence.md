# Champion: premise-false evidence rules (#8593)

Reference for the two places `champion-issue-promo.md` decides whether a
proposal's central factual claim is false: **"The `premise-false` finding
kind"** (writing the finding) and **"Premise-false close gate"** (re-running
it on the N=2 pass, the one place Champion may close an issue). The rules
themselves are stated there; this file carries the resolver and the evidence
table they point at.

## What went wrong (#8494)

On 2026-09-21 the close gate closed a sound proposal `not planned`. Both of
its recurring findings were "verified via" a `grep` of a `.loom/docs/*.md`
path against `origin/main`, each returning no output — and both premises were
**true** at that commit. Since #7842 every `.loom/docs/*.md` with a
`defaults/docs/` counterpart is a **symlink**: its tree entry is mode
`120000`, so reading it *through git* (`git show origin/main:<path>`,
`git cat-file -p`, `git archive`) yields the **link-target string**
(`../../defaults/docs/x.md`), never the document.

Every `grep`/`wc` over that read comes back empty *whatever the document
says*. The gate's own escape hatch — "if any re-run check now shows the
premise IS true, go to Step 1" — could not fire either, because the N=2 re-run
was told to re-run the cited check *verbatim*, so it reproduced the identical
false negative.

## Resolve a cited path before checking it

```bash
resolve_main_path() {   # the blob git will actually read, bounded to 3 hops
  local p="$1" i
  for i in 1 2 3; do
    [ "$(git ls-tree origin/main -- "$p" | awk '{print $1}')" = 120000 ] || break
    p="$(dirname "$p")/$(git show "origin/main:$p")"   # relative to the LINK's dir
    # collapse "." / ".." lexically — the target is a path IN A COMMIT, so
    # realpath/readlink (which stat the working tree) are both wrong here
    p="$(printf '%s' "$p" | awk -F/ '{n=0;for(i=1;i<=NF;i++){if($i=="."||$i=="")continue;
      if($i==".."){if(n>0)n--;continue}a[++n]=$i}s=a[1];for(i=2;i<=n;i++)s=s"/"a[i];print s}')"
  done
  printf '%s\n' "$p"
}

P=$(resolve_main_path ".loom/docs/transcript-token-ingest.md")
# → defaults/docs/transcript-token-ingest.md
git show "origin/main:$P" | grep -c '30-day fuse'   # 3, not (no output)
```

`defaults/scripts/tests/test-champion-premise-false-close.sh` extracts this
exact snippet from this file and runs it against a fixture repo containing a
real symlink, so the recipe and the test cannot drift apart.

## Positive evidence vs. inconclusive

An empty result on a path that **exists** on `main` proves nothing: it is
indistinguishable from a check that could not read the file at all (the
symlink case above, a typo'd argument, a tool error). Write every content
check in a **counting** form so a false premise produces a *non-empty* output,
and tag `[premise-false]` only on the left column.

| Claim | Positive evidence — premise IS false | Inconclusive — do NOT tag |
|---|---|---|
| path exists | `git ls-tree -r origin/main --name-only \| grep -c -Fx '<path>'` → `0` | a command that errored; a `grep -Fx` that merely printed nothing |
| string appears | `git show origin/main:<resolved> \| grep -c '<string>'` → `0`, on a `100644`/`100755` blob | empty output; any read of a `120000` entry |
| line range | `git show origin/main:<resolved> \| wc -l` → below the cited range, on a regular blob | empty output; an unresolved symlink read (always `0`) |
| tracked count | `git ls-files -- '<pattern>' \| wc -l` → `0` | any non-zero count |

**A count mismatch is premise-false only when the correct count is zero**
(#8313). "26 files lack the marker" against a re-count of 38 is an
*undercount of a still-true problem* — the premise holds and is worse than
stated, which is ordinary imprecision.

## Why this direction of caution

The close gate is the single place Champion may close an issue with no
operator in the loop, so its two error directions are not symmetric: an
operator reading an escalation about an unreadable check costs one comment,
while a close taken from a check that *cannot match* silently destroys sound
work — which is what #8494 cost. Hence: close only on positive evidence,
escalate on anything inconclusive, and re-evaluate (Step 1) the moment a
re-run shows the premise is now true.
