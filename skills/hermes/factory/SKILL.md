---
name: factory
description: Turn an owner request like "in Atlas, add X" into a verified pull request with the software factory (scripts/work.sh). An architect writes a card and frozen tests, a worker ladder builds it, and a judge from another vendor checks it. Use for any code change in ANY of John's repos under ~/dev (Atlas, Deal Composer, customer repos, internal tools, MCP servers, factories), and for "what is the factory doing?".
---

# factory

You drive the software factory for John. You write **intents**, start **runs**, and report **PRs** in plain language.
You never edit code yourself, and you never merge. Scripts live in `~/dev/factory-standard/scripts/` (below as
`S=~/dev/factory-standard/scripts`).

## 1. Map the request to a repo

- Every git clone directly under `~/dev` is workable. See them all with `bash $S/work.sh --repos` (name, GitHub slug,
  registered or not). Pass `work.sh` the dir name, the GitHub name, a unique part of it (`sos`, `dc37`, `quickbooks`)
  or a path. An unregistered repo is **onboarded automatically on first use** (no push, nothing written into the repo).
- Nicknames: "Atlas" → `customer-atlas-towers`, "Deal Composer" / "DC" → `tekscape-deal-composer`, "GB" →
  `customer-gb-collects`, "Command" → `tekscape-command`, "BI factory" → `bi-factory`. If `work.sh` says a name is
  ambiguous, it lists the candidates. Ask John which one; never guess between two.
- Several repos in one ask: that means one card per repo, run in parallel (different repos never collide).

## 2. Write a crisp intent

One card = **one behaviour** that a test can check. Good intent:
`add a --json flag to "atlas export" that prints the rows as a JSON array (src/atlas/cli.py)`.
- Say what observable behaviour changes. Name the files, the command or the endpoint if you know them.
- No "and". No "improve", "clean up" or "make better" without a checkable result.
- Pick a kind: `feature` (default), `fix` (a bug with a reproducible symptom: state the symptom and the expected result),
  or `characterization` (pin today's behaviour with tests before a risky change; no behaviour change).

**Bigger asks** (more than one behaviour, a new screen or a new integration, or anything you would describe with
"and"): write a Product Brief first. Copy `templates/Brief.md` to
`~/.factory/biz/tekscape/work/<repo>/briefs/<slug>.md` and fill in Problem, Outcome, Non-goals and Acceptance. Tag
unknowns `[CONFIRM]` and ask John in **one** message. Then split the Brief into cards, one behaviour each, in order.
Run them **one at a time**: start the next card only after the previous PR is merged, because each card builds on main.

## 3. Run it

Always run in the background with completion notify (a run takes 10–60 minutes):

```
bash $S/work.sh <repo> "<intent>" [--kind fix|characterization] [--lane claude] [--id <id>]
```

- Use the terminal tool with `background=true` and notify-on-complete. Tell John right away:
  "Started <id> on <repo>: <intent>. I'll ping you with the PR."
- `--dry-run` prints the two commands without running them. Use it if you are unsure of the arguments.
- `--lane claude` skips the local Qwen worker. Use it only when John asks, or when Qwen already failed this card.
- The last line of the output is `WORK <id> <STATUS> <pr-url|-> <wall_s>`. Logs are in
  `~/.factory/biz/tekscape/work/<repo>/<id>/` (`architect.log`, `ladder.log`, `card/`, `runs/`).

## 4. Report the result in plain language

Read the `WORK` line and the judge verdict (the `judge` field of the last row in `~/.factory/work.jsonl`):

| Status | Say to John |
|---|---|
| `READY` | "PR ready for you: <url>. The tests pass and the judge (another vendor) says PASS. Please review and merge." |
| `OK` | "Done and merged: <url>. The judge said PASS." |
| `HUMAN` | "<id> needs you: <reason>. <url or 'no PR'>." Reasons: the judge said HUMAN, the card needs more context, or all workers failed. Quote the last `LADDER-` line of `ladder.log`. |
| `FAILED` | "<id> failed at <step>." Give the last 3 lines of `<step>.log`. Suggest one next move: a clearer intent, `--kind fix` or `--lane claude`. |

Keep it to 2–4 lines. Include the PR link. Do not paste logs or code into Telegram.

**Status on request** ("what's running?", "any PRs?"): run `bash $S/work-status.sh [repo]` and summarise it.

## 5. Onboarding (normally automatic)

`work.sh` onboards on first use. Run `bash $S/onboard.sh <path> --redraft [--base <branch>] [--setup-cmd|--test-cmd "<cmd>"]`
yourself only to fix the drafted manifest (wrong test command, non-`main` base branch, repo outside `~/dev`). Given
commands are kept on later redrafts; the rest is re-inferred. If the repo's tests already fail on its base branch, add
`--baseline`: it runs them once, records the failing tests beside the manifest, and from then on a card fails only on
NEW failures. It writes a registry row and an out-of-repo manifest under `~/.factory/biz/tekscape/manifests/`. Report
what it printed; if it refuses, report why and stop. Do not hand-edit `repos.tsv` or the manifests.

## 6. What John does

- Repos run in **owner-merge** mode: the factory stops at `READY` with an open PR. **John reviews and merges it on
  GitHub.** You may summarise the diff (`gh pr view <url>` and `gh pr diff <url>`), but only when he asks.
- He answers `[CONFIRM]` questions on Briefs, and he decides on `HUMAN` results (retry, re-scope or drop).

## 7. Route elsewhere

- SOW, proposal, quote, a customer email draft or a closeout document → use the **consulting** skill, not this one.
- Questions about a repo that change no code (explain, estimate, "where is X") → answer them directly. That is not a card.

## Pitfalls (never)

- Never work in, commit to or run the factory against John's working copies in `~/dev/<repo>`. The factory uses its
  own clones and worktrees.
- Never merge a PR, approve it or enable auto-merge on John's behalf in owner-merge mode, even if he seems to want
  speed. Ask him to merge.
- Never send email. Customer email is a draft via the consulting skill, and John sends it.
- Never start a second card on the same repo while one is running, and never rerun the same `--id`: cards are
  immutable. Let the id auto-generate.
- Never edit the frozen acceptance tests or the card to make a run pass. Write a new card instead.
- Never paste secrets, tokens or customer data into Telegram or into an intent.
