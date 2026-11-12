"""新增三条数据源链路的单元测试（全部 monkeypatch，不碰实网）：

1. 新浪全市场快照作股票池第三源（data_source.get_stock_list_akshare_sina）
2. 腾讯实时行情解析（data_source.get_realtime_quotes_tencent）
3. Tushare moneyflow 作资金流第二源（pre_screener_v2，需 TUSHARE_TOKEN）

conftest 的 autouse 会拒绝真实 socket，这里所有网络都靠注入假模块/假响应完成。
"""

import sys
import types

import pandas as pd
import pytest


# ============================================================
# 假模块构造
# ============================================================

def _fake_akshare(**funcs):
    """构造一个只带指定函数的假 akshare 模块（供 import akshare as ak 使用）。"""
    mod = types.ModuleType("akshare")
    for name, fn in funcs.items():
        setattr(mod, name, fn)
    return mod


def _gtimg_line(symbol: str, **fields) -> str:
    """拼一行腾讯行情文本：默认 39 个 ~ 字段，按索引覆盖指定值。"""
    arr = [""] * 39
    arr[1] = fields.pop("name", "测试股")
    arr[2] = fields.pop("code", symbol[2:])
    for idx, key in {
        3: "price", 4: "prev_close", 32: "pct_chg",
        33: "high", 34: "low", 37: "amount", 38: "turnover",
    }.items():
        if key in fields:
            arr[idx] = str(fields.pop(key))
    return f'v_{symbol}="{"~".join(arr)}";'


# ============================================================
# 1. 新浪股票池第三源
# ============================================================

@pytest.mark.unit
def test_sina_source_filters_and_returns_clean_dicts(monkeypatch):
    from tradingagents.screener import data_source

    df = pd.DataFrame({
        "代码": ["sh600519", "sz000001", "sz300750", "sz000998", "sh600111"],
        "名称": ["贵州茅台", "平安银行", "宁德时代", "ST测试", "为零科技"],
        "最新价": [1650.0, 11.2, 180.5, 3.1, 0.0],   # 最后一行价格 <=0 应剔除
        "成交额": [3e9, 1e9, 2e9, 5e7, 4e7],
    })
    monkeypatch.setitem(sys.modules, "akshare", _fake_akshare(stock_zh_a_spot=lambda: df))

    stocks = data_source.get_stock_list_akshare_sina()
    codes = {s["code"] for s in stocks}
    assert codes == {"600519", "000001", "300750"}          # ST / 零价被剔除
    assert all("_amount" not in s for s in stocks)          # 排序辅助键已剔除
    assert {s["ticker"] for s in stocks} == {"600519.SS", "000001.SZ", "300750.SZ"}
    # 按成交额降序：茅台(3e9) > 宁德(2e9) > 平安(1e9)
    assert [s["code"] for s in stocks] == ["600519", "300750", "000001"]


@pytest.mark.unit
def test_fallback_uses_sina_when_em_and_baostock_fail(monkeypatch, tmp_path):
    from tradingagents.screener import data_source

    def _raise(*a, **k):
        raise Exception("被限流 Too Many Requests")

    df = pd.DataFrame({
        "代码": ["sh600519", "sz000001"],
        "名称": ["贵州茅台", "平安银行"],
        "最新价": [1650.0, 11.2],
        "成交额": [3e9, 1e9],
    })
    monkeypatch.setitem(sys.modules, "akshare", _fake_akshare(
        stock_zh_a_spot_em=_raise,        # 东财 spot 限流
        stock_zh_a_spot=lambda: df,        # 新浪可取
    ))
    monkeypatch.setitem(sys.modules, "baostock", None)   # BaoStock 不可用
    monkeypatch.setattr(data_source, "_STOCK_LIST_CACHE", tmp_path / "list.json")

    stocks = data_source.get_stock_list_with_fallback()
    assert {s["ticker"] for s in stocks} == {"600519.SS", "000001.SZ"}
    # 成功取数应写回缓存
    assert (tmp_path / "list.json").exists()


@pytest.mark.unit
def test_fallback_all_online_sources_fail_no_cache_builtin(monkeypatch, tmp_path):
    from tradingagents.screener import data_source

    def _raise(*a, **k):
        raise Exception("限流")

    monkeypatch.setitem(sys.modules, "akshare", _fake_akshare(
        stock_zh_a_spot_em=_raise, stock_zh_a_spot=_raise,
    ))
    monkeypatch.setitem(sys.modules, "baostock", None)
    monkeypatch.setattr(data_source, "_STOCK_LIST_CACHE", tmp_path / "missing.json")

    stocks = data_source.get_stock_list_with_fallback()
    # 三级在线源都失败、无缓存 → 内置池（去重后 38 只）
    assert len(stocks) == 38


# ============================================================
# 2. 腾讯实时行情解析
# ============================================================

class _Resp:
    def __init__(self, text):
        self.text = text
        self.encoding = "utf-8"


@pytest.mark.unit
def test_tencent_realtime_parses_price_pct_turnover(monkeypatch):
    from tradingagents.screener import data_source

    text = (
        _gtimg_line("sh600519", name="贵州茅台", code="600519",
                    price=1650.00, pct_chg=1.23, turnover=0.45, amount=8888.0)
        + "\n"
        + _gtimg_line("sz000001", name="平安银行", code="000001",
                      price=11.20, pct_chg=-0.5, turnover=0.9, amount=777.0)
    )

    def fake_get(url, **kwargs):
        return _Resp(text)

    req = types.ModuleType("requests")
    req.get = fake_get
    monkeypatch.setitem(sys.modules, "requests", req)

    quotes = data_source.get_realtime_quotes_tencent(["600519.SS", "000001.SZ"])
    assert quotes["600519"]["price"] == 1650.00
    assert quotes["600519"]["pct_chg"] == pytest.approx(1.23)
    assert quotes["600519"]["turnover"] == pytest.approx(0.45)
    assert quotes["000001"]["price"] == 11.20
    assert quotes["000001"]["pct_chg"] == pytest.approx(-0.5)


@pytest.mark.unit
def test_tencent_realtime_swallows_network_error(monkeypatch):
    from tradingagents.screener import data_source

    def boom(url, **kwargs):
        raise OSError("connect refused")

    req = types.ModuleType("requests")
    req.get = boom
    monkeypatch.setitem(sys.modules, "requests", req)

    # best-effort：网络失败不抛出、返回空
    assert data_source.get_realtime_quotes_tencent(["600519.SS"]) == {}


@pytest.mark.unit
def test_tencent_realtime_skips_non_sh_sz(monkeypatch):
    from tradingagents.screener import data_source

    called = {"n": 0}

    def fake_get(url, **kwargs):
        called["n"] += 1
        return _Resp("")

    req = types.ModuleType("requests")
    req.get = fake_get
    monkeypatch.setitem(sys.modules, "requests", req)

    # 北交所/非法代码不产生 symbol，直接返回空、不发请求
    assert data_source.get_realtime_quotes_tencent(["830799"]) == {}
    assert called["n"] == 0


# ============================================================
# 3. Tushare 资金流第二源
# ============================================================

@pytest.mark.unit
def test_tushare_dormant_without_token(monkeypatch):
    from tradingagents.screener import pre_screener_v2 as ps

    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    assert ps._tushare_ready() is False
    assert ps._tushare_session_flow(["600519"]) == {}


def _fake_tushare(trade_date: str, rows: list[dict]):
    """rows: [{ts_code, buy_elg_amount, sell_elg_amount, buy_lg_amount, sell_lg_amount}]"""
    cal = pd.DataFrame({"cal_date": [trade_date]})
    flow = pd.DataFrame(rows)

    class _Pro:
        def trade_cal(self, **k):
            return cal

        def moneyflow(self, **k):
            return flow

    mod = types.ModuleType("tushare")
    mod.pro_api = lambda token: _Pro()
    return mod


@pytest.mark.unit
def test_tushare_session_flow_computes_main_net(monkeypatch):
    from tradingagents.screener import pre_screener_v2 as ps

    monkeypatch.setenv("TUSHARE_TOKEN", "t")
    # 主力净额（万元）=（超大单买-卖）+（大单买-卖）
    rows = [
        {"ts_code": "600519.SH", "buy_elg_amount": 100, "sell_elg_amount": 40,
         "buy_lg_amount": 30, "sell_lg_amount": 20},      # net=70 万 → 700000 元
        {"ts_code": "000001.SZ", "buy_elg_amount": 10, "sell_elg_amount": 50,
         "buy_lg_amount": 5, "sell_lg_amount": 5},        # net=-40 万 → 负
    ]
    monkeypatch.setitem(sys.modules, "tushare", _fake_tushare("20261111", rows))

    flows = ps._tushare_session_flow(["600519", "000001"])
    assert flows["600519"] == ("2026-11-11", pytest.approx(700000.0))
    # 000001 也返回（值可为负，由上层 >min_inflow 过滤）
    assert flows["000001"][1] == pytest.approx(-400000.0)


@pytest.mark.unit
def test_tushare_activates_when_em_rank_and_probe_fail(monkeypatch):
    """东财排行 + 个股精查都失败、universe 提供候选、Tushare 就绪 → 出结果。"""
    from tradingagents.screener import pre_screener_v2 as ps

    def _raise(*a, **k):
        raise Exception("push2 限流")

    monkeypatch.setitem(sys.modules, "akshare", _fake_akshare(
        stock_individual_fund_flow_rank=_raise,   # 候选池排行失败
        stock_individual_fund_flow=_raise,         # 个股精查失败
    ))
    monkeypatch.setattr(ps, "_prev_day_flow_cache", {})
    monkeypatch.setenv("TUSHARE_TOKEN", "t")
    rows = [{"ts_code": "600519.SH", "buy_elg_amount": 100, "sell_elg_amount": 10,
             "buy_lg_amount": 20, "sell_lg_amount": 10}]    # net=100 万 → 正
    monkeypatch.setitem(sys.modules, "tushare", _fake_tushare("20261111", rows))

    universe = [{"ticker": "600519.SS", "name": "贵州茅台"}]
    stocks = ps.get_stock_list_by_previous_day_flow(
        min_inflow=0, max_stocks=100, universe=universe
    )
    assert stocks == [{"ticker": "600519.SS", "name": "贵州茅台"}]


# ============================================================
# 4. 第 4 步消息面降级：软放行不再中断 + 第 5 步不重引爆东财
# ============================================================

@pytest.mark.unit
def test_apply_news_screen_degrades_on_empty_sectors(monkeypatch):
    """拿不到利好板块（限流/本周无利好）→ 透传资金面候选、degraded=True，不调成分股。"""
    from tradingagents.screener import pre_screener_v2 as ps

    called = {"filter": 0}

    def _no_filter(stocks, sectors):
        called["filter"] += 1
        return []

    monkeypatch.setattr(ps, "filter_by_news_sector", _no_filter)
    ff = [{"ticker": "600519.SS", "name": "贵州茅台"}]
    out, degraded = ps.apply_news_screen(ff, {})
    assert out is ff and degraded is True
    assert called["filter"] == 0            # positive_sectors 为空时根本不会进筛选


@pytest.mark.unit
def test_apply_news_screen_degrades_on_no_match(monkeypatch):
    """有利好板块但无股票落入（成分股缺失或未命中）→ 透传、degraded=True。"""
    from tradingagents.screener import pre_screener_v2 as ps

    monkeypatch.setattr(ps, "filter_by_news_sector", lambda stocks, sectors: [])
    ff = [{"ticker": "600519.SS", "name": "贵州茅台"}]
    out, degraded = ps.apply_news_screen(ff, {"白酒": 60.0})
    assert out is ff and degraded is True


@pytest.mark.unit
def test_apply_news_screen_keeps_filter_when_hits(monkeypatch):
    """消息面正常筛出股票 → 返回子集、degraded=False。"""
    from tradingagents.screener import pre_screener_v2 as ps

    hit = [{"ticker": "600519.SS", "name": "贵州茅台", "sector": "白酒", "sector_news_score": 60.0}]
    monkeypatch.setattr(ps, "filter_by_news_sector", lambda stocks, sectors: hit)
    ff = [
        {"ticker": "600519.SS", "name": "贵州茅台"},
        {"ticker": "000001.SZ", "name": "平安银行"},
    ]
    out, degraded = ps.apply_news_screen(ff, {"白酒": 60.0})
    assert out == hit and degraded is False


def _stub_step5_env(monkeypatch, ps):
    """把第 5 步里会碰东财的 helper 全部改为计数假函数，不碰网络。"""
    calls = {"sector": 0, "news": 0}

    def fake_sector(ticker):
        calls["sector"] += 1
        return "白酒"

    def fake_news(sector, days=7):
        calls["news"] += 1
        return 60

    monkeypatch.setattr(ps, "get_stock_sector", fake_sector)
    monkeypatch.setattr(ps, "get_sector_news_sentiment", fake_news)
    monkeypatch.setattr(ps, "get_hot_sectors", lambda top_n=15: set())
    monkeypatch.setattr(
        ps, "_build_fund_flow_map",
        lambda codes: ({
            c: {"main_force_net_inflow": 1e7, "main_force_net_pct": 1.0,
                "super_large_net_inflow": 1e7} for c in codes
        }, "今日"),
    )
    return calls


_STEP5_TECH = {
    "ticker": "600519.SS", "rsi": 40, "above_ma20": True, "macd_golden_cross": True,
    "volume_ratio": 1.5, "price": 100.0, "ma20": 98.0, "ma60": 95.0,
}


@pytest.mark.unit
def test_step5_skips_per_stock_lookup_when_news_degraded(monkeypatch):
    """降级透传（无 sector）+ lookup_news=False → 不逐只回查东财行业/新闻。"""
    from tradingagents.screener import pre_screener_v2 as ps

    calls = _stub_step5_env(monkeypatch, ps)
    stocks = [{"ticker": "600519.SS", "name": "贵州茅台"}]   # 无 sector，模拟透传
    screened = ps.pre_screen_stocks_multi_factor(
        stocks, [_STEP5_TECH], min_main_force_inflow=0,
        require_hot_sector=False, lookup_news=False,
    )
    assert len(screened) == 1
    assert calls["sector"] == 0 and calls["news"] == 0        # 关键：未重新引爆东财
    assert "sector" not in screened[0] and "sector_news_score" not in screened[0]


@pytest.mark.unit
def test_step5_does_per_stock_lookup_when_news_available(monkeypatch):
    """非降级（lookup_news=True）且股票无 sector → 仍会逐只回查（保留旧行为）。"""
    from tradingagents.screener import pre_screener_v2 as ps

    calls = _stub_step5_env(monkeypatch, ps)
    stocks = [{"ticker": "600519.SS", "name": "贵州茅台"}]
    screened = ps.pre_screen_stocks_multi_factor(
        stocks, [_STEP5_TECH], min_main_force_inflow=0,
        require_hot_sector=False, lookup_news=True,
    )
    assert len(screened) == 1
    assert calls["sector"] == 1 and calls["news"] == 1         # 行业与新闻各回查 1 次
    assert screened[0].get("sector") == "白酒"
    assert screened[0].get("sector_news_score") == 60


# --------------------------------------------------------------------------- # #
# 盘前资金面逐只精查：整池上限 + 限流熔断（不 hammer 被限流的东财个股接口）
# --------------------------------------------------------------------------- # #
@pytest.mark.unit
def test_ensure_prev_day_flow_circuit_breaks_when_first_batch_all_fail(monkeypatch):
    import tradingagents.screener.pre_screener_v2 as ps

    monkeypatch.setattr(ps, "_prev_day_flow_cache", {})
    calls = []

    def fake_inflow(code):
        calls.append(code)
        return None  # 模拟东财个股接口被限流，全部拿不到

    monkeypatch.setattr(ps, "_latest_session_inflow", fake_inflow)
    codes = [f"{600000 + i}" for i in range(100)]
    flows = ps._ensure_prev_day_flow(codes)

    # 首批全失败即熔断：只查第一批，不会把 100 只全打出去
    assert flows == {}
    assert len(calls) == ps._PROBE_CIRCUIT_BATCH < 100


@pytest.mark.unit
def test_ensure_prev_day_flow_continues_when_first_batch_has_hit(monkeypatch):
    import tradingagents.screener.pre_screener_v2 as ps

    monkeypatch.setattr(ps, "_prev_day_flow_cache", {})
    calls = []
    codes = [f"{600000 + i}" for i in range(100)]

    def fake_inflow(code):
        calls.append(code)
        if code == codes[0]:
            return ("2025-08-08", 1.0e7)  # 首批有命中 → 不熔断
        return None

    monkeypatch.setattr(ps, "_latest_session_inflow", fake_inflow)
    flows = ps._ensure_prev_day_flow(codes)

    assert len(calls) == 100  # 未熔断，全量精查
    assert codes[0] in flows


@pytest.mark.unit
def test_universe_fallback_caps_probe_count(monkeypatch):
    import tradingagents.screener.pre_screener_v2 as ps

    monkeypatch.setattr(ps, "_candidate_pool_from_rank", lambda *a, **k: {})  # 排行不可用
    captured = {}

    def fake_ensure(codes):
        captured["n"] = len(codes)
        return {}

    monkeypatch.setattr(ps, "_ensure_prev_day_flow", fake_ensure)
    monkeypatch.setattr(ps, "_tushare_session_flow", lambda *a, **k: {})  # 未配置 token

    universe = [{"ticker": f"{600000 + i}.SH", "name": f"股{i}"} for i in range(400)]
    out = ps.get_stock_list_by_previous_day_flow(min_inflow=0, max_stocks=100, universe=universe)

    assert out == []
    assert captured["n"] == ps._PRE_OPEN_UNIVERSE_PROBE_MAX  # 整池 400 被截到上限 300
