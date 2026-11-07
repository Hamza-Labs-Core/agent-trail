---
description: Write the trail event for the current branch's pull request (or the PR number given)
argument-hint: "[PR number]"
---

Write the agent trail event for pull request $ARGUMENTS. If no number was given, use the open
pull request for the current branch (`gh pr view --json number,title,url`). If the branch has
no pull request yet, open it first: the event is named for its number.

1. Read `.trail.toml` (if any) for the events folder and `events.pr_digits`. The defaults are
   `docs/trail/events` and 4.
2. Gather what only this session knows: the wrong first guesses, the root cause, what a tool
   or the code really does, the numbers. Use `gh pr view <N>` and `git log` for real numbers
   and shas, and never guess one.
3. Write `<events>/<N zero-padded>-<slug>.md` in the format the `trail` skill shows:
   `schema: 1`, `kind: pr`, `pr: N`, `title`, `date`, `session` (this session's id), plus
   `agent: claude-code`, plus `prs:` or `issue:` when they apply. Write `## What happened` (required), then only the
   sections the work really produced: `## What we learned`, `## Keep doing`,
   `## Stop doing`, `## Open` (with issue numbers). An empty section is left out, never padded.
4. If the pull request already merged, the event goes in a new trail-only pull request. If an
   event for N already merged, write `<N>-after-merge.md` instead and don't edit the old one.
5. Run `trail check` and fix what it reports. Then commit the event to the pull request's
   branch and push.

Never write secrets, and never quote anyone's words: state each decision neutrally.
