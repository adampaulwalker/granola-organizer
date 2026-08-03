"""Renders meetings to markdown and writes them atomically.

The legacy exporter appended a numeric suffix whenever it saw a meeting again,
which is how one 17KB meeting became 263 identical files. Here a meeting has
exactly one path, derived deterministically from its date and title, and a
rewrite overwrites in place. Content hashing means an unchanged note is not
rewritten at all.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .api import Meeting

logger = logging.getLogger(__name__)

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
_MAX_SLUG = 60

# Label used for the account holder's own lines in a transcript.
# Override per call, or set "own_name" in settings.json.
DEFAULT_OWN_NAME = "Me"


def _parse(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def slugify(text: str) -> str:
    """Lowercase, hyphenated, filesystem-safe form of a title."""
    slug = _SLUG_STRIP.sub("-", (text or "").lower()).strip("-")
    return (slug[:_MAX_SLUG].rstrip("-")) or "untitled"


def meeting_local_time(meeting: Meeting) -> Optional[datetime]:
    """The meeting's start in the timezone it was actually scheduled in.

    Deliberately NOT the machine's timezone. This laptop is often set to a
    different zone than the one meetings are booked in, and converting an
    evening US call into, say, IST rolls it onto the following day - which
    would put the transcript under the wrong date and rename the file every
    time the laptop moved. The offset carried in scheduled_start_time is
    stable, so the date stays put.
    """
    scheduled = _parse(meeting.scheduled_start)
    if scheduled is not None:
        return scheduled
    created = _parse(meeting.created_at)
    return created.astimezone() if created else None


def filename_for(meeting: Meeting) -> str:
    """Stable filename: <date>-<title-slug>.md.

    Deliberately excludes the note id so that a regenerated note maps to the
    same file and overwrites rather than accumulating duplicates.
    """
    dt = meeting_local_time(meeting)
    date = (dt or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    return f"{date}-{slugify(meeting.title)}.md"


def existing_note_id(path: Path) -> Optional[str]:
    """Read the granola_id out of an already-written file's front matter.

    Used to tell "this is the same meeting, overwrite it" apart from "a
    different meeting happens to share a date and title".
    """
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            for _ in range(20):  # front matter is at the top or not present
                line = fh.readline()
                if not line:
                    break
                if line.startswith("granola_id:"):
                    return line.split(":", 1)[1].strip()
                if line.startswith("## "):
                    break
    except OSError as exc:
        logger.warning("Could not inspect %s: %s", path, exc)
    return None


def resolve_path(folder: Path, meeting: Meeting) -> Path:
    """Pick the output path, disambiguating only on a genuine collision.

    Two different meetings can share a date and title - it happened twice in
    the real history ("Phone call with Brad Wilson Alma Motors" recorded twice
    in twelve minutes). Without this, the second silently overwrites the first
    and a transcript is lost. The common case keeps the clean name.
    """
    folder = Path(folder)
    base = filename_for(meeting)
    candidate = folder / base

    owner = existing_note_id(candidate)
    if owner is None or owner == meeting.id:
        return candidate

    suffix = (meeting.id or "").replace("not_", "")[:6].lower() or "dup"
    disambiguated = folder / f"{base[:-3]}-{suffix}.md"
    logger.info(
        "Filename collision on %s (held by %s); using %s",
        base, owner, disambiguated.name,
    )
    return disambiguated


def _speaker_label(entry: Dict[str, Any], me: str = DEFAULT_OWN_NAME) -> str:
    """Best available name for a transcript line.

    Granola names the other party on many one-to-one calls but collapses every
    non-owner voice into `them` on group calls, where no per-speaker labels are
    provided. Falls back accordingly rather than inventing attribution.
    """
    sp = entry.get("speaker") or {}
    if sp.get("attribution") == "me":
        return me
    return sp.get("name") or sp.get("diarization_label") or "Them"


def render(meeting: Meeting, own_name: str = DEFAULT_OWN_NAME) -> str:
    """Produce the full markdown document for a meeting."""
    local = meeting_local_time(meeting)

    others = [
        e for e in meeting.attendee_emails
        if e and not e.endswith("group.calendar.google.com")
    ]

    lines: List[str] = ["---"]
    lines.append(f'title: "{meeting.title.replace(chr(34), chr(39))}"')
    if local:
        lines.append(f"date: {local.strftime('%Y-%m-%d %H:%M %Z')}".rstrip())
    lines.append(f"granola_id: {meeting.id}")
    if meeting.web_url:
        lines.append(f"granola_url: {meeting.web_url}")
    if meeting.organiser:
        lines.append(f"organiser: {meeting.organiser}")
    if others:
        lines.append("attendees:")
        for email in sorted(others):
            lines.append(f"  - {email}")
    lines.append(f"transcript_lines: {len(meeting.transcript_entries)}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {meeting.title}")
    lines.append("")

    if meeting.summary_markdown:
        lines.append("## Summary")
        lines.append("")
        lines.append(meeting.summary_markdown.strip())
        lines.append("")

    lines.append("## Transcript")
    lines.append("")

    if not meeting.transcript_entries:
        lines.append("_No transcript available for this meeting._")
        lines.append("")
    else:
        first = _parse(meeting.transcript_entries[0].get("start_time"))
        for entry in meeting.transcript_entries:
            text = (entry.get("text") or "").strip()
            if not text:
                continue
            start = _parse(entry.get("start_time"))
            stamp = ""
            if start and first:
                offset = int((start - first).total_seconds())
                stamp = f"[{offset // 60:02d}:{offset % 60:02d}] "
            lines.append(f"{stamp}**{_speaker_label(entry, own_name)}:** {text}")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def content_hash(text: str) -> str:
    """Stable digest of rendered content, used to skip unchanged rewrites."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def write_atomic(path: Path, text: str) -> None:
    """Write via a temp file + rename so a crash cannot leave a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
