#!/bin/bash
# Build the Claude Desktop extension (.mcpb).
#
# Produces a self-contained binary so users need no Python, then packs it with
# the manifest. Console build, never --windowed: a stdio MCP server must keep
# stdout clean and unbuffered.
set -euo pipefail
cd "$(dirname "$0")"

VENV=.venv-build
[ -x "$VENV/bin/pyinstaller" ] || { echo "run: uv pip install --python $VENV/bin/python 'mcp>=1.2' pyinstaller"; exit 1; }

rm -rf build dist extension/granola-router-mcp
"$VENV/bin/pyinstaller" \
  --onefile --console --clean --noconfirm \
  --name granola-router-mcp \
  --collect-submodules mcp.server \
  --collect-submodules mcp.shared \
  --collect-submodules mcp.types \
  --collect-submodules mcp_types \
  --exclude-module mcp.cli \
  --exclude-module typer \
  --hidden-import granola_router.service \
  --hidden-import granola_router.mcp_server \
  --hidden-import granola_router.cli \
  entry_mcp.py

cp dist/granola-router-mcp extension/granola-router-mcp
chmod +x extension/granola-router-mcp
echo "binary: $(du -h extension/granola-router-mcp | cut -f1)"

# The binary has to be two things at once, and shipping one that could only be
# an MCP server is exactly what broke automatic filing: launchd ran it with
# `poll`, it started a server, read EOF, exited 0, and filed nothing forever.
# Prove both modes before packing.
echo "smoke test: CLI mode"
./extension/granola-router-mcp poll --help > /dev/null || {
    echo "FAIL: the binary does not accept CLI arguments"; exit 1; }

echo "smoke test: the daemon path does not exit immediately"
# Two things this test must never do, both learned the hard way:
#
# 1. Touch the real config. An earlier version ran the poller with whatever
#    config the builder happened to have, so building the extension filed live
#    meetings into a real Dropbox folder. It runs in a throwaway home with no
#    API key, so it fails fast on config instead of syncing anything.
# 2. Leave the poller running. PyInstaller onefile forks a child, so killing
#    the pid returned by $! kills the bootloader and orphans the poller. Two of
#    those survived a build and spent half an hour fighting the real daemon
#    over the same files. Kill the whole process group.
SMOKE_HOME="$(mktemp -d)"
trap 'rm -rf "$SMOKE_HOME"' EXIT
printf '{"transcript_folder": "%s/out"}\n' "$SMOKE_HOME" > "$SMOKE_HOME/settings.json"

SMOKE_BIN="$PWD/extension/granola-router-mcp"
GRANOLA_ROUTER_HOME="$SMOKE_HOME" "$SMOKE_BIN" poll --interval 120 \
    < /dev/null > "$SMOKE_HOME/smoke.out" 2>&1 &
SMOKE=$!
sleep 4
if kill -0 $SMOKE 2>/dev/null; then
    kill -9 $SMOKE 2>/dev/null || true
    wait $SMOKE 2>/dev/null || true
    echo "  ok: still polling after 4s"
else
    wait $SMOKE 2>/dev/null || RC=$?
    echo "FAIL: the daemon exited after under 4s (code ${RC:-0}). launchd would file nothing."
    cat "$SMOKE_HOME/smoke.out"; exit 1
fi

# Killing $! only reaps the PyInstaller bootloader; the forked poller survives.
# macOS has no setsid, so sweep by the exact binary path instead. Two of these
# once outlived a build and spent half an hour rewriting real transcripts.
pkill -9 -f "^$SMOKE_BIN poll" 2>/dev/null || true
sleep 1
if pgrep -f "^$SMOKE_BIN poll" > /dev/null 2>&1; then
    echo "FAIL: a smoke-test poller is still running after the build"
    pgrep -lf "^$SMOKE_BIN poll"; exit 1
fi

echo "smoke test: MCP mode still starts"
GRANOLA_ROUTER_HOME="$SMOKE_HOME" ./extension/granola-router-mcp < /dev/null \
    > /dev/null 2> "$SMOKE_HOME/mcp.err" || {
    echo "FAIL: MCP mode did not start"; cat "$SMOKE_HOME/mcp.err"; exit 1; }
grep -q "MCP server starting" "$SMOKE_HOME/mcp.err" || {
    echo "FAIL: no MCP startup line on stderr"; cat "$SMOKE_HOME/mcp.err"; exit 1; }

if command -v mcpb >/dev/null 2>&1; then
    mcpb validate extension/manifest.json && mcpb pack extension granola-router.mcpb
else
    ( cd extension && zip -qr ../granola-router.mcpb . )
    echo "packed with zip (install 'mcpb' for manifest validation)"
fi
echo "built: $(du -h granola-router.mcpb | cut -f1) granola-router.mcpb"
