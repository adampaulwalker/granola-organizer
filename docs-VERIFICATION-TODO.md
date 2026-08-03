# Verification prompt for granola-router

Paste this into a fresh session.

---

granola-router shipped today and is publicly downloadable, but several claims on
the landing page have never been executed. Read
`~/.claude/projects/-Users-adam-Library-CloudStorage-Dropbox-Atlantic-Labs/memory/granola_router_state.md`
first, then close the gaps below in order. Stop and report if any step fails
rather than working around it.

Repo: `~/code/granola-router`. Site source: `~/code/atlantic-labs-ai`.
Page: https://atlanticlabs.ai/granola-router/

Before touching anything, protect the live setup: Adam's real config is
`~/.granola-saver/` and his real transcripts are in Dropbox. Use
`GRANOLA_ROUTER_HOME` to point at a scratch config for every test. Never run
`granola_sync_now` or `backfill` against the real config while testing - a
previous run pulled 16 real client meetings into a demo folder.

## 1. Install the extension into Claude Desktop, for real

This is the headline claim and nobody has ever done it.

Download from https://atlanticlabs.ai/downloads/granola-router.mcpb using a
browser, so the file carries a real quarantine attribute. Then:

- Does macOS block it? Capture the exact wording of the dialog.
- Does the documented workaround work: right-click Open, and
  `xattr -d com.apple.quarantine`?
- Does Claude Desktop list it under Settings, and do all seven tools appear?
- Ask Claude "are my meetings being saved?" and confirm `granola_status` returns.

If the Gatekeeper wording differs from what the page says, fix the page.

## 2. Exercise enable/disable for real

`granola_enable_always_on` and `granola_disable_always_on` have only ever run
against mocked `subprocess` calls. From Claude Desktop, with a scratch
`GRANOLA_ROUTER_HOME`:

- Enable it. Confirm a launch agent named `com.granola-router.poll` exists and is
  loaded (`launchctl list | grep granola-router`).
- Confirm the binary it points at is under `~/.granola-router/bin/versions/<hash>/`
  and NOT inside Claude's extension directory. This is the failure that killed
  the previous daemon.
- Disable it. Confirm the agent is gone and saved files are untouched.

## 3. Prove the upgrade path

The versioned-binary scheme is unit-tested with fake files only. Rebuild the
extension (`./build-extension.sh` produces a new hash), install the new version,
enable again, and confirm `current` repoints to the new hash and launchd runs the
new binary. Without this, an upgrade silently leaves users on the old build.

## 4. Watch one real meeting file itself

The backfill is proven; the live loop is not. Record a short Granola call, wait
for Granola to generate the summary, and confirm the background job picks it up
and files it. Time how long it actually takes, and correct the page if "a few
minutes" is wrong.

## 5. Reboot

Adam's Mac has 53+ days uptime, so autostart is inferred, not observed. After a
restart, `granola-router status` should report automatic filing ON with no
intervention.

## 6. Settle two open questions

- Does the claude.ai **Dropbox** connector read a markdown file? The Google Drive
  connector's reader does not list text/markdown. Put a transcript in Dropbox and
  ask claude.ai to read it. Update the README either way.
- Does **Cowork** accept a `.mcpb`? Adam says yes from experience; his
  `claude_runtime_surfaces` memory says web is hosted-MCP-only. Resolve it and
  correct whichever is wrong.

## 7. Housekeeping

- `~/code/atlantic-labs-ai` has uncommitted changes including a 20MB binary.
  Commit, and decide whether the binary belongs in git history or should be
  gitignored and uploaded at deploy time.
- Private repo `adampaulwalker/granola-saver` PR #4 is still open.
- Gemini's API key is reported leaked ("Your API key was reported as leaked"),
  which breaks every `multi-llm model="all"` call. Rotate or remove it.

## Ground rules

Run `python3 -m pytest tests/` in the repo before and after any change; 49 pass
and 1 skips on system python by design. Use Codex for review before writing code.
Anything you fix on the landing page needs redeploying with
`wrangler pages deploy . --project-name atlantic-labs` from `~/code/atlantic-labs-ai`
- and check the whole site still serves afterwards, because a Pages upload
replaces the entire deployment.
