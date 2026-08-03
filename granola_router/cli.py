"""granola-router: save Granola transcripts to markdown, filed by client."""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from pathlib import Path

from .api import GranolaAPIError, MissingAPIKey
from . import service
from .sync import LockHeld, ProcessLock, STATE_FILE, Syncer, load_json, transcript_root

POLL_SECONDS = 120


def _log(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


def cmd_backfill(args: argparse.Namespace) -> int:
    # Wait out a background poll cycle rather than failing outright.
    with ProcessLock(wait_seconds=180):
        syncer = Syncer(dry_run=args.dry_run)
        print(f"transcript root: {syncer.root}")
        stats = syncer.backfill(since=args.since, limit=args.limit)
        print(("[dry-run] " if args.dry_run else "") + stats.render())
    return 0


def cmd_poll(args: argparse.Namespace) -> int:
    if args.once:
        # Lock first, then construct: Syncer reads state in __init__, so building
        # it outside the lock can capture state another writer is mid-way through.
        with ProcessLock(wait_seconds=180):
            stats = Syncer(dry_run=args.dry_run).poll_once()
            print(stats.render())
        return 0

    logging.info("polling every %ds", args.interval)
    while True:
        try:
            with ProcessLock():
                stats = Syncer(dry_run=args.dry_run).poll_once()
                if stats.written or stats.errors or stats.quarantined:
                    logging.info("%s", stats.render())
                else:
                    logging.debug("%s", stats.render())
        except LockHeld as exc:
            logging.info("%s", exc)
        except GranolaAPIError as exc:
            logging.error("%s", exc)
        except Exception:
            logging.exception("poll failed")
        time.sleep(args.interval)


def cmd_install(args: argparse.Namespace) -> int:
    r = service.install_launch_agent(interval=args.interval)
    if not r.get("ok"):
        print(f"error: {r.get('error')}", file=sys.stderr)
        return 5
    print(f"Automatic filing is on. It checks every {r['interval']} seconds.")
    print("It starts by itself when you log in and runs in the background.")
    print("Claude does not need to be open.\n")
    if r.get("upgraded"):
        print(f"  Updated the background copy to {r.get('binary')}")
    print("  Turn it off : granola-router uninstall")
    print("  Check it    : granola-router status")
    print(f"  Logs        : {service.LOG_DIR / 'poll.log'}")
    return 0


def cmd_uninstall(_: argparse.Namespace) -> int:
    r = service.uninstall_launch_agent()
    if not r.get("was_installed"):
        print("Automatic filing was not set up, so there is nothing to stop.")
        return 0
    print("Automatic filing is off. It will not start again at login.")
    print("Everything already saved stays where it is.")
    print("You can still file on demand with: granola-router poll --once")
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    state = load_json(STATE_FILE, {"notes": {}})
    notes = state.get("notes", {})
    counts: dict = {}
    outcomes: dict = {}
    for rec in notes.values():
        counts[rec.get("status", "?")] = counts.get(rec.get("status", "?"), 0) + 1
        if rec.get("outcome"):
            outcomes[rec["outcome"]] = outcomes.get(rec["outcome"], 0) + 1

    ag = service.launch_agent_state()
    if ag["broken"]:
        label = "BROKEN - points at a missing binary; run granola-router install"
    elif ag["running"]:
        label = "ON - checks every couple of minutes, with or without Claude open"
    elif ag["installed"]:
        label = "INSTALLED BUT NOT RUNNING - run granola-router install"
    else:
        label = "OFF - run granola-router install to turn it on"
    print(f"automatic filing: {label}")
    print(f"transcript root : {transcript_root()}")
    print(f"state file      : {STATE_FILE}")
    print(f"last watermark  : {state.get('watermark') or 'never'}")
    print(f"notes tracked   : {len(notes)}")
    for k, v in sorted(counts.items()):
        print(f"  {k:<14} {v}")
    if outcomes:
        print("routing outcomes:")
        for k, v in sorted(outcomes.items()):
            print(f"  {k:<14} {v}")

    flagged = [
        (r.get("title", "?"), r.get("outcome"), r.get("reason", ""))
        for r in notes.values()
        if r.get("outcome") in ("no_attendees", "unknown", "ambiguous")
    ]
    if flagged:
        print(f"\nquarantined ({len(flagged)}) - needs a routing rule:")
        for title, outcome, reason in sorted(flagged)[:25]:
            print(f"  [{outcome}] {title[:48]:<50} {reason[:60]}")
        if len(flagged) > 25:
            print(f"  ... and {len(flagged) - 25} more")
    return 0


def cmd_domains(args: argparse.Namespace) -> int:
    """Report external attendee domains, to help build the routing map."""
    from collections import Counter

    syncer = Syncer(dry_run=True)
    counter: Counter = Counter()
    unmatched: Counter = Counter()
    known = syncer.router._email_domains  # noqa: SLF001 - diagnostic command
    n = 0
    for summary in syncer.api.iter_notes():
        note = syncer.api.get_note(summary["id"], with_transcript=False)
        if not note:
            continue
        n += 1
        for email in syncer.router._external_emails(  # noqa: SLF001
            [a.get("email") for a in (note.get("attendees") or [])]
            + [i.get("email") for i in ((note.get("calendar_event") or {}).get("invitees") or [])]
        ):
            domain = email.split("@")[-1]
            counter[domain] += 1
            if domain not in known:
                unmatched[domain] += 1
        if args.limit and n >= args.limit:
            break

    print(f"notes examined: {n}\n")
    print("=== domains WITHOUT a routing rule ===")
    for domain, count in unmatched.most_common(40):
        print(f"  {count:4}  {domain}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="granola-router", description=__doc__)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("backfill", help="pull full history and write everything")
    b.add_argument("--since", help="only notes created after this date (YYYY-MM-DD)")
    b.add_argument("--limit", type=int, help="stop after N notes")
    b.add_argument("--dry-run", action="store_true")
    b.set_defaults(func=cmd_backfill)

    q = sub.add_parser("poll", help="watch for new notes")
    q.add_argument("--once", action="store_true", help="single pass, then exit")
    q.add_argument("--interval", type=int, default=POLL_SECONDS)
    q.add_argument("--dry-run", action="store_true")
    q.set_defaults(func=cmd_poll)

    s = sub.add_parser("status", help="show what has been saved and what is quarantined")
    s.set_defaults(func=cmd_status)

    i = sub.add_parser("install", help="turn on automatic filing in the background")
    i.add_argument("--interval", type=int, default=POLL_SECONDS,
                   help="seconds between checks (default 120)")
    i.set_defaults(func=cmd_install)

    u = sub.add_parser("uninstall", help="turn off automatic filing")
    u.set_defaults(func=cmd_uninstall)

    d = sub.add_parser("domains", help="list attendee domains with no routing rule")
    d.add_argument("--limit", type=int)
    d.set_defaults(func=cmd_domains)

    args = p.parse_args(argv)
    _log(args.verbose)
    try:
        return args.func(args)
    except MissingAPIKey as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except LockHeld as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except GranolaAPIError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
