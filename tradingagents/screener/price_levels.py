"""交易点位计算 - 为最终推荐股票给出阻力位、支撑位与建议买卖价。

点位口径（可解释、可手工复算）：
- 支撑位　　 = 近 20 个交易日最低价的 10% 分位（下沿支撑区，避开单根长下影）
- 阻力位　　 = 近 20 个交易日最高价的 90% 分位（上沿压力区，避开单根长上影）
- 建议买入价 = 支撑位上方 2%，且不低于现价 8%（回调过深的价位实际等不到）；
               若算出的买入价贴现价太近（回调不足 2%），则改为现价下方 3% 分批介入
- 建议卖出价 = 阻力位下方 2%，且不高于现价 12%（保住与买入价的盈亏比）；
               若现价已贴近/突破压力区（获利空间不足 3%），则改为现价上方 3% 的目标位

K 线取数沿用 ``data_source.py`` 的国内多源回退思路，不走 Yahoo：
东方财富（前复权）→ 腾讯 → 新浪 → BaoStock。

"""

from __future__ import annotations

import contextlib
import io
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd

from tradingagents.dataflows import net

logger = logging.getLogger(__name__)

# 波段窗口：20 个交易日 ≈ 1 个月，是短线常用的近高低点参照
_SWING_BARS = 20
# 取数回看自然日（覆盖 20 个交易日 + 节假日余量）
_FETCH_DAYS = 90

# 买/卖点位相对波段位的缓冲，以及现价贴边时的兜底幅度
_SUPPORT_BUFFER = 0.02
_RESISTANCE_BUFFER = 0.02
_FALLBACK_ENTRY_PCT = 0.03
_FALLBACK_TARGET_PCT = 0.03

# 支撑/阻力取波段高低点的分位数而非绝对 min/max：单根异常影线会把交易位推到
# 现价 ±20% 以外，给出一个根本等不到、也突破不了的价格。
_SUPPORT_QUANTILE = 0.10
_RESISTANCE_QUANTILE = 0.90

# 建议买入价最多低于现价 8%，建议卖出价最多高于现价 12%（约 1.5:1 盈亏比）
_MAX_ENTRY_DISTANCE = 0.08
_MAX_TARGET_DISTANCE = 0.12

# 最少操作空间：贴现价太近的买卖价没有执行价值（一个几乎等于现价的
# 卖点等于建议现价卖出），不够时一律让到 fallback 幅度。
_MIN_ENTRY_GAP_PCT = 0.02
_MIN_TARGET_GAP_PCT = 0.03


def _to_numeric(df: pd.DataFrame, cols: List[str]) -> pd.DataFrame:
    for col in cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _ohlc_eastmoney(ticker: str) -> pd.DataFrame:
    """East Money daily bars, qfq adjusted (has the full high/low range)."""
    import akshare as ak

    code = ticker.split(".")[0]
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=_FETCH_DAYS)).strftime("%Y%m%d")
    df = ak.stock_zh_a_hist(
        symbol=code, period="daily",
        start_date=start, end_date=end, adjust="qfq",
    )
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.rename(columns={"日期": "date", "最高": "high", "最低": "low", "收盘": "close"})
    return _to_numeric(df, ["high", "low", "close"])


def _ohlc_tencent(ticker: str) -> pd.DataFrame:
    """Tencent bars — fewer rate limits than East Money."""
    import akshare as ak

    code = ticker.split(".")[0]
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=_FETCH_DAYS)).strftime("%Y%m%d")
    df = ak.stock_zh_a_hist_tx(symbol=code, start_date=start, end_date=end, adjust="qfq")
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.rename(columns={"date": "date", "high": "high", "low": "low", "close": "close"})
    return _to_numeric(df, ["high", "low", "close"])


def _ohlc_sina(ticker: str) -> pd.DataFrame:
    """Sina bars, full history trimmed to the recent window."""
    import akshare as ak

    code = ticker.split(".")[0]
    prefix = "sh" if ticker.upper().endswith(".SS") else "sz"
    df = ak.stock_zh_a_daily(symbol=f"{prefix}{code}", adjust="qfq")
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.reset_index()
    return _to_numeric(df, ["high", "low", "close"])


def _ohlc_baostock(ticker: str) -> pd.DataFrame:
    """BaoStock bars — free, unmetered fallback.

    The SDK prints "login success!" / "logout success!" to stdout on every
    call, which would land in the middle of the recommendation block, so the
    whole exchange is muted. It also opens its socket with no timeout and reads
    it in a loop, so a server that stops mid-response would hold the run up
    forever; ``bounded_sockets`` is what keeps this fallback on a budget.
    """
    import baostock as bs

    code = ticker.split(".")[0]
    bs_code = f"sh.{code}" if ticker.upper().endswith(".SS") else f"sz.{code}"
    with net.bounded_sockets(), contextlib.redirect_stdout(io.StringIO()):
        lg = bs.login()
        if lg.error_code != "0":
            raise RuntimeError(f"BaoStock 登录失败：{lg.error_msg}")
        try:
            end = datetime.now().strftime("%Y-%m-%d")
            start = (datetime.now() - timedelta(days=_FETCH_DAYS)).strftime("%Y-%m-%d")
            rs = bs.query_history_k_data_plus(
                bs_code, "date,high,low,close",
                start_date=start, end_date=end, frequency="d", adjustflag="2",
            )
            rows = []
            while rs.error_code == "0" and rs.next():
                rows.append(rs.get_row_data())
            if not rows:
                return pd.DataFrame()
            df = pd.DataFrame(rows, columns=["date", "high", "low", "close"])
            return _to_numeric(df, ["high", "low", "close"])
        finally:
            bs.logout()


# 顺序与 data_source.py 的指标链（BaoStock 优先）不同：K 线要前复权，东财数据最全放首位；两条链都只走境内源，不访 Yahoo。
_OHLC_SOURCES = (
    ("东方财富", _ohlc_eastmoney),
    ("腾讯", _ohlc_tencent),
    ("新浪", _ohlc_sina),
    ("BaoStock", _ohlc_baostock),
)


def _fetch_ohlc(ticker: str) -> Optional[pd.DataFrame]:
    """First source that yields usable high/low/close bars, or None."""
    for label, fetcher in _OHLC_SOURCES:
        try:
            df = fetcher(ticker)
        except Exception as exc:
            logger.debug(f"{label} 获取 {ticker} K 线失败：{exc}")
            continue
        if df is None or df.empty or not {"high", "low", "close"} <= set(df.columns):
            logger.debug(f"{label} 返回 {ticker} 空 K 线数据")
            continue
        usable = df.dropna(subset=["high", "low", "close"])
        usable = usable[(usable["close"] > 0) & (usable["high"] > 0) & (usable["low"] > 0)]
        if len(usable) >= 5:
            logger.debug(f"{label} 成功获取 {ticker} K 线：{len(usable)} 根")
            return usable
    return None


def get_price_levels(ticker: str, bars: int = _SWING_BARS) -> Optional[Dict[str, float]]:
    """Compute swing resistance/support plus entry/target prices for one ticker.

    Returns None when no source can supply at least 5 usable bars, so the
    caller can print "N/A" instead of inventing a level.
    """
    df = _fetch_ohlc(ticker)
    if df is None:
        return None

    window = df.tail(bars)
    price = float(df["close"].iloc[-1])
    resistance = float(window["high"].quantile(_RESISTANCE_QUANTILE))
    support = float(window["low"].quantile(_SUPPORT_QUANTILE))

    # 取更接近现价的位：支撑过深时不致于给出等不到的挂单位（下限 price×0.92）
    buy_price = max(support * (1 + _SUPPORT_BUFFER),
                    price * (1 - _MAX_ENTRY_DISTANCE))
    if buy_price > price * (1 - _MIN_ENTRY_GAP_PCT):
        # 支撑就在脚下 / 已跌破支撑：改成现价下方回调介入位
        buy_price = price * (1 - _FALLBACK_ENTRY_PCT)

    # 同理封顶目标位：阻力过远时不致于给出涨不到的卖点（上限 price×1.12）
    sell_price = min(resistance * (1 - _RESISTANCE_BUFFER),
                     price * (1 + _MAX_TARGET_DISTANCE))
    if sell_price < price * (1 + _MIN_TARGET_GAP_PCT):
        # 现价已贴近/突破压力区：把目标位顺延到现价上方，而不是建议现价卖出
        sell_price = price * (1 + _FALLBACK_TARGET_PCT)

    return {
        "current_price": round(price, 2),
        "support": round(support, 2),
        "resistance": round(resistance, 2),
        "buy_price": round(buy_price, 2),
        "sell_price": round(sell_price, 2),
        "bars": int(len(window)),
    }


def attach_price_levels(stocks: List[Dict]) -> List[Dict]:
    """Fetch levels for each ranked stock and store them under ``price_levels``.

    Never raises: a data-source outage must not hide the ranking result, and a
    missing level is reported as N/A by :func:`format_level_line`.
    """
    for stock in stocks:
        ticker = stock.get("ticker", "")
        try:
            stock["price_levels"] = get_price_levels(ticker)
        except Exception as exc:
            logger.warning(f"计算 {ticker} 交易点位失败：{exc}")
            stock["price_levels"] = None
    return stocks


def format_level_line(stock: Dict) -> str:
    """One stock per line, in the screener's requested output format."""
    code = stock.get("ticker", "").split(".")[0]

    def _fmt(key: str) -> str:
        value = (stock.get("price_levels") or {}).get(key)
        return f"{value:.2f}" if isinstance(value, (int, float)) and value > 0 else "N/A"

    return (
        f"股票代码：{code}，股票名称：{stock.get('name', '')}，"
        f"阻力位：{_fmt('resistance')}，支撑位：{_fmt('support')}，"
        f"建议买入价：{_fmt('buy_price')}，建议卖出价：{_fmt('sell_price')}"
    )
