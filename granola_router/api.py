"""Client for Granola's official public API.

Replaces the local cache reader, which stopped working when Granola encrypted
cache-v6.json in May 2026. Requires a Granola Business plan API key.

Docs: https://docs.granola.ai/api-reference/list-notes
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

logger = logging.getLogger(__name__)

BASE_URL = "https://public-api.granola.ai/v1"
def _data_dir() -> Path:
    """Config location, preferring ~/.granola-router.

    Falls back to ~/.granola-saver when that exists and the new directory does
    not, so an install predating the rename keeps working untouched.
    """
    new = Path.home() / ".granola-router"
    legacy = Path.home() / ".granola-saver"
    if not new.exists() and legacy.exists():
        return legacy
    return new


DATA_DIR = _data_dir()
API_KEY_FILE = DATA_DIR / "api-key"

# Published limits are burst 25/5s and sustained 5/s. Stay meaningfully under
# the sustained ceiling so a backfill never trips 429 on a shared connection.
MIN_INTERVAL_SECONDS = 0.30
MAX_PAGE_SIZE = 30  # API caps page_size at 30


def to_api_datetime(value: Optional[str]) -> Optional[str]:
    """Coerce a timestamp into the only datetime format the API accepts.

    Verified against the live API: it takes `YYYY-MM-DDTHH:MM:SSZ` or a bare
    `YYYY-MM-DD`, and returns HTTP 400 for a `+00:00` offset or for fractional
    seconds - both of which datetime.isoformat() produces by default.
    """
    if not value:
        return None
    text = str(value).strip()
    if len(text) == 10 and text.count("-") == 2:
        return text  # bare date, accepted as-is
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("Unparseable datetime %r; sending unchanged", text)
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class GranolaAPIError(RuntimeError):
    """Raised when the API cannot be reached or returns an unusable response."""


class MissingAPIKey(GranolaAPIError):
    """Raised when no API key is configured."""


def load_api_key(explicit: Optional[str] = None) -> str:
    """Resolve the API key from an argument, the environment, or the key file.

    Raises:
        MissingAPIKey: If no key is found anywhere.
    """
    if explicit:
        return explicit.strip()

    env = os.environ.get("GRANOLA_API_KEY")
    if env and env.strip():
        return env.strip()

    if API_KEY_FILE.exists():
        key = API_KEY_FILE.read_text(encoding="utf-8").strip()
        if key:
            return key

    raise MissingAPIKey(
        f"No Granola API key. Put it in {API_KEY_FILE} or set GRANOLA_API_KEY. "
        "Generate one in Granola: Settings > Connectors > API keys "
        "(requires a Business plan)."
    )


@dataclass
class Meeting:
    """A Granola note, normalized for routing and rendering.

    Mirrors the shape the legacy cache extractor produced so downstream code
    stays comparable, but sourced entirely from the public API.
    """

    id: str
    title: str
    created_at: str
    updated_at: str
    attendee_emails: List[str] = field(default_factory=list)
    attendee_names: List[str] = field(default_factory=list)
    organiser: Optional[str] = None
    web_url: Optional[str] = None
    scheduled_start: Optional[str] = None
    scheduled_end: Optional[str] = None
    summary_markdown: Optional[str] = None
    transcript_entries: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def has_transcript(self) -> bool:
        return bool(self.transcript_entries)


def _emails_from_note(note: Dict[str, Any]) -> tuple[List[str], List[str]]:
    """Collect every email/name pair the note exposes.

    The API surfaces participants in three places and they do not always agree,
    so all three are merged: `attendees`, `calendar_event.invitees`, and
    `calendar_event.organiser`. Only `attendees` carries names.
    """
    pairs: Dict[str, str] = {}

    for att in note.get("attendees") or []:
        email = (att.get("email") or "").strip().lower()
        if email:
            pairs.setdefault(email, att.get("name") or "")

    cal = note.get("calendar_event") or {}
    for inv in cal.get("invitees") or []:
        email = (inv.get("email") or "").strip().lower()
        if email:
            pairs.setdefault(email, "")

    organiser = (cal.get("organiser") or "").strip().lower()
    if organiser:
        pairs.setdefault(organiser, "")

    emails = list(pairs.keys())
    return emails, [pairs[e] for e in emails]


def note_to_meeting(note: Dict[str, Any]) -> Meeting:
    """Convert a raw API note object into a Meeting."""
    emails, names = _emails_from_note(note)
    cal = note.get("calendar_event") or {}
    transcript = note.get("transcript") or []
    if not isinstance(transcript, list):
        transcript = []

    return Meeting(
        id=note.get("id", ""),
        title=(note.get("title") or "Untitled Meeting").strip() or "Untitled Meeting",
        created_at=note.get("created_at") or "",
        updated_at=note.get("updated_at") or "",
        attendee_emails=emails,
        attendee_names=names,
        organiser=(cal.get("organiser") or None),
        web_url=note.get("web_url"),
        scheduled_start=cal.get("scheduled_start_time"),
        scheduled_end=cal.get("scheduled_end_time"),
        summary_markdown=note.get("summary_markdown") or note.get("summary_text"),
        transcript_entries=transcript,
    )


class GranolaAPI:
    """Rate-limited, retrying client for the Granola public API."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = BASE_URL,
        min_interval: float = MIN_INTERVAL_SECONDS,
        max_retries: int = 4,
        opener: Optional[Any] = None,
    ) -> None:
        self._key = load_api_key(api_key)
        self._base = base_url.rstrip("/")
        self._min_interval = min_interval
        self._max_retries = max_retries
        self._last_call = 0.0
        # Injectable for tests; defaults to urllib.
        self._opener = opener or urllib.request.urlopen

    # -- transport -------------------------------------------------------

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_call = time.monotonic()

    def _request(self, path: str) -> Optional[Dict[str, Any]]:
        """GET a path, honoring rate limits and retrying transient failures.

        Returns None for 404 (note deleted or never summarized). Raises
        GranolaAPIError when retries are exhausted or auth fails.
        """
        url = f"{self._base}{path}"
        last_exc: Optional[Exception] = None

        for attempt in range(self._max_retries):
            self._throttle()
            req = urllib.request.Request(
                url, headers={"Authorization": f"Bearer {self._key}"}
            )
            try:
                with self._opener(req, timeout=60) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    logger.debug("404 for %s (no summary/transcript yet)", path)
                    return None
                if exc.code in (401, 403):
                    raise GranolaAPIError(
                        f"Granola rejected the API key ({exc.code}). "
                        "Confirm the key is current and the plan includes API access."
                    ) from exc
                if exc.code == 429:
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    delay = float(retry_after) if retry_after else 2.0 * (attempt + 1)
                    logger.warning("Rate limited, sleeping %.1fs", delay)
                    time.sleep(delay)
                    last_exc = exc
                    continue
                if 500 <= exc.code < 600:
                    time.sleep(1.0 + attempt)
                    last_exc = exc
                    continue
                raise GranolaAPIError(f"HTTP {exc.code} for {path}") from exc
            except Exception as exc:  # network hiccups, timeouts, bad JSON
                last_exc = exc
                time.sleep(1.0 + attempt)

        raise GranolaAPIError(f"Giving up on {path} after {self._max_retries} tries: {last_exc}")

    # -- endpoints -------------------------------------------------------

    def iter_notes(
        self,
        updated_after: Optional[str] = None,
        created_after: Optional[str] = None,
        page_size: int = MAX_PAGE_SIZE,
    ) -> Iterator[Dict[str, Any]]:
        """Yield note summaries, following cursor pagination to exhaustion.

        The list endpoint returns only id/title/owner/timestamps. Attendees and
        transcripts require get_note().
        """
        cursor: Optional[str] = None
        seen = 0
        while True:
            params: Dict[str, str] = {"page_size": str(min(page_size, MAX_PAGE_SIZE))}
            if cursor:
                params["cursor"] = cursor
            normalized_updated = to_api_datetime(updated_after)
            if normalized_updated:
                params["updated_after"] = normalized_updated
            normalized_created = to_api_datetime(created_after)
            if normalized_created:
                params["created_after"] = normalized_created

            payload = self._request("/notes?" + urllib.parse.urlencode(params))
            if not payload:
                return

            batch = payload.get("notes") or []
            for note in batch:
                seen += 1
                yield note

            if not payload.get("hasMore"):
                logger.debug("Listed %d notes", seen)
                return
            cursor = payload.get("cursor")
            if not cursor:
                logger.warning("hasMore was true but no cursor returned; stopping")
                return

    def get_note(self, note_id: str, with_transcript: bool = True) -> Optional[Dict[str, Any]]:
        """Fetch one note in full. Returns None if the note is not retrievable."""
        suffix = "?include=transcript" if with_transcript else ""
        return self._request(f"/notes/{note_id}{suffix}")

    def get_meeting(self, note_id: str, with_transcript: bool = True) -> Optional[Meeting]:
        """Fetch one note and normalize it to a Meeting."""
        note = self.get_note(note_id, with_transcript=with_transcript)
        return note_to_meeting(note) if note else None

    def whoami(self) -> Optional[str]:
        """Best-effort owner email, read off the first available note.

        Used to identify the account holder so routing can exclude their own
        address without blanket-excluding a public domain like gmail.com.
        """
        for note in self.iter_notes(page_size=1):
            owner = (note.get("owner") or {}).get("email")
            return owner.strip().lower() if owner else None
        return None
