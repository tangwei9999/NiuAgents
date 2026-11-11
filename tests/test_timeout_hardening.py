"""Offshore data sources must fail fast, once, and then stay skipped.

A host this network cannot reach is not a slow host: FRED and Polymarket sit
behind a wall that no read budget crosses, so the old flat 30s timeout charged
half a minute per topic per stock and filled the log with the same warning. The
fix is a short connect budget, a parking lot for hosts that fail at the connect
level, and a way to switch an optional category off without a request at all.
"""

import _socket
import socket
import time
from unittest import mock

import pandas as pd
import pytest
import requests

from tradingagents.dataflows import net


@pytest.mark.unit
def test_connect_budget_is_short_and_read_budget_stays_patient():
    """A connect that will never finish is worth three seconds; a slow body is
    still allowed to arrive, so the two budgets must not be one number."""
    assert net.CONNECT_TIMEOUT <= 3.0
    assert net.DEFAULT_TIMEOUT == (net.CONNECT_TIMEOUT, net.READ_TIMEOUT)
    assert net.READ_TIMEOUT >= 10.0

    from tradingagents.dataflows.vendors import fred, polymarket, sec_edgar
    from tradingagents.dataflows.vendors.alpha_vantage import common as alpha_vantage

    for budget in (polymarket.REQUEST_TIMEOUT, fred.REQUEST_TIMEOUT,
                   alpha_vantage.REQUEST_TIMEOUT, sec_edgar._REQUEST_TIMEOUT):
        assert isinstance(budget, tuple), "vendors must pass a split timeout"
        assert budget[0] == net.CONNECT_TIMEOUT


@pytest.mark.unit
@pytest.mark.parametrize("exc, parks", [
    (requests.ConnectTimeout("t"), True),
    (requests.ConnectionError("c"), True),
    (requests.ReadTimeout("r"), False),      # connected, merely slow: says nothing about the host
    (requests.HTTPError("404"), False),      # the host answered
    (TimeoutError("timed out"), True),
    (ConnectionResetError("reset"), True),
    (ValueError("bad json"), False),
])
def test_only_a_connect_level_failure_parks_a_host(exc, parks):
    assert net.is_connect_level_failure(exc) is parks


@pytest.mark.unit
def test_a_parked_host_costs_no_request_and_no_wait(polymarket_vendor):
    """The first failure pays the connect budget once; the rest of the run is
    charged nothing, which is the whole point of the parking lot."""
    calls = {"n": 0}

    def _boom(*a, **k):
        calls["n"] += 1
        raise requests.ConnectTimeout("simulated connect timeout")

    with mock.patch.object(polymarket_vendor.requests, "get", side_effect=_boom):
        first = polymarket_vendor.get_prediction_markets("Fed rate cut")
        t0 = time.perf_counter()
        second = polymarket_vendor.get_prediction_markets("recession 2026")
        third = polymarket_vendor.get_prediction_markets("US election")
        parked_for = time.perf_counter() - t0

    assert calls["n"] == 1, "a parked host must not be requested again"
    assert parked_for < 0.05, f"the skip path should cost nothing, took {parked_for:.3f}s"
    assert "unavailable" in first
    assert "unavailable" in second.lower()
    assert "skipped" in third


@pytest.mark.unit
def test_a_host_that_answers_is_not_parked(polymarket_vendor):
    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"events": []}

    with mock.patch.object(polymarket_vendor.requests, "get", return_value=_Resp()):
        polymarket_vendor.get_prediction_markets("Fed rate cut")

    assert not net.is_host_blocked(polymarket_vendor.GAMMA_BASE), (
        "a host that answered must be left open for the next call"
    )


@pytest.mark.unit
def test_parking_expires_and_the_host_is_probed_again(polymarket_vendor, monkeypatch):
    """The parking bounds the cost of an outage; it must not black the vendor
    for the life of a long-running process."""
    monkeypatch.setenv(net._BLOCK_COOLDOWN_ENV, "60")
    calls = {"n": 0}

    def _refuse(*a, **k):
        calls["n"] += 1
        raise requests.ConnectTimeout("simulated connect timeout")

    with mock.patch.object(polymarket_vendor.requests, "get", side_effect=_refuse):
        polymarket_vendor.get_prediction_markets("Fed rate cut")
        parked_at = net._blocked_until[net.host_of(polymarket_vendor.GAMMA_BASE)]
        assert parked_at > time.monotonic(), "the fresh parking should still be in force"
        polymarket_vendor.get_prediction_markets("US election")

    assert calls["n"] == 1, "a parked host is not requested"

    # Expire it by hand: the next call must pay the connect budget again rather
    # than skip forever.
    net._blocked_until[net.host_of(polymarket_vendor.GAMMA_BASE)] = time.monotonic() - 1
    with mock.patch.object(polymarket_vendor.requests, "get", side_effect=_refuse):
        polymarket_vendor.get_prediction_markets("Fed rate cut")

    assert calls["n"] == 2, "an expired parking must be retried"


@pytest.mark.unit
def test_cooldown_is_configurable(monkeypatch):
    """0 turns the parking off: a caller who would rather re-probe every time
    than risk a long blackout of a recovered host can say so."""
    monkeypatch.setenv(net._BLOCK_COOLDOWN_ENV, "0")
    net.block_host("https://api.fred.stlouisfed.org/graph")
    assert not net.is_host_blocked("https://api.fred.stlouisfed.org/graph")

    monkeypatch.setenv(net._BLOCK_COOLDOWN_ENV, "45")
    assert net.block_cooldown() == 45.0
    assert "45 seconds" in net.blocked_notice("https://api.fred.stlouisfed.org/graph")

    monkeypatch.setenv(net._BLOCK_COOLDOWN_ENV, "not-a-number")
    assert net.block_cooldown() == net._BLOCK_COOLDOWN_SECONDS


@pytest.mark.unit
def test_disabling_an_optional_category_makes_no_request(fred_vendor):
    """`none` is the answer for a source this network can never reach: the tool
    returns the sentinel without even reading the vendor's API key."""
    from tradingagents.dataflows import router
    from tradingagents.dataflows.config import run_config

    with (
        mock.patch.object(fred_vendor, "get_api_key",
                          side_effect=AssertionError("FRED must not be consulted")),
        run_config({"data_vendors": {"macro_data": "none",
                                     "prediction_markets": "none"}}),
    ):
        macro = router.route_to_vendor("get_macro_indicators", "cpi", "2026-11-10", 365)
        markets = router.route_to_vendor("get_prediction_markets", "Fed rate cut", 5, None)

    assert macro.startswith("DATA_UNAVAILABLE")
    assert markets.startswith("DATA_UNAVAILABLE")


@pytest.mark.unit
def test_a_core_category_cannot_be_disabled():
    """Prices, statements and news carry the decision; refusing to configure them
    beats silently reporting a market that was never queried."""
    from tradingagents.dataflows import router
    from tradingagents.dataflows.config import run_config

    with (
        run_config({"data_vendors": {"core_stock_apis": "none"}}),
        pytest.raises(ValueError, match="core and cannot be disabled"),
    ):
        router.route_to_vendor("get_stock_data", "600519.SS", "2026-08-01", "2026-11-10")


@pytest.mark.unit
def test_an_empty_vendor_list_is_not_read_as_disabled():
    """An unset category means "use what is available", not "skip the data"."""
    from tradingagents.dataflows import router
    from tradingagents.dataflows.config import run_config

    with mock.patch.object(router, "VENDOR_METHODS", {
        **router.VENDOR_METHODS,
        "get_stock_data": {"akshare": lambda *a, **k: "OK"},
    }), run_config({"data_vendors": {"core_stock_apis": ""}}):
        assert router.route_to_vendor("get_stock_data", "600519.SS", "a", "b") == "OK"


@pytest.mark.unit
def test_a_share_sentiment_reads_news_only():
    """StockTwits and Reddit do not index A-shares, and the TypeSafe screening
    call ahead of them is an offshore round trip for nothing."""
    from unittest.mock import MagicMock

    from tradingagents.agents.analysts import sentiment_analyst as sa

    fetched = []
    llm = MagicMock()
    llm.with_structured_output.side_effect = NotImplementedError("no structured")
    llm.invoke.return_value = MagicMock(content="report")

    def _run(ticker, screen):
        with mock.patch.object(sa, "fetch_stocktwits_messages",
                               side_effect=lambda *a, **k: fetched.append("st") or "st"), \
             mock.patch.object(sa, "fetch_reddit_posts",
                               side_effect=lambda *a, **k: fetched.append("rd") or "rd"), \
             mock.patch.object(sa, "jev_screen", screen), \
             mock.patch.object(sa.get_news, "func", lambda *a, **k: "news"):
            return sa.create_sentiment_analyst(llm)({
                "company_of_interest": ticker,
                "trade_date": "2026-11-10",
                "asset_type": "stock",
                "messages": [],
            })

    # An A-share must not even reach the screening call, so make it fatal.
    def _forbidden(*a, **k):
        raise AssertionError("no screening call for an A-share")

    report = _run("002371.SZ", _forbidden)["sentiment_report"]
    assert fetched == [], f"an A-share must not touch the offshore social sources: {fetched}"
    assert report, "the report still has to be produced from news alone"

    fetched.clear()
    _run("NVDA", lambda *a, **k: None)
    assert fetched == ["st", "rd"], "a US listing keeps the sources it has always used"


@pytest.mark.unit
def test_the_prompt_says_the_social_sources_do_not_cover_an_a_share():
    """Skipping the fetch is only half of it: the prompt must not describe a
    community that is not there, or the model will summarize thin air."""
    from tradingagents.agents.analysts import sentiment_analyst as sa

    message = sa._build_system_message(
        ticker="002371.SZ",
        start_date="2026-11-03",
        end_date="2026-11-10",
        news_block="news",
        stocktwits_block="<no A-share coverage>",
        reddit_block="<no A-share coverage>",
        subreddits=(),
    )

    assert "not covered for this listing" in message
    assert "neither social source indexes this listing" in message


@pytest.mark.unit
def test_akshare_news_answers_with_text_and_declines_other_listings():
    """The tool contract is a string: an empty list handed back for a symbol
    AKShare does not cover skipped the router's fallback and then broke the
    prompt that tried to treat it as news."""
    from tradingagents.dataflows.errors import NoMarketDataError
    from tradingagents.dataflows.vendors import akshare

    with pytest.raises(NoMarketDataError):
        akshare.get_news("AAPL", "2026-10-04", "2026-11-10")

    with pytest.raises(NoMarketDataError):
        akshare.get_insider_transactions("002371.SZ")

    frame = mock.Mock(empty=False)
    frame.iterrows.return_value = [(0, {
        "新闻标题": "北方华创三季度营收增长", "新闻内容": "设备出货放量",
        "新闻链接": "https://finance.eastmoney.com/a/1.html",
        "发布时间": "2026-11-06 17:01:14", "文章来源": "证券时报网",
    })]
    ak = mock.Mock()
    ak.stock_news_em.return_value = frame
    with mock.patch.dict("sys.modules", {"akshare": ak}), \
         mock.patch.object(akshare, "_check_akshare_available", lambda: True):
        out = akshare.get_news("002371.SZ", "2026-10-04", "2026-11-10")

    assert isinstance(out, str)
    assert "北方华创三季度营收增长" in out
    assert "2026-11-06" in out


@pytest.mark.unit
def test_a_failed_news_request_is_the_vendors_problem_not_the_listings():
    """Swallowing a raised request into an empty result told the router AKShare
    had answered "no news", so no fallback vendor was ever asked."""
    from tradingagents.dataflows.errors import VendorUnavailableError
    from tradingagents.dataflows.vendors import akshare

    ak = mock.Mock()
    ak.stock_news_em.side_effect = RuntimeError("connection reset")
    with (
        mock.patch.dict("sys.modules", {"akshare": ak}),
        mock.patch.object(akshare, "_check_akshare_available", lambda: True),
        pytest.raises(VendorUnavailableError),
    ):
        akshare.get_news("002371.SZ", "2026-10-04", "2026-11-10")


# ---------------------------------------------------------------------------
# Budgets held by the libraries themselves: AKShare's requests, BaoStock's
# sockets. Neither goes through net.py, so neither was covered by the connect
# budget and the parking lot above.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_akshare_is_put_on_a_budget_of_its_own():
    """AKShare ships (10, 60) and retries three times inside its own helper, so
    one interface meeting a stalled East Money could spend three quarters of a
    minute before the router saw the failure. Importing the vendor caps that."""
    pytest.importorskip("akshare")  # caps the real library's own default timeout
    from akshare.utils.context import config

    from tradingagents.dataflows.vendors import akshare as akshare_vendor

    connect, read = config.timeout
    assert connect <= 5.0, "a connect AKShare will never finish is not worth 10s"
    assert read <= 20.0
    assert config.timeout == akshare_vendor._akshare_timeout(), \
        "the budget parsed from the environment is the budget applied"


@pytest.mark.unit
@pytest.mark.parametrize("raw, want", [
    ("3,8", (3.0, 8.0)),
    ("7", (7.0, 7.0)),
    ("nonsense", (5.0, 20.0)),
    ("0,0", (5.0, 20.0)),
])
def test_the_akshare_budget_can_be_set_for_a_slower_network(monkeypatch, raw, want):
    """A network where the Chinese endpoints are genuinely slow should be able
    to ask for more time without editing the vendor."""
    from tradingagents.dataflows.vendors import akshare as akshare_vendor

    monkeypatch.setenv("TRADINGAGENTS_AKSHARE_TIMEOUT", raw)
    assert akshare_vendor._akshare_timeout() == want


@pytest.mark.unit
def test_raw_socket_budget_is_finite():
    assert 0 < net.RAW_SOCKET_TIMEOUT <= 30.0
    assert net.raw_socket_timeout() == net.RAW_SOCKET_TIMEOUT


@pytest.mark.unit
@pytest.mark.parametrize("raw, want", [("2.5", 2.5), ("junk", net.RAW_SOCKET_TIMEOUT)])
def test_the_raw_socket_budget_is_configurable(monkeypatch, raw, want):
    monkeypatch.setenv("TRADINGAGENTS_RAW_SOCKET_TIMEOUT", raw)
    assert net.raw_socket_timeout() == want


@pytest.mark.unit
def test_a_socket_opened_under_the_guard_inherits_it_and_nothing_does():
    """The point of setting the process default is that a library which builds
    a bare ``socket.socket()`` gets the budget copied onto it at creation --
    and that the guard is removed again on the way out."""
    previous = socket.getdefaulttimeout()
    with (
        net.bounded_sockets(seconds=2.0),
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock,
    ):
        assert sock.gettimeout() == 2.0
    assert socket.getdefaulttimeout() == previous
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as later:
        assert later.gettimeout() == previous


@pytest.mark.unit
def test_the_guard_cuts_a_server_that_accepts_and_then_says_nothing():
    """The BaoStock failure mode, reproduced: the connection opens and the
    response never ends. Inheriting a timeout only matters if the read gives
    up, so measure the stall instead of trusting the documentation.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    client = None
    try:
        with net.bounded_sockets(seconds=0.5):
            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        assert client.gettimeout() == 0.5, "the budget must be copied at creation"

        # The refusal installed for the suite is aimed at the internet; this is
        # the loopback listener above, so reach it through the C-level connect
        # the Python wrapper delegates to.
        _socket.socket.connect(client, listener.getsockname())
        started = time.monotonic()
        with pytest.raises(socket.timeout):
            client.recv(1024)
        assert time.monotonic() - started < 2.0
    finally:
        if client is not None:
            client.close()
        listener.close()


@pytest.mark.unit
def test_the_baostock_fallbacks_run_under_a_socket_budget(monkeypatch):
    """BaoStock is the last K-line source and the second source for the stock
    list, and its SDK reads in a ``while True`` with no timeout at all: an
    outage there used to stop the run instead of moving to the next source.

    The SDK is not contacted here -- the fake fails where its socket would be
    opened, which is what the guard has to have already covered."""
    bs = pytest.importorskip("baostock")

    seen = []

    def _stall(*args, **kwargs):
        seen.append(socket.getdefaulttimeout())
        raise RuntimeError("simulated BaoStock stall")

    from tradingagents.screener import data_source, price_levels

    monkeypatch.setattr(bs, "login", _stall)
    monkeypatch.setenv("TRADINGAGENTS_RAW_SOCKET_TIMEOUT", "2.5")

    with pytest.raises(RuntimeError):
        price_levels._ohlc_baostock("002371.SZ")
    assert data_source.get_technical_data_baostock("600519.SS") is None
    with pytest.raises(data_source.DataSourceError):
        data_source.get_stock_list_baostock()

    assert seen == [2.5, 2.5, 2.5], "every BaoStock exchange must be budgeted"


@pytest.mark.unit
def test_a_stalled_baostock_does_not_stop_the_price_levels_chain(monkeypatch):
    """The caller must be handed None and a finished run, not a hung process."""
    bs = pytest.importorskip("baostock")

    from tradingagents.screener import price_levels

    def _stall(*args, **kwargs):
        raise RuntimeError("simulated BaoStock stall")

    monkeypatch.setattr(bs, "login", _stall)
    monkeypatch.setattr(bs, "logout", lambda *a, **k: None)
    for name in ("_ohlc_eastmoney", "_ohlc_tencent", "_ohlc_sina"):
        monkeypatch.setattr(price_levels, name, lambda *a, **k: pd.DataFrame())

    started = time.monotonic()
    assert price_levels._fetch_ohlc("002371.SZ") is None
    assert time.monotonic() - started < 5.0


@pytest.fixture(autouse=True)
def _clean_parking_lot():
    net.reset_host_blocks()
    yield
    net.reset_host_blocks()


@pytest.fixture
def polymarket_vendor():
    from tradingagents.dataflows.vendors import polymarket

    return polymarket


@pytest.fixture
def fred_vendor():
    from tradingagents.dataflows.vendors import fred

    return fred
