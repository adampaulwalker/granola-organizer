"""Background-job management, with no printing.

Deliberately free of I/O to stdout. The CLI wraps these and prints; the MCP
server wraps them and returns structured data. A stdio MCP server must emit
nothing but JSON-RPC on stdout, so any function reachable from a tool has to
stay silent.

The hard lesson encoded here: "launchctl accepted the job" is not the same as
"filing works". An earlier version reported automatic filing as on while the
job started, exited 0, and never filed anything. Every claim this module makes
about the daemon is now backed by a live process id and a heartbeat the daemon
itself writes.
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .api import DATA_DIR

LABEL = "com.granola-organizer.poll"
# The label this tool used before it was renamed. An agent left behind under the
# old label keeps polling forever, is invisible to a status command that only
# looks for the current one, and two daemons filing the same meetings overwrite
# each other every cycle.
#
# Deliberately NOT including com.granola-saver.poll. That is a different tool
# that people may still be running on purpose, and this one has no business
# deciding otherwise. Guessing wrong there cost a working install: an earlier
# version deleted that agent, and because the deletion happened through the
# filesystem rather than through the mocked subprocess layer, it fired from the
# test suite against a real home directory.
LEGACY_LABELS = ("com.granola-router.poll",)
LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
PLIST_PATH = LAUNCH_AGENTS_DIR / f"{LABEL}.plist"
LOG_DIR = Path.home() / "Library" / "Logs" / "granola-organizer"

# The daemon must not run the binary inside Claude's extension folder: removing
# or upgrading the extension would leave launchd pointing at something gone or
# stale. A copy lives here instead, keyed by content hash, with `current`
# repointed on upgrade so launchd picks the new one up on its next start.
BIN_ROOT = DATA_DIR / "bin"
VERSIONS = BIN_ROOT / "versions"
CURRENT = BIN_ROOT / "current"
BIN_NAME = "granola-organizer"

# The daemon writes here every cycle. Status reads it to tell a healthy idle
# daemon apart from one that launchd loaded and that then died quietly.
HEALTH_FILE = DATA_DIR / "daemon.json"

# How long past a due tick before the heartbeat counts as stale. Generous: a
# slow API call should not read as a failure.
HEARTBEAT_GRACE = 180
# A freshly started daemon has not written a tick yet.
START_GRACE = 90


def _sha8(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


def running_frozen() -> bool:
    """True when executing from a PyInstaller bundle rather than a Python install."""
    return bool(getattr(sys, "frozen", False))


# Attributes that make a copied binary unlaunchable. Removing them from our own
# already-validated executable is safe: the signature lives inside the file, so
# Gatekeeper still verifies it on first run.
_BLOCKING_XATTRS = ("com.apple.quarantine", "com.apple.provenance")


def _strip_xattrs(path: Path) -> Dict[str, Any]:
    """Try to clear extended attributes from a staged copy. Best effort only.

    Two things measured here, both worth stating plainly because they defeat
    the obvious implementation:

    - os.removexattr and its siblings are Linux-only in CPython, so the
      natural version of this function is a silent no-op on macOS.
    - `xattr -c` does not remove com.apple.provenance, and macOS re-applies
      com.apple.quarantine to a copy of a quarantined file regardless of
      whether the copy was made with copyfile or copy2.

    So this cannot be relied on, and nothing downstream should assume the
    staged binary is attribute-free. What actually makes the staged copy
    runnable is notarization; a quarantined copy of an unnotarized binary is
    killed no matter how it was written. This returns what is left so callers
    can report it rather than guess.
    """
    if sys.platform != "darwin":
        return {"stripped": False, "remaining": []}
    try:
        subprocess.run(["xattr", "-c", str(path)], capture_output=True, timeout=10)
        r = subprocess.run(["xattr", str(path)], capture_output=True, text=True, timeout=10)
        return {"stripped": True, "remaining": [a for a in r.stdout.split() if a]}
    except Exception:
        return {"stripped": False, "remaining": []}


# -- daemon heartbeat --------------------------------------------------------

def read_health() -> Dict[str, Any]:
    try:
        return json.loads(HEALTH_FILE.read_text())
    except Exception:
        return {}


def _atomic_write(path: Path, payload: Dict[str, Any]) -> None:
    """Write JSON via a pid-unique temp file.

    A shared temp name lets two pollers clobber each other mid-write, which is
    exactly the situation the heartbeat exists to detect.
    """
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    os.replace(tmp, path)


def write_health(**fields: Any) -> None:
    """Merge fields into the heartbeat.

    Never raises. The daemon must not die because its health file is
    unwritable; a missing heartbeat is reported as a failure by status, which
    is the correct outcome anyway.

    A poller only updates a record it owns. Two daemons sharing a config would
    otherwise take turns overwriting each other and both look healthy.
    """
    try:
        current = read_health()
        if current.get("pid") not in (None, os.getpid()):
            return
        current.update(fields)
        HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(HEALTH_FILE, current)
    except Exception:
        pass


def daemon_started(interval: int) -> None:
    """Called once by the poll loop at startup, replacing any previous record.

    Records the generation token launchd passed in. Enable stamps a fresh token
    into the plist and then waits for a heartbeat carrying that exact token, so
    a heartbeat left behind by some other poller cannot be mistaken for proof
    that this install works.
    """
    try:
        HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(HEALTH_FILE, {
            "pid": os.getpid(),
            "binary": sys.executable,
            "frozen": running_frozen(),
            "config_home": str(DATA_DIR),
            "generation": os.environ.get("GRANOLA_ORGANIZER_GENERATION"),
            "interval": interval,
            "started_at": time.time(),
            "consecutive_failures": 0,
        })
    except Exception:
        pass


# -- versioned binary staging ------------------------------------------------

def install_stable_binary(source: Optional[Path] = None) -> Dict[str, Any]:
    """Copy the running binary somewhere launchd can rely on.

    Content-addressed, so re-running with an unchanged binary is a no-op and
    shipping a new build lands beside the old one before `current` moves. Only
    meaningful when frozen; a plain Python install already has a stable path.

    Pruning deliberately does not happen here. Old versions are the rollback
    path, and deleting them before the new daemon is proven running throws that
    away at the exact moment it is most needed.
    """
    if not running_frozen() and source is None:
        return {"stable": False, "reason": "not a frozen binary; using the installed package"}

    src = Path(source or sys.executable).resolve()
    if not src.exists():
        return {"stable": False, "reason": f"binary not found at {src}"}

    digest = _sha8(src)
    target_dir = VERSIONS / digest
    target = target_dir / BIN_NAME

    upgraded = False
    if not target.exists():
        target_dir.mkdir(parents=True, exist_ok=True)
        tmp = target_dir / (BIN_NAME + ".partial")
        # copyfile, not copy2. copy2 also copies extended attributes, and a
        # binary carrying com.apple.provenance (or com.apple.quarantine) hangs
        # indefinitely on launch from its new path while macOS evaluates it -
        # measured: identical bytes run in one second without those attributes
        # and never start with them. launchd would just sit there.
        shutil.copyfile(src, tmp)
        _strip_xattrs(tmp)
        os.chmod(tmp, 0o755)
        os.replace(tmp, target)
        upgraded = True

    # Point `current` at this version. Replace atomically so a running daemon
    # never observes a missing symlink.
    BIN_ROOT.mkdir(parents=True, exist_ok=True)
    previous = os.readlink(CURRENT) if CURRENT.is_symlink() else None
    if previous != str(target_dir):
        tmp_link = BIN_ROOT / ".current.new"
        if tmp_link.exists() or tmp_link.is_symlink():
            tmp_link.unlink()
        tmp_link.symlink_to(target_dir)
        os.replace(tmp_link, CURRENT)
        upgraded = True

    return {"stable": True, "path": str(CURRENT / BIN_NAME),
            "version": digest, "upgraded": upgraded,
            "previous_version": Path(previous).name if previous else None}


def prune_old_versions(keep: str, retain: int = 2) -> None:
    """Drop stale copies, keeping the live one and a rollback.

    Called only after the new daemon is confirmed running, so a failed upgrade
    still has something to fall back to.
    """
    if not VERSIONS.exists():
        return
    protected = {keep}
    # Whatever launchd actually resolved is never a deletion candidate, even if
    # it is not the version this process believes is current.
    try:
        protected.add((CURRENT / BIN_NAME).resolve().parent.name)
    except Exception:
        pass
    dirs = sorted(
        (d for d in VERSIONS.iterdir() if d.is_dir() and d.name not in protected),
        key=lambda d: d.stat().st_mtime, reverse=True,
    )
    for d in dirs[max(0, retain - 1):]:
        shutil.rmtree(d, ignore_errors=True)


def daemon_argv(interval: int = 120) -> List[str]:
    """Command launchd should run.

    A frozen build invokes the stable copy of itself, which dispatches on argv
    (see entry_mcp.py). A Python install invokes the interpreter and module,
    which already lives at a fixed path.
    """
    if running_frozen():
        return [str(CURRENT / BIN_NAME), "poll", "--interval", str(interval)]
    return [sys.executable, "-m", "granola_organizer.cli", "poll", "--interval", str(interval)]


# -- launchd -----------------------------------------------------------------

_KV = re.compile(r'^\s*"?(PID|LastExitStatus)"?\s*=\s*(-?\d+);?\s*$')


def _launchctl_job() -> Dict[str, Any]:
    """Parse `launchctl list <label>`.

    Returns {} when the job is not loaded at all. A loaded job with no PID key
    has exited; that is the state the old code misread as running, because it
    only checked whether the label appeared anywhere in the output.
    """
    try:
        r = subprocess.run(["launchctl", "list", LABEL],
                           capture_output=True, text=True, timeout=10)
    except Exception:
        return {}
    if r.returncode != 0:
        return {}
    out: Dict[str, Any] = {"loaded": True}
    for line in (r.stdout or "").splitlines():
        m = _KV.match(line)
        if m:
            out[m.group(1)] = int(m.group(2))
    return out


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), capture_output=True, text=True)


def _bootout() -> None:
    """Stop the job. Modern interface first, falling back to the old one."""
    r = _run("launchctl", "bootout", f"{_domain()}/{LABEL}")
    if r.returncode != 0 and PLIST_PATH.exists():
        _run("launchctl", "unload", str(PLIST_PATH))


def detect_legacy_agents() -> List[Dict[str, Any]]:
    """Report launch agents left behind under this tool's previous label.

    Reports; does not remove. Deleting a launch agent is not a side effect to
    take on someone's behalf, and an earlier version that did it removed a
    different tool's working daemon from a test run. Anything destructive here
    has to be the person's explicit choice, so this returns what it found and
    the caller surfaces it.
    """
    found: List[Dict[str, Any]] = []
    for label in LEGACY_LABELS:
        plist = LAUNCH_AGENTS_DIR / f"{label}.plist"
        loaded = _run("launchctl", "list", label).returncode == 0
        if plist.exists() or loaded:
            found.append({
                "label": label,
                "loaded": loaded,
                "plist": str(plist),
                "how_to_remove": f"launchctl bootout gui/{os.getuid()}/{label} "
                                 f"&& rm {plist}",
            })
    return found


def _bootstrap() -> subprocess.CompletedProcess:
    r = _run("launchctl", "bootstrap", _domain(), str(PLIST_PATH))
    if r.returncode != 0:
        legacy = _run("launchctl", "load", str(PLIST_PATH))
        if legacy.returncode == 0:
            return legacy
    return r


# A staged copy is a file macOS has not seen before, so its first launch
# triggers an online Gatekeeper check. Measured at 11-18s locally and slower on
# a cold lookup, against a poll cycle that then takes time of its own. The wait
# is generous because the alternative is telling someone their install failed
# when it was only slow.
def _wait_for_job(seconds: float = 150.0, since: Optional[float] = None,
                  generation: Optional[str] = None) -> Dict[str, Any]:
    """Wait until the daemon proves it is really filing, or give up.

    A live process id alone is not proof, and testing showed exactly why: with
    KeepAlive on, a binary that exits instantly still shows a pid for a moment
    on every restart, so a naive pid check reports a crash loop as healthy.

    Proof has three parts, in increasing strength:
      - `started`   the poll loop ran and wrote a heartbeat for THIS install,
                    matched by generation token so another poller's heartbeat
                    cannot stand in for it;
      - `heartbeat` that daemon also completed a full check, which additionally
                    proves it can read the config and reach the API.

    Only a completed check justifies telling someone their meetings are being
    filed. A started-but-not-yet-finished daemon is reported as starting.
    """
    t0 = since if since is not None else time.time()
    deadline = t0 + seconds
    job: Dict[str, Any] = {}
    started_seen = False

    while time.time() < deadline:
        job = _launchctl_job()
        health = read_health()
        started = health.get("started_at")
        mine = (
            isinstance(started, (int, float))
            and started >= t0 - 1
            and (generation is None or health.get("generation") == generation)
        )
        if mine:
            started_seen = True
            finished = health.get("last_tick_finished")
            if isinstance(finished, (int, float)) and finished >= t0 - 1:
                job["heartbeat"] = True
                job["started"] = True
                return job
        time.sleep(0.5)

    job = _launchctl_job()
    job["started"] = started_seen
    return job


def launch_agent_state() -> Dict[str, Any]:
    """What the background job is actually doing, not what it was asked to do."""
    job = _launchctl_job()
    pid = job.get("PID")
    running = isinstance(pid, int) and pid > 0

    argv: List[str] = []
    plist_env: Dict[str, str] = {}
    if PLIST_PATH.exists():
        try:
            data = plistlib.loads(PLIST_PATH.read_bytes())
            argv = data.get("ProgramArguments", [])
            plist_env = data.get("EnvironmentVariables", {}) or {}
        except Exception:
            argv = []

    # A launch agent pointing at a binary that no longer exists is the failure
    # this whole versioned-copy scheme is meant to prevent, so report it.
    broken = False
    if argv:
        target = Path(argv[0])
        broken = not (target.exists() and os.access(target, os.X_OK))

    # The daemon outlives Claude and cannot inherit config from it. If the
    # plist pins a different config folder than the one this process reads,
    # status would describe an archive the daemon is not writing to.
    # An older plist with no pinned folder resolves its own config at runtime,
    # which may not be the folder this process reads. Treating "unset" as
    # "matches" would let status describe an archive the daemon is not writing.
    daemon_home = plist_env.get("GRANOLA_ORGANIZER_HOME")
    if argv and not daemon_home:
        config_mismatch = True
        daemon_home = None
    else:
        config_mismatch = bool(daemon_home) and Path(daemon_home) != Path(str(DATA_DIR))

    health = read_health()
    now = time.time()
    last_tick = health.get("last_tick_finished") or health.get("last_tick_started")
    started = health.get("started_at")
    interval = health.get("interval") or 120
    heartbeat_age = (now - last_tick) if isinstance(last_tick, (int, float)) else None
    stale = heartbeat_age is not None and heartbeat_age > interval + HEARTBEAT_GRACE
    starting = (
        last_tick is None
        and isinstance(started, (int, float))
        and (now - started) < START_GRACE
    )

    return {
        "installed": PLIST_PATH.exists(),
        "running": running,
        "loaded": bool(job.get("loaded")),
        "pid": pid,
        "last_exit_status": job.get("LastExitStatus"),
        "broken": broken,
        "command": argv,
        "config_home": daemon_home or str(DATA_DIR),
        "config_mismatch": config_mismatch,
        "heartbeat_age_seconds": round(heartbeat_age) if heartbeat_age is not None else None,
        "heartbeat_stale": stale,
        "starting": starting,
        "last_error": health.get("last_error"),
        "consecutive_failures": health.get("consecutive_failures", 0) or 0,
        "plist": str(PLIST_PATH),
        "logs": str(LOG_DIR / "poll.log"),
    }


def filing_status(state: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    """One word for what automatic filing is doing, plus why.

    Kept beside launch_agent_state so the CLI and the MCP server cannot drift
    into describing the same machine differently.
    """
    s = state if state is not None else launch_agent_state()
    logs = s.get("logs")

    if not s.get("installed"):
        return {"status": "off", "reason": "Automatic filing has not been turned on."}
    if s.get("broken"):
        where = s["command"][0] if s.get("command") else "an unknown path"
        return {"status": "broken",
                "reason": f"The background job points at {where}, which is missing or "
                          "not executable. Turn automatic filing on again to repair it."}
    if s.get("config_mismatch"):
        where = s.get("config_home")
        detail = (f"files into {where}, but this session reads {DATA_DIR}"
                  if where else
                  "was set up by an older version and does not record which folder "
                  "it files into")
        return {"status": "broken",
                "reason": f"The background job {detail}. Turn it on again to repoint it."}
    if not s.get("running"):
        if s.get("loaded"):
            return {"status": "failed",
                    "reason": f"The background job is loaded but not running (last exit "
                              f"status {s.get('last_exit_status')}). Nothing is being "
                              f"filed. Check {logs}."}
        return {"status": "installed_not_running",
                "reason": "The background job is installed but launchd is not running it. "
                          "Turn it on again to start it now."}
    if s.get("starting"):
        return {"status": "starting", "reason": "The background job has just started."}
    if s.get("heartbeat_age_seconds") is None:
        # launchd shows a process, but nothing has ever reported in. That is
        # what a binary which starts and dies looks like from the outside.
        return {"status": "failed",
                "reason": f"A process is running (pid {s.get('pid')}) but it has never "
                          f"reported completing a check, so nothing is being filed. "
                          f"Check {logs}."}
    if s.get("heartbeat_stale"):
        return {"status": "failed",
                "reason": f"The background job is running (pid {s.get('pid')}) but has not "
                          f"finished a check in {s.get('heartbeat_age_seconds')}s. "
                          f"Check {logs}."}
    if (s.get("consecutive_failures") or 0) >= 3:
        return {"status": "failed",
                "reason": f"The last {s['consecutive_failures']} checks failed: "
                          f"{s.get('last_error')}"}
    return {"status": "on", "reason": "Meetings are being filed in the background."}


def install_launch_agent(interval: int = 120,
                         source_binary: Optional[Path] = None) -> Dict[str, Any]:
    """Turn on automatic filing. Safe to call repeatedly; upgrades in place.

    Returns ok:false unless the job is confirmed running afterwards, because
    reporting success for a job that exited immediately is how this shipped
    broken the first time.
    """
    if sys.platform != "darwin":
        return {"ok": False, "error":
                "automatic filing is macOS only. Run 'granola-organizer poll "
                "--interval 120' from a systemd user service or cron instead."}

    interval = max(60, min(int(interval), 3600))

    stable = install_stable_binary(source_binary)
    if running_frozen() and not stable.get("stable"):
        return {"ok": False, "error": stable.get("reason", "could not stage the binary")}

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Stamped into the plist and echoed back in the heartbeat, so verification
    # can tell this install's daemon from any other poller on the machine.
    generation = f"{int(time.time())}-{os.getpid()}"

    plist: Dict[str, Any] = {
        "Label": LABEL,
        "ProgramArguments": daemon_argv(interval),
        "RunAtLoad": True,
        # A clean exit from a poll loop means it stopped early, which is a
        # failure. KeepAlive {SuccessfulExit: false} would leave it dead in
        # exactly the case that matters most.
        "KeepAlive": True,
        "ThrottleInterval": 60,
        "StandardOutPath": str(LOG_DIR / "poll.log"),
        "StandardErrorPath": str(LOG_DIR / "poll.err"),
        # Pinned unconditionally. The daemon outlives Claude, so it cannot
        # inherit config from Claude's environment, and leaving it unset lets
        # the daemon and the status tool resolve different folders.
        "EnvironmentVariables": {"GRANOLA_ORGANIZER_HOME": str(DATA_DIR),
                                 "GRANOLA_ORGANIZER_GENERATION": generation},
    }

    tmp = PLIST_PATH.with_suffix(".plist.tmp")
    with open(tmp, "wb") as fh:
        plistlib.dump(plist, fh)
    os.replace(tmp, PLIST_PATH)

    _bootout()
    legacy = detect_legacy_agents()

    # Clear the old heartbeat before starting. A previous daemon's record would
    # otherwise make a newly installed, immediately dying binary look healthy -
    # measured, not theoretical.
    t0 = time.time()
    try:
        HEALTH_FILE.unlink(missing_ok=True)
    except Exception:
        pass

    def _abandon() -> None:
        """Leave nothing behind. A plist left on disk after a failed enable is
        loaded again at next login and crash-loops there, out of sight."""
        _bootout()
        PLIST_PATH.unlink(missing_ok=True)

    r = _bootstrap()
    if r.returncode != 0:
        _abandon()
        return {"ok": False,
                "error": (r.stderr or r.stdout or "").strip()
                         or "launchctl could not start the background job"}

    job = _wait_for_job(since=t0, generation=generation)
    if not job.get("heartbeat"):
        if job.get("started"):
            # It is alive and running our code, but has not finished a check.
            # Usually a slow first sync; sometimes a missing API key. Say so
            # rather than claiming meetings are being filed.
            return {"ok": True, "interval": interval, "pending_first_check": True,
                    "upgraded": stable.get("upgraded", False),
                    "binary": stable.get("path"), "pid": job.get("PID"),
                    "message": ("The background job started but has not finished its "
                                f"first check yet. Check status in a minute, or see "
                                f"{LOG_DIR / 'poll.err'} if it stays that way."),
                    "state": launch_agent_state()}
        _abandon()
        return {
            "ok": False,
            "error": ("The background job was installed but never started polling "
                      f"(last exit status {job.get('LastExitStatus')}). Nothing "
                      f"would be filed, so it has been removed again rather than "
                      f"left to fail quietly. Check {LOG_DIR / 'poll.err'}."),
            "state": launch_agent_state(),
        }

    # Only now is the new build proven good enough to discard the old one.
    if stable.get("stable") and stable.get("version"):
        prune_old_versions(keep=stable["version"])

    return {"ok": True, "interval": interval, "upgraded": stable.get("upgraded", False),
            "binary": stable.get("path"), "pid": job.get("PID"),
            "legacy_agents_found": legacy,
            "state": launch_agent_state()}


def uninstall_launch_agent() -> Dict[str, Any]:
    """Turn off automatic filing. Saved transcripts are left alone."""
    if not PLIST_PATH.exists():
        return {"ok": True, "was_installed": False}
    _bootout()
    PLIST_PATH.unlink(missing_ok=True)
    return {"ok": True, "was_installed": True}
