"""The domestic stand-in for event probabilities: what the option chain prices.

Polymarket answers "what are the odds the Fed cuts" and a China network cannot
reach it at all. The nearest thing China does price, on a host that answers in
0.1s, is where a broad index closes on an expiry date, so the vendor inverts the
SSE ETF option board into an expiry forward and a strike ladder of closing
odds. These tests pin the inversion, the vendor taxonomy, and the shapes of the
report, all against a synthetic chain -- no board call, no network.

The synthetic chain is a three-point distribution: S_T lands on 4.0, 4.5 or 5.0
with probabilities 0.5, 0.3, 0.2, so the forward is 4.35 and the odds of closing
above any strike between 4.0 and 4.5 are exactly 0.5, and above one between 4.5
and 5.0 exactly 0.2. Whatever the vendor reads off that chain must match, not
merely look plausible.
"""

from __future__ import annotations

import datetime
from unittest import mock

import pandas as pd
import pytest
import requests

from tradingagents.dataflows import router
from tradingagents.dataflows.config import run_config
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.vendors import sse_option

OUTCOMES = ((4.0, 0.5), (4.5, 0.3), (5.0, 0.2))
STRIKES = [round(4.1 + 0.1 * i, 2) for i in range(10)]  # 4.1 ... 5.0


def _call_price(strike: float) -> float:
    return round(sum(p * max(s - strike, 0.0) for s, p in OUTCOMES), 4)


def _put_price(strike: float) -> float:
    return round(sum(p * max(strike - s, 0.0) for s, p in OUTCOMES), 4)


def _frame(board: str, month: str, *, stale: bool = False) -> pd.DataFrame:
    """One SSE-style board: contract code, last price, strike, plus filler."""
    rows = []
    for strike in STRIKES:
        code_month = f"26{month}M"
        rows.append(
            {
                "日期": "20261110113218",
                "合约交易代码": f"510300C{code_month}{int(strike * 1000):05d}",
                "当前价": _call_price(strike),
                "涨跌幅": 0.0,
                "前结算价": 0.0,
                "行权价": strike,
                "数量": len(STRIKES) * 2,
            }
        )
        rows.append(
            {
                "日期": "20261110113218",
                "合约交易代码": f"510300P{code_month}{int(strike * 1000):05d}",
                "当前价": _put_price(strike),
                "涨跌幅": 0.0,
                "前结算价": 0.0,
                "行权价": strike,
                "数量": len(STRIKES) * 2,
            }
        )
    if stale:
        # A far strike nobody is quoting: the call prints above the one below it
        # and the put an impossible slope, so a reader that clipped instead of
        # dropping would report odds outside 0-100.
        rows.append(
            {
                "日期": "20261110113218",
                "合约交易代码": "510300C2611M05100",
                "当前价": 0.10,
                "涨跌幅": 0.0,
                "前结算价": 0.0,
                "行权价": 5.1,
                "数量": len(STRIKES) * 2,
            }
        )
        rows.append(
            {
                "日期": "20261110113218",
                "合约交易代码": "510300P2611M05100",
                "当前价": 0.90,
                "涨跌幅": 0.0,
                "前结算价": 0.0,
                "行权价": 5.1,
                "数量": len(STRIKES) * 2,
            }
        )
    return pd.DataFrame(rows)


@pytest.fixture
def boards():
    """Patch the AKShare board call, recording what was asked for."""
    calls: list[tuple[str, str]] = []

    def fake(symbol: str, end_month: str):
        calls.append((symbol, end_month))
        return _frame(symbol, end_month)

    with mock.patch("akshare.option_finance_board", side_effect=fake):
        yield calls


@pytest.mark.unit
def test_the_parity_of_the_chain_gives_the_expiry_forward():
    calls, puts = sse_option._quotes(_frame("b", "11"), "b", "11", "topic")
    forward = sse_option._forward(calls, puts, 1.0)
    assert forward == pytest.approx(4.35, abs=1e-6), (
        "every strike in a consistent chain prices the same forward"
    )


@pytest.mark.unit
def test_the_strike_slope_gives_the_odds_of_closing_above_it():
    calls, puts = sse_option._quotes(_frame("b", "11"), "b", "11", "topic")
    rows = sse_option._ladder(calls, puts, 1.0)
    assert rows, "a two-sided chain must invert"
    for strike, prob in rows:
        expected = 0.5 if strike < 4.5 else 0.2
        assert prob == pytest.approx(expected, abs=1e-6), (
            f"above {strike} the chain says {prob}, the distribution says {expected}"
        )
    assert [s for s, _ in rows] == sorted(s for s, _ in rows), "the ladder is ordered"
    assert all(p for _, p in rows) and max(p for _, p in rows) <= 1.0


@pytest.mark.unit
def test_an_impossible_quote_is_dropped_rather_than_making_odds_over_one():
    calls, puts = sse_option._quotes(_frame("b", "11", stale=True), "b", "11", "topic")
    rows = sse_option._ladder(calls, puts, 1.0)
    assert all(0.0 <= prob <= 1.0 for _, prob in rows), rows
    assert not any(strike >= 5.05 for strike, _ in rows), (
        "a strike whose only quotes are nonsense should not reach the report"
    )


@pytest.mark.unit
def test_odds_that_rise_with_the_strike_are_dropped_and_not_clipped():
    """A stale print can imply a higher barrier is easier to clear.

    The live 300 board read "0.2% above 4.75, 15.1% above 4.85, 0.2% above 4.95"
    in its tail, which contradicts itself. The offending row goes, and a later
    row that still decays survives on its own terms.
    """
    calls = {4.0: 0.50, 4.5: 0.40, 5.0: 0.30, 5.5: 0.45, 6.0: 0.05, 6.5: 0.0}
    rows = sse_option._ladder(calls, {}, 1.0)
    probs = [prob for _, prob in rows]
    assert all(
        a >= b - sse_option._ODDS_TOL for a, b in zip(probs, probs[1:])
    ), rows
    assert [round(strike, 3) for strike, _ in rows] == [4.25, 4.75, 6.25], (
        "5.25 and 5.75 sit on a curve that turns up, so neither may be shown"
    )
    assert rows[0][1] == pytest.approx(rows[1][1], abs=sse_option._ODDS_TOL), (
        "two equal slopes are two rows of one ladder, not a rise to censor"
    )


@pytest.mark.unit
def test_a_board_without_a_strike_column_is_reported_as_uncovered():
    """The SZSE and CFFEX payloads use other column names: say so, do not crash."""
    frame = pd.DataFrame([{"合约编码": "90001", "合约简称": "300ETF购12月4400", "类型": "认购"}])
    with pytest.raises(NoMarketDataError, match="无法反解概率"):
        sse_option._quotes(frame, "嘉实沪深300ETF期权", "12", "topic")


@pytest.mark.unit
def test_a_board_call_that_fails_asks_the_router_to_move_on(boards):
    with mock.patch(
        "akshare.option_finance_board",
        side_effect=requests.ConnectTimeout("yunhq.sse.com.cn timed out"),
    ):
        with pytest.raises(VendorUnavailableError, match="期权板获取失败"):
            sse_option.get_prediction_markets("沪深300 政策")


@pytest.mark.unit
def test_a_month_the_exchange_does_not_list_is_no_coverage(boards):
    def unlisted(symbol: str, end_month: str):
        raise KeyError("list")

    with mock.patch("akshare.option_finance_board", side_effect=unlisted):
        with pytest.raises(NoMarketDataError, match="挂牌合约"):
            sse_option.get_prediction_markets("沪深300 政策")


@pytest.mark.unit
def test_a_past_run_date_withholds_the_chain_without_reading_it(boards):
    today = datetime.date.today()
    past = (today - datetime.timedelta(days=30)).strftime("%Y-%m-%d")
    report = sse_option.get_prediction_markets("沪深300 政策", as_of_date=past)
    assert "不可用" in report and past in report
    assert boards == [], "a withheld report must not reach for the network"


@pytest.mark.unit
def test_the_report_is_text_and_labels_the_numbers_as_priced_odds(boards):
    report = sse_option.get_prediction_markets("沪深300 政策")
    assert isinstance(report, str), "the vendor contract is a text block"
    assert "隐含前向价 4.350" in report, report
    assert "50.0%" in report, "the odds of clearing 4.1-4.4 are half"
    assert "市场定价" in report and "不是预测" in report
    assert "波动率溢价" in report, "a risk-neutral number prices tails rich"


@pytest.mark.unit
def test_a_sector_topic_adds_the_board_that_carries_it(boards):
    sse_option.get_prediction_markets("沪深300 政策")
    assert {symbol for symbol, _ in boards} == {sse_option.DEFAULT_BOARD}, (
        "a topic naming no style is priced by the whole-market board alone"
    )

    boards.clear()
    sse_option.get_prediction_markets("半导体 国产替代 设备")
    asked = {symbol for symbol, _ in boards}
    assert "华夏科创50ETF期权" in asked, "chips are priced by the STAR board index"


@pytest.mark.unit
def test_an_english_sector_topic_still_picks_the_sector_board(boards):
    """Models ask in English as often as in Chinese, so both are keywords.

    The live A-share run asked "China semiconductor stock index" for a chip
    tooling name and got only the whole-market board; that miss is why the
    keyword lists carry English aliases.
    """
    sse_option.get_prediction_markets("China semiconductor stock index")
    asked = {symbol for symbol, _ in boards}
    assert "华夏科创50ETF期权" in asked, f"only {asked} was read"


@pytest.mark.unit
def test_an_unpriced_event_is_caveated_before_the_odds_are_shown(boards):
    report = sse_option.get_prediction_markets("Fed rate cut")
    assert "先说明" in report
    assert report.index("先说明") < report.index("收盘高于"), (
        "the caveat has to be read before the numbers, not under two tables of them"
    )

    plain = sse_option.get_prediction_markets("沪深300 估值")
    assert "先说明" not in plain, "an index topic has nothing to disclaim"


@pytest.mark.unit
def test_a_domestic_rate_cut_is_not_mislabelled_as_an_overseas_event(boards):
    """A PBoC cut is just as unpriced, so the wording must not name a geography."""
    report = sse_option.get_prediction_markets("央行降息 流动性")
    assert "先说明" in report, "an event the chain cannot price is still flagged"
    assert "海外" not in report, report


@pytest.mark.unit
def test_a_batch_reads_one_chain_rather_than_one_per_stock(boards):
    sse_option.get_prediction_markets("沪深300 政策")
    first = len(boards)
    sse_option.get_prediction_markets("沪深300 估值")
    assert len(boards) == first, "the parsed board is the day's, not per topic"

    sse_option.reset_quote_cache()
    sse_option.get_prediction_markets("沪深300 政策")
    assert len(boards) > first, "a cleared cache reads the chain again"


@pytest.mark.unit
def test_the_configured_domestic_chain_answers_the_tool(boards):
    """The A-share batch routes prediction_markets here; the tool must answer."""
    with run_config({"data_vendors": {"prediction_markets": "sse_option"}}):
        report = router.route_to_vendor("get_prediction_markets", "沪深300 政策", None, None)
    assert isinstance(report, str) and "隐含前向价" in report
    assert not report.startswith(("DATA_UNAVAILABLE", "NO_DATA_AVAILABLE")), report


@pytest.mark.unit
def test_a_historical_run_keeps_the_option_odds_out_of_the_past(boards):
    today = datetime.date.today()
    past = (today - datetime.timedelta(days=10)).strftime("%Y-%m-%d")
    with run_config({"data_vendors": {"prediction_markets": "sse_option"}}):
        report = router.route_to_vendor("get_prediction_markets", "沪深300 政策", None, past)
    assert "不可用" in report
