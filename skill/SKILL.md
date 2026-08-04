---
name: granola-organizer
description: Save Granola meeting transcripts to markdown and file them into per-client folders. Use for finding what was said in a past meeting, checking whether meetings are being saved, turning automatic saving on or off, and diagnosing why a meeting was not filed. Triggers on "what did we decide in the call with", "where is the transcript from", "save my meetings", "sync my granola notes", "why is this meeting unrouted", "turn on automatic saving".
---

# granola-organizer

Meeting transcripts are pulled from Granola's API and written as markdown, each
one filed into a client folder chosen from the attendees and the title.

## Work out which interface you have, first

This ships in two places with different capabilities. Check before doing anything.

**If `granola_*` MCP tools are available** (Claude Desktop extension), use only
those. There is no shell and no `granola-organizer` command. Never suggest
installing a command-line tool unless the person asks for it.

**If Bash is available and `granola-organizer` runs**, use the command line.

**If neither**, say so plainly and stop. In a shell environment where the
command is missing, the fix is one line:

```bash
uv tool install git+https://github.com/adampaulwalker/granola-organizer
```

Do not go looking for a similar tool elsewhere on the machine. A confident
answer about the wrong tool is worse than no answer.

## What reads and what writes

Check this before every action. Getting it wrong pulls real meeting data onto
disk during what someone thought was a look around.

| | Reads only | Writes files | Changes background filing |
|---|---|---|---|
| MCP | `granola_status`, `granola_list_meetings`, `granola_get_transcript`, `granola_search` | `granola_sync_now` | `granola_enable_always_on`, `granola_disable_always_on` |
| CLI | `granola-organizer status`, `granola-organizer domains`, `granola-organizer backfill --dry-run` | `granola-organizer poll --once`, `granola-organizer backfill` | `granola-organizer install`, `granola-organizer uninstall` |

Never run anything in the right-hand columns while inspecting, diagnosing,
searching, or setting up a demo. Only when the person has actually asked to
sync, backfill, or change automatic filing.

`granola_sync_now` and `granola-organizer backfill` reach Granola's API and write
to disk. Say what will happen and where before running either. On the command
line, offer `granola-organizer backfill --dry-run` first; the MCP tool has no
preview, so ask instead.

## Who does the filing

A background job does, installed on the machine and run by the operating
system. Not this skill, and not Claude. It keeps filing with Claude closed, and
turning it on is a one-off:

- MCP: `granola_enable_always_on` / `granola_disable_always_on`
- CLI: `granola-organizer install` / `granola-organizer uninstall`
- Check the current state with `granola_status` or `granola-organizer status`, whichever you have

Removing the Claude Desktop extension does **not** stop the background job,
because the job is deliberately independent of Claude. Turn it off first, or run
`granola-organizer uninstall` from a terminal afterwards.

A meeting appears a few minutes after the call, not immediately. Granola has to
finish writing its summary before the API will return it. If something is
missing, say that rather than reporting it as lost.

## Finding what was said

**MCP:** `granola_search` for a phrase, then `granola_get_transcript` with the
`note_id` it returns. Do not pass a path or a URL; the id comes from
`granola_search` or `granola_list_meetings`.

**CLI:** the transcript folder is the `transcript_folder` value in `granola-organizer status`. Files are markdown,
so search them directly rather than calling the API.

Each file carries front matter with the title, date, attendees and `granola_id`,
then the summary, then the full timestamped transcript. Quote the transcript
when asked what somebody said, rather than paraphrasing it.

## When a meeting was not filed

Unfiled meetings sit in a quarantine folder. `granola_status` and `granola-organizer status` both list them with a reason:

| Reason | Meaning | What fixes it |
|---|---|---|
| `no_attendees` | Only the account holder was on the invite | A `title_keywords` rule |
| `unknown` | External attendees, but no rule matched their domain | An `email_domains` rule |
| `ambiguous` | Two rules disagreed | A scoped `title_overrides` rule |

This is deliberate. When nothing matches, the tool quarantines rather than
guessing, because a transcript in the wrong client's folder is worse than one
that is visibly unsorted. Never infer a client from weak evidence.

## Changing the routing rules

Rules live in `routing-map.json` in the config folder, which status reports as `config_folder`.
Precedence runs `note_overrides`, `title_overrides`, `email_domains`,
`title_keywords`.

Use `email_domains` when the client has its own domain. Use `title_keywords`
when they are on a personal address but the title names them. Use
`note_overrides`, keyed by the meeting's `granola_id`, for a single meeting no
rule can reach.

**CLI:** read the map, confirm the client and folder with the person, then edit
it. Editing changes configuration immediately but moves nothing on its own;
files are re-filed by the next background run, or by `granola-organizer backfill`.
Preview with `granola-organizer backfill --dry-run` before writing.

**MCP:** there is no tool for editing rules. Work out which rule is needed and
tell the person exactly what to add and where. Do not claim to have changed
anything.

`granola-organizer domains` lists attendee domains with no rule yet, which is the
quickest way to see what is worth adding. There is no MCP equivalent; from
Desktop, infer it from `granola_list_meetings` and say that is what you did.

## Things to get right

Never invent a routing rule for a client the person has not confirmed.

Never report a meeting as filed unless a tool result says so.

If asked where to keep transcripts: any folder works. A folder inside Dropbox,
Google Drive, Box or OneDrive syncs, and Claude Code and Cowork read local files
directly. Claude in the browser cannot reach the filesystem.
