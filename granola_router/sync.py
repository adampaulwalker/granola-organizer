"""Backfill and incremental sync of Granola notes to routed markdown files.

State lives in the config directory (see api._data_dir). It is keyed by the API's note id
(`not_...`), which is NOT the same as the UUID keys the old cache-based state
used, so v3 starts from a fresh file rather than misreading the old one.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .api import DATA_DIR, GranolaAPI, Meeting, note_to_meeting
from .routing import ROUTING_LOGIC_VERSION, Outcome, Router, RoutingDecision
from .writer import content_hash, existing_note_id, render, resolve_path, write_atomic

logger = logging.getLogger(__name__)

STATE_FILE = DATA_DIR / "state.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
ROUTING_FILE = DATA_DIR / "routing-map.json"
LOCK_FILE = DATA_DIR / "sync.lock"

# Notes become API-visible only once Granola generates the summary, which can
# lag the meeting by an unknown amount. Re-examine a trailing window on every
# poll instead of trusting a high-water mark alone.
LOOKBACK = timedelta(days=7)


class LockHeld(RuntimeError):
    """Another sync process owns the lock."""


class ProcessLock:
    """Cross-process lock so the daemon and a manual run cannot both write state.

    Uses fcntl.flock rather than an O_EXCL sentinel file. The kernel releases a
    flock when the holding process dies, so a killed backfill cannot strand the
    lock, and there is no stale-timeout heuristic that could let two writers run
    concurrently or make one process delete another's lock file.
    """

    def __init__(self, path: Path = LOCK_FILE, wait_seconds: float = 0.0) -> None:
        self._path = path
        self._wait = wait_seconds
        self._fh: Optional[Any] = None

    def __enter__(self) -> "ProcessLock":
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Opened 'a+' so an existing holder's pid stays readable for the message.
        self._fh = open(self._path, "a+", encoding="utf-8")

        # A manual run should not simply fail because the background poller
        # happened to be mid-cycle. A poll takes seconds, so waiting briefly
        # nearly always succeeds; the poller itself waits 0 and skips its turn.
        deadline = time.monotonic() + self._wait
        while True:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    holder = ""
                    try:
                        self._fh.seek(0)
                        holder = self._fh.read().strip()
                    except OSError:
                        pass
                    self._fh.close()
                    self._fh = None
                    raise LockHeld(
                        f"Another granola-router sync is running (pid {holder or 'unknown'}). "
                        "If that is the background poller, pause it with: "
                        "launchctl unload ~/Library/LaunchAgents/com.granola-router.poll.plist"
                    ) from exc
                logger.info("Waiting for the running sync to finish...")
                time.sleep(2)

        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(str(os.getpid()))
        self._fh.flush()
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._fh is None:
            return
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None


@dataclass
class SyncStats:
    written: int = 0
    unchanged: int = 0
    quarantined: int = 0
    no_transcript: int = 0
    errors: int = 0
    examined: int = 0
    by_outcome: Dict[str, int] = field(default_factory=dict)

    def render(self) -> str:
        parts = [
            f"examined={self.examined}",
            f"written={self.written}",
            f"unchanged={self.unchanged}",
            f"quarantined={self.quarantined}",
            f"no_transcript={self.no_transcript}",
            f"errors={self.errors}",
        ]
        if self.by_outcome:
            detail = ", ".join(f"{k}={v}" for k, v in sorted(self.by_outcome.items()))
            parts.append(f"[{detail}]")
        return " ".join(parts)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Could not read %s: %s", path, exc)
        return default


def save_state(state: Dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def transcript_root() -> Path:
    settings = load_json(SETTINGS_FILE, {})
    raw = settings.get("transcript_folder") or str(Path.home() / "Granola Transcripts")
    return Path(raw).expanduser()


class Syncer:
    """Pulls notes from the API, routes them, and writes markdown."""

    def __init__(
        self,
        api: Optional[GranolaAPI] = None,
        root: Optional[Path] = None,
        routing_map: Optional[Dict[str, Any]] = None,
        own_emails: Optional[Iterable[str]] = None,
        dry_run: bool = False,
    ) -> None:
        self.api = api or GranolaAPI()
        self.root = Path(root) if root else transcript_root()
        self.dry_run = dry_run
        rmap = routing_map if routing_map is not None else load_json(ROUTING_FILE, {})
        # Fingerprint of the rules AND the routing code version. When either
        # changes, previously written notes are re-routed instead of being
        # skipped as "unchanged". Including the logic version matters: a stale
        # process running older code with a newer map would otherwise stamp the
        # new fingerprint while ignoring the new behaviour, permanently marking
        # those notes as up to date.
        self.routing_version = hashlib.sha256(
            (json.dumps(rmap, sort_keys=True) + f"|logic={ROUTING_LOGIC_VERSION}")
            .encode("utf-8")
        ).hexdigest()[:12]
        owners = list(own_emails) if own_emails is not None else self._discover_owner()
        self.router = Router(rmap, self.root, own_emails=owners)
        # Label for the account holder's own transcript lines.
        self.own_name = str(load_json(SETTINGS_FILE, {}).get("own_name") or "Me")
        self.state: Dict[str, Any] = load_json(STATE_FILE, {"notes": {}, "watermark": None})
        self.state.setdefault("notes", {})

    def _discover_owner(self) -> List[str]:
        """Addresses belonging to the account holder, so they are not read as clients.

        Configured `own_emails` win. The API is only consulted as a fallback:
        the list endpoint reports each note's owner, which is not necessarily
        the caller, so trusting it blindly can exclude the wrong person.
        """
        configured = [
            str(e).strip().lower()
            for e in (load_json(SETTINGS_FILE, {}).get("own_emails") or [])
            if str(e).strip()
        ]
        if configured:
            return configured

        logger.info("No own_emails configured; falling back to the API note owner")
        try:
            who = self.api.whoami()
            return [who] if who else []
        except Exception as exc:
            logger.warning("Could not determine account owner: %s", exc)
            return []

    # -- core ------------------------------------------------------------

    def sync_note(self, summary: Dict[str, Any], stats: SyncStats) -> None:
        """Fetch, route, and persist one note."""
        note_id = summary.get("id")
        if not note_id:
            return
        stats.examined += 1

        prior = self.state["notes"].get(note_id) or {}
        remote_updated = summary.get("updated_at") or ""
        # Skip only when everything that determines the output is unchanged:
        # the upstream note, the routing rules, and the file actually on disk.
        # Comparing the file's real content (not just its existence) means a
        # manual edit, a truncated write, or a clobbered file gets repaired.
        if (
            prior.get("status") == "written"
            and prior.get("source_updated_at") == remote_updated
            and prior.get("routing_version") == self.routing_version
            and prior.get("output_path")
            and self._file_matches(Path(prior["output_path"]), note_id, prior.get("content_hash"))
        ):
            stats.unchanged += 1
            return

        try:
            raw = self.api.get_note(note_id, with_transcript=True)
        except Exception as exc:
            logger.error("Fetch failed for %s: %s", note_id, exc)
            stats.errors += 1
            # Must be recorded, or the note is invisible to _retry_ids() and a
            # transient network failure silently drops it forever.
            self._record(
                note_id,
                {
                    "status": "error",
                    "source_updated_at": remote_updated,
                    "error": str(exc)[:200],
                    "title": summary.get("title") or "",
                },
            )
            return
        if raw is None:
            stats.no_transcript += 1
            self._record(note_id, {"status": "unavailable", "source_updated_at": remote_updated})
            return

        meeting = note_to_meeting(raw)
        if not meeting.has_transcript:
            stats.no_transcript += 1
            self._record(
                note_id,
                {"status": "no_transcript", "source_updated_at": remote_updated,
                 "title": meeting.title},
            )
            return

        decision = self.router.route(meeting.title, meeting.attendee_emails, note_id)
        stats.by_outcome[decision.outcome.value] = (
            stats.by_outcome.get(decision.outcome.value, 0) + 1
        )
        if decision.quarantined:
            stats.quarantined += 1

        text = render(meeting, own_name=self.own_name)
        digest = content_hash(text)
        path = resolve_path(Path(decision.folder), meeting)

        record = {
            "status": "written",
            "source_updated_at": remote_updated,
            "routing_version": self.routing_version,
            "content_hash": digest,
            "output_path": str(path),
            "outcome": decision.outcome.value,
            "reason": decision.reason,
            "title": meeting.title,
        }

        if self._file_matches(path, note_id, digest):
            stats.unchanged += 1
            self._record(note_id, record)
            return

        if self.dry_run:
            logger.info("[dry-run] would write %s (%s)", path, decision.reason)
        else:
            write_atomic(path, text)
            self._retire_old_copy(prior.get("output_path"), str(path), note_id)

        stats.written += 1
        self._record(note_id, record)

    # -- safety helpers --------------------------------------------------

    def _file_matches(self, path: Path, note_id: str, digest: Optional[str]) -> bool:
        """True when `path` already holds exactly this note's current content."""
        if not digest or not path.exists():
            return False
        try:
            actual = path.read_text(encoding="utf-8")
        except OSError:
            return False
        if content_hash(actual) != digest:
            return False
        owner = existing_note_id(path)
        return owner is None or owner == note_id

    def _retire_old_copy(self, old: Optional[str], new: str, note_id: str) -> None:
        """Delete a note's previous file after it moves folders.

        Deliberately conservative. A stale path is only removed once it is
        proven to belong to this note and to sit inside the transcript root,
        because a same-date same-title file could legitimately belong to a
        different meeting.
        """
        if not old or old == new:
            return
        old_path = Path(old)
        if not old_path.exists():
            return
        try:
            old_path.resolve().relative_to(self.root.resolve())
        except ValueError:
            logger.warning("Refusing to remove %s: outside transcript root", old)
            return
        owner = existing_note_id(old_path)
        if owner != note_id:
            logger.warning(
                "Refusing to remove %s: belongs to %s, not %s", old, owner, note_id
            )
            return
        if any(
            rec.get("output_path") == old
            for nid, rec in self.state["notes"].items()
            if nid != note_id
        ):
            logger.warning("Refusing to remove %s: another note claims it", old)
            return
        try:
            old_path.unlink()
            logger.info("Removed superseded copy at %s", old)
        except OSError as exc:
            logger.warning("Could not remove old copy %s: %s", old, exc)

    def _record(self, note_id: str, payload: Dict[str, Any]) -> None:
        if not self.dry_run:
            self.state["notes"][note_id] = payload

    # -- entry points ----------------------------------------------------

    def backfill(self, since: Optional[str] = None, limit: Optional[int] = None) -> SyncStats:
        """Walk the full note history (optionally from a date) and write everything."""
        stats = SyncStats()
        count = 0
        for summary in self.api.iter_notes(created_after=since):
            self.sync_note(summary, stats)
            count += 1
            if limit and count >= limit:
                break
            if count % 25 == 0:
                logger.info("progress: %s", stats.render())
                if not self.dry_run:
                    save_state(self.state)
        if not self.dry_run:
            self.state["watermark"] = datetime.now(timezone.utc).isoformat()
            save_state(self.state)
        return stats

    def _poll_since(self) -> str:
        """Start of the incremental window.

        Anchored to the last successful run minus an overlap, never to `now`.
        Anchoring to now would silently skip everything that changed while the
        daemon was down for longer than the lookback.
        """
        watermark = self.state.get("watermark")
        if watermark:
            try:
                anchor = datetime.fromisoformat(watermark)
                if anchor.tzinfo is None:
                    anchor = anchor.replace(tzinfo=timezone.utc)
                return (anchor - LOOKBACK).isoformat()
            except ValueError:
                logger.warning("Unparseable watermark %r; using default window", watermark)
        return (datetime.now(timezone.utc) - LOOKBACK).isoformat()

    def _retry_ids(self) -> List[str]:
        """Notes previously unwritable, retried regardless of the time window.

        A meeting whose transcript was still generating returns no transcript
        and would otherwise fall out of the window before it ever succeeds.
        """
        return [
            nid
            for nid, rec in self.state["notes"].items()
            if rec.get("status") in ("no_transcript", "unavailable", "error")
        ]

    def poll_once(self) -> SyncStats:
        """Incremental pass: the window since the last run, plus pending retries."""
        stats = SyncStats()
        started = datetime.now(timezone.utc)
        seen: set = set()

        for summary in self.api.iter_notes(updated_after=self._poll_since()):
            seen.add(summary.get("id"))
            self.sync_note(summary, stats)

        for note_id in self._retry_ids():
            if note_id in seen:
                continue
            prior = self.state["notes"].get(note_id) or {}
            self.sync_note(
                {"id": note_id, "updated_at": prior.get("source_updated_at") or ""},
                stats,
            )

        if not self.dry_run:
            # Stamp the time the pass STARTED, so anything modified mid-run is
            # still inside the next window rather than falling through the gap.
            self.state["watermark"] = started.isoformat()
            save_state(self.state)
        return stats
