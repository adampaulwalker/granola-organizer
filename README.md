# granola-router

Saves your [Granola](https://www.granola.ai) meeting transcripts to markdown files on disk, and files each one into the right client folder automatically.

> **Requires a Granola Business plan.** This reads Granola's official public API, and API keys are a Business feature. It cannot work on the free plan. See [Why paid only](#why-paid-only).

## What it does

Every meeting becomes one markdown file: front matter, Granola's AI summary, then the full timestamped transcript with speaker labels.

```markdown
---
title: "Quarterly review"
date: 2026-07-31 11:30 EDT
granola_id: not_jR3mNj7wjyXTJM
attendees:
  - dana@example-client.com
transcript_lines: 263
---

# Quarterly review

## Summary
...

## Transcript

[00:00] **Me:** Thanks for making the time.
[00:04] **Dana Smith:** Of course. Where do you want to start?
```

The file lands in a folder chosen from the meeting's attendees and title, so a call with `dana@example-client.com` goes to `clients/Example Client/transcripts/` without you doing anything.

## Install

```bash
uv tool install granola-router     # or: pipx install granola-router
```

Generate an API key in the Granola desktop app under **Settings → Connectors → API keys**, then:

```bash
mkdir -p ~/.granola-router
echo 'grn_your_key_here' > ~/.granola-router/api-key
chmod 600 ~/.granola-router/api-key
```

Point it at a destination and tell it which addresses are yours:

```json
// ~/.granola-router/settings.json
{
  "transcript_folder": "~/Documents/Meetings",
  "own_emails": ["you@example.com", "you@personal.example"],
  "own_name": "Me"
}
```

Then pull your history:

```bash
granola-router backfill --dry-run   # see where everything would go
granola-router backfill             # write it
```

## Where to put the files

The tool writes markdown files to a folder. That is the whole integration surface, so anything that reads files off disk works with no configuration on this side.

| Set `transcript_folder` to | You get |
|---|---|
| A folder in Dropbox, Google Drive, Box or OneDrive | Transcripts sync, and Claude reads them in the browser through that service's connector |
| A local folder | Claude Code and Claude Desktop read them directly |
| An Obsidian vault | Notes appear in the vault, backlinks and search included |
| A git repository | Version history, if you commit on a schedule |

### Which Claude can read them

Local file access differs by surface, and it decides where this is useful:

| Surface | Reads the transcripts | Notes |
|---|---|---|
| **Claude Code** | Yes | Reads and greps the folder directly |
| **Claude Cowork** | Yes | Local filesystem and Python, no command line needed |
| Claude Desktop | Only via an MCP filesystem server | Not available by default |
| claude.ai in the browser | No | Cannot reach your filesystem |

**Cowork is the easiest fit if you don't live in a terminal.** It reads local files and runs the tool without you needing the command line, so the skill in `skill/` works there the same way it does in Claude Code.

On the browser: claude.ai has connectors for Dropbox, Google Drive, Box and OneDrive, so a chat can reach a folder in one of those. But the Google Drive connector's file reader documents its supported types as Google Docs, Slides, Sheets, PDF, Office formats and images. Plain text and markdown are not on that list, and these transcripts are markdown. A browser chat may surface them in search without being able to read the contents. I have not verified the round trip, so treat browser access as untested rather than supported.

### Installing the skill

`skill/SKILL.md` teaches Claude to drive the tool in plain language, so you can ask "what did we decide with Dana" instead of remembering subcommands.

- **Claude Code:** copy the `skill/` folder into `~/.claude/skills/granola-router/`
- **Cowork:** add the markdown skill to your Cowork project

Triggers are conversational in both. Slash commands only exist in Claude Code.

### Two things to watch

**Keep the files downloaded, not online-only.** Google Drive's streaming mode and Dropbox's online-only setting leave placeholders on disk rather than real files. Anything reading them gets nothing. Mark the folder "Available offline".

**Run the poller on one machine.** The lock that stops two writers is per-machine. Two machines polling into the same synced folder will each write the same filenames, and your sync service resolves that by making conflicted copies. Run it in one place and let sync distribute the results.

**A shared folder shares the transcripts.** Filing client calls into a folder other people can see means they can read those calls. Worth a deliberate look at who has access before pointing this at a shared drive.

## Routing

Copy `routing-map.example.json` to `~/.granola-router/routing-map.json`. Rules are checked in this order, and the first confident match wins:

| Tier | Matches on | Use it for |
|---|---|---|
| `note_overrides` | a specific note id | one-off meetings no rule can reach |
| `title_overrides` | title keyword, optionally scoped to a domain | one company spanning several engagements |
| `email_domains` | attendee email domain | the normal case |
| `title_keywords` | title keyword | calls with no company domain on the invite |

Anything that doesn't match confidently goes to a quarantine folder rather than a client folder. `granola-router status` lists what landed there and why, so you can add a rule.

That matters more than it sounds. Roughly half of most people's meetings are one-to-ones with someone on a personal Gmail address, where nothing on the invite identifies a company. Guessing puts a client transcript in the wrong client's folder. Quarantining puts it somewhere visible.

Editing the routing map re-files existing transcripts on the next run. State records a fingerprint of the rules, so changing them invalidates what was written under the old ones.

## Commands

```
granola-router backfill [--since YYYY-MM-DD] [--limit N] [--dry-run]
granola-router poll [--once] [--interval 120]
granola-router status
granola-router domains          # attendee domains with no rule yet
```

`domains` is the fastest way to build a routing map: run it, see which companies you actually meet, add the ones you care about.

## Running it continuously

`poll` checks for new meetings on an interval. On macOS, run it under launchd:

```bash
granola-router poll --interval 120
```

A meeting appears once Granola has generated its summary and transcript, which is some minutes after the call ends rather than immediately. That's an API constraint, not a bug — the API only returns notes that have finished processing.

## Why paid only

Granola encrypted its local cache in 2026. Tools that read `cache-v6.json` stopped working, and the ones that tried to keep up have been retired by their authors:

- [`wassimk/granary`](https://github.com/wassimk/granary) — *"keeping up with Granola's cache format changes is a game of whack a mole that isn't worth the maintenance"*
- [`tomelliot/obsidian-granola-sync`](https://github.com/tomelliot/obsidian-granola-sync) — Granola 7.427.0 moved the data-encryption key into a Keychain item locked to Granola's own app. *"There is no workaround."*
- [`varadhjain/granola-claude-plugin`](https://github.com/varadhjain/granola-claude-plugin) — deprecated for the same reason

The official API is the only durable way to get this data out. It costs $14/month. This tool takes that trade deliberately rather than building on something that breaks with the next release.

## Notes on the API, in case you're building something similar

- `updated_after` and `created_after` accept `YYYY-MM-DDTHH:MM:SSZ` or a bare date. A `+00:00` offset or fractional seconds returns HTTP 400 — which is what `datetime.isoformat()` produces by default.
- The list endpoint returns no attendees. Routing needs list-then-fetch-detail, so budget two calls per note.
- One-to-one calls usually name the other speaker. Group calls collapse everyone but you into `them`, with no per-speaker labels.
- Rate limits are 25 requests per 5 seconds burst, 5/second sustained. This client stays under that and honours `Retry-After`.

## Design notes

A few decisions that aren't obvious:

**Filenames use the meeting's own timezone, not your machine's.** An evening US call on a laptop set to another zone would otherwise file under the next day, and the filename would change whenever you travelled.

**A meeting has exactly one file, and rewrites overwrite in place.** Two different meetings that share a date and title get distinguished by checking the existing file's `granola_id` — without that, one silently overwrites the other.

**`gmail.com` is not treated as "yours".** Only the exact addresses in `own_emails` are excluded. Clients use Gmail; excluding the whole domain silently discards real counterparties.

**One writer at a time.** A `flock` prevents a manual backfill and the background poller from both writing state. The kernel releases it if a process dies, so a killed run can't strand the lock.

## Development

```bash
uv sync
python -m pytest tests/ -q
```

## Licence

MIT
