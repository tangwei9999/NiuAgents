"""AKShare vendor for A-share market data.

AKShare is a free, open-source financial data interface library that aggregates
data from East Money (东方财富), Tencent Finance (腾讯财经), Sina Finance (新浪财经),
and other Chinese financial data sources.

This vendor provides:
- OHLCV stock price data (get_stock_data)
- Technical indicators (get_indicators)
- Company fundamentals (get_fundamentals)
- News data (get_news)

Data source priority (internal to AKShare):
1. East Money (东方财富) - most comprehensive
2. Tencent Finance (腾讯财经) - less rate limiting
3. Sina Finance (新浪财经) - stable
"""

import logging
import os
import time
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
from stockstats import wrap

from tradingagents.dataflows.errors import (
    NoMarketDataError,
    VendorUnavailableError,
)

logger = logging.getLogger(__name__)

# AKShare ships a process-wide (connect, read) budget of (10, 60) and retries a
# failed request three times inside its own helper, so one interface meeting a
# stalled East Money could spend three quarters of a minute before the router
# ever saw a failure. Measured here on live endpoints: a daily-history call takes
# 2.5s, a news call 0.2s, and one page of the full-market snapshot under 3s, so
# a 20s read still leaves most of a page's worth of slack while cutting the
# worst case to about a minute. TRADINGAGENTS_AKSHARE_TIMEOUT="connect,read"
# overrides both, for a network where the vendors are slower than they are here.
_AKSHARE_TIMEOUT_ENV = "TRADINGAGENTS_AKSHARE_TIMEOUT"
_AKSHARE_TIMEOUT_DEFAULT = (5.0, 20.0)


def _akshare_timeout() -> tuple[float, float]:
    """The budget to hand AKShare, honoring the environment override."""
    raw = os.getenv(_AKSHARE_TIMEOUT_ENV, "").strip()
    if not raw:
        return _AKSHARE_TIMEOUT_DEFAULT
    try:
        connect, _, read = raw.partition(",")
        budget = (float(connect), float(read or connect))
        if min(budget) <= 0:
            raise ValueError(raw)
        return budget
    except ValueError:
        logger.warning(
            "%s=%r is not 'connect,read' in seconds; using %s",
            _AKSHARE_TIMEOUT_ENV, raw, _AKSHARE_TIMEOUT_DEFAULT,
        )
        return _AKSHARE_TIMEOUT_DEFAULT


def _apply_request_budget() -> None:
    """Set AKShare's global timeout once, at import, for the whole process.

    AKShare reads its config on every request rather than caching it, so this
    covers the screener's direct ``ak.stock_*`` calls as well as the ones made
    here. A release without the shared HTTP config keeps its own defaults: the
    vendor works either way, only the budget differs.
    """
    try:
        from akshare.utils.context import config
    except ImportError:
        return
    config.timeout = _akshare_timeout()


_apply_request_budget()


def _check_akshare_available():
    """Check if AKShare is installed and available."""
    try:
        import akshare as ak
        return True
    except ImportError:
        return False


def _convert_ticker_to_akshare_code(ticker: str) -> str:
    """
    Convert Yahoo-style ticker to AKShare code.

    Args:
        ticker: Yahoo-style ticker (e.g., "600519.SS", "000858.SZ")

    Returns:
        AKShare code (e.g., "600519", "000858")
    """
    return ticker.split(".")[0]


def _get_date_range(days: int = 90) -> tuple[str, str]:
    """Get start and end date strings for AKShare API calls."""
    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    return start_date, end_date


def _normalize_date_arg(value) -> str:
    """Compact YYYYMMDD, the only date format East Money APIs accept.

    Callers (tools, snapshot, indicators) pass ISO dates ("2025-10-05");
    handed through unchanged, East Money silently returns an empty frame
    instead of an error, which reads as "no data" and triggers a fallback
    to the next vendor in the chain.
    """
    return str(value).replace("-", "").replace("/", "")


def get_stock_data(
    symbol: str,
    start_date: str | None = None,
    end_date: str | None = None,
    **kwargs
) -> pd.DataFrame:
    """
    Get OHLCV stock price data from AKShare.

    Args:
        symbol: Ticker symbol (e.g., "600519.SS")
        start_date: Start date in YYYYMMDD format (default: 90 days ago)
        end_date: End date in YYYYMMDD format (default: today)

    Returns:
        DataFrame with columns: Date, Open, High, Low, Close, Volume
    """
    if not _check_akshare_available():
        raise VendorUnavailableError("AKShare is not installed")

    import akshare as ak

    code = _convert_ticker_to_akshare_code(symbol)

    if not start_date or not end_date:
        start_date, end_date = _get_date_range(90)
    # East Money needs compact YYYYMMDD; ISO separators yield empty frames.
    start_date = _normalize_date_arg(start_date)
    end_date = _normalize_date_arg(end_date)

    try:
        # Try East Money first (most comprehensive)
        df = ak.stock_zh_a_hist(
            symbol=code,
            period="daily",
            start_date=start_date,
            end_date=end_date,
            adjust="qfq"  # 前复权
        )
    except Exception as e:
        error_msg = str(e)
        if "限流" in error_msg or "Rate" in error_msg or "push2" in error_msg:
            raise VendorUnavailableError(f"AKShare East Money rate limited: {e}") from e
        raise VendorUnavailableError(f"AKShare East Money failed: {e}") from e

    # East Money occasionally returns an empty frame right after throttling
    # (especially under batch analysis load); retry once before giving up.
    if df.empty:
        time.sleep(1.5)
        try:
            df = ak.stock_zh_a_hist(
                symbol=code,
                period="daily",
                start_date=start_date,
                end_date=end_date,
                adjust="qfq"
            )
        except Exception as e:
            raise VendorUnavailableError(f"AKShare East Money failed: {e}") from e

    if df.empty:
        # Raised outside the request try/except so the router can treat it as
        # "no data" instead of misclassifying it as a vendor failure.
        raise NoMarketDataError(f"No data for {symbol} from AKShare East Money")

    # Rename columns to match expected format
    df = df.rename(columns={
        "日期": "Date",
        "开盘": "Open",
        "最高": "High",
        "最低": "Low",
        "收盘": "Close",
        "成交量": "Volume",
    })

    # Ensure correct data types
    df["Date"] = pd.to_datetime(df["Date"])
    df["Open"] = pd.to_numeric(df["Open"], errors="coerce")
    df["High"] = pd.to_numeric(df["High"], errors="coerce")
    df["Low"] = pd.to_numeric(df["Low"], errors="coerce")
    df["Close"] = pd.to_numeric(df["Close"], errors="coerce")
    df["Volume"] = pd.to_numeric(df["Volume"], errors="coerce")

    df = df.dropna(subset=["Close", "Volume"])
    df = df.set_index("Date")

    return df


def get_indicators(
    symbol: str,
    indicator: str | None = None,
    curr_date: str | None = None,
    look_back_days: int = 30,
    **kwargs
) -> str:
    """
    Get a single technical indicator window from AKShare.

    Matches the router/tool calling convention:
    route_to_vendor("get_indicators", symbol, indicator, curr_date, look_back_days)

    Args:
        symbol: Ticker symbol (e.g., "002371.SZ")
        indicator: stockstats-style indicator name (e.g. "rsi", "macd", "close_50_sma")
        curr_date: The current trading date, YYYY-mm-dd
        look_back_days: How many days of values to return

    Returns:
        Formatted string with one indicator value per day (same shape as the
        yfinance implementation so agents get a consistent report).
    """
    if not _check_akshare_available():
        raise VendorUnavailableError("AKShare is not installed")

    ind = (indicator or "").strip().lower()
    if not ind:
        raise VendorUnavailableError("No indicator specified")

    days = int(look_back_days or 30)
    end = curr_date or datetime.now().strftime("%Y-%m-%d")
    # Enough history for long-window indicators (e.g. close_200_sma)
    start = (pd.Timestamp(end) - pd.Timedelta(days=days + 320)).strftime("%Y-%m-%d")

    # Date-indexed OHLCV; raises VendorUnavailableError / NoMarketDataError
    df = get_stock_data(symbol, start, end)
    if df.empty:
        raise NoMarketDataError(f"No data for {symbol}")

    frame = df.reset_index()
    frame["Date"] = pd.to_datetime(frame["Date"], errors="coerce")
    frame = frame.dropna(subset=["Date"])
    frame = frame[frame["Date"] <= pd.Timestamp(end)].sort_values("Date")
    if frame.empty:
        raise NoMarketDataError(f"No data for {symbol} on or before {end}")

    stock_df = wrap(frame.copy())  # stockstats lowercases columns, adds indicator columns
    try:
        series = stock_df[ind]
    except Exception as e:
        raise VendorUnavailableError(f"Indicator '{ind}' not supported via stockstats: {e}") from e

    cutoff = pd.Timestamp(end) - pd.Timedelta(days=days)
    mask = frame["Date"] >= cutoff
    lines = []
    for dt, val in zip(frame.loc[mask, "Date"], series.loc[mask], strict=False):
        value = "N/A" if pd.isna(val) else f"{float(val):.4f}"
        lines.append(f"{dt.strftime('%Y-%m-%d')}: {value}")

    return (
        f"## {ind} values from {cutoff.strftime('%Y-%m-%d')} to {end}:\n\n"
        + "\n".join(lines)
        + "\n\n"
        + f"Source: AKShare East Money ({symbol})"
    )


def get_fundamentals(
    symbol: str,
    as_of_date: str | None = None,
    **kwargs
) -> dict:
    """
    Get company fundamentals from AKShare.

    Provides basic stock info and key financial metrics.

    Args:
        symbol: Ticker symbol (e.g., "600519.SS")

    Returns:
        Dictionary with fundamental data
    """
    if not _check_akshare_available():
        raise VendorUnavailableError("AKShare is not installed")

    import akshare as ak

    code = _convert_ticker_to_akshare_code(symbol)

    try:
        # Get individual stock info
        df = ak.stock_individual_info_em(symbol=code)

        if df.empty:
            raise NoMarketDataError(f"No fundamental data for {symbol}")

        # Convert to dictionary
        fundamentals = {}
        for _, row in df.iterrows():
            key = row.iloc[0]
            value = row.iloc[1]
            fundamentals[key] = value

        return {
            "symbol": symbol,
            "fundamentals": fundamentals,
            "source": "AKShare East Money",
        }

    except Exception as e:
        error_msg = str(e)
        if "限流" in error_msg or "Rate" in error_msg:
            raise VendorUnavailableError(f"AKShare rate limited: {e}") from e
        raise VendorUnavailableError(f"AKShare fundamentals failed: {e}") from e


def get_balance_sheet(
    symbol: str,
    freq: str = "quarterly",
    as_of_date: str | None = None,
    **kwargs
) -> pd.DataFrame:
    """
    Get balance sheet from AKShare.

    Uses stock_financial_analysis_indicator for reliable financial data.

    Args:
        symbol: Ticker symbol (e.g., "600519.SS")

    Returns:
        DataFrame with financial data
    """
    if not _check_akshare_available():
        raise VendorUnavailableError("AKShare is not installed")

    import akshare as ak

    code = _convert_ticker_to_akshare_code(symbol)

    try:
        df = ak.stock_financial_analysis_indicator(symbol=code)

        if df.empty:
            raise NoMarketDataError(f"No financial data for {symbol}")

        return df

    except Exception as e:
        error_msg = str(e)
        if "限流" in error_msg or "Rate" in error_msg:
            raise VendorUnavailableError(f"AKShare rate limited: {e}") from e
        raise VendorUnavailableError(f"AKShare balance sheet failed: {e}") from e


# Router/tool calling convention: (symbol, freq, as_of_date). All three
# statements share the same East Money financial-indicator table on AKShare,
# but they must be独立函数 with the right arity so the router can call them.
def get_cashflow(
    symbol: str,
    freq: str = "quarterly",
    as_of_date: str | None = None,
    **kwargs
) -> pd.DataFrame:
    return get_balance_sheet(symbol, freq=freq, as_of_date=as_of_date, **kwargs)


def get_income_statement(
    symbol: str,
    freq: str = "quarterly",
    as_of_date: str | None = None,
    **kwargs
) -> pd.DataFrame:
    return get_balance_sheet(symbol, freq=freq, as_of_date=as_of_date, **kwargs)


def _china_code(symbol: str) -> str | None:
    """The 6-digit East Money code for a China listing, or None for anything else.

    AKShare's endpoints are keyed on the A-share code, so a US, HK or crypto
    symbol has nothing to look up. Saying so as a typed error rather than
    answering with an empty frame matters: "empty" would read as "this
    listing has no news" instead of "not my listing, try the next vendor".
    """
    head = str(symbol).upper().split(".")[0].strip()
    return head if len(head) == 6 and head.isdigit() else None


def _article_lines(rows: list[dict]) -> str:
    """Format articles the way the other news vendors do, so one block shape reaches
    the agent whatever served it."""
    out = ""
    for row in rows:
        out += f"### {row['title']} (source: {row['source']}, {row['date']})\n"
        if row["content"]:
            out += f"{row['content']}\n"
        if row["url"]:
            out += f"Link: {row['url']}\n"
        out += "\n"
    return out


def _read_news_frame(fetch, symbol: str, vendor_note: str):
    """Run an East Money news call, turning any failure into a typed vendor error.

    A raised request is the vendor's problem, not the listing's: swallowing it
    into an empty result told the router AKShare had answered "no news", so a
    configured fallback was never tried and the empty list reached the prompt
    where a string was expected.
    """
    try:
        return fetch()
    except Exception as e:
        raise VendorUnavailableError(f"{vendor_note}: {e}") from None


def get_news(
    symbol: str,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int | None = None,
    **kwargs
) -> str:
    """
    News about a China-listed stock within [start_date, end_date], from East Money.

    Matches the router/tool calling convention:
    route_to_vendor("get_news", symbol, start_date, end_date)

    Args:
        symbol: Ticker symbol (e.g., "002371.SZ")
        start_date: Window start, YYYY-mm-dd (optional)
        end_date: Window end, YYYY-mm-dd (optional)
        limit: Maximum number of news articles to return (default 30)

    Returns:
        Formatted news block, the shape every news vendor hands the agent.

    Raises:
        NoMarketDataError: the symbol is not China-listed, or nothing fell in
            the window -- either way another vendor may still answer.
        VendorUnavailableError: AKShare is missing, or the request failed.
    """
    code = _china_code(symbol)
    if code is None:
        raise NoMarketDataError(
            symbol, detail="AKShare covers China-listed A-shares only"
        )

    if not _check_akshare_available():
        raise VendorUnavailableError("AKShare is not installed")

    import akshare as ak

    max_items = int(limit) if limit else 30

    df = _read_news_frame(
        lambda: ak.stock_news_em(symbol=code), symbol, "AKShare news request failed"
    )
    if df is None or df.empty:
        raise NoMarketDataError(symbol, canonical=code, detail="AKShare has no news for this listing")

    # Keep only the requested window. Dates are compared as compact YYYYMMDD
    # regardless of the caller's format, so ISO and compact bounds both behave.
    articles = []
    for _, row in df.iterrows():
        published = str(row.get("发布时间", ""))
        if start_date and end_date:
            day = _normalize_date_arg(published[:10])
            start_cmp = _normalize_date_arg(str(start_date)[:10])
            end_cmp = _normalize_date_arg(str(end_date)[:10])
            if day and (day < start_cmp or day > end_cmp):
                continue
        articles.append({
            "title": row.get("新闻标题", "") or "No title",
            "content": row.get("新闻内容", "") or "",
            "url": row.get("新闻链接", "") or "",
            "date": published,
            "source": row.get("文章来源", "") or "东方财富",
        })
        if len(articles) >= max_items:
            break

    if not articles:
        # East Money's feed is a recent sample, so an empty window is its
        # coverage, not the listing's: say which window is empty and let the
        # router ask the next vendor.
        raise NoMarketDataError(
            symbol, canonical=code,
            detail=f"no AKShare articles between {start_date} and {end_date}",
        )

    window = f", from {start_date} to {end_date}" if start_date and end_date else ""
    return f"## {symbol}{window} 新闻（东方财富）:\n\n{_article_lines(articles)}"


def get_global_news(
    curr_date: str | None = None,
    look_back_days: int | None = None,
    limit: int | None = None,
    **kwargs
) -> str:
    """
    China market press from East Money, as the global-news slot for A-share runs.

    Matches the router/tool calling convention:
    route_to_vendor("get_global_news", curr_date, look_back_days, limit)

    The block is labelled as the Chinese press it is, because a US-listed
    analysis configured to read this vendor would otherwise be handed Shanghai
    headlines as "global" news.

    Args:
        curr_date: Current date, YYYY-mm-dd (kept for convention)
        look_back_days: Days to look back (kept for convention)
        limit: Maximum number of news articles to return (default 30)

    Returns:
        Formatted news block.
    """
    if not _check_akshare_available():
        raise VendorUnavailableError("AKShare is not installed")

    import akshare as ak

    max_items = int(limit) if limit else 30

    # Shanghai Composite's feed is the market-wide proxy: East Money has no
    # all-market news endpoint, and the index carries the broadest coverage.
    df = _read_news_frame(
        lambda: ak.stock_news_em(symbol="000001"), "China market",
        "AKShare market news request failed",
    )
    if df is None or df.empty:
        raise NoMarketDataError("China market", detail="AKShare returned no market news")

    articles = []
    for _, row in df.head(max_items).iterrows():
        articles.append({
            "title": row.get("新闻标题", "") or "No title",
            "content": row.get("新闻内容", "") or "",
            "url": row.get("新闻链接", "") or "",
            "date": str(row.get("发布时间", "")),
            "source": row.get("文章来源", "") or "东方财富",
        })
    if not articles:
        raise NoMarketDataError("China market", detail="AKShare returned no usable market news")

    return f"## 中国市场新闻（东方财富，上证指数口径）:\n\n{_article_lines(articles)}"


def get_insider_transactions(symbol: str, *args, **kwargs) -> str:
    """Decline insider transactions: there is no such filing in the A-share market.

    Registered so a chain configured to AKShare alone (the A-share batch does
    exactly that) hands the tool a coverage answer instead of dying in the
    router, which drops any vendor not listed for a method and then has no
    vendor left to ask.
    """
    raise NoMarketDataError(
        symbol,
        detail="insider trading reports are a US filing concept; AKShare does not "
               "publish them, and no Chinese exchange does either",
    )
