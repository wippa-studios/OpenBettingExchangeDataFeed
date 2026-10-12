"""Compatibility entry point.

The MCP server now lives in :mod:`betfeed.mcp_server` so it ships inside the
installed package and ``betfeed-mcp`` works from any working directory. This
file stays because existing MCP client configs point at it:

    {"command": ["uv", "run", "--directory", "<this folder>", "betfeed-mcp"]}

and because ``uv run server.py`` is the familiar way to start it by hand.
"""

import sys

from betfeed.mcp_server import main, mcp  # noqa: F401  (re-exported on purpose)

if __name__ == "__main__":
    sys.exit(main())