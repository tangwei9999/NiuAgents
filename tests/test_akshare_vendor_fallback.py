"""AKShare vendor 的 get_stock_data 同库内回退测试（全 monkeypatch，不碰实网）。

东财 push2 被限流时，价格数据依次回退腾讯、新浪（都是 AKShare 内的国内源、不回退
Yahoo）；三家都失败时才抛哨兵级错误，保持 router 原有语义：任一限流→
VendorUnavailableError（router → DATA_UNAVAILABLE），三家皆空→NoMarketDataError。
"""

import unittest.mock as mock

import pandas as pd
import pytest

from tradingagents.dataflows.errors import (
    NoMarketDataError,
    VendorUnavailableError,
)
from tradingagents.dataflows.vendors import akshare as vendor


def _tx_frame(start="2025-08-01", periods=5):
    idx = pd.date_range(start, periods=periods, freq="D")
    return pd.DataFrame({
        "date": idx, "open": 10.0, "close": 11.0, "high": 12.0, "low": 9.0,
        "volume": 1000, "turnover": 1.0, "amount": 1e4,
    })


def _sina_frame(start="2025-07-25", periods=30):
    idx = pd.date_range(start, periods=periods, freq="D")
    return pd.DataFrame({
        "date": idx, "open": 10.0, "high": 12.0, "low": 9.0, "close": 11.0,
        "volume": 1000, "amount": 1e4, "outstanding_share": 1e6, "turnover": 1.0,
    })


def _empty_tx():
    return pd.DataFrame(columns=["date", "open", "close", "high", "low", "volume",
                                 "turnover", "amount"])


def _empty_sina():
    return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume",
                                 "amount", "outstanding_share", "turnover"])


@pytest.mark.unit
def test_falls_to_tencent_when_eastmoney_rate_limited(monkeypatch):
    ak = mock.Mock()
    ak.stock_zh_a_hist.side_effect = Exception("push2 限流 Too Many Requests")
    ak.stock_zh_a_hist_tx.return_value = _tx_frame()
    monkeypatch.setitem(__import__("sys").modules, "akshare", ak)

    df = vendor.get_stock_data("600519.SS", "20250801", "20250810")

    assert not df.empty
    assert {"Open", "High", "Low", "Close", "Volume"} <= set(df.columns)
    assert isinstance(df.index, pd.DatetimeIndex)
    # 东财抛错即交回退链，不重试；腾讯被调用取数
    assert ak.stock_zh_a_hist_tx.called


@pytest.mark.unit
def test_falls_to_sina_and_slices_window_when_em_and_tencent_fail(monkeypatch):
    ak = mock.Mock()
    ak.stock_zh_a_hist.side_effect = Exception("限流")
    ak.stock_zh_a_hist_tx.side_effect = Exception("connection reset")
    # 新浪返回 07-25..08-23 全量，需裁到 [08-01, 08-10] 窗口
    ak.stock_zh_a_daily.return_value = _sina_frame("2025-07-25", periods=30)
    monkeypatch.setitem(__import__("sys").modules, "akshare", ak)

    df = vendor.get_stock_data("000001.SZ", "20250801", "20250810")

    assert df.index.min() == pd.Timestamp("2025-08-01")
    assert df.index.max() == pd.Timestamp("2025-08-10")
    # 深市代码拼成 sz 前缀（接口以关键字 symbol 传入）
    assert ak.stock_zh_a_daily.call_args.kwargs.get("symbol") == "sz000001"


@pytest.mark.unit
def test_all_throttled_raises_vendor_unavailable(monkeypatch):
    ak = mock.Mock()
    ak.stock_zh_a_hist.side_effect = Exception("push2 限流")
    ak.stock_zh_a_hist_tx.side_effect = Exception("限流")
    ak.stock_zh_a_daily.side_effect = Exception("限流")
    monkeypatch.setitem(__import__("sys").modules, "akshare", ak)

    with pytest.raises(VendorUnavailableError):
        vendor.get_stock_data("600519.SS", "20250801", "20250810")


@pytest.mark.unit
def test_all_empty_raises_no_market_data(monkeypatch):
    ak = mock.Mock()
    ak.stock_zh_a_hist.return_value = pd.DataFrame()   # 东财空 → 重试仍空
    ak.stock_zh_a_hist_tx.return_value = _empty_tx()   # 腾讯空
    ak.stock_zh_a_daily.return_value = _empty_sina()   # 新浪空
    monkeypatch.setitem(__import__("sys").modules, "akshare", ak)
    monkeypatch.setattr(vendor.time, "sleep", lambda _s: None)  # 跳过东财空重试等待

    with pytest.raises(NoMarketDataError):
        vendor.get_stock_data("600519.SS", "20250801", "20250810")


@pytest.mark.unit
def test_indicators_survive_eastmoney_throttle(monkeypatch):
    """get_indicators 走 get_stock_data，回退后技术指标仍出数。"""
    ak = mock.Mock()
    ak.stock_zh_a_hist.side_effect = Exception("限流")
    ak.stock_zh_a_hist_tx.return_value = _tx_frame("2024-01-01", periods=400)
    monkeypatch.setitem(__import__("sys").modules, "akshare", ak)

    out = vendor.get_indicators("600519.SS", "close_50_sma", "2025-08-05", look_back_days=5)

    assert isinstance(out, str) and "close_50_sma" in out
