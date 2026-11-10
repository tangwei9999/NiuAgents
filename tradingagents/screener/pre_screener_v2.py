"""多因子预筛选器 - 技术面 + 资金面 + 板块热度三维筛选。"""

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pandas as pd

logger = logging.getLogger(__name__)

# 盘前 / 非交易时段：东财“今日”资金流尚未生成（数值全为 NaN），此时若仍按
# 今日净流入筛选会得到 0 只股票。降级为以最近一个已完成交易日的净流入为准。
_PRE_OPEN_RANK_INDICATOR = "5日"  # 全市场活跃候选池来源（近 5 日，含最近交易日）
_PRE_OPEN_CANDIDATE_MULT = 2      # 候选池 = max_stocks × 该倍数
_PRE_OPEN_WORKERS = 16            # 逐只精查的并发度（实测 30 只 7.4s、0 失败）

_last_window_note = ""


def get_last_window_note() -> str:
    """最近一次资金面筛选使用的数据窗口（“今日”或“最近交易日 YYYY-MM-DD”）。"""
    return _last_window_note


def _set_window_note(note: str) -> None:
    global _last_window_note
    _last_window_note = note


def _find_inflow_col(df) -> str | None:
    """主力净流入净额列名（东财列名随接口变化，动态匹配）。"""
    for col in df.columns:
        if "主力净流入" in col and "净额" in col:
            return col
    return None


def _has_inflow_data(df, inflow_col) -> bool:
    """资金流数值是否已生成（盘前全为 NaN）。"""
    return int(pd.to_numeric(df[inflow_col], errors="coerce").notna().sum()) > 0


def _to_float(value) -> float | None:
    """数值化，无效/缺失返回 None（东财盘前数值列为 NaN 或空串）。"""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(result) else result


def _today_flow_expected_generated() -> bool:
    """东财“今日”资金流是否可能已生成（交易日 09:30 开盘后才有数值）。

    盘前/周末/节假日请求全市场排行要 30s+ 且必然返回 NaN，所以直接跳过。
    开盘后数据延迟等边界情况仍由“筛出 0 只则降级”兜底。
    """
    now = datetime.now()
    return now.weekday() < 5 and now.strftime("%H:%M") >= "09:30"


def _code_to_ticker(code) -> str | None:
    """6 位代码转 Yahoo 风格 ticker；非沪深（北交所等）返回 None。"""
    code = str(code).zfill(6)
    if code.startswith("6"):
        return f"{code}.SS"
    if code.startswith(("0", "3")):
        return f"{code}.SZ"
    return None


def _market_of(code) -> str | None:
    code = str(code).zfill(6)
    if code.startswith("6"):
        return "sh"
    if code.startswith(("0", "3")):
        return "sz"
    return None


def _latest_session_inflow(code: str) -> tuple | None:
    """(日期, 主力净流入) for a stock's latest completed session, or None.

    AKShare 的个股历史资金流表最后一行即为最近已完成交易日（盘前即昨日），
    正是盘前运行需要的参考口径。
    """
    import akshare as ak

    market = _market_of(code)
    if market is None:
        return None
    try:
        df = ak.stock_individual_fund_flow(stock=code, market=market)
    except Exception as e:
        logger.debug(f"获取 {code} 历史资金流失败：{e}")
        return None

    if df is None or df.empty:
        return None
    inflow_col = _find_inflow_col(df)
    if inflow_col is None:
        return None
    date_col = "日期" if "日期" in df.columns else df.columns[0]

    last = df.iloc[-1]
    value = _to_float(last.get(inflow_col))
    if value is None:
        return None
    return str(last[date_col]), value


# 最近交易日资金流缓存：第 3 步精查后第 5 步直接复用，避免重复请求。
# 查询失败的代码也缓存为 None，不会反复重试。
_prev_day_flow_cache: dict[str, tuple | None] = {}


def _ensure_prev_day_flow(codes: list[str]) -> dict[str, tuple]:
    """并发补齐 codes 的最近交易日主力净流入，返回查得有效值的 {code: (日期, 值)}。"""
    missing = [c for c in codes if c not in _prev_day_flow_cache]
    if missing:
        logger.info(f"精查 {len(missing)} 只股票的最近交易日主力净流入…")
        with ThreadPoolExecutor(max_workers=_PRE_OPEN_WORKERS) as ex:
            for code, hit in zip(missing, ex.map(_latest_session_inflow, missing), strict=False):
                _prev_day_flow_cache[code] = hit
    return {c: _prev_day_flow_cache[c] for c in codes if _prev_day_flow_cache.get(c)}


def get_hot_sectors(top_n: int = 10) -> set[str]:
    """
    获取资金净流入最多的热门板块。

    盘前/非交易时段今日板块资金流尚未生成，自动降级为近 5 日板块资金流。

    Args:
        top_n: 返回前 N 个热门板块

    Returns:
        热门板块名称集合
    """
    try:
        import akshare as ak

        indicator = "今日"
        df = ak.stock_sector_fund_flow_rank(indicator=indicator, sector_type="行业资金流")
        inflow_col = _find_inflow_col(df) if df is not None and not df.empty else None

        if inflow_col is None or not _has_inflow_data(df, inflow_col):
            indicator = _PRE_OPEN_RANK_INDICATOR
            logger.info("今日板块资金流尚未生成（盘前/非交易时段），改用近 5 日板块资金流")
            df = ak.stock_sector_fund_flow_rank(indicator=indicator, sector_type="行业资金流")
            inflow_col = _find_inflow_col(df) if df is not None and not df.empty else None

        if df is None or df.empty or inflow_col is None:
            logger.warning("无法获取板块资金流向数据")
            return set()

        # 显式按净流入降序：接口返回顺序并不保证是排名（盘前会返回无意义顺序）
        ranked = df.assign(
            _inflow=pd.to_numeric(df[inflow_col], errors="coerce")
        ).sort_values("_inflow", ascending=False)

        hot_sectors = set(ranked.head(top_n)["名称"].tolist())
        logger.info(f"热门板块（{indicator}，{len(hot_sectors)} 个）：{hot_sectors}")
        return hot_sectors

    except Exception as e:
        logger.warning(f"获取热门板块失败：{e}")
        return set()


def get_sector_news_sentiment(sector: str, days: int = 7) -> float:
    """
    获取板块消息面情绪评分。

    通过获取板块代表股的新闻数量和质量来评估板块消息面。

    Args:
        sector: 板块名称
        days: 回顾天数

    Returns:
        情绪评分 (0-100)，越高越利好
    """
    try:

        import akshare as ak

        # 获取板块成分股
        df = ak.stock_board_industry_cons_em(symbol=sector)

        if df.empty:
            return 0

        # 取前 3 只代表股获取新闻
        representative_stocks = df.head(3)["代码"].tolist()

        total_news = 0
        positive_keywords = ["利好", "增长", "突破", "创新", "政策支持", "订单", "盈利", "上涨", "景气"]

        for code in representative_stocks:
            try:
                news_df = ak.stock_news_em(symbol=code)

                if news_df.empty:
                    continue

                recent_news = news_df.head(20)  # 新闻接口返回按时间倒序，取最新 20 条

                for _, row in recent_news.iterrows():
                    title = str(row.get("新闻标题", ""))
                    content = str(row.get("新闻内容", ""))

                    # 检查是否包含利好关键词
                    for keyword in positive_keywords:
                        if keyword in title or keyword in content:
                            total_news += 1
                            break

            except Exception:
                continue

        # 计算情绪评分（新闻数量越多，评分越高，最高 100 分）
        sentiment_score = min(100, total_news * 10)
        return sentiment_score

    except Exception as e:
        logger.debug(f"获取 {sector} 消息面失败：{e}")
        return 0


def get_positive_news_sectors(days: int = 7, min_score: float = 30, sector_top_n: int = 20) -> dict[str, float]:
    """
    获取近 N 天有利好消息的板块（消息面筛选）。

    先取今日资金流入前 sector_top_n 的热门板块，再逐个评估消息面评分，
    返回评分 >= min_score 的板块及其评分。

    Args:
        days: 回顾天数
        min_score: 最低消息面评分阈值（0-100）
        sector_top_n: 参评的候选板块数量

    Returns:
        {板块名称: 消息面评分} 字典
    """
    candidate_sectors = get_hot_sectors(top_n=sector_top_n)
    if not candidate_sectors:
        logger.warning("无候选热门板块，消息面筛选跳过")
        return {}

    positive_sectors = {}
    for sector in candidate_sectors:
        score = get_sector_news_sentiment(sector, days=days)
        if score >= min_score:
            positive_sectors[sector] = score
            logger.info(f"利好板块：{sector}（消息面评分 {score:.0f}）")

    logger.info(f"消息面筛选：{len(candidate_sectors)} 个热门板块中 {len(positive_sectors)} 个近{days}天有利好")
    return positive_sectors


def filter_by_news_sector(stocks: list[dict[str, str]], positive_sectors: dict[str, float]) -> list[dict]:
    """
    按利好板块筛选股票，并附带板块消息面评分。

    采用反向匹配：拉取各利好板块的成分股列表构建“代码 → 板块”映射
    （每个板块仅 1 次调用），避免逐只查询个股行业信息（每只 1 次调用，
    100 只股票会串行调用 100 次导致长时间无响应）。

    Args:
        stocks: 股票列表
        positive_sectors: 利好板块及其评分 {板块名: 评分}

    Returns:
        属于利好板块的股票列表（含 sector / sector_news_score 字段）
    """
    if not positive_sectors:
        return []

    # 1. 拉取每个利好板块的成分股，构建 代码 -> (板块, 评分) 映射
    code_to_sector: dict[str, tuple] = {}
    try:
        import akshare as ak
    except ImportError:
        logger.warning("AKShare 不可用，无法构建板块成分股映射，消息面筛选跳过")
        return []

    for i, sector in enumerate(positive_sectors, 1):
        try:
            cons = ak.stock_board_industry_cons_em(symbol=sector)
            if cons is not None and not cons.empty and "代码" in cons.columns:
                for code in cons["代码"].astype(str):
                    code_to_sector.setdefault(code.zfill(6), (sector, positive_sectors[sector]))
            logger.info(f"板块成分股映射进度：{i}/{len(positive_sectors)} {sector}")
        except Exception as e:
            logger.debug(f"获取板块 {sector} 成分股失败：{e}")

    logger.info(f"已构建 {len(positive_sectors)} 个利好板块的成分股映射（共 {len(code_to_sector)} 只成分股）")

    # 2. 内存查表匹配，无需再逐只调接口
    filtered = []
    for stock in stocks:
        code = stock["ticker"].split(".")[0].zfill(6)
        hit = code_to_sector.get(code)
        if hit:
            sector, score = hit
            filtered.append({**stock, "sector": sector, "sector_news_score": score})

    logger.info(f"消息面筛选：{len(stocks)} -> {len(filtered)} 只股票属于利好板块")
    return filtered


def get_fund_flow_rank() -> pd.DataFrame | None:
    """
    获取全市场个股资金流向排名。

    Returns:
        DataFrame with columns: 代码, 名称, 今日主力净流入-净额, 今日主力净流入-净占比, etc.
    """
    try:
        import akshare as ak
        df = ak.stock_individual_fund_flow_rank(indicator="今日")

        if df.empty:
            logger.warning("无法获取个股资金流向数据")
            return None

        return df

    except Exception as e:
        logger.warning(f"获取资金流向排名失败：{e}")
        return None


# 全市场资金流排行在同一次选股过程中会被第 3 步和第 5 步各读一次，而单次请求
# 要 30s+，因此按日缓存复用（跨天自动失效；接口失败不缓存，下次会重试）。
_fund_flow_rank_cache: pd.DataFrame | None = None
_fund_flow_rank_cached_date = ""


def _get_fund_flow_rank_cached() -> pd.DataFrame | None:
    """当日内的全市场资金流排行副本（重复调用不重复请求）。"""
    global _fund_flow_rank_cache, _fund_flow_rank_cached_date

    today = datetime.now().strftime("%Y-%m-%d")
    if _fund_flow_rank_cached_date != today:
        _fund_flow_rank_cache = None
        _fund_flow_rank_cached_date = today

    if _fund_flow_rank_cache is None:
        _fund_flow_rank_cache = get_fund_flow_rank()
    # 返回副本：调用方的列变换不得污染缓存（否则 NaN 被填成 0，
    # 下游会误判“今日数据已生成”而跳过盘前降级）。
    return _fund_flow_rank_cache.copy() if _fund_flow_rank_cache is not None else None


def get_stock_list_by_previous_day_flow(
    min_inflow: float = 0,
    max_stocks: int = 200,
) -> list[dict[str, str]]:
    """盘前/非交易时段的资金面筛选：以最近一个交易日的净流入为准。

    东财没有“按指定历史交易日批量取全市场个股资金流”的接口，因此分两步：
    1) 用近 5 日排行取活跃候选池（1 次调用，含最近交易日）；
    2) 并发精查候选池每只股票的最近交易日主力净流入，按该值降序取前 N。

    候选池仅 max_stocks 的 2 倍，精查成本可控；且所有入选股必须属于
    同一个交易日，避免混入不同日期的数据。
    """
    try:
        import akshare as ak
        rank = ak.stock_individual_fund_flow_rank(indicator=_PRE_OPEN_RANK_INDICATOR)
    except Exception as e:
        logger.warning(f"获取{_PRE_OPEN_RANK_INDICATOR}资金流排行失败：{e}")
        return []

    if rank is None or rank.empty:
        logger.warning(f"{_PRE_OPEN_RANK_INDICATOR}资金流排行为空，资金面筛选跳过")
        return []

    rank_col = _find_inflow_col(rank)
    if rank_col is None:
        logger.warning(f"{_PRE_OPEN_RANK_INDICATOR}排行缺少主力净流入列，可用列：{rank.columns.tolist()}")
        return []

    pool = rank.assign(
        _inflow=pd.to_numeric(rank[rank_col], errors="coerce")
    ).sort_values("_inflow", ascending=False)
    pool = pool[pool["_inflow"] > min_inflow].head(max_stocks * _PRE_OPEN_CANDIDATE_MULT)

    names = dict(zip(pool["代码"].astype(str).str.zfill(6), pool["名称"], strict=False))
    codes = list(names.keys())
    if not codes:
        logger.warning(f"{_PRE_OPEN_RANK_INDICATOR}排行无净流入个股，资金面筛选跳过")
        return []

    logger.info(f"盘前模式：从{_PRE_OPEN_RANK_INDICATOR}排行取 {len(codes)} 只候选，精查最近交易日主力净流入…")
    flows = _ensure_prev_day_flow(codes)

    if not flows:
        logger.warning("未能获取任何个股的最近交易日资金流")
        return []

    session = max(hit[0] for hit in flows.values())
    ranked = sorted(
        ((code, value) for code, (day, value) in flows.items() if day == session and value > min_inflow),
        key=lambda kv: kv[1],
        reverse=True,
    )[:max_stocks]

    stocks = []
    for code, _ in ranked:
        ticker = _code_to_ticker(code)
        if ticker:
            stocks.append({"ticker": ticker, "name": str(names.get(code, ""))})

    _set_window_note(session)
    logger.info(
        f"资金面筛选（盘前模式）：{len(stocks)} 只股票 {session} 主力净流入 > {min_inflow/10000:.0f}万"
    )
    return stocks


def get_stock_list_by_fund_flow(min_inflow: float = 0, max_stocks: int = 200) -> list[dict[str, str]]:
    """
    从资金流向排名中获取股票列表。

    盘前/非交易时段今日净流入全为 NaN 而筛出 0 只时，自动改用最近一个
    交易日的净流入（见 get_stock_list_by_previous_day_flow）。

    Args:
        min_inflow: 最小主力净流入（元）
        max_stocks: 最大股票数量

    Returns:
        股票列表 [{"ticker": "600519.SS", "name": "贵州茅台"}, ...]
    """
    if not _today_flow_expected_generated():
        # 开盘前东财今日资金流必然未生成，省去一次 30s+ 的全市场排行请求
        logger.info("当前为盘前/非交易时段，直接按最近交易日主力净流入筛选")
        return get_stock_list_by_previous_day_flow(min_inflow=min_inflow, max_stocks=max_stocks)

    df = _get_fund_flow_rank_cached()
    if df is None:
        return []

    inflow_col = _find_inflow_col(df)
    if inflow_col is None:
        logger.warning(f"无法找到主力净流入列，可用列：{df.columns.tolist()}")
        return []

    # 数值化但保留 NaN：NaN 与阈值比较为假，盘前全 NaN 时自然筛出 0 只并触发降级
    inflow = pd.to_numeric(df[inflow_col], errors="coerce")
    filtered = df[inflow > min_inflow].head(max_stocks)

    if filtered.empty:
        # 今日资金数据尚未生成（盘前/周末/节假日）：改用最近一个交易日，
        # 避免整个选股流程在“没有股票满足资金面条件”处直接终止。
        logger.info("今日主力净流入无满足条件的股票（可能为盘前/非交易时段），改用最近交易日数据")
        return get_stock_list_by_previous_day_flow(min_inflow=min_inflow, max_stocks=max_stocks)

    stocks = []
    for _, row in filtered.iterrows():
        ticker = _code_to_ticker(row["代码"])
        if ticker is None:
            continue
        stocks.append({"ticker": ticker, "name": row["名称"]})

    _set_window_note("今日")
    logger.info(f"资金面筛选：{len(stocks)} 只股票主力净流入 > {min_inflow/10000:.0f}万")
    return stocks


def _build_fund_flow_map(codes: list[str]) -> tuple:
    """code -> 主力净流入等字段，以及所用数据窗口标签。

    今日排行有数据时直接用（1 次调用覆盖全市场）；盘前今日数值全为 NaN 时改用
    最近交易日精查结果——否则 NaN 与阈值的比较永远为假，资金面条件会被静默
    放行，潜力评分的资金面加分也全部丢失。
    """
    df = None if not _today_flow_expected_generated() else _get_fund_flow_rank_cached()
    inflow_col = _find_inflow_col(df) if df is not None and not df.empty else None

    if inflow_col and _has_inflow_data(df, inflow_col):
        pct_col = next((c for c in df.columns if "主力净流入" in c and "净占比" in c), None)
        super_col = next((c for c in df.columns if "超大单净流入" in c and "净额" in c), None)
        mapping = {}
        for _, row in df.iterrows():
            mapping[str(row["代码"]).zfill(6)] = {
                "main_force_net_inflow": _to_float(row.get(inflow_col)) or 0.0,
                "main_force_net_pct": (_to_float(row.get(pct_col)) or 0.0) if pct_col else 0.0,
                "super_large_net_inflow": (_to_float(row.get(super_col)) or 0.0) if super_col else 0.0,
            }
        return mapping, "今日"

    # 盘前/非交易时段：复用第 3 步精查结果，仅对未缓存的代码补查
    flows = _ensure_prev_day_flow(codes)
    mapping = {
        code: {
            "main_force_net_inflow": value,
            "main_force_net_pct": 0.0,
            "super_large_net_inflow": value,
        }
        for code, (_, value) in flows.items()
    }
    session = max((day for day, _ in flows.values()), default="")
    return mapping, (f"最近交易日 {session}" if session else "最近交易日")


def get_stock_sector(ticker: str) -> str | None:
    """
    获取股票所属行业板块。

    Args:
        ticker: 股票代码 (e.g., "600519.SS")

    Returns:
        行业名称
    """
    try:
        import akshare as ak
        code = ticker.split(".")[0]

        # 获取个股信息
        df = ak.stock_individual_info_em(symbol=code)

        if df.empty:
            return None

        # 查找行业字段
        for _, row in df.iterrows():
            if row.iloc[0] == "行业":
                return row.iloc[1]

        return None

    except Exception as e:
        logger.debug(f"获取 {ticker} 行业信息失败：{e}")
        return None


def pre_screen_stocks_multi_factor(
    stocks: list[dict[str, str]],
    tech_data: list[dict],
    min_main_force_inflow: float = 0,  # 主力净流入最小值（元）
    require_hot_sector: bool = True,
    hot_sector_top_n: int = 15,
    rsi_min: float = 30,  # 放宽 RSI 下限，找低位启动的
    rsi_max: float = 65,  # 上限降低，避免追高
    require_above_ma20: bool = True,
    require_macd_golden_cross: bool = True,
    min_volume_ratio: float = 0.8,
    find_early_uptrend: bool = True,  # 找早期上升趋势
) -> list[dict]:
    """
    多因子预筛选：技术面 + 资金面 + 板块热度 + 潜力识别。

    策略：找技术面早期上升趋势、热门板块、主力买入、还没大涨的潜力股。

    Args:
        stocks: 股票列表
        tech_data: 技术指标数据列表
        min_main_force_inflow: 主力净流入最小值（默认 0，即要求净流入）
        require_hot_sector: 是否要求属于热门板块
        hot_sector_top_n: 热门板块取前 N 个
        rsi_min: RSI 下限（降低到 30，找低位股）
        rsi_max: RSI 上限（降低到 65，避免追高）
        require_above_ma20: 是否要求股价 > MA20
        require_macd_golden_cross: 是否要求 MACD 金叉
        min_volume_ratio: 最小量比
        find_early_uptrend: 是否优先选择早期上升趋势（股价刚突破 MA20）

    Returns:
        通过筛选的股票列表（含潜力评分）
    """
    # 1. 获取热门板块
    hot_sectors = set()
    if require_hot_sector:
        hot_sectors = get_hot_sectors(hot_sector_top_n)

    # 2. 获取资金流向映射（盘前今日数值未生成时自动改用最近交易日）
    fund_flow_map, inflow_window = _build_fund_flow_map(
        [s["ticker"].split(".")[0].zfill(6) for s in stocks]
    )

    # 3. 构建技术指标映射
    tech_map = {t["ticker"]: t for t in tech_data}

    # 4. 多因子筛选
    screened = []
    for stock in stocks:
        ticker = stock["ticker"]
        name = stock.get("name", "")
        code = ticker.split(".")[0].zfill(6)

        reasons = []  # 未通过原因

        # === 技术面筛选 ===
        tech = tech_map.get(ticker)
        if not tech:
            reasons.append("无技术指标数据")
        else:
            # RSI 区间
            if not (rsi_min <= tech["rsi"] <= rsi_max):
                reasons.append(f"RSI={tech['rsi']:.1f} 超出 [{rsi_min},{rsi_max}]")

            # 股价 > MA20
            if require_above_ma20 and not tech.get("above_ma20", False):
                reasons.append("股价 < MA20")

            # MACD 金叉（可选）
            if require_macd_golden_cross and not tech.get("macd_golden_cross", False):
                reasons.append("MACD 未金叉")

            # 量比
            if tech.get("volume_ratio", 0) < min_volume_ratio:
                reasons.append(f"量比={tech.get('volume_ratio', 0):.2f} < {min_volume_ratio}")

        # === 资金面筛选 ===
        fund_flow = fund_flow_map.get(code)
        if fund_flow:
            main_inflow = fund_flow["main_force_net_inflow"]
            if main_inflow < min_main_force_inflow:
                reasons.append(f"主力净流入={main_inflow/10000:.0f}万 < {min_main_force_inflow/10000:.0f}万")
        else:
            reasons.append("无资金流向数据")

        # === 板块热度筛选（优先用第4步消息面筛选已附带的 sector，避免重复查询） ===
        sector = stock.get("sector")
        if sector is None:
            if require_hot_sector and hot_sectors:
                sector = get_stock_sector(ticker)
                if sector and sector not in hot_sectors:
                    reasons.append(f"行业'{sector}'不在热门板块前{hot_sector_top_n}")
            elif not require_hot_sector:
                sector = get_stock_sector(ticker)

        # 记录结果
        if reasons:
            logger.debug(f"{name} ({ticker}) 未通过：{'; '.join(reasons[:3])}")
        else:
            # 合并数据
            result = {**stock, **tech}
            if fund_flow:
                result["main_force_net_inflow"] = fund_flow["main_force_net_inflow"]
                result["main_force_net_pct"] = fund_flow["main_force_net_pct"]
            if sector:
                result["sector"] = sector

            # === 计算潜力评分 ===
            potential_score = 0

            # 1. RSI 低位加分（30-50 分最佳，说明还没大涨）
            rsi = tech.get("rsi", 50)
            if 30 <= rsi <= 50:
                potential_score += 30  # 低位启动
            elif 50 < rsi <= 60:
                potential_score += 20  # 中位
            else:
                potential_score += 10  # 高位

            # 2. 早期上升趋势加分（股价刚突破 MA20，但离 MA60 不远）
            if find_early_uptrend:
                price = tech.get("price", 0)
                ma20 = tech.get("ma20", price)
                ma60 = tech.get("ma60", price)
                if ma20 > 0 and ma60 > 0:
                    # 股价在 MA20 上方但离 MA20 不超过 5%（刚突破）
                    if 0 < (price - ma20) / ma20 < 0.05:
                        potential_score += 25  # 刚突破 MA20
                    # 股价离 MA60 不超过 10%（还没涨太多）
                    if 0 < (price - ma60) / ma60 < 0.10:
                        potential_score += 20  # 离 MA60 近

            # 3. 主力净流入加分
            if fund_flow:
                inflow = fund_flow["main_force_net_inflow"]
                if inflow > 10000000:  # > 1000 万
                    potential_score += 15
                elif inflow > 0:
                    potential_score += 10

            # 4. MACD 金叉加分
            if tech.get("macd_golden_cross", False):
                potential_score += 10

            # 5. 消息面加分（优先用第4步已附带的板块评分，避免重复查新闻）
            news_score = stock.get("sector_news_score")
            if news_score is None and sector:
                news_score = get_sector_news_sentiment(sector, days=7)
            if news_score is not None:
                result["sector_news_score"] = news_score
                if news_score >= 50:
                    potential_score += 15  # 板块消息面利好
                elif news_score >= 30:
                    potential_score += 10  # 板块消息面中性偏好
                elif news_score > 0:
                    potential_score += 5   # 板块有少量消息

            result["potential_score"] = potential_score
            logger.info(f"{name} ({ticker}) 通过筛选，潜力评分={potential_score}")
            screened.append(result)

    logger.info(f"多因子筛选完成（资金面窗口：{inflow_window}）：{len(stocks)} -> {len(screened)} 只股票")

    # 按潜力评分排序（高分在前）
    screened.sort(key=lambda x: x.get("potential_score", 0), reverse=True)

    return screened
