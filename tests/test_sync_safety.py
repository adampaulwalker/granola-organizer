"""Tests for the sync-layer safety fixes raised in code review."""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from granola_router import sync as S
from granola_router.routing import Router
from granola_router.writer import render, write_atomic
from granola_router.api import Meeting


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(S, "LOCK_FILE", tmp_path / "sync.lock")
    monkeypatch.setattr(S, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(S, "ROUTING_FILE", tmp_path / "routing.json")
    return tmp_path


def _meeting(note_id="not_abc123", title="Weekly sync"):
    return Meeting(
        id=note_id, title=title,
        created_at="2026-07-31T15:00:00Z", updated_at="2026-07-31T16:00:00Z",
        attendee_emails=["dana@example-client.com"],
        transcript_entries=[{"text": "hi", "start_time": "2026-07-31T15:00:00Z",
                             "speaker": {"attribution": "me"}}],
    )


def _syncer(tmp_path, **kw):
    kw.setdefault("routing_map", {"quarantine_folder": "_unrouted"})
    kw.setdefault("own_emails", ["owner@example.com"])
    kw.setdefault("root", tmp_path / "out")
    return S.Syncer(api=object(), **kw)


# -- lock ------------------------------------------------------------------

def test_lock_is_exclusive(isolate):
    with S.ProcessLock(isolate / "sync.lock"):
        with pytest.raises(S.LockHeld):
            with S.ProcessLock(isolate / "sync.lock"):
                pass


def test_lock_released_on_exit(isolate):
    with S.ProcessLock(isolate / "sync.lock"):
        pass
    with S.ProcessLock(isolate / "sync.lock"):
        pass  # must be re-acquirable


def test_lock_freed_when_holder_process_dies(isolate):
    """A killed backfill must not strand the lock; flock is released by the kernel."""
    lock = isolate / "sync.lock"
    code = (
        f"import sys; sys.path.insert(0,{str(Path.cwd())!r});"
        "from granola_router import sync as S;"
        f"l=S.ProcessLock({str(lock)!r}); l.__enter__();"
        "import time; time.sleep(30)"
    )
    proc = subprocess.Popen([sys.executable, "-c", code])
    try:
        import time
        for _ in range(50):
            if lock.exists():
                break
            time.sleep(0.1)
        proc.kill()
        proc.wait(timeout=10)
        # The lock file may still exist, but the flock is gone, so we can take it.
        with S.ProcessLock(lock):
            pass
    finally:
        if proc.poll() is None:
            proc.kill()


# -- watermark -------------------------------------------------------------

def test_poll_window_anchors_to_watermark_not_now(isolate, tmp_path):
    """Offline longer than the lookback must not silently skip that period."""
    s = _syncer(tmp_path)
    old = datetime.now(timezone.utc) - timedelta(days=40)
    s.state["watermark"] = old.isoformat()
    since = datetime.fromisoformat(s._poll_since())
    assert since < datetime.now(timezone.utc) - timedelta(days=30)
    assert abs((since - (old - S.LOOKBACK)).total_seconds()) < 2


def test_poll_window_defaults_when_no_watermark(isolate, tmp_path):
    s = _syncer(tmp_path)
    s.state["watermark"] = None
    since = datetime.fromisoformat(s._poll_since())
    assert since > datetime.now(timezone.utc) - S.LOOKBACK - timedelta(minutes=1)


def test_unparseable_watermark_falls_back(isolate, tmp_path):
    s = _syncer(tmp_path)
    s.state["watermark"] = "not-a-date"
    assert datetime.fromisoformat(s._poll_since())  # does not raise


def test_failed_notes_are_retried_outside_the_window(isolate, tmp_path):
    s = _syncer(tmp_path)
    s.state["notes"] = {
        "not_a": {"status": "no_transcript"},
        "not_b": {"status": "written"},
        "not_c": {"status": "error"},
        "not_d": {"status": "unavailable"},
    }
    assert set(s._retry_ids()) == {"not_a", "not_c", "not_d"}


# -- destructive-delete guards --------------------------------------------

def test_old_copy_removed_only_when_it_belongs_to_this_note(isolate, tmp_path):
    s = _syncer(tmp_path)
    root = s.root
    a, b = root / "A.md", root / "B.md"
    write_atomic(a, render(_meeting("not_MINE")))
    write_atomic(b, render(_meeting("not_OTHER")))

    s._retire_old_copy(str(a), str(root / "new.md"), "not_MINE")
    assert not a.exists(), "own previous copy should be removed"

    s._retire_old_copy(str(b), str(root / "new.md"), "not_MINE")
    assert b.exists(), "another note's file must never be deleted"


def test_old_copy_outside_root_is_never_removed(isolate, tmp_path):
    s = _syncer(tmp_path)
    outside = tmp_path / "elsewhere.md"
    write_atomic(outside, render(_meeting("not_MINE")))
    s._retire_old_copy(str(outside), str(s.root / "new.md"), "not_MINE")
    assert outside.exists()


def test_old_copy_claimed_by_another_state_record_is_kept(isolate, tmp_path):
    s = _syncer(tmp_path)
    shared = s.root / "shared.md"
    write_atomic(shared, render(_meeting("not_MINE")))
    s.state["notes"]["not_SOMEONE_ELSE"] = {"output_path": str(shared)}
    s._retire_old_copy(str(shared), str(s.root / "new.md"), "not_MINE")
    assert shared.exists()


# -- skip logic ------------------------------------------------------------

def test_file_matches_requires_real_content_not_just_existence(isolate, tmp_path):
    from granola_router.writer import content_hash
    s = _syncer(tmp_path)
    p = s.root / "x.md"
    text = render(_meeting("not_abc123"))
    write_atomic(p, text)
    assert s._file_matches(p, "not_abc123", content_hash(text))
    # Tampered/truncated file must NOT be treated as up to date.
    write_atomic(p, "corrupted")
    assert not s._file_matches(p, "not_abc123", content_hash(text))


def test_routing_version_changes_when_rules_change(isolate, tmp_path):
    a = _syncer(tmp_path, routing_map={"email_domains": {"a.com": "A"}})
    b = _syncer(tmp_path, routing_map={"email_domains": {"a.com": "B"}})
    assert a.routing_version != b.routing_version


# -- api datetime formatting ----------------------------------------------

def test_api_datetime_format_matches_what_the_api_accepts():
    """Verified live: +00:00 offsets and microseconds return HTTP 400."""
    from granola_router.api import to_api_datetime
    assert to_api_datetime("2026-07-27T06:17:07.705031+00:00") == "2026-07-27T06:17:07Z"
    assert to_api_datetime("2026-07-27T06:17:07+00:00") == "2026-07-27T06:17:07Z"
    assert to_api_datetime("2026-07-27T06:17:07Z") == "2026-07-27T06:17:07Z"
    assert to_api_datetime("2026-07-27") == "2026-07-27"
    assert to_api_datetime(None) is None
    # A non-UTC offset must be converted, not passed through.
    assert to_api_datetime("2026-07-27T02:17:07-04:00") == "2026-07-27T06:17:07Z"
    # datetime.isoformat() output, the exact thing that caused the 400.
    assert "+" not in to_api_datetime(
        datetime.now(timezone.utc).isoformat()
    )


def test_fetch_error_is_recorded_so_it_can_be_retried(isolate, tmp_path):
    """A transient network failure must not silently drop the note forever."""
    class Boom:
        def get_note(self, *a, **k):
            raise RuntimeError("nodename nor servname provided")
    s = _syncer(tmp_path)
    s.api = Boom()
    stats = S.SyncStats()
    s.sync_note({"id": "not_flaky", "updated_at": "2026-07-01T00:00:00Z"}, stats)
    assert stats.errors == 1
    assert s.state["notes"]["not_flaky"]["status"] == "error"
    assert "not_flaky" in s._retry_ids()


def test_lock_waits_then_fails_with_actionable_message(isolate):
    """A manual run waits briefly; if it still cannot get in, it says what to do."""
    import time as _t
    lock = isolate / "sync.lock"
    with S.ProcessLock(lock):
        start = _t.monotonic()
        with pytest.raises(S.LockHeld) as err:
            with S.ProcessLock(lock, wait_seconds=3):
                pass
        assert _t.monotonic() - start >= 3, "should have waited"
        assert "launchctl unload" in str(err.value)


def test_routing_version_covers_code_version_not_just_config(isolate, tmp_path, monkeypatch):
    """A logic change must invalidate the skip cache even if the map is identical."""
    from granola_router import routing as R
    rmap = {"email_domains": {"a.com": "A"}}
    before = _syncer(tmp_path, routing_map=rmap).routing_version
    monkeypatch.setattr(R, "ROUTING_LOGIC_VERSION", R.ROUTING_LOGIC_VERSION + 1)
    monkeypatch.setattr(S, "ROUTING_LOGIC_VERSION", R.ROUTING_LOGIC_VERSION)
    after = _syncer(tmp_path, routing_map=rmap).routing_version
    assert before != after


def test_config_dir_honours_env_override(tmp_path, monkeypatch):
    """GRANOLA_ROUTER_HOME lets a demo or a test run without touching real state."""
    import importlib
    from granola_router import api as A
    monkeypatch.setenv("GRANOLA_ROUTER_HOME", str(tmp_path / "cfg"))
    importlib.reload(A)
    assert A.DATA_DIR == (tmp_path / "cfg").resolve()
    assert A.API_KEY_FILE == (tmp_path / "cfg").resolve() / "api-key"
    monkeypatch.delenv("GRANOLA_ROUTER_HOME")
    importlib.reload(A)
    assert "granola" in str(A.DATA_DIR)



# -- background job management --------------------------------------------

def test_install_plist_is_wellformed(tmp_path, monkeypatch):
    """The generated launchd job must use absolute paths and survive plistlib."""
    import plistlib, subprocess as sp
    from granola_router import service
    monkeypatch.setattr(service, "PLIST_PATH", tmp_path / "test.plist")
    monkeypatch.setattr(service, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(service.sys, "platform", "darwin")
    monkeypatch.setattr(service.subprocess, "run",
                        lambda *a, **k: sp.CompletedProcess(a, 0, "", ""))
    # The real function refuses to report success without a fresh heartbeat
    # from the poll loop, so the happy path has to supply one.
    monkeypatch.setattr(service, "_wait_for_job",
                        lambda *a, **k: {"loaded": True, "PID": 4242, "heartbeat": True})
    r = service.install_launch_agent(interval=120)
    assert r["ok"], r
    data = plistlib.loads((tmp_path / "test.plist").read_bytes())
    assert data["Label"] == service.LABEL
    assert data["RunAtLoad"] is True
    assert data["ProgramArguments"][0].startswith("/"), "launchd needs an absolute path"
    assert "poll" in data["ProgramArguments"]
    # A poll loop that exits cleanly has stopped early. SuccessfulExit:false
    # would leave it dead in exactly that case.
    assert data["KeepAlive"] is True, "a clean exit from the poller is still a failure"
    assert data["EnvironmentVariables"]["GRANOLA_ROUTER_HOME"], \
        "the daemon cannot inherit config from Claude, so it must be pinned"


def test_daemon_never_points_into_the_extension_folder(tmp_path, monkeypatch):
    """A launch agent aimed inside Claude's extension dir breaks on uninstall."""
    from granola_router import service
    ext = tmp_path / "Claude" / "extensions" / "granola" / "granola-router"
    ext.parent.mkdir(parents=True)
    ext.write_bytes(b"binary")
    monkeypatch.setattr(service, "BIN_ROOT", tmp_path / "stable")
    monkeypatch.setattr(service, "VERSIONS", tmp_path / "stable" / "versions")
    monkeypatch.setattr(service, "CURRENT", tmp_path / "stable" / "current")
    monkeypatch.setattr(service, "running_frozen", lambda: True)
    monkeypatch.setattr(service.sys, "executable", str(ext))
    r = service.install_stable_binary()
    assert r["stable"] and "extensions" not in r["path"]
    assert "extensions" not in service.daemon_argv(120)[0]


def test_new_build_repoints_current_so_upgrades_take_effect(tmp_path, monkeypatch):
    """Without this, launchd keeps running the old binary after an upgrade."""
    from granola_router import service
    monkeypatch.setattr(service, "BIN_ROOT", tmp_path / "s")
    monkeypatch.setattr(service, "VERSIONS", tmp_path / "s" / "versions")
    monkeypatch.setattr(service, "CURRENT", tmp_path / "s" / "current")
    monkeypatch.setattr(service, "running_frozen", lambda: True)
    live = tmp_path / "s" / "current" / "granola-router"

    v1 = tmp_path / "v1"; v1.write_bytes(b"BUILD ONE")
    monkeypatch.setattr(service.sys, "executable", str(v1))
    a = service.install_stable_binary()
    assert a["upgraded"] and live.read_bytes() == b"BUILD ONE"

    assert service.install_stable_binary()["upgraded"] is False, "same binary should no-op"

    v2 = tmp_path / "v2"; v2.write_bytes(b"BUILD TWO")
    monkeypatch.setattr(service.sys, "executable", str(v2))
    b = service.install_stable_binary()
    assert b["upgraded"] and b["version"] != a["version"]
    assert live.read_bytes() == b"BUILD TWO", "current must point at the new build"


def test_status_flags_a_launch_agent_pointing_at_nothing(tmp_path, monkeypatch):
    """The exact failure that killed the previous daemon when its repo moved."""
    import plistlib
    from granola_router import service
    plist = tmp_path / "p.plist"
    with open(plist, "wb") as fh:
        plistlib.dump({"Label": service.LABEL,
                       "ProgramArguments": [str(tmp_path / "gone"), "poll"]}, fh)
    monkeypatch.setattr(service, "PLIST_PATH", plist)
    st = service.launch_agent_state()
    assert st["installed"] and st["broken"]


def test_service_functions_print_nothing(capsys, tmp_path, monkeypatch):
    """stdout must stay clean: these run inside a stdio MCP server."""
    from granola_router import service
    monkeypatch.setattr(service, "PLIST_PATH", tmp_path / "none.plist")
    service.launch_agent_state()
    service.uninstall_launch_agent()
    service.daemon_argv(120)
    assert capsys.readouterr().out == "", "service layer must not write to stdout"


def test_archive_is_visible_without_state(tmp_path, monkeypatch):
    """Losing state.json must not make an intact archive look empty."""
    pytest.importorskip("mcp", reason="MCP SDK is only needed to build the extension")
    from granola_router import mcp_server as ms
    from granola_router.writer import render, write_atomic
    from granola_router.api import Meeting

    root = tmp_path / "Meetings" / "clients" / "Acme"
    m = Meeting(id="not_ONDISK00001", title="Acme sync",
                created_at="2026-07-01T10:00:00Z", updated_at="2026-07-01T10:00:00Z",
                attendee_emails=["a@acme.com"],
                scheduled_start="2026-07-01T06:00:00-04:00",
                transcript_entries=[{"text": "we agreed on the price",
                                     "start_time": "2026-07-01T10:00:00Z",
                                     "speaker": {"attribution": "me"}}])
    write_atomic(root / "2026-07-01-acme-sync.md", render(m))

    monkeypatch.setattr(ms, "transcript_root", lambda: tmp_path / "Meetings")
    monkeypatch.setattr(ms, "_state_notes", dict)   # state wiped

    found = ms._saved_meetings()
    assert "not_ONDISK00001" in found, "disk scan must recover the archive"
    assert ms.granola_search("agreed on the price")["count"] == 1


# -- the defects found by the first real end-to-end run --------------------
#
# The extension shipped with a binary that ignored argv, so launchd ran the MCP
# server instead of the poller. It exited 0 against launchd's empty stdin,
# KeepAlive:{SuccessfulExit:false} declined to restart it, and status reported
# "on" the whole time. Three independent failures, each with a test below.

def test_frozen_entry_point_dispatches_on_argv():
    """The one binary must be both a stdio MCP server and a launchd poller."""
    import entry_mcp
    called = {}

    def fake_cli(argv):
        called["cli"] = argv
        return 0

    def fake_serve():
        called["serve"] = True

    import granola_router.cli as C
    import granola_router.mcp_server as M
    orig_cli, orig_serve = C.main, getattr(M, "main", None)
    C.main = fake_cli
    try:
        sys.argv = ["granola-router", "poll", "--interval", "120"]
        assert entry_mcp.main() == 0
        assert called.get("cli") == ["poll", "--interval", "120"], \
            "launchd's argv must reach the CLI, not start an MCP server"
        assert "serve" not in called, "poll must never start the MCP server"
    finally:
        C.main = orig_cli
        if orig_serve is not None:
            M.main = orig_serve


def test_frozen_entry_point_serves_mcp_with_no_arguments(monkeypatch):
    """Claude Desktop spawns it bare; that path must still be the MCP server."""
    import entry_mcp
    seen = {}
    import granola_router.mcp_server as M
    monkeypatch.setattr(M, "main", lambda: seen.setdefault("serve", True))
    monkeypatch.setattr(sys, "argv", ["granola-router-mcp"])
    assert entry_mcp.main() == 0
    assert seen.get("serve") is True


def test_frozen_entry_point_rejects_an_unknown_command(monkeypatch, capsys):
    import entry_mcp
    monkeypatch.setattr(sys, "argv", ["granola-router", "wat"])
    assert entry_mcp.main() == 2
    assert "unknown command" in capsys.readouterr().err


def test_a_loaded_job_with_no_pid_is_not_running(tmp_path, monkeypatch):
    """`-  0  com.granola-router.poll` means it exited, not that it is filing."""
    import plistlib, subprocess as sp
    from granola_router import service
    plist = tmp_path / "p.plist"
    binary = tmp_path / "granola-router"
    binary.write_bytes(b"#!/bin/sh\n"); os.chmod(binary, 0o755)
    with open(plist, "wb") as fh:
        plistlib.dump({"Label": service.LABEL,
                       "ProgramArguments": [str(binary), "poll"],
                       # Pinned so this test isolates the pid check rather than
                       # tripping the separate config-mismatch rule.
                       "EnvironmentVariables": {
                           "GRANOLA_ROUTER_HOME": str(service.DATA_DIR)}}, fh)
    monkeypatch.setattr(service, "PLIST_PATH", plist)
    monkeypatch.setattr(service, "HEALTH_FILE", tmp_path / "daemon.json")
    # launchctl reports the job as loaded, with a last exit status and no PID.
    monkeypatch.setattr(service.subprocess, "run", lambda *a, **k: sp.CompletedProcess(
        a, 0, '{\n\t"LastExitStatus" = 0;\n\t"Label" = "com.granola-router.poll";\n}\n', ""))
    st = service.launch_agent_state()
    assert st["loaded"] is True
    assert st["running"] is False, "no PID key means the daemon is not running"
    assert service.filing_status(st)["status"] == "failed", \
        "reporting 'on' here is the exact bug that shipped"


def test_enable_refuses_to_claim_success_when_the_job_dies(tmp_path, monkeypatch):
    """A binary that exits immediately must surface as an error, not 'on'."""
    import subprocess as sp
    from granola_router import service
    monkeypatch.setattr(service, "PLIST_PATH", tmp_path / "p.plist")
    monkeypatch.setattr(service, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(service, "HEALTH_FILE", tmp_path / "daemon.json")
    monkeypatch.setattr(service.sys, "platform", "darwin")
    monkeypatch.setattr(service.subprocess, "run",
                        lambda *a, **k: sp.CompletedProcess(a, 0, "", ""))
    monkeypatch.setattr(service, "_wait_for_job",
                        lambda *a, **k: {"loaded": True, "LastExitStatus": 0})
    r = service.install_launch_agent(interval=120)
    assert r["ok"] is False
    assert "never started polling" in r["error"]


def test_a_stale_heartbeat_reads_as_failed(tmp_path, monkeypatch):
    """A live pid is not enough; the daemon has to be completing checks."""
    import time as T
    from granola_router import service
    monkeypatch.setattr(service, "HEALTH_FILE", tmp_path / "daemon.json")
    state = {"installed": True, "running": True, "loaded": True, "pid": 99,
             "broken": False, "command": ["/x", "poll"], "config_mismatch": False,
             "heartbeat_age_seconds": 9999, "heartbeat_stale": True,
             "starting": False, "consecutive_failures": 0, "logs": "/tmp/l"}
    assert service.filing_status(state)["status"] == "failed"


def test_config_mismatch_is_reported(tmp_path):
    """A daemon filing into a different folder than status reads is broken."""
    from granola_router import service
    state = {"installed": True, "running": True, "loaded": True, "pid": 99,
             "broken": False, "command": ["/x", "poll"], "config_mismatch": True,
             "config_home": "/somewhere/else", "heartbeat_stale": False,
             "starting": False, "consecutive_failures": 0, "logs": "/tmp/l"}
    v = service.filing_status(state)
    assert v["status"] == "broken" and "/somewhere/else" in v["reason"]


def test_prune_keeps_the_running_binary(tmp_path, monkeypatch):
    """Pruning before the new daemon is proven throws away the rollback."""
    from granola_router import service
    monkeypatch.setattr(service, "BIN_ROOT", tmp_path / "s")
    monkeypatch.setattr(service, "VERSIONS", tmp_path / "s" / "versions")
    monkeypatch.setattr(service, "CURRENT", tmp_path / "s" / "current")
    monkeypatch.setattr(service, "running_frozen", lambda: True)
    for name in ("aaa", "bbb", "ccc"):
        d = tmp_path / "s" / "versions" / name
        d.mkdir(parents=True)
        (d / "granola-router").write_bytes(b"x")
    (tmp_path / "s" / "current").symlink_to(tmp_path / "s" / "versions" / "ccc")
    service.prune_old_versions(keep="ccc", retain=2)
    left = {p.name for p in (tmp_path / "s" / "versions").iterdir()}
    assert "ccc" in left, "must never delete the running version"
    assert len(left) == 2, "keeps the live one and one rollback"


def test_poll_interval_is_clamped(monkeypatch):
    """time.sleep(-1) inside a daemon crashes it outside the loop's own try."""
    from granola_router import cli as C
    slept = []
    monkeypatch.setattr(C.time, "sleep", lambda s: (slept.append(s), (_ for _ in ()).throw(KeyboardInterrupt()))[0])
    monkeypatch.setattr(C.service, "daemon_started", lambda i: None)
    monkeypatch.setattr(C.service, "write_health", lambda **k: None)

    class FakeSyncer:
        def __init__(self, **k): pass
        def poll_once(self):
            class S: written = errors = quarantined = 0
            def render(self=None): return "ok"
            S.render = render
            return S()
    monkeypatch.setattr(C, "Syncer", FakeSyncer)
    monkeypatch.setattr(C, "ProcessLock", lambda *a, **k: __import__("contextlib").nullcontext())
    import argparse
    args = argparse.Namespace(once=False, interval=-5, dry_run=True)
    with pytest.raises(KeyboardInterrupt):
        C.cmd_poll(args)
    assert slept and slept[0] >= 30, f"negative interval must be clamped, slept {slept}"


def test_enable_is_not_fooled_by_a_crash_looping_binary(tmp_path, monkeypatch):
    """A live pid is not proof. With KeepAlive on, a binary that exits instantly
    still shows a pid on every restart. Only a fresh heartbeat proves the poll
    loop actually started. Found by a live run, not by reading the code."""
    import subprocess as sp
    from granola_router import service
    monkeypatch.setattr(service, "PLIST_PATH", tmp_path / "p.plist")
    monkeypatch.setattr(service, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(service, "HEALTH_FILE", tmp_path / "daemon.json")
    monkeypatch.setattr(service.sys, "platform", "darwin")
    monkeypatch.setattr(service.subprocess, "run",
                        lambda *a, **k: sp.CompletedProcess(a, 0, "", ""))
    # launchctl always reports a pid, as it does for a crash loop.
    monkeypatch.setattr(service, "_launchctl_job",
                        lambda: {"loaded": True, "PID": 1234, "LastExitStatus": 0})
    monkeypatch.setattr(service.time, "sleep", lambda s: None)
    r = service.install_launch_agent(interval=60)
    assert r["ok"] is False, "a pid alone must not count as proof of filing"
    assert "never started polling" in r["error"]
    assert not (tmp_path / "p.plist").exists(), \
        "a failed enable must not leave a plist to crash-loop at next login"


def test_a_stale_heartbeat_from_a_previous_daemon_is_not_trusted():
    """Running, but nothing has ever reported in, is a failure not 'on'."""
    from granola_router import service
    state = {"installed": True, "running": True, "loaded": True, "pid": 5,
             "broken": False, "command": ["/x", "poll"], "config_mismatch": False,
             "heartbeat_age_seconds": None, "heartbeat_stale": False,
             "starting": False, "consecutive_failures": 0, "logs": "/tmp/l"}
    v = service.filing_status(state)
    assert v["status"] == "failed" and "never reported" in v["reason"]


def test_a_foreign_heartbeat_cannot_verify_this_install(tmp_path, monkeypatch):
    """Codex's race: another poller writes a fresh heartbeat while the job we
    just installed is dead. Generation tokens make the two distinguishable."""
    import json as J, time as T
    from granola_router import service
    health = tmp_path / "daemon.json"
    monkeypatch.setattr(service, "HEALTH_FILE", health)
    monkeypatch.setattr(service, "_launchctl_job",
                        lambda: {"loaded": True, "LastExitStatus": 0})
    monkeypatch.setattr(service.time, "sleep", lambda s: None)
    # A different daemon, started just now, with a completed tick.
    now = T.time()
    health.write_text(J.dumps({"started_at": now, "last_tick_finished": now,
                               "generation": "someone-else"}))
    job = service._wait_for_job(seconds=1, since=now, generation="mine")
    assert not job.get("heartbeat"), "a foreign heartbeat must not count as proof"
    assert not job.get("started")


def test_started_but_no_completed_check_is_not_reported_as_filing(tmp_path, monkeypatch):
    """Proving the loop started is weaker than proving it completed a check."""
    import json as J, time as T
    from granola_router import service
    health = tmp_path / "daemon.json"
    monkeypatch.setattr(service, "HEALTH_FILE", health)
    monkeypatch.setattr(service, "_launchctl_job", lambda: {"loaded": True, "PID": 7})
    monkeypatch.setattr(service.time, "sleep", lambda s: None)
    now = T.time()
    health.write_text(J.dumps({"started_at": now, "generation": "mine"}))
    job = service._wait_for_job(seconds=2, since=now, generation="mine")
    assert job.get("started") is True
    assert not job.get("heartbeat"), "no completed tick means no claim of filing"


def test_a_plist_with_no_pinned_config_is_flagged(tmp_path, monkeypatch):
    """An old plist resolves config at runtime, which may not be ours."""
    import plistlib
    from granola_router import service
    plist = tmp_path / "p.plist"
    binary = tmp_path / "gr"; binary.write_bytes(b"x"); os.chmod(binary, 0o755)
    with open(plist, "wb") as fh:
        plistlib.dump({"Label": service.LABEL,
                       "ProgramArguments": [str(binary), "poll"]}, fh)
    monkeypatch.setattr(service, "PLIST_PATH", plist)
    monkeypatch.setattr(service, "HEALTH_FILE", tmp_path / "d.json")
    st = service.launch_agent_state()
    assert st["config_mismatch"] is True
    assert service.filing_status(st)["status"] == "broken"


def test_health_writes_do_not_clobber_another_daemons_record(tmp_path, monkeypatch):
    """Two pollers sharing a config must not take turns looking healthy."""
    import json as J
    from granola_router import service
    health = tmp_path / "daemon.json"
    monkeypatch.setattr(service, "HEALTH_FILE", health)
    health.write_text(J.dumps({"pid": os.getpid() + 99999, "started_at": 1.0}))
    service.write_health(last_tick_finished=123.0)
    assert "last_tick_finished" not in J.loads(health.read_text()), \
        "a poller must only update a record it owns"


def test_failed_bootstrap_leaves_no_plist(tmp_path, monkeypatch):
    """launchctl refusing the job must not leave one to load at next login."""
    import subprocess as sp
    from granola_router import service
    monkeypatch.setattr(service, "PLIST_PATH", tmp_path / "p.plist")
    monkeypatch.setattr(service, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(service, "HEALTH_FILE", tmp_path / "d.json")
    monkeypatch.setattr(service.sys, "platform", "darwin")
    monkeypatch.setattr(service, "_bootstrap",
                        lambda: sp.CompletedProcess([], 1, "", "Load failed: 5"))
    monkeypatch.setattr(service, "_bootout", lambda: None)
    r = service.install_launch_agent(interval=120)
    assert r["ok"] is False
    assert not (tmp_path / "p.plist").exists()


def test_staged_binary_carries_no_blocking_xattrs(tmp_path, monkeypatch):
    """shutil.copy2 copies extended attributes, and a binary carrying
    com.apple.provenance or com.apple.quarantine hangs on launch from its new
    path. Measured: identical bytes run in a second without them and never
    start with them, so launchd just sits there filing nothing."""
    import sys as S
    from granola_router import service
    if S.platform != "darwin":
        pytest.skip("extended attributes are a macOS concern")
    monkeypatch.setattr(service, "BIN_ROOT", tmp_path / "s")
    monkeypatch.setattr(service, "VERSIONS", tmp_path / "s" / "versions")
    monkeypatch.setattr(service, "CURRENT", tmp_path / "s" / "current")
    monkeypatch.setattr(service, "running_frozen", lambda: True)

    src = tmp_path / "src-binary"
    src.write_bytes(b"#!/bin/sh\nexit 0\n")
    # os.setxattr/listxattr are Linux-only in CPython, hence the shelling out.
    subprocess.run(["xattr", "-w", "com.apple.quarantine", "0083;0;Safari;t", str(src)],
                   check=True)
    def attrs_of(p):
        r = subprocess.run(["xattr", str(p)], capture_output=True, text=True)
        return [a for a in r.stdout.split() if a]
    assert "com.apple.quarantine" in attrs_of(src)

    monkeypatch.setattr(service.sys, "executable", str(src))
    r = service.install_stable_binary()
    assert r["stable"]

    staged = tmp_path / "s" / "current" / "granola-router"
    attrs = attrs_of(staged)
    assert "com.apple.quarantine" not in attrs, f"staged copy still quarantined: {attrs}"
    assert "com.apple.provenance" not in attrs, f"staged copy carries provenance: {attrs}"
    assert staged.read_bytes() == src.read_bytes(), "content must be identical"
