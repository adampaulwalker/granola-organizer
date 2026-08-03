---
name: granola-router
description: Save Granola meeting transcripts to markdown and file them into per-client folders. Use for finding what was said in a past meeting, saving new meetings to disk, diagnosing why a meeting was not filed, and adding routing rules. Triggers on "save my meetings", "sync my granola notes", "what did we decide in the call with", "where is the transcript from", "why is this meeting unrouted", "file my transcripts".
---

# granola-router

Drives the `granola-router` command line tool, which pulls meetings from Granola's
official API and writes each one as a markdown file in the right client folder.

## Before anything else

Run `granola-router status`. It prints the transcript folder, how many meetings are
tracked, and everything currently unfiled with the reason. Almost every question is
answerable from that output plus a grep, without touching the API.

```bash
granola-router status
```

**If the command is not found, stop there.** Do not search the machine for a similar
tool, do not inspect other installs, and do not try to run the package another way.
Tell the user granola-router is not installed and give them the one line that fixes
it:

```bash
uv tool install granola-router     # or: pipx install granola-router
```

Then stop and wait. Everything below assumes the command exists, and guessing at a
substitute produces confident answers about the wrong tool.

**If it reports no API key**, the user needs a Granola **Business** plan, then
Settings → Connectors → API keys, saved to `~/.granola-router/api-key`. Say that and
stop; there is nothing to diagnose until a key exists.

## Finding what was said

Transcripts are markdown on disk. Search them directly rather than calling the API.

```bash
grep -ril "pricing" "$(granola-router status | awk -F': ' '/transcript root/{print $2}')"
```

Each file has front matter with `title`, `date`, `attendees` and `granola_id`, then
the AI summary, then the full timestamped transcript. Read the file and answer from
it. Quote the transcript rather than paraphrasing when the user asks what someone
said.

## Saving new meetings

```bash
granola-router poll --once        # pull anything new since the last run
granola-router backfill --dry-run # show where everything would go, write nothing
granola-router backfill           # write it
```

Always offer `--dry-run` first when the user has not run a backfill before. A meeting
only appears once Granola has generated its summary and transcript, so a call that
just ended may not be there yet. Say that rather than reporting it as missing.

## When a meeting was not filed

Unfiled meetings sit in the quarantine folder, and `status` lists each with a reason:

| Reason | Meaning | Fix |
|---|---|---|
| `no_attendees` | Only the account holder was on the invite | Add a `title_keywords` rule |
| `unknown` | External attendees, but no rule matched their domain | Add an `email_domains` rule |
| `ambiguous` | Two rules disagreed | Add a scoped `title_overrides` rule |

`granola-router domains` lists attendee domains that have no rule yet, which is the
fastest way to see what is worth adding.

Rules live in `~/.granola-router/routing-map.json`. Editing it re-files existing
transcripts on the next run, so a rule added today fixes the whole history.

## Adding a rule

Read the current map first, then add to the right tier. Precedence runs
`note_overrides`, `title_overrides`, `email_domains`, `title_keywords`.

Use `email_domains` when the client has their own domain. Use `title_keywords` when
they are on a personal address and the meeting title names them. Use `note_overrides`
for a single meeting nothing else can reach.

After editing, confirm with a dry run before writing:

```bash
granola-router backfill --dry-run
```

## Things to get right

Never invent a routing rule for a client the user has not confirmed. Filing a
transcript into the wrong client's folder is worse than leaving it unfiled, which is
why the tool quarantines rather than guesses.

Do not run `backfill` without saying what it will do first. It can write hundreds of
files.

If the user asks where to keep the transcripts, the tool writes to any folder. A
folder inside Dropbox, Google Drive, Box or OneDrive is readable from Claude in the
browser through that service's connector. A local folder is readable by Claude Code
and Claude Desktop but not from a browser chat.
