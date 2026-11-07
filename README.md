# agent-trail

The agent trail is a repository's record of what coding-agent sessions learned while working
on it, so the next session starts from that record instead of from nothing. Any agent can
read and write it: Claude Code, Codex, Cursor, or any other. It isn't a changelog, and it isn't
a list of pull requests (git already has that). It holds what only the session saw: the
approach that failed, the root cause found after the wrong guesses, the rule that was
corrected partway through, the gotcha that cost an hour.

The trail has three parts, all kept in the repository:

- **Events** (`docs/trail/events/`): one Markdown file per piece of work, usually one per
  merged pull request. The session that did the work writes it.
- **Snapshots** (`docs/trail/snapshots/vX.Y.Z.md`): the lessons so far, rebuilt at each tag
  from the previous snapshot plus the events since. **`current.md`** is a copy of the newest
  snapshot. A session reads it first.
- **Missing events**: every merged pull request with no event. Git computes this list, so a
  session that forgot to write an event shows up.

This repository holds the tools that work on the trail:

| Piece | What it does |
| --- | --- |
| **`trail` CLI** (Python package `agent-trail`) | `check`, `remind`, `missing`, `index`, `skeleton`, `unsnapshotted`, `init [--agent …]`, `is-trail-only`, `hook`. No runtime dependencies; Python 3.9+. |
| **Agent integrations** | Claude Code (a plugin), Codex (`AGENTS.md` and hooks), Cursor (a rule and hooks), and any agent that reads `AGENTS.md`. Each one asks the CLI the same question: `trail remind`, or `trail hook` with a per-agent adapter. |
| **GitHub Action** (`Hamza-Labs-Core/agent-trail@v0`) | Runs `trail check` on pull requests. It can also comment on a merged pull request that has no event. |

It was extracted from [Maktaba](https://github.com/Hamza-Labs-Core/Maktaba), where the trail
was built (as "the Claude trail", Maktaba PRs #655 and #674). The formats are unchanged.

## Install the CLI

There's no PyPI release yet (see [Releasing](#releasing)). Install from git:

```bash
uv tool install git+https://github.com/Hamza-Labs-Core/agent-trail@v0.2.0   # puts `trail` on PATH
# or run it once
uvx --from git+https://github.com/Hamza-Labs-Core/agent-trail@v0.2.0 trail check
# or with pip
pip install git+https://github.com/Hamza-Labs-Core/agent-trail@v0.2.0
```

Once it's on PyPI: `uv tool install agent-trail`, or `uvx agent-trail check`. The command is
available as both `trail` and `agent-trail`.

Then, in a repository:

```bash
trail init                        # .trail.toml, docs/trail/events/ and docs/trail/snapshots/
trail init --agent codex          # plus the files for an agent (repeatable; see below)
trail check                       # validate every event and snapshot
trail remind                      # what's missing now: PRs with no event, tags with no snapshot
```

## Agent integrations

`trail init --agent <name>` writes each agent's files. It merges into what's already there:
JSON files keep their other keys and hooks, and the `AGENTS.md` block is replaced between its
markers, never duplicated. Running it twice changes nothing.

Every integration asks the same question: did this session merge a pull request without
writing its event? The logic lives in the CLI (`trail remind`, `remind.py`). A pull request
counts as covered when an event for it exists anywhere git can see: any local or
remote-tracking branch, any past version (including its `prs:` list), or any worktree's
files. The hooks only adapt each agent's input and output to that check.

| Agent | `--agent` | Instructions | Hooks | Reminds at |
| --- | --- | --- | --- | --- |
| Claude Code | `claude` | the plugin's `trail` skill and `/trail:*` commands | the plugin's `hooks/hooks.json` | Stop (blocks once), PreCompact, SessionStart, SessionEnd |
| Codex | `codex` | `AGENTS.md` block | `.codex/hooks.json` | Stop (blocks once), SessionStart, SessionEnd |
| Cursor | `cursor` | `.cursor/rules/trail.mdc` (always applied) | `.cursor/hooks.json` | stop (one follow-up), sessionStart, sessionEnd |
| Anything else | `agents` | `AGENTS.md` block | none | `trail remind`, when the instructions say to run it |

All the hooks follow the same safety rules:
- A hook always exits 0, and Cursor's hook always prints JSON (`{}` when it has nothing to say).
- In a repository with no `.trail.toml`, a hook does nothing.
- Every git call has a timeout.
- A hook fetches the main branch at most every 10 minutes per repository, and writes its
  fetch stamp *before* fetching, so a hung fetch delays one stop, not every one.
- State is kept in `$AGENT_TRAIL_STATE_DIR`, the Claude Code plugin's data folder, or
  `~/.local/state/agent-trail`. It holds per-session merges and the SessionEnd log, both keyed
  by repository.

### Claude Code

The repository is its own Claude Code marketplace (`.claude-plugin/marketplace.json`, named
`agent-trail`), and the plugin in it is named `trail`. Claude Code reserves plugin names that
start with `claude-`. In a session:

```text
/plugin marketplace add Hamza-Labs-Core/agent-trail
/plugin install trail@agent-trail
```

To turn it on for everyone working in a repository, run `trail init --agent claude`, or commit
this to `.claude/settings.json`. Claude Code registers the marketplace once a contributor
trusts the folder:

```json
{
  "extraKnownMarketplaces": {
    "agent-trail": { "source": { "source": "github", "repo": "Hamza-Labs-Core/agent-trail", "ref": "v0" } }
  },
  "enabledPlugins": { "trail@agent-trail": true }
}
```

The plugin adds:
- **The `trail` skill**: when and how to read the trail, write events and build snapshots.
- **Commands**:
  - `/trail:event [N]` writes the event for the current branch's pull request, or for PR N.
  - `/trail:snapshot [vX.Y.Z]` builds every missing snapshot in one trail-only pull request.
  - `/trail:read [topic]` loads `current.md` and the events added since it, and sums up what
    bears on the topic.
- **`trail`** on the Bash tool's PATH (the plugin's `bin/`, run with the machine's `python3`;
  nothing to install).
- **The hook** (`trail hook --agent claude`). It finds `gh pr merge N` in the session's
  transcript and its subagents' transcripts.

### Codex

`trail init --agent codex` writes two things:

- **An `AGENTS.md` block.** Codex reads `AGENTS.md` from the repository root down to the
  working folder.
- **`.codex/hooks.json`**, which runs `trail hook --agent codex` at four points:
  - **PostToolUse:** it records `gh pr merge N` from `tool_input.command`. Codex documents its
    transcript format as unstable for hooks, so the hook doesn't parse the transcript.
  - **Stop:** if a merged PR has no event, it returns `decision: block` with a reason. Codex
    continues with that reason as its next prompt. It does this once, guarded by
    `stop_hook_active`.
  - **SessionStart:** at startup, it lists what earlier sessions left unwritten; on a resume or
    after a compact, it reminds the session.
  - **SessionEnd:** it logs what's still unwritten. Codex caps SessionEnd at 3 seconds, so
    this one doesn't fetch.

Codex runs project hooks only once the project's `.codex/` layer is trusted and the hooks are
approved in `/hooks`. The hooks need `trail` on the PATH (`uv tool install …`, above); without
it they do nothing.

### Cursor

`trail init --agent cursor` writes:
- **`.cursor/rules/trail.mdc`**, the instructions as an `alwaysApply` project rule.
- **`.cursor/hooks.json`** (`version: 1`), which runs `trail hook --agent cursor` at four
  points:
  - **afterShellExecution:** it records `gh pr merge N` from `command`.
  - **stop:** if a merged PR has no event, it returns a `followup_message`, which Cursor sends
    as the next prompt. It does this once, and only while `loop_count` is 0.
  - **sessionStart:** it returns `additional_context` listing what earlier sessions left
    unwritten.
  - **sessionEnd:** it logs what's still unwritten.

The hooks need `trail` on the PATH. Cursor also reads `AGENTS.md`, so `--agent agents` works
there too.

### Any other agent

`trail init --agent agents` writes the trail block into `AGENTS.md`, which many agents read.
The block covers:
- reading `current.md` and the newer events at the start;
- writing an event for each merged pull request;
- running `trail check` before pushing and `trail remind` before finishing;
- building snapshots after tags.

The paths in the block come from `.trail.toml`. An agent with no hooks gets its reminder by
running `trail remind` itself:

```bash
trail remind                 # PRs merged in the last 14 days with no event, tags with no snapshot
trail remind --pr 702        # just the PRs this session merged
trail remind --json --strict # machine-readable; exit 1 when anything is missing
```

## The GitHub Action

```yaml
# in a pull request workflow
- uses: actions/checkout@v6
  with:
    fetch-depth: 0  # snapshots and Missing read history and tags
- uses: Hamza-Labs-Core/agent-trail@v0
```

To comment on merged pull requests that have no event:

```yaml
on:
  pull_request:
    types: [closed]
permissions:
  contents: read
  pull-requests: write
jobs:
  trail:
    if: github.event.pull_request.merged
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
        with: { fetch-depth: 0 }
      - uses: Hamza-Labs-Core/agent-trail@v0
        with:
          command: ""            # just the comment
          comment-missing: true
```

| Input | Default | |
| --- | --- | --- |
| `command` | `check` | The trail command and its arguments. Empty runs nothing. |
| `working-directory` | `.` | Where `.trail.toml` is. |
| `fetch-tags` | `true` | Fetch the tags first, and the full history on a shallow clone. |
| `comment-missing` | `false` | On a merged PR's `closed` event, comment once (a hidden marker prevents repeats) when it has no event. Trail-only PRs and PRs at or below `missing_after` never get a comment. |
| `github-token` | `github.token` | The token that posts the comment. |
| `python` | `python3` | Any Python 3.9 or later. The runner's own `python3` is enough, with nothing installed. |

The Action runs the checker from its own copy (`${{ github.action_path }}/src`) with the
runner's Python, so it installs nothing.

## The formats

These formats are fixed (`schema: 1`) and work for any agent. `.trail.toml` changes only the
paths and the naming.

### Events

One file per pull request, named for its number and zero-padded so `ls` sorts them:
`events/0634-youtube-sources-from-links.md`. Work with no pull request is named for its issue
(`events/i0649-player-shards-left-running.md`) or its day
(`events/d20261104-live-db-sweep.md`).

```markdown
---
schema: 1
kind: pr                # pr | incident | decision | correction | release | migration
pr: 634                 # with an NNNN- name; issue: N with an iNNNN- name
title: YouTube sources from linked videos
date: 2026-11-04        # the day the work happened (or merged)
session: 7c0a4c88-…     # the session that learned it (any agent's id); unknown for a migrated entry
agent: codex            # optional, free text: claude-code, codex, cursor, …
prs: [628]              # optional: other pull requests this event is also about
corrects: 0608-epic-51  # kind correction only: the event it corrects
source: claude-trail.md # optional: where a migrated entry came from
---

## What happened
Required. What was built or found, briefly: the change, the root cause, the numbers.

## What we learned
The things a later session should know: gotchas, causes, what the code or the tools really do.

## Keep doing
## Stop doing
## Open
What is left, with its issue number.
```

- Only `What happened` is required. Leave an empty section out rather than padding it, and
  don't invent a lesson to fill one. The sections that are there keep this order.
- Use real pull request numbers, issue numbers and shas only (from `gh` and `git`), never
  guessed ones.
- **Events are append-only.** Don't edit an event after it merges. A later fact or a fix goes
  in a new event (`kind: correction` with `corrects:`), or in a second event for the same pull
  request (`0634-after-release.md`).
- Never write secrets. `trail check` refuses the common shapes: GitHub, Anthropic, OpenAI,
  Slack and AWS tokens and keys, private keys, bearer tokens, URLs with passwords, and
  `password=…`-style values (but not prose such as `password: rotated`).

### Snapshots

```markdown
---
schema: 1
tag: v0.3.82
up_to_commit: 1a2b3c4d5     # the tag's commit
built_from: v0.3.81         # the previous snapshot, or full for a rebuild from every event
---

# Trail snapshot: v0.3.82

## Keep doing
- Branch stacks point HOME at a scratch folder. (#621)
## Stop doing
## What we learned
## Open
## Missing events
## Index
```

- Every item cites at least one event: `#634` (a pull request's events), `i649` (an issue's),
  or a `dYYYYMMDD-slug` name. `trail check` refuses an item with no citation, or one whose
  citation matches no event at `up_to_commit`.
- To name an issue, write `issue #649` (the word, a space, then `#N`). That form cites
  nothing and needs no event. Any other `#N` is read as a pull request.
- `snapshots/` holds only `vX.Y.Z.md` files.
- `Missing events` and `Index` are computed, never written by hand
  (`trail skeleton --tag vX.Y.Z` prints them).
- Each snapshot carries the previous one forward, so a lesson keeps its citation until someone
  drops it on purpose. At each minor version, a rebuild from every event
  (`built_from: full`) keeps the summary from drifting.

## Building snapshots

**A session builds snapshots, never a model in CI.** Nothing in this repository runs a model:
the Action and the CLI only check. Any agent can build a snapshot with the same steps:

1. Run `trail unsnapshotted`. It lists the tags with no snapshot, oldest first. Before the
   first snapshot exists, it lists only the newest tag.
2. For each tag, run `trail skeleton --tag <tag> > docs/trail/snapshots/<tag>.md`.
3. Fold the new events into the four sections, with their citations.
4. Copy the newest snapshot to `current.md`.
5. Run `trail check`.
6. Merge it all as one trail-only pull request.

In Claude Code, `/trail:snapshot` does all of this. You can also run it as a scheduled
session, for example an hourly routine.

## Tags

Trail-only changes must never cause a release. A commit is trail-only when every file it
changes matches `trail_only.paths`. Tag automation should ask before tagging:

```bash
# the change since the last tag is only the trail: no release
if trail is-trail-only "$(git describe --tags --abbrev=0)..HEAD"; then exit 0; fi
```

`trail missing` and `trail remind` use the same rule: a trail-only pull request never needs
an event of its own. If your CI uses `paths-ignore` so that trail-only pushes start no run,
keep that list equal to `trail_only.paths`. Maktaba's CI tests check this.

## Commands

| Command | |
| --- | --- |
| `trail init [--force] [--agent A …]` | Write a commented `.trail.toml` and create the events and snapshots folders. Add an agent's files with `--agent claude\|codex\|cursor\|agents`. |
| `trail check [--no-git]` | Validate every event and snapshot: front matter, sections, names, citations, the computed tables, no secrets. Exits 1 when it finds problems. CI runs this. |
| `trail remind [--pr N …] [--days D] [--no-tags] [--no-fetch] [--json] [--strict]` | What's missing now: the given merged PRs (or those merged in the last D days, default 14) with no event on any branch, and the tags with no snapshot. |
| `trail missing [--ref R] [--pr N]` | The merged PRs up to R with no event at R (the snapshot's `Missing events`). With `--pr`, exits 1 when N is one of them. |
| `trail index [--ref R]` | Print the Index table. |
| `trail skeleton --tag vX.Y.Z` | Print a new snapshot to finish: the last snapshot's sections carried forward, the new events listed in a comment, and the computed tables. |
| `trail unsnapshotted` | The tags with no snapshot yet. |
| `trail is-trail-only RANGE` | Exit 0 when `A..B`, or a single commit (compared with its first parent), changes only trail paths. Exit 1 when it changes anything else. |
| `trail hook --agent claude\|codex\|cursor` | An agent's session hook. It reads the hook's JSON on stdin and always exits 0. |

The repository root is the nearest folder at or above the current one that holds a
`.trail.toml`, then git's top level. `--root DIR` overrides both. Exit code 2 means the command
couldn't run: a bad `.trail.toml`, or a git error such as an unknown revision.

## Configuration

`.trail.toml` sits at the repository root. Every key is optional, and `trail init` writes this
file with the defaults:

```toml
[paths]
events = "docs/trail/events"        # one file per piece of work
snapshots = "docs/trail/snapshots"  # vX.Y.Z.md, one per tag
current = "docs/trail/current.md"   # a copy of the newest snapshot
legacy = ""                         # a frozen trail file from before the events, if any

[git]
remote = "origin"
main_branch = "main"

[events]
pr_digits = 4        # 0634-slug.md: pull request numbers zero-padded to this many digits
issue_prefix = "i"   # i0649-slug.md: work with no pull request, named for its issue
day_prefix = "d"     # d20261104-slug.md: work with neither, named for its day
missing_after = 0    # pull requests up to this number are never listed as Missing

[snapshots]
tag_prefix = "v"     # a snapshot per <prefix>X.Y.Z tag

[checks]
default_secrets = true   # refuse tokens, keys, passwords and URLs with passwords
extra_secrets = []       # more regular expressions to refuse
owner_quotes = false     # refuse text that quotes or attributes the owner's words

[trail_only]
# Empty: the events, snapshots, current and legacy paths above.
paths = []
```

Maktaba sets `legacy = "docs/claude-trail.md"`, `missing_after = 653` and
`owner_quotes = true`. An unknown table or key, or a value of the wrong type, is an error, so a
typo can't pass silently. On Python 3.11+ the file is read with `tomllib`. On 3.9 and 3.10, a
small built-in reader handles this subset, so the hooks and the Action run on whatever
`python3` the machine already has, such as macOS's 3.9 or Ubuntu 22.04's 3.10.

## Development

```bash
uv run --group dev pytest       # or: python3 -m pytest (only pytest is needed)
uvx ruff check . && uvx ruff format --check .
uv run --with mypy mypy
claude plugin validate .
```

CI runs:
- ruff, mypy (strict), shellcheck and actionlint;
- the tests on Python 3.9 to 3.13 and on macOS;
- `claude plugin validate`;
- the Action itself, on a scratch repository on Ubuntu 22.04 (Python 3.10, so the fallback
  TOML reader).

## Releasing

1. Bump the version in `src/agent_trail/__init__.py`, `pyproject.toml` and
   `.claude-plugin/plugin.json`. The release checks that they match the tag, and the tests
   check that they match each other.
2. Tag `vX.Y.Z` on main and push the tag. `release.yml` then:
   - builds the wheel and sdist;
   - attaches them to a GitHub release;
   - moves the major tag (`v0`), so that `@v0` users and the `ref: v0` marketplace source pick
     up the release;
   - publishes to PyPI, but only once PyPI is set up (below).

The Claude Code plugin's `version` pins installed copies, so bump it with every release.

### PyPI (not set up yet)

PyPI publishing uses [trusted publishing](https://docs.pypi.org/trusted-publishers/), so no
token is stored anywhere. Until the owner does these steps, the `pypi` job is skipped:

1. On PyPI, open *Your projects → Publishing → Add a new pending publisher*, choose GitHub,
   and enter:
   - project name `agent-trail`
   - owner `Hamza-Labs-Core`
   - repository `agent-trail`
   - workflow `release.yml`
   - environment `pypi`
2. In this repository, go to *Settings → Environments* and create an environment named `pypi`.
   Optionally, add required reviewers to it.
3. Go to *Settings → Secrets and variables → Actions → Variables* and add `PYPI_PUBLISH` set
   to `true`.
4. Re-run the `Release` workflow for the latest tag, or push the next tag.

## License

MIT. See [LICENSE](LICENSE).
