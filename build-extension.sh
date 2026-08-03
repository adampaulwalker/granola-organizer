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
  entry_mcp.py

cp dist/granola-router-mcp extension/granola-router-mcp
chmod +x extension/granola-router-mcp
echo "binary: $(du -h extension/granola-router-mcp | cut -f1)"

if command -v mcpb >/dev/null 2>&1; then
    mcpb validate extension/manifest.json && mcpb pack extension granola-router.mcpb
else
    ( cd extension && zip -qr ../granola-router.mcpb . )
    echo "packed with zip (install 'mcpb' for manifest validation)"
fi
echo "built: $(du -h granola-router.mcpb | cut -f1) granola-router.mcpb"
