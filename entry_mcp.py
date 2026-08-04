"""Entry point for the frozen single-file build.

One binary has to be two things. Claude Desktop spawns it with no arguments and
talks JSON-RPC over stdio; launchd spawns the same file as `... poll --interval
120` and expects a long-running poller. An earlier version ignored argv and
always started the MCP server, so the background job started a server, read EOF
from launchd's /dev/null stdin, exited 0, and filed nothing - while the status
tool still reported that automatic filing was on.

Dispatch on argv, and import lazily: the MCP path must not pay for argparse and
the CLI path must not require the `mcp` package to be importable.
"""

import sys

# Kept in step with the subparsers in granola_organizer.cli.main. A name here that
# argparse does not know still reaches argparse, which prints its own usage.
CLI_COMMANDS = {"backfill", "poll", "status", "install", "uninstall", "domains"}
SERVE_ALIASES = {"mcp", "serve"}


def main() -> int:
    argv = sys.argv[1:]

    if not argv or argv[0] in SERVE_ALIASES:
        from granola_organizer.mcp_server import main as serve
        serve()
        return 0

    if argv[0] in CLI_COMMANDS or argv[0] in ("-h", "--help", "-v", "--verbose"):
        from granola_organizer.cli import main as cli
        return cli(argv)

    sys.stderr.write(
        f"granola-organizer: unknown command {argv[0]!r}\n"
        "  no arguments, or 'mcp': run the MCP server on stdio\n"
        f"  {', '.join(sorted(CLI_COMMANDS))}: run that command\n"
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
