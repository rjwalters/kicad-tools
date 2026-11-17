# Citation Scope (proposer roles)

The rule Hermit and Architect apply before filing any proposal (#7659). Both
role prompts carry only a short pointer here — the full text lives in this
reference doc so the prompts stay inside the markdown token budget (#7725).

The repo under review is `$LOOM_WORKSPACE` (= `$PWD`) — every cited path, line
number, and code-state claim in a proposal must hold on **that repo's**
`origin/main`. Sibling or source repos named in the target repo's own docs
(e.g., a CLAUDE.md saying "copy the `verification/` layout from
`some-org/sibling-repo`") are read-only context for understanding intent —
they are **never a citation target**. Never cite a path, line, or file from a
sibling repo as if it exists in this repo.

If a target repo's docs say a file "will be ported from" a sibling and that
file does not exist yet in this repo, a proposal about it must be phrased as a
follow-up to the port issue ("when X is ported, do not carry over Y") — never
as a removal from this repo, since there is nothing here yet to remove.
