"""Background-job management, with no printing.

Deliberately free of I/O to stdout. The CLI wraps these and prints; the MCP
server wraps them and returns structured data. A stdio MCP server must emit
nothing but JSON-RPC on stdout, so any function reachable from a tool has to
stay silent.
"""

from __future__ import annotations

import hashlib
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from .api import DATA_DIR

LABEL = "com.granola-router.poll"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG_DIR = Path.home() / "Library" / "Logs" / "granola-router"

# The daemon must not run the binary inside Claude's extension folder: removing
# or upgrading the extension would leave launchd pointing at something gone or
# stale. A copy lives here instead, keyed by content hash, with `current`
# repointed on upgrade so launchd picks the new one up on its next start.
BIN_ROOT = DATA_DIR / "bin"
VERSIONS = BIN_ROOT / "versions"
CURRENT = BIN_ROOT / "current"
BIN_NAME = "granola-router"


def _sha8(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


def running_frozen() -> bool:
    """True when executing from a PyInstaller bundle rather than a Python install."""
    return bool(getattr(sys, "frozen", False))


def install_stable_binary(source: Optional[Path] = None) -> Dict[str, Any]:
    """Copy the running binary somewhere launchd can rely on.

    Content-addressed, so re-running with an unchanged binary is a no-op and
    shipping a new build lands beside the old one before `current` moves. Only
    meaningful when frozen; a plain Python install already has a stable path.
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
        shutil.copy2(src, tmp)
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

    _prune_old_versions(keep=digest)
    return {"stable": True, "path": str(CURRENT / BIN_NAME),
            "version": digest, "upgraded": upgraded}


def _prune_old_versions(keep: str, retain: int = 2) -> None:
    """Drop stale copies, keeping the live one and a rollback."""
    if not VERSIONS.exists():
        return
    dirs = sorted(
        (d for d in VERSIONS.iterdir() if d.is_dir() and d.name != keep),
        key=lambda d: d.stat().st_mtime, reverse=True,
    )
    for d in dirs[retain - 1:]:
        shutil.rmtree(d, ignore_errors=True)


def daemon_argv(interval: int = 120) -> List[str]:
    """Command launchd should run.

    A frozen build invokes the stable copy of itself. A Python install invokes
    the interpreter and module, which already lives at a fixed path.
    """
    if running_frozen():
        return [str(CURRENT / BIN_NAME), "poll", "--interval", str(interval)]
    return [sys.executable, "-m", "granola_router.cli", "poll", "--interval", str(interval)]


def launch_agent_state() -> Dict[str, Any]:
    """Whether automatic filing is set up and currently loaded."""
    loaded = False
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True,
                             text=True, timeout=10)
        loaded = LABEL in out.stdout
    except Exception:
        pass

    argv: List[str] = []
    if PLIST_PATH.exists():
        try:
            argv = plistlib.loads(PLIST_PATH.read_bytes()).get("ProgramArguments", [])
        except Exception:
            argv = []

    # A launch agent pointing at a binary that no longer exists is the failure
    # this whole versioned-copy scheme is meant to prevent, so report it.
    broken = bool(argv) and not Path(argv[0]).exists()
    return {
        "installed": PLIST_PATH.exists(),
        "running": loaded,
        "broken": broken,
        "command": argv,
        "plist": str(PLIST_PATH),
        "logs": str(LOG_DIR / "poll.log"),
    }


def install_launch_agent(interval: int = 120,
                         source_binary: Optional[Path] = None) -> Dict[str, Any]:
    """Turn on automatic filing. Safe to call repeatedly; upgrades in place."""
    if sys.platform != "darwin":
        return {"ok": False, "error":
                "automatic filing is macOS only. Run 'granola-router poll "
                "--interval 120' from a systemd user service or cron instead."}

    stable = install_stable_binary(source_binary)
    if running_frozen() and not stable.get("stable"):
        return {"ok": False, "error": stable.get("reason", "could not stage the binary")}

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)

    plist: Dict[str, Any] = {
        "Label": LABEL,
        "ProgramArguments": daemon_argv(interval),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 60,
        "StandardOutPath": str(LOG_DIR / "poll.log"),
        "StandardErrorPath": str(LOG_DIR / "poll.err"),
    }
    # The daemon outlives Claude, so it cannot inherit config from Claude's
    # environment. Pin it explicitly when a non-default location is in use.
    if os.environ.get("GRANOLA_ROUTER_HOME"):
        plist["EnvironmentVariables"] = {
            "GRANOLA_ROUTER_HOME": os.environ["GRANOLA_ROUTER_HOME"]
        }

    tmp = PLIST_PATH.with_suffix(".plist.tmp")
    with open(tmp, "wb") as fh:
        plistlib.dump(plist, fh)
    os.replace(tmp, PLIST_PATH)

    subprocess.run(["launchctl", "unload", str(PLIST_PATH)], capture_output=True)
    r = subprocess.run(["launchctl", "load", str(PLIST_PATH)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return {"ok": False, "error": r.stderr.strip() or "launchctl load failed"}

    return {"ok": True, "interval": interval, "upgraded": stable.get("upgraded", False),
            "binary": stable.get("path"), "state": launch_agent_state()}


def uninstall_launch_agent() -> Dict[str, Any]:
    """Turn off automatic filing. Saved transcripts are left alone."""
    if not PLIST_PATH.exists():
        return {"ok": True, "was_installed": False}
    subprocess.run(["launchctl", "unload", str(PLIST_PATH)], capture_output=True)
    PLIST_PATH.unlink(missing_ok=True)
    return {"ok": True, "was_installed": True}
