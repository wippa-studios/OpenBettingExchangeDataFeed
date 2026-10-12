"""betfeed — two-tier Betfair feed on Postgres.

FEED 1  fixtures only, no odds.   Refreshed every 12h (default).
FEED 2  feed 1 + live odds.       Refreshed every 5m  (default).

Both feeds read from the same registry tables, so feed 2 knows what to poll
because feed 1 told it. Current state is upserted (always freshest); history is
appended to partitioned snapshot tables.

The package is deliberately import-safe: nothing here logs in to Betfair or
touches the database at import time. The MCP server, the collector and the
tests all import these modules and decide for themselves what to connect.
"""

__version__ = "1.0.0"