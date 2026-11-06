# claude-trail

The Claude trail is a repository's record of what Claude sessions learned while working on it,
so the next session starts from that record instead of from nothing. It isn't a changelog
and it isn't a list of pull requests (git already has that). It holds what only the session
saw: the approach that failed, the root cause found after the wrong guesses, the rule that was
corrected partway through, the gotcha that cost an hour.

The trail has three parts, all kept in the repository:

- **Events** (`docs/trail/events/`): one Markdown file per piece of work, usually one per
  merged pull request. The session that did the work writes it.
- **Snapshots** (`docs/trail/snapshots/vX.Y.Z.md`): the lessons so far, rebuilt at each tag
  from the previous snapshot plus the events since. **`current.md`** is a copy of the newest
  snapshot. A session reads it first.
- **Missing events**: every merged pull request with no event. Git computes this list, so a
  session that forgot to write an event shows up.

This repository holds three pieces that work on the trail:

| Piece | What it does |
| --- | --- |
| **`trail` CLI** (Python package `claude-trail`) | `check`, `missing`, `index`, `skeleton`, `unsnapshotted`, `init`, `is-trail-only`. No runtime dependencies; Python 3.9+. |
| **Claude Code plugin** (`trail`) | The `trail` skill, `/trail:event`, `/trail:snapshot` and `/trail:read`, the reminder hook, and `trail` on the session's PATH. |
| **GitHub Action** (`Hamza-Labs-Core/claude-trail@v0`) | Runs `trail check` on pull requests. It can also comment on a merged pull request that has no event. |

It was extracted from [Maktaba](https://github.com/Hamza-Labs-Core/Maktaba), where the trail
was built (Maktaba PRs #655 and #674). Maktaba's settings are the defaults here, apart from
the few that only make sense there (see [Configuration](#configuration)).

## Install

### The Claude Code plugin

In a Claude Code session:

```text
/plugin marketplace add Hamza-Labs-Core/claude-trail
/plugin install trail@claude-trail
```

The repository is its own marketplace (`.claude-plugin/marketplace.json`, named
`claude-trail`), and the plugin in it is named `trail`. Claude Code reserves plugin names that
start with `claude-` for Anthropic's own plugins (`claude plugin validate` refuses them), so the
plugin can't be called `claude-trail`. Its commands are therefore `/trail:event`,
`/trail:snapshot` and `/trail:read`, and its skill is `trail`.

To turn it on for everyone who works in a repository, commit this to the repository's
`.claude/settings.json`. Claude Code registers the marketplace once a contributor trusts the
folder, and enables the plugin:

```json
{
  "extraKnownMarketplaces": {
    "claude-trail": {
      "source": { "source": "github", "repo": "Hamza-Labs-Core/claude-trail", "ref": "v0" }
    }
  },
  "enabledPlugins": {
    "trail@claude-trail": true
  }
}
```

**A private repository needs git credentials.** Claude Code clones the marketplace with the
machine's own `git` and never prompts. On GitHub, `gh auth login` followed by
`gh auth setup-git` is enough, or use an SSH key loaded in `ssh-agent`. Without a GitHub SSH
key, set `CLAUDE_CODE_PLUGIN_PREFER_HTTPS=1`.

What the plugin adds:

- **The hook** (`hooks/hooks.json`), at Stop, PreCompact, SessionStart and SessionEnd, in any
  repository that has a `.trail.toml`. It finds `gh pr merge N` in the session's transcript
  (and its subagents' transcripts), keeps the numbers git shows merged into the main branch,
  and looks for an event for each one on any branch or worktree. What it does with the result
  depends on the hook point:
  - **Stop:** if a merged PR still has no event, it blocks once and asks the session to write it.
  - **PreCompact:** it tells you, before compaction drops the details.
  - **SessionStart:** after a compact or resume, it reminds the session; at startup, it lists
    the PRs that earlier sessions left without events.
  - **SessionEnd:** it tells you, and logs what's still unwritten.

  The hook always exits 0 and keeps its timeouts: 30 s per hook, 20 s per git call and 15 s
  for the fetch. It fetches at most every 10 minutes per repository, and it writes its fetch
  stamp *before* the fetch, so a fetch that hangs delays one Stop, not every Stop. In a
  repository with no `.trail.toml`, or on a machine with no `python3`, it does nothing. It
  keeps its state in the plugin's data folder (`CLAUDE_TRAIL_HOOK_DIR` overrides that).
- **`trail`** on the Bash tool's PATH (`bin/trail`), run with the machine's `python3`.
- **The `trail` skill**: when and how to read the trail, write events and build snapshots.
- **`/trail:event [N]`** writes the event for the current branch's pull request (or for PR N).
  **`/trail:snapshot [vX.Y.Z]`** builds every missing snapshot in one trail-only pull request.
  **`/trail:read [topic]`** loads `current.md` and the events added since it, then sums up the
  lessons that bear on the topic.

### The CLI

There's no PyPI release yet (see [Releasing](#releasing)). Run it from git:

```bash
uvx --from git+https://github.com/Hamza-Labs-Core/claude-trail@v0.1.0 trail check
# or install it
pip install git+https://github.com/Hamza-Labs-Core/claude-trail@v0.1.0
```

Once it's on PyPI: `uvx claude-trail check`, or `pip install claude-trail` and then
`trail check`.

The CLI looks for the repository root in this order: the nearest folder at or above the
current one that holds a `.trail.toml`, then git's top level. `--root DIR` overrides both.

| Command | |
| --- | --- |
| `trail init` | Write a commented `.trail.toml` and create the events and snapshots folders. |
| `trail check [--no-git]` | Validate every event and snapshot: front matter, sections, names, citations, the computed tables, no secrets. Exits 1 when it finds problems. CI runs this. |
| `trail missing [--ref R] [--pr N]` | The merged PRs up to R that have no event. With `--pr`, exits 1 when N is one of them. |
| `trail index [--ref R]` | Print the Index table. |
| `trail skeleton --tag vX.Y.Z` | Print a new snapshot to finish: the last snapshot's sections carried forward, the new events listed in a comment, and the computed tables. |
| `trail unsnapshotted` | The tags with no snapshot yet, oldest first. Before the first snapshot exists, only the newest tag. |
| `trail is-trail-only RANGE` | Exit 0 when `A..B`, or a single commit (compared with its first parent), changes only trail paths. Exit 1 when it changes anything else. For tag automation: see [Tags](#tags). |
| `trail hook` | The Claude Code hook, which reads the hook's JSON on stdin. |

Exit code 2 means the command couldn't run: a bad `.trail.toml`, or a git error such as an
unknown revision.

### The GitHub Action

```yaml
# in the lint job of a pull request workflow
- uses: actions/checkout@v6
  with:
    fetch-depth: 0  # snapshots and Missing read history and tags
- uses: Hamza-Labs-Core/claude-trail@v0
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
      - uses: Hamza-Labs-Core/claude-trail@v0
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
runner's Python, so it installs nothing. This repository is private, so its Actions access is
set to *accessible from repositories in the organization*. That setting lets the organization's
other repositories use `Hamza-Labs-Core/claude-trail@v0`.

## The formats

These formats are fixed (`schema: 1`). `.trail.toml` changes only the paths and the naming.

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
session: local_7c0a4c88-…   # the session that learned it; unknown for a migrated entry
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
the Action and the CLI only check. In a Claude Code session, run `/trail:snapshot`. It runs
`trail unsnapshotted`, builds each missing snapshot with `trail skeleton`, folds in the new
events, copies the newest snapshot to `current.md`, runs `trail check`, and opens one
trail-only pull request.

You can run this as a scheduled session, for example an hourly cloud routine that runs
`/trail:snapshot` in the repository. A scheduled session needs the plugin, so its
environment needs git access to this private repository. Otherwise, run the same steps with
`uvx --from git+https://github.com/Hamza-Labs-Core/claude-trail trail …`.

## Tags

Trail-only changes must never cause a release. A commit is trail-only when every file it
changes matches `trail_only.paths`. Tag automation should ask before tagging:

```bash
# the change since the last tag is only the trail: no release
if trail is-trail-only "$(git describe --tags --abbrev=0)..HEAD"; then exit 0; fi
```

`trail missing` uses the same rule: a trail-only pull request never needs an event of its own.
If your CI uses `paths-ignore` so that trail-only pushes start no run, keep that list equal
to `trail_only.paths`. Maktaba's `tools/ci/ci_paths.py` has a test that checks this.

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

Three defaults differ from Maktaba's values, because Maktaba's values only make sense there.
Maktaba sets them explicitly:

| Key | Default | Maktaba | Why |
| --- | --- | --- | --- |
| `paths.legacy` | `""` | `docs/claude-trail.md` | The frozen trail from before events existed. |
| `events.missing_after` | `0` | `653` | Pull requests up to #653 are covered by the frozen trail. |
| `checks.owner_quotes` | `false` | `true` | Maktaba's CLAUDE.md rule 5. |

An unknown table or key, or a value of the wrong type, is an error (exit code 2), so a typo
can't pass silently. On Python 3.11+ the file is read with `tomllib`. On 3.9 and 3.10, a small
built-in reader handles the subset above: tables, strings, integers, booleans and arrays. That
fallback lets the hook and the Action run on whatever `python3` the machine already has, such
as macOS's 3.9 or Ubuntu 22.04's 3.10, with no install step.

## Development

```bash
uv run --group dev pytest       # or: python3 -m pytest (only pytest is needed)
uvx ruff check . && uvx ruff format --check .
uv run --with mypy mypy
claude plugin validate .
```

CI (`.github/workflows/ci.yml`) runs:
- ruff and mypy (strict);
- the tests on Python 3.9 to 3.13 and on macOS;
- `claude plugin validate`;
- the Action itself, on a scratch repository on Ubuntu 22.04 (Python 3.10, so the fallback
  TOML reader).

## Releasing

1. Bump the version in `src/claude_trail/__init__.py`, `pyproject.toml` and
   `.claude-plugin/plugin.json`. The release checks that all three match the tag, and the tests
   check the first two against each other and against the plugin.
2. Tag `vX.Y.Z` on main and push the tag. `release.yml` then:
   - builds the wheel and sdist;
   - attaches them to a GitHub release;
   - moves the major tag (`v0`), so that `@v0` users and the `ref: v0` marketplace source pick
     up the release;
   - publishes to PyPI, but only once PyPI is set up (below).

The plugin's `version` in `plugin.json` pins installed copies: users get a new copy only when
that string changes, so bump it with every release.

### PyPI (not set up yet)

PyPI publishing uses [trusted publishing](https://docs.pypi.org/trusted-publishers/), so no
token is stored anywhere. Until the owner does these steps, the `pypi` job is skipped:

1. On PyPI, open *Your projects → Publishing → Add a new pending publisher*, choose GitHub,
   and enter:
   - project name `claude-trail`
   - owner `Hamza-Labs-Core`
   - repository `claude-trail`
   - workflow `release.yml`
   - environment `pypi`
2. In this repository, go to *Settings → Environments* and create an environment named `pypi`.
   Optionally, add required reviewers to it.
3. Go to *Settings → Secrets and variables → Actions → Variables* and add `PYPI_PUBLISH` set
   to `true`.
4. Re-run the `Release` workflow for the latest tag, or push the next tag.

## License

MIT. See [LICENSE](LICENSE).
