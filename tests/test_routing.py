"""Tests for the API-era routing decisions and markdown writer."""

import json
from pathlib import Path

import pytest

from granola_router.api import Meeting, note_to_meeting
from granola_router.routing import Outcome, Router
from granola_router.writer import (
    content_hash,
    filename_for,
    render,
    slugify,
    write_atomic,
)

MAP = {
    "title_overrides": {"vendor portal": "clients/Multi - Vendor/transcripts"},
    "email_domains": {
        "acme.com": "clients/Acme/transcripts",
        "beta.io": "clients/Beta/transcripts",
        "multi-engagement.com": "clients/Multi - Main/transcripts",
    },
    "title_keywords": {
        "ai builder": "hiring/transcripts",
        "swp": "clients/Weekly/transcripts",
        "claude code 101": "clients/Coaching/transcripts",
        "code": "clients/Generic/transcripts",
    },
    "quarantine_folder": "_unrouted",
}
OWN = ["owner@example.com", "owner@work.example"]


@pytest.fixture
def router(tmp_path):
    return Router(MAP, tmp_path, own_emails=OWN)


# -- routing ---------------------------------------------------------------

def test_email_domain_match_wins(router, tmp_path):
    d = router.route("Some call", ["owner@example.com", "jane@acme.com"])
    assert d.outcome is Outcome.MATCHED_EMAIL
    assert d.matched_on == "acme.com"
    assert d.folder == str(tmp_path / "clients/Acme/transcripts")
    assert not d.quarantined


def test_own_address_is_excluded_not_own_domain(router):
    """A client on gmail.com must still route; only the exact owner is skipped."""
    r = Router(
        {**MAP, "email_domains": {"gmail.com": "clients/Gmail/transcripts"}},
        Path("/tmp"),
        own_emails=["owner@gmail.com"],
    )
    # Owner alone -> nothing external.
    assert r.route("x", ["owner@gmail.com"]).outcome is Outcome.NO_ATTENDEES
    # A different gmail.com person is a real counterparty.
    assert r.route("x", ["owner@gmail.com", "client@gmail.com"]).outcome is Outcome.MATCHED_EMAIL


def test_ambiguous_when_two_client_domains_present(router):
    d = router.route("Joint call", ["jane@acme.com", "bob@beta.io"])
    assert d.outcome is Outcome.AMBIGUOUS
    assert d.quarantined
    assert len(d.candidates) == 2
    assert "acme.com" in d.reason and "beta.io" in d.reason


def test_title_override_beats_email_domain(router, tmp_path):
    """multi-engagement.com spans two engagements; the override must win."""
    d = router.route("Vendor portal sync", ["patrick@multi-engagement.com"])
    assert d.outcome is Outcome.MATCHED_TITLE
    assert d.folder == str(tmp_path / "clients/Multi - Vendor/transcripts")


def test_email_used_when_no_override(router, tmp_path):
    d = router.route("Main sync", ["dan@multi-engagement.com"])
    assert d.folder == str(tmp_path / "clients/Multi - Main/transcripts")


def test_title_keyword_when_no_email_rule(router):
    d = router.route("Jane Doe AI Builder screen", ["jane@nowhere.example"])
    assert d.outcome is Outcome.MATCHED_TITLE
    assert d.matched_on == "ai builder"


def test_longest_keyword_wins(router, tmp_path):
    """'claude code 101' must beat the shorter 'code'."""
    d = router.route("Claude Code 101 session", [])
    assert d.folder == str(tmp_path / "clients/Coaching/transcripts")


def test_no_attendees_is_distinct_from_unknown(router):
    assert router.route("Solo recording", []).outcome is Outcome.NO_ATTENDEES
    assert router.route("Solo recording", ["owner@example.com"]).outcome is Outcome.NO_ATTENDEES
    d = router.route("Mystery", ["someone@unmapped.example"])
    assert d.outcome is Outcome.UNKNOWN
    assert "unmapped.example" in d.reason


def test_quarantine_never_lands_in_a_client_folder(router, tmp_path):
    for title, emails in [("Mystery", ["x@unmapped.example"]), ("Solo", [])]:
        assert router.route(title, emails).folder == str(tmp_path / "_unrouted")


def test_calendar_resource_accounts_ignored(router):
    d = router.route("Room booking", ["abc@group.calendar.google.com"])
    assert d.outcome is Outcome.NO_ATTENDEES


def test_path_traversal_is_quarantined_not_written_to_root(tmp_path):
    """A malformed rule must be visible, not silently dumped in the root."""
    r = Router(
        {"email_domains": {"evil.com": "../../etc"}, "quarantine_folder": "_q"},
        tmp_path,
        own_emails=OWN,
    )
    d = r.route("x", ["a@evil.com"])
    assert d.folder == str(tmp_path / "_q")
    assert d.quarantined
    assert "invalid routing rule" in d.reason


def test_absolute_path_in_map_is_quarantined(tmp_path):
    r = Router(
        {"email_domains": {"evil.com": "/etc/passwd"}, "quarantine_folder": "_q"},
        tmp_path,
        own_emails=OWN,
    )
    d = r.route("x", ["a@evil.com"])
    assert d.folder == str(tmp_path / "_q")
    assert d.quarantined


# -- api normalization -----------------------------------------------------

def test_note_to_meeting_merges_all_three_email_sources():
    note = {
        "id": "not_abc",
        "title": "Sync",
        "created_at": "2026-07-01T10:00:00Z",
        "updated_at": "2026-07-01T11:00:00Z",
        "attendees": [{"name": "Jane", "email": "Jane@Acme.com"}],
        "calendar_event": {
            "invitees": [{"email": "bob@beta.io"}],
            "organiser": "carol@gamma.net",
            "scheduled_start_time": "2026-07-01T10:00:00Z",
        },
        "transcript": [{"text": "hi", "start_time": "2026-07-01T10:00:01Z",
                        "speaker": {"attribution": "me"}}],
    }
    m = note_to_meeting(note)
    assert set(m.attendee_emails) == {"jane@acme.com", "bob@beta.io", "carol@gamma.net"}
    assert m.has_transcript


def test_note_without_transcript_flagged():
    m = note_to_meeting({"id": "not_x", "title": "T", "transcript": None})
    assert not m.has_transcript


# -- writer ----------------------------------------------------------------

def _meeting(**kw):
    base = dict(
        id="not_abc123",
        title="Weekly sync",
        created_at="2026-07-31T15:00:00Z",
        updated_at="2026-07-31T16:00:00Z",
        attendee_emails=["dana@example-client.com"],
        attendee_names=["Dana"],
        transcript_entries=[
            {"text": "Hello there", "start_time": "2026-07-31T15:00:00Z",
             "speaker": {"attribution": "me"}},
            {"text": "Hi there", "start_time": "2026-07-31T15:01:30Z",
             "speaker": {"attribution": "them", "name": "Dana Smith"}},
        ],
    )
    base.update(kw)
    return Meeting(**base)


def test_filename_is_stable_and_has_no_note_id():
    """Regenerated notes must overwrite, not accumulate -N duplicates."""
    m = _meeting()
    assert filename_for(m) == filename_for(_meeting(updated_at="2026-08-02T00:00:00Z"))
    assert "not_abc123" not in filename_for(m)
    assert filename_for(m).startswith("2026-07-31-")


def test_slugify():
    assert slugify("Weekly sync!") == "weekly-sync"
    assert slugify("") == "untitled"
    assert len(slugify("x" * 200)) <= 60


def test_render_includes_transcript_and_speakers():
    out = render(_meeting())
    assert "## Transcript" in out
    assert "**Me:** Hello there" in out
    assert "**Dana Smith:** Hi there" in out
    assert "[00:00]" in out and "[01:30]" in out
    assert "granola_id: not_abc123" in out
    assert "dana@example-client.com" in out


def test_unnamed_speaker_falls_back_to_them():
    m = _meeting(transcript_entries=[
        {"text": "anon", "start_time": "2026-07-31T15:00:00Z",
         "speaker": {"attribution": "them"}}
    ])
    assert "**Them:** anon" in render(m)


def test_render_marks_missing_transcript():
    assert "_No transcript available" in render(_meeting(transcript_entries=[]))


def test_content_hash_detects_change():
    a = render(_meeting())
    b = render(_meeting(title="Different"))
    assert content_hash(a) == content_hash(render(_meeting()))
    assert content_hash(a) != content_hash(b)


def test_write_atomic_creates_dirs_and_overwrites(tmp_path):
    p = tmp_path / "deep" / "nested" / "x.md"
    write_atomic(p, "one")
    assert p.read_text() == "one"
    write_atomic(p, "two")
    assert p.read_text() == "two"
    assert list(p.parent.glob("*.tmp")) == []


# -- collision handling ----------------------------------------------------

def test_same_meeting_reuses_its_own_file(tmp_path):
    from granola_router.writer import resolve_path
    m = _meeting()
    p1 = resolve_path(tmp_path, m)
    write_atomic(p1, render(m))
    # Re-syncing the same note must target the same path, not a new one.
    assert resolve_path(tmp_path, m) == p1


def test_different_meeting_same_date_and_title_gets_its_own_file(tmp_path):
    """Two distinct notes sharing a date+title must not clobber each other."""
    from granola_router.writer import resolve_path
    a = _meeting(id="not_AAAAAA1111")
    b = _meeting(id="not_BBBBBB2222")
    pa = resolve_path(tmp_path, a)
    write_atomic(pa, render(a))
    pb = resolve_path(tmp_path, b)
    assert pb != pa
    write_atomic(pb, render(b))
    assert pa.exists() and pb.exists()
    assert "not_AAAAAA1111" in pa.read_text()
    assert "not_BBBBBB2222" in pb.read_text()


def test_collision_suffix_is_stable_across_runs(tmp_path):
    from granola_router.writer import resolve_path
    a, b = _meeting(id="not_AAAAAA1111"), _meeting(id="not_BBBBBB2222")
    write_atomic(resolve_path(tmp_path, a), render(a))
    first = resolve_path(tmp_path, b)
    write_atomic(first, render(b))
    assert resolve_path(tmp_path, b) == first


def test_date_uses_meeting_timezone_not_machine_timezone(monkeypatch):
    """An evening US call must not roll to the next day on a laptop set to IST."""
    import os, time
    m = _meeting(
        title="Evening call",
        # 21:30 on Jul 31 in US Eastern == 07:00 Aug 1 in India.
        created_at="2026-08-01T01:30:00Z",
    )
    m.scheduled_start = "2026-07-31T21:30:00-04:00"
    for tz in ("Asia/Kolkata", "America/New_York", "UTC"):
        os.environ["TZ"] = tz
        time.tzset()
        assert filename_for(m).startswith("2026-07-31"), f"wrong date under {tz}"
    os.environ.pop("TZ", None)
    time.tzset()


def test_note_override_beats_every_other_rule(tmp_path):
    r = Router(
        {**MAP, "note_overrides": {"not_SPECIFIC": "clients/Manual/transcripts"}},
        tmp_path,
        own_emails=OWN,
    )
    # Would otherwise match acme.com by email.
    d = r.route("Some call", ["jane@acme.com"], note_id="not_SPECIFIC")
    assert d.folder == str(tmp_path / "clients/Manual/transcripts")
    assert not d.quarantined
    # A different note is unaffected.
    assert r.route("Some call", ["jane@acme.com"], note_id="not_OTHER").matched_on == "acme.com"


def test_route_without_note_id_still_works(router):
    assert router.route("Jane AI Builder", []).outcome is Outcome.MATCHED_TITLE
