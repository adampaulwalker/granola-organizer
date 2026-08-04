"""Routing decisions for API-sourced meetings.

Replaces the string-returning legacy router. The problem with returning a bare
folder path is that "matched a client by email" and "fell through to the default
because we had no attendee emails at all" are indistinguishable, so misfiled
transcripts look identical to correctly filed ones.

This module returns a RoutingDecision instead, so the caller can quarantine
uncertain meetings rather than burying them in a client folder.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

logger = logging.getLogger(__name__)

# Bump whenever routing BEHAVIOUR changes, not just the rules.
#
# The skip-if-unchanged check fingerprints the routing map, which is not enough
# on its own: a process running older code alongside a newer map records the
# new fingerprint while ignoring the new logic, and every later run then treats
# those notes as up to date. Including this constant in the fingerprint means a
# logic change also invalidates the cache.
#
# 1: email domains + title keywords
# 2: scoped title overrides
# 3: per-note overrides
ROUTING_LOGIC_VERSION = 3


class Outcome(str, Enum):
    """Why a meeting landed where it did."""

    MATCHED_EMAIL = "matched_email"
    MATCHED_TITLE = "matched_title"
    AMBIGUOUS = "ambiguous"          # multiple rules disagreed
    NO_ATTENDEES = "no_attendees"    # nothing but the owner on the invite
    UNKNOWN = "unknown"              # had emails, none matched a rule

    @property
    def is_confident(self) -> bool:
        return self in (Outcome.MATCHED_EMAIL, Outcome.MATCHED_TITLE)


@dataclass
class RoutingDecision:
    """The outcome of routing one meeting."""

    outcome: Outcome
    folder: str                      # absolute destination path
    reason: str                      # human-readable explanation
    matched_on: Optional[str] = None # the domain or keyword that matched
    candidates: Sequence[str] = ()   # competing folders, when ambiguous

    @property
    def quarantined(self) -> bool:
        return not self.outcome.is_confident


class Router:
    """Maps meetings to folders using email domains and title keywords.

    Precedence: email domain, then title keyword, then quarantine. An email
    domain match is preferred because it is derived from calendar data rather
    than free text, so it is far less likely to collide by accident.
    """

    def __init__(
        self,
        routing_map: Dict[str, Any],
        transcript_root: Path,
        own_emails: Iterable[str] = (),
    ) -> None:
        self._root = Path(transcript_root).expanduser()
        self._email_domains: Dict[str, str] = {
            k.lower(): v for k, v in (routing_map.get("email_domains") or {}).items()
        }
        self._title_keywords: Dict[str, str] = {
            k.lower(): v for k, v in (routing_map.get("title_keywords") or {}).items()
        }
        # Checked BEFORE email domains. Needed when one domain spans several
        # engagements: one company can cover both a main project and a
        # separate vendor portal, so the domain alone cannot decide.
        #
        # An entry is either "keyword": "folder" (applies anywhere) or
        # "keyword": {"folder": ..., "domains": [...]} which applies only when
        # the meeting involves one of those domains, or has no external domain
        # at all. Scoping matters because a bare first name like "patrick"
        # would otherwise hijack an unrelated client's meeting.
        self._title_overrides: Dict[str, Dict[str, Any]] = {}
        for keyword, value in (routing_map.get("title_overrides") or {}).items():
            if isinstance(value, dict):
                entry = {
                    "folder": value.get("folder", ""),
                    "domains": {d.lower() for d in (value.get("domains") or [])},
                }
            else:
                entry = {"folder": value, "domains": set()}
            self._title_overrides[keyword.lower()] = entry
        self._note_overrides: Dict[str, str] = dict(
            routing_map.get("note_overrides") or {}
        )
        self._quarantine = routing_map.get("quarantine_folder") or "_unrouted"
        # Exact addresses belonging to the account holder. Deliberately NOT a
        # domain list: clients routinely use gmail.com, and excluding a whole
        # public domain silently discards real counterparties.
        self._own = {e.strip().lower() for e in own_emails if e and e.strip()}

    # -- public ----------------------------------------------------------

    def route(
        self,
        title: str,
        attendee_emails: Sequence[str],
        note_id: Optional[str] = None,
    ) -> RoutingDecision:
        """Decide where one meeting belongs."""
        # Highest precedence: a per-note assignment. Used for meetings that no
        # rule can reach - a one-to-one with someone on a personal address,
        # where only the content identifies the engagement. Kept in the routing
        # map rather than applied as a manual file move so the decision
        # survives re-runs and is visible alongside every other rule.
        if note_id and note_id in self._note_overrides:
            return self._decide(
                Outcome.MATCHED_TITLE,
                self._note_overrides[note_id],
                "per-note assignment",
                matched_on=note_id,
            )

        external = self._external_emails(attendee_emails)

        override = self._match_override(title, external)
        if override:
            keyword, folder = override
            return self._decide(
                Outcome.MATCHED_TITLE,
                folder,
                f"title override {keyword!r}",
                matched_on=keyword,
            )

        email_hits = self._email_matches(external)
        distinct = {folder for _, folder in email_hits}

        if len(distinct) > 1:
            folders = sorted(distinct)
            return RoutingDecision(
                outcome=Outcome.AMBIGUOUS,
                folder=self._quarantine_path(),
                reason=(
                    "attendee domains matched more than one client: "
                    + ", ".join(f"{d} -> {f}" for d, f in sorted(email_hits))
                ),
                candidates=folders,
            )

        if len(distinct) == 1:
            domain, folder = email_hits[0]
            return self._decide(
                Outcome.MATCHED_EMAIL, folder, f"attendee domain {domain}", matched_on=domain
            )

        keyword_hit = self._title_match(title)
        if keyword_hit:
            keyword, folder = keyword_hit
            return self._decide(
                Outcome.MATCHED_TITLE,
                folder,
                f"title keyword {keyword!r}",
                matched_on=keyword,
            )

        if not external:
            return RoutingDecision(
                outcome=Outcome.NO_ATTENDEES,
                folder=self._quarantine_path(),
                reason="no external attendees on the calendar event",
            )

        return RoutingDecision(
            outcome=Outcome.UNKNOWN,
            folder=self._quarantine_path(),
            reason=(
                "no rule matched; external domains seen: "
                + ", ".join(sorted({e.split('@')[-1] for e in external}))
            ),
        )

    # -- internals -------------------------------------------------------

    def _quarantine_path(self) -> str:
        """Quarantine destination, falling back to the root if it is misconfigured."""
        return self._abs(self._quarantine) or str(self._root.resolve())

    def _decide(
        self,
        outcome: Outcome,
        subfolder: str,
        reason: str,
        matched_on: Optional[str] = None,
    ) -> RoutingDecision:
        """Build a decision, downgrading to quarantine if the rule is malformed.

        A rule that points outside the transcript root used to fall back to the
        root itself, which quietly scattered client transcripts at the top
        level. Quarantining instead makes the bad rule visible in `status`.
        """
        resolved = self._abs(subfolder)
        if resolved is None:
            return RoutingDecision(
                outcome=Outcome.AMBIGUOUS,
                folder=self._quarantine_path(),
                reason=f"invalid routing rule for {matched_on or subfolder!r}: {subfolder!r}",
            )
        return RoutingDecision(
            outcome=outcome, folder=resolved, reason=reason, matched_on=matched_on
        )

    def _match_override(
        self, title: str, external: Sequence[str]
    ) -> Optional[tuple]:
        """Match a title override, respecting its domain scope."""
        if not self._title_overrides:
            return None
        domains = {e.split("@", 1)[1] for e in external}
        applicable = {
            keyword: entry["folder"]
            for keyword, entry in self._title_overrides.items()
            # Unscoped rules always apply. Scoped rules apply when the meeting
            # touches one of their domains, or has no external domain to judge by.
            if not entry["domains"] or not domains or (entry["domains"] & domains)
        }
        return self._match_keywords(title, applicable)

    def _external_emails(self, emails: Sequence[str]) -> List[str]:
        out = []
        for raw in emails or ():
            email = (raw or "").strip().lower()
            if not email or "@" not in email:
                continue
            if email in self._own:
                continue
            # Calendar resource/room accounts are not people.
            if email.endswith("group.calendar.google.com") or email.endswith(
                "resource.calendar.google.com"
            ):
                continue
            out.append(email)
        return out

    def _email_matches(self, emails: Sequence[str]) -> List[tuple]:
        """Return [(domain, folder)] for every attendee domain with a rule."""
        hits: Dict[str, str] = {}
        for email in emails:
            domain = email.split("@", 1)[1]
            folder = self._email_domains.get(domain)
            if folder:
                hits[domain] = folder
        return sorted(hits.items())

    def _title_match(self, title: str) -> Optional[tuple]:
        return self._match_keywords(title, self._title_keywords)

    @staticmethod
    def _match_keywords(title: str, table: Dict[str, str]) -> Optional[tuple]:
        """Longest matching keyword wins, so 'prompt vendor' beats 'prompt'."""
        if not title or not table:
            return None
        lowered = title.lower()
        best: Optional[tuple] = None
        for keyword, folder in table.items():
            if keyword in lowered:
                if best is None or len(keyword) > len(best[0]):
                    best = (keyword, folder)
        return best

    def _abs(self, subfolder: str) -> Optional[str]:
        """Resolve a routing-map entry to an absolute path under the root.

        Returns None for a malformed entry (absolute path, or one that escapes
        the transcript root) so the caller can quarantine it rather than
        silently writing to the root.
        """
        root = self._root.resolve()
        if not subfolder:
            return str(root)
        if subfolder.startswith("/"):
            logger.error("Absolute path in routing map rejected: %s", subfolder)
            return None

        target = (root / subfolder).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            logger.error("Routing entry escapes transcript root: %s", subfolder)
            return None
        return str(target)
