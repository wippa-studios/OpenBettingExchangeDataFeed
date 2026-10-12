"""Collector jobs. Each one is a function: (connection, client, config) -> stats.

Keeping them as plain functions rather than classes means the scheduler, the CLI
and the tests all drive exactly the same code path, and a job can be run once
by hand against a live database to see what it did.
"""

from .fixtures import run_fixtures
from .odds import run_odds

__all__ = ["run_fixtures", "run_odds"]