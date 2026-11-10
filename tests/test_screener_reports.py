"""The screener archives real reports, not just its summary table.

``--save``/``--html`` used to accept the flag and write nothing but a score
digest. These tests pin the deep-analysis path down: one report tree per stock,
the same tree the CLI writes, and a failed write that cannot lose a finished
analysis.
"""

import pytest

from tradingagents.dataflows.symbols import safe_ticker_component
from tradingagents.reporting import write_report_tree
from tradingagents.screener import batch_analyzer

NEWS_TEXT = "news analyst said: option ladder implies 26.6% below 1.275"
DECISION_TEXT = "portfolio manager: Hold with a reduced position"

STATE = {
    "market_report": "market report body",
    "sentiment_report": "sentiment report body",
    "news_report": NEWS_TEXT,
    "fundamentals_report": "fundamentals report body",
    "investment_debate_state": {"bull_history": "bull body", "bear_history": "bear body"},
    "investment_plan": "research manager plan",
    "trader_investment_plan": "trader plan",
    "risk_debate_state": {
        "aggressive_history": "aggressive body",
        "conservative_history": "conservative body",
        "neutral_history": "neutral body",
    },
    "final_trade_decision": DECISION_TEXT,
}


class _StubGraph:
    """propagate()/save_reports() with the same surface TradingAgentsGraph offers."""

    def __init__(self, debug=False, config=None):
        self.config = config or {}

    def propagate(self, ticker, trade_date):
        return dict(STATE, trade_date=trade_date), "Hold"

    def save_reports(self, final_state, ticker, save_path=None, html=True):
        return write_report_tree(final_state, ticker, save_path, settings={}, html=html)


@pytest.fixture
def stub_graph(monkeypatch):
    monkeypatch.setattr(batch_analyzer, "TradingAgentsGraph", _StubGraph)
    return _StubGraph


@pytest.mark.unit
def test_deep_analysis_writes_a_full_report_tree(tmp_path, stub_graph):
    result = batch_analyzer.analyze_single_stock(
        "002371.SZ", "北方华创", "2026-11-10", {}, report_dir=str(tmp_path)
    )

    stock_dir = tmp_path / safe_ticker_component("002371.SZ")
    complete = stock_dir / "complete_report.md"
    assert result["report_path"] == str(complete)
    assert complete.read_text(encoding="utf-8").count(NEWS_TEXT) == 1
    assert (stock_dir / "1_analysts" / "news.md").read_text(encoding="utf-8") == NEWS_TEXT
    assert (stock_dir / "5_portfolio" / "decision.md").exists()


@pytest.mark.unit
def test_html_page_accompanies_the_markdown(tmp_path, stub_graph):
    batch_analyzer.analyze_single_stock(
        "002371.SZ", "北方华创", "2026-11-10", {}, report_dir=str(tmp_path)
    )

    page = tmp_path / safe_ticker_component("002371.SZ") / "complete_report.html"
    assert page.exists() and "26.6%" in page.read_text(encoding="utf-8")


@pytest.mark.unit
def test_html_is_optional(tmp_path, stub_graph):
    batch_analyzer.analyze_single_stock(
        "002371.SZ", "北方华创", "2026-11-10", {}, report_dir=str(tmp_path), html=False
    )

    stock_dir = tmp_path / safe_ticker_component("002371.SZ")
    assert (stock_dir / "complete_report.md").exists()
    assert not (stock_dir / "complete_report.html").exists()


@pytest.mark.unit
def test_no_report_dir_means_no_files(tmp_path, stub_graph):
    result = batch_analyzer.analyze_single_stock("002371.SZ", "北方华创", "2026-11-10", {})

    assert result["success"] and "report_path" not in result
    assert list(tmp_path.iterdir()) == []


@pytest.mark.unit
def test_a_failed_write_cannot_lose_a_finished_analysis(tmp_path, monkeypatch):
    class _Unwrappable(_StubGraph):
        def save_reports(self, final_state, ticker, save_path=None, html=True):
            raise OSError("disk full")

    monkeypatch.setattr(batch_analyzer, "TradingAgentsGraph", _Unwrappable)
    result = batch_analyzer.analyze_single_stock(
        "002371.SZ", "北方华创", "2026-11-10", {}, report_dir=str(tmp_path)
    )

    assert result["success"] is True and result["rating"] == "Hold"
    assert "report_path" not in result


@pytest.mark.unit
def test_batch_forwards_the_archive_options(monkeypatch):
    seen = []

    def _record(ticker, name, analysis_date, config, report_dir=None, html=True):
        seen.append((ticker, report_dir, html))
        return {"ticker": ticker, "name": name, "rating": "Hold", "success": True}

    monkeypatch.setattr(batch_analyzer, "analyze_single_stock", _record)
    batch_analyzer.batch_analyze(
        [{"ticker": "002371.SZ", "name": "北方华创"}, {"ticker": "600519.SS", "name": "贵州茅台"}],
        analysis_date="2026-11-10",
        max_workers=1,
        report_dir="/tmp/reports",
        html=False,
    )

    assert seen == [("002371.SZ", "/tmp/reports", False), ("600519.SS", "/tmp/reports", False)]
