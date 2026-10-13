"""The MCP surface: every tool registered, and every documented parameter real.

``mcp_server.py`` shipped with no tests at all — nothing in the repository
imported it — so a tool could be renamed, lose a schema, or accept a parameter
it silently ignored and CI would stay green. Issue #8 notes that a
dependency-hygiene pass would delete the undeclared-but-imported package and
kill this module outright; ``test_every_module_imports`` below is the cheap
guard that covers that too.

Nothing here contacts Betfair: the client is replaced with a fake that records
what it was asked for.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil

import pytest

import betfeed.mcp_server as mcp_server
from conftest import (
    FakeBetting,
    FakeMarketBook,
    FakeRunnerBook,
    FakeScores,
    install_fake_client,
    make_cfg,
)

TOOLS = [
    "collector_config",
    "feed_health",
    "get_account_funds",
    "get_feed1_fixtures",
    "get_feed2_odds",
    "get_market_history",
    "list_event_types",
    "list_events",
    "list_market_book",
    "list_market_catalogue",
]


def _registered() -> set[str]:
    return set(mcp_server.mcp._tool_manager._tools)


# ── the module exists and every tool is there ─────────────────────────────

def test_every_module_imports():
    """Declared dependencies must match imports in BOTH directions.

    ``mcp_server.py`` imports `mcp.server.fastmcp` while pyproject declares
    `fastmcp` and not `mcp`. It works only because fastmcp depends on mcp, so
    an automated "remove unused packages" pass would break `betfeed-mcp` with
    no test noticing.
    """
    import betfeed

    failures = []
    for info in pkgutil.walk_packages(betfeed.__path__, prefix="betfeed."):
        try:
            importlib.import_module(info.name)
        except Exception as exc:  # noqa: BLE001 - the point is that ANY failure matters
            failures.append(f"{info.name}: {type(exc).__name__}: {exc}")
    assert not failures, "modules failed to import:\n" + "\n".join(failures)


def test_all_ten_tools_are_registered_with_a_schema():
    """A rename or a lost type annotation must fail here, not in a client."""
    registered = _registered()
    missing = set(TOOLS) - registered
    assert not missing, f"tools not registered: {sorted(missing)}"
    assert len(registered) == len(TOOLS), f"unexpected tools: {sorted(registered)}"

    for name in TOOLS:
        tool = mcp_server.mcp._tool_manager._tools[name]
        assert tool.description, f"{name} has no description"
        schema = tool.parameters
        assert schema.get("type") == "object", f"{name} has no object schema"
        assert "properties" in schema, f"{name} publishes no parameters"


def _code_without_docstring(fn) -> str:
    """The function's source with its docstring removed.

    Without this the docstring's own "Args:" list satisfies the check below on
    its own — a test that passes because the parameter is DESCRIBED rather than
    USED is exactly the failure this file exists to prevent.
    """
    src = inspect.getsource(fn)
    doc = fn.__doc__ or ""
    if doc:
        src = src.replace(doc, "")
    return src


def test_no_tool_parameter_is_decorative():
    """Every declared parameter must be referenced in its own body.

    list_market_catalogue accepted market_type_codes, max_results and locale
    and forwarded none of them; list_market_book accepted include_best_offers
    and ladder_levels and forwarded neither. A parameter that changes nothing
    advertises control the caller does not have.

    Counted over the AST with the docstring removed. Counting occurrences in
    the raw source does not work: the docstring's own "Args:" list supplies the
    second occurrence, so a test that passes because the parameter is DESCRIBED
    rather than USED is precisely the failure this file exists to prevent.
    (Forwarding specifically is pinned by the recorder tests below.)
    """
    import ast

    for name in TOOLS:
        source = _code_without_docstring(getattr(mcp_server, name))
        tree = ast.parse(source)
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
        declared = {a.arg for a in list(fn.args.args) + list(fn.args.kwonlyargs)} - {"self"}
        used = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
        unused = declared - used
        assert not unused, f"{name} accepts {sorted(unused)} and never uses them"


def test_include_best_offers_is_gone_from_the_book_tool():
    """It was documented as 'include available to back/lay ladders' and did
    nothing; a book without best offers has no ladder, which is the one thing
    this tool exists to return."""
    params = inspect.signature(mcp_server.list_market_book).parameters
    assert "include_best_offers" not in params
    assert set(params) == {
        "market_ids", "include_traded_volume", "ladder_levels", "locale"
    }


def test_get_market_history_clamps_its_limit(monkeypatch):
    """min(max(limit, 1), 1000) — the one piece of real logic the tool has."""
    from betfeed import feeds
    import betfeed.feeds as feeds_mod

    calls = []

    def fake_history(conn, market_id, limit=200):
        calls.append(limit)
        return []

    monkeypatch.setattr(feeds_mod, "market_history", fake_history)
    monkeypatch.setattr(mcp_server, "_db", lambda: _nullcontext())

    for limit in (-5, 10**9, 7):
        assert "error" not in mcp_server.get_market_history("1.1", limit=limit), limit
    assert calls == [1, 1000, 7]
    assert feeds.MAX_MARKETS_PER_DOCUMENT == 1000


class _nullcontext:
    def __enter__(self):
        return None

    def __exit__(self, *a):
        return False


# ── the parameters actually reach the client ──────────────────────────────

class RecordingClient:
    """Stands in for BetfairClient and remembers how it was called."""

    def __init__(self):
        self.catalogue_calls: list[dict] = []
        self.book_calls: list[dict] = []

    def list_market_catalogue(self, event_ids, **kwargs):
        self.catalogue_calls.append(kwargs)
        return iter([])

    def list_market_book(self, market_ids, **kwargs):
        self.book_calls.append(kwargs)
        return iter([FakeMarketBook("1.1", [FakeRunnerBook(1, back=[(2.3, 10)])])])


@pytest.fixture
def recorder(monkeypatch):
    client = RecordingClient()
    monkeypatch.setattr(mcp_server, "trading", lambda: client)
    monkeypatch.setattr(mcp_server, "_cfg", lambda: make_cfg())
    return client


def test_catalogue_parameters_are_forwarded(recorder):
    mcp_server.list_market_catalogue(["3001"], market_type_codes=["MATCH_ODDS"], locale="fr")
    assert recorder.catalogue_calls[0]["market_type_codes"] == ["MATCH_ODDS"]
    assert recorder.catalogue_calls[0]["locale"] == "fr"

    mcp_server.list_market_catalogue(["3001"])
    assert recorder.catalogue_calls[1]["market_type_codes"] is None
    assert recorder.catalogue_calls[1]["locale"] is None


def test_book_parameters_are_forwarded(recorder):
    mcp_server.list_market_book(["1.1"], include_traded_volume=True, ladder_levels=5, locale="de")
    assert recorder.book_calls[0]["traded"] is True
    assert recorder.book_calls[0]["ladder_levels"] == 5
    assert recorder.book_calls[0]["locale"] == "de"


def test_a_catalogue_max_result_above_the_delay_key_cap_is_clamped(recorder):
    """A DELAY app key answers listMarketCatalogue above 200 with ANGX-0001,
    so a per-call max_results cannot raise the cap — but a smaller one is
    honoured."""
    from betfeed.betfair import BetfairClient

    cfg = make_cfg(CATALOGUE_MAX_RESULTS="200")
    sent = []

    class Recorder:
        def list_market_catalogue(self, filter=None, market_projection=None, max_results=1,
                                  sort=None, locale=None):
            sent.append(max_results)
            return []

        def list_events(self, *a, **k):
            return []

    client = BetfairClient(cfg, sleeper=lambda _s: None)
    install_fake_client(client, betting=Recorder(), scores=FakeScores())

    list(client._catalogue_for_events(["3001"], max_results=1000))
    list(client._catalogue_for_events(["3001"], max_results=50))
    assert sent == [200, 50], sent