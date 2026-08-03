"""MCP server, shipped as a Claude Desktop extension.

Two rules shape this file.

First, stdout carries JSON-RPC and nothing else. Every log goes to stderr, and
nothing reachable from a tool may print. That is why the launch-agent work lives
in service.py, which returns data instead of printing it.

Second, the tool surface is deliberately narrow. A model driving this should be
able to read transcripts, pull in new ones, and turn the background job on or
off. It should not be able to rewrite routing rules, delete transcripts, remove
state, or trigger an unbounded write of hundreds of files.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

try:  # MCP SDK 2.x
    from mcp.server.mcpserver import MCPServer as _Server
    from mcp.types import ToolAnnotations
except ImportError:  # 1.x fallback
    from mcp.server.fastmcp import FastMCP as _Server
    from mcp.types import ToolAnnotations

from . import service
from .api import DATA_DIR, GranolaAPIError, MissingAPIKey
from .sync import LockHeld, ProcessLock, STATE_FILE, Syncer, load_json, transcript_root
from .writer import existing_note_id

# stderr only. A stray byte on stdout breaks the transport.
logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("granola-router")

mcp = _Server("granola-router", version="1.0.0")

# A single sync must not run for minutes inside a tool call, and a model should
# never be able to rewrite the whole archive by accident.
MAX_SYNC_NOTES = 40
MAX_SEARCH_HITS = 25
SNIPPET_CHARS = 400


def _err(message: str, **extra: Any) -> Dict[str, Any]:
    return {"ok": False, "error": message, **extra}


def _state_notes() -> Dict[str, Any]:
    return (load_json(STATE_FILE, {"notes": {}}) or {}).get("notes", {}) or {}


def _saved_meetings() -> Dict[str, Dict[str, Any]]:
    """Every transcript we can see, keyed by note id.

    State is treated as a cache, not the source of truth. The files carry their
    own `granola_id` in front matter, so the archive is self-describing and can
    be rebuilt by reading it. Without this, losing state.json - a restore from
    backup, or deleting it to force a resync - makes an intact archive look
    empty, which is alarming and wrong.
    """
    found: Dict[str, Dict[str, Any]] = {}

    for nid, rec in _state_notes().items():
        if rec.get("status") == "written" and rec.get("output_path"):
            found[nid] = {
                "note_id": nid,
                "title": rec.get("title"),
                "path": rec["output_path"],
                "filed_because": rec.get("reason"),
                "outcome": rec.get("outcome"),
            }

    root = transcript_root()
    if not root.exists():
        return found
    for p in root.rglob("*.md"):
        try:
            nid = existing_note_id(p)
        except OSError:
            continue
        if not nid or nid in found:
            continue
        title = None
        try:
            for line in p.read_text(encoding="utf-8").splitlines()[:12]:
                if line.startswith("title:"):
                    title = line.split(":", 1)[1].strip().strip('"')
                    break
        except OSError:
            continue
        found[nid] = {
            "note_id": nid,
            "title": title or p.stem,
            "path": str(p),
            "filed_because": "found on disk (not in the local index)",
            "outcome": None,
        }
    return found


def _within_root(p: Path) -> bool:
    """Guard every path we hand back or read from."""
    try:
        p.resolve().relative_to(transcript_root().resolve())
        return True
    except (ValueError, OSError):
        return False


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
def granola_status() -> Dict[str, Any]:
    """Whether automatic filing is on, where transcripts are saved, and what is unfiled."""
    notes = _state_notes()
    agent = service.launch_agent_state()
    # "on" now requires a live process id and a fresh heartbeat, not merely a
    # plist that launchctl accepted. Reporting "on" for a job that loaded and
    # exited is how this shipped broken the first time.
    verdict = service.filing_status(agent)
    quarantined = [
        {"title": r.get("title"), "reason": r.get("reason"), "outcome": r.get("outcome")}
        for r in notes.values()
        if r.get("outcome") in ("no_attendees", "unknown", "ambiguous")
    ]
    return {
        "ok": True,
        "automatic_filing": verdict["status"],
        "automatic_filing_detail": verdict["reason"],
        "runs_without_claude": verdict["status"] in ("on", "starting"),
        "daemon_pid": agent.get("pid"),
        "last_check_seconds_ago": agent.get("heartbeat_age_seconds"),
        "transcript_folder": str(transcript_root()),
        "config_folder": str(DATA_DIR),
        "meetings_saved": len(_saved_meetings()),
        "unfiled_count": len(quarantined),
        "unfiled": quarantined[:20],
        "log": agent["logs"],
    }


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def granola_list_meetings(limit: int = 20) -> Dict[str, Any]:
    """List saved meetings, newest first, with the folder each was filed into."""
    limit = max(1, min(int(limit), 100))
    rows = [
        {"note_id": m["note_id"], "title": m["title"],
         "folder": str(Path(m["path"]).parent), "filed_because": m["filed_because"]}
        for m in _saved_meetings().values()
    ]
    rows.sort(key=lambda x: (Path(x["folder"]).name, x["title"] or ""))
    return {"ok": True, "count": len(rows), "meetings": rows[:limit]}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def granola_get_transcript(note_id: str, max_chars: int = 12000) -> Dict[str, Any]:
    """Read one saved transcript. Takes a note_id from granola_list_meetings."""
    rec = _saved_meetings().get(note_id)
    if not rec:
        return _err("No saved meeting with that id. Call granola_list_meetings first.")
    p = Path(rec["path"])
    # Only ever read inside the transcript folder, and only a file this note owns.
    if not _within_root(p) or not p.exists():
        return _err("That transcript is no longer on disk.")
    if existing_note_id(p) not in (None, note_id):
        return _err("That file belongs to a different meeting.")
    text = p.read_text(encoding="utf-8")
    return {
        "ok": True, "note_id": note_id, "title": rec.get("title"), "path": str(p),
        "truncated": len(text) > max_chars, "content": text[:max_chars],
    }


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def granola_search(query: str, limit: int = 10) -> Dict[str, Any]:
    """Find saved meetings whose transcript mentions a phrase."""
    q = (query or "").strip().lower()
    if len(q) < 3:
        return _err("Give me at least three characters to search for.")
    limit = max(1, min(int(limit), MAX_SEARCH_HITS))

    hits: List[Dict[str, Any]] = []
    for nid, rec in _saved_meetings().items():
        p = Path(rec["path"])
        if not _within_root(p) or not p.exists():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        i = text.lower().find(q)
        if i == -1:
            continue
        start = max(0, i - SNIPPET_CHARS // 2)
        hits.append({
            "note_id": nid, "title": rec.get("title"), "path": str(p),
            "snippet": text[start:start + SNIPPET_CHARS].strip(),
        })
        if len(hits) >= limit:
            break
    return {"ok": True, "query": query, "count": len(hits), "matches": hits}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, openWorldHint=True))
def granola_sync_now() -> Dict[str, Any]:
    """Fetch meetings that finished processing since the last run and file them.

    Incremental and bounded. This does not rewrite the archive; use the command
    line if you need a full backfill.
    """
    try:
        # Lock before constructing: Syncer reads state in __init__, and the
        # background job may be writing it right now.
        with ProcessLock(wait_seconds=60):
            syncer = Syncer()
            stats = syncer.poll_once()
    except LockHeld:
        return _err("The background job is syncing right now. Try again shortly.")
    except MissingAPIKey as exc:
        return _err(str(exc))
    except GranolaAPIError as exc:
        return _err(f"Granola's API could not be reached: {exc}")

    return {
        "ok": True, "saved": stats.written, "already_current": stats.unchanged,
        "unfiled": stats.quarantined, "still_processing": stats.no_transcript,
        "errors": stats.errors,
        "note": ("A meeting shows up a few minutes after the call, once Granola "
                 "has finished writing its summary."),
    }


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True))
def granola_enable_always_on(interval_seconds: int = 120) -> Dict[str, Any]:
    """Turn on automatic filing, so meetings are saved even when Claude is closed."""
    interval = max(60, min(int(interval_seconds), 3600))
    # install_launch_agent waits for a live process id before returning ok, so
    # this cannot report success for a job that exited on startup.
    r = service.install_launch_agent(interval=interval)
    if not r.get("ok"):
        return _err(r.get("error", "could not turn on automatic filing"),
                    state=r.get("state"))
    return {
        "ok": True, "interval_seconds": interval,
        "upgraded_binary": r.get("upgraded", False),
        "daemon_pid": r.get("pid"),
        "message": ("Automatic filing is on and confirmed running. It starts by "
                    "itself when you log in and keeps running with Claude closed. "
                    "Removing this extension will NOT stop it; turn it off here "
                    "first, or run 'granola-router uninstall' from Terminal."),
    }


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def granola_disable_always_on() -> Dict[str, Any]:
    """Turn off automatic filing. Transcripts already saved are left alone."""
    r = service.uninstall_launch_agent()
    if not r.get("was_installed"):
        return {"ok": True, "message": "Automatic filing was not on."}
    return {"ok": True, "message": ("Automatic filing is off and will not restart "
                                    "at login. Saved transcripts are untouched.")}


def main() -> None:
    logger.info("granola-router MCP server starting (config: %s)", DATA_DIR)
    mcp.run()


if __name__ == "__main__":
    main()
