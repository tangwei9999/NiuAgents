"""股票池获取模块 - 多源回退机制获取 A 股股票列表。

数据源优先级：AKShare（东方财富）> BaoStock > AKShare（新浪）> 本地缓存 >
内置股票池，实现在 ``data_source.get_stock_list_with_fallback``。全部是境内源或本地兜底。
"""

import logging

logger = logging.getLogger(__name__)


def _get_builtin_stocks() -> list[dict[str, str]]:
    """获取内置股票池（兜底方案）。"""
    return [
        # 白酒/消费
        {"ticker": "600519.SS", "name": "贵州茅台"},
        {"ticker": "000858.SZ", "name": "五粮液"},
        {"ticker": "000568.SZ", "name": "泸州老窖"},
        {"ticker": "002304.SZ", "name": "洋河股份"},
        # 金融
        {"ticker": "601318.SS", "name": "中国平安"},
        {"ticker": "600036.SS", "name": "招商银行"},
        {"ticker": "601166.SS", "name": "兴业银行"},
        {"ticker": "600016.SS", "name": "民生银行"},
        {"ticker": "601398.SS", "name": "工商银行"},
        {"ticker": "601288.SS", "name": "农业银行"},
        {"ticker": "601988.SS", "name": "中国银行"},
        {"ticker": "600000.SS", "name": "浦发银行"},
        # 新能源
        {"ticker": "300750.SZ", "name": "宁德时代"},
        {"ticker": "002594.SZ", "name": "比亚迪"},
        {"ticker": "601012.SS", "name": "隆基绿能"},
        {"ticker": "002459.SZ", "name": "晶澳科技"},
        # 科技/半导体
        {"ticker": "002475.SZ", "name": "立讯精密"},
        {"ticker": "603501.SS", "name": "韦尔股份"},
        {"ticker": "002049.SZ", "name": "紫光国微"},
        {"ticker": "600588.SS", "name": "用友网络"},
        {"ticker": "002230.SZ", "name": "科大飞"},
        # 医药
        {"ticker": "600276.SS", "name": "恒瑞医药"},
        {"ticker": "300760.SZ", "name": "迈瑞医疗"},
        {"ticker": "000538.SZ", "name": "云南白药"},
        {"ticker": "600196.SS", "name": "复星医药"},
        # 制造/家电
        {"ticker": "000333.SZ", "name": "美的集团"},
        {"ticker": "000651.SZ", "name": "格力电器"},
        {"ticker": "600690.SS", "name": "海尔智家"},
        # 资源/周期
        {"ticker": "601899.SS", "name": "紫金矿业"},
        {"ticker": "600028.SS", "name": "中国石化"},
        {"ticker": "601088.SS", "name": "中国神华"},
        {"ticker": "600031.SS", "name": "三一重工"},
        # 其他蓝筹
        {"ticker": "600900.SS", "name": "长江电力"},
        {"ticker": "601601.SS", "name": "中国太保"},
        {"ticker": "600048.SS", "name": "保利发展"},
        {"ticker": "000002.SZ", "name": "万科 A"},
        {"ticker": "601138.SS", "name": "工业富联"},
        {"ticker": "000725.SZ", "name": "京东方 A"},
    ]


def get_csi300_stocks() -> list[dict[str, str]]:
    """
    获取股票池（使用多源回退机制）。

    优先级：AKShare（东方财富）> BaoStock > AKShare（新浪）> 本地缓存 > 内置股票池。函数名里的 csi300 是历史
    名称，实际范畴是全 A 股再交给 filter_st_stocks 剔除。

    Returns:
        股票列表，格式：[{"ticker": "600519.SS", "name": "贵州茅台"}, ...]
    """
    from tradingagents.screener.data_source import get_stock_list_with_fallback

    try:
        # 尝试多源获取
        stocks = get_stock_list_with_fallback()
        logger.info(f"获取到 {len(stocks)} 只股票")
        return stocks
    except Exception as e:
        logger.warning(f"多源获取失败：{e}，使用内置股票池")
        # 回退到内置股票池
        stocks = _get_builtin_stocks()
        logger.info(f"内置股票池：{len(stocks)} 只")
        return stocks


def filter_st_stocks(stocks: list[dict[str, str]]) -> list[dict[str, str]]:
    """
    剔除 ST/*ST 股票、科创板、北交所股票。

    只保留：主板（600/601/603/605）、中小板（000/001/002/003）、创业板（300/301）。

    Args:
        stocks: 股票列表

    Returns:
        过滤后的股票列表
    """
    filtered = []
    for stock in stocks:
        name = stock.get("name", "")
        ticker = stock.get("ticker", "")
        code = ticker.split(".")[0]  # 提取纯数字代码，如 600519

        # 剔除 ST 股票（名称中包含 ST）
        if "ST" in name.upper():
            logger.info(f"剔除 ST 股票：{name} ({ticker})")
            continue

        # 剔除科创板（688 开头）
        if code.startswith("688"):
            logger.info(f"剔除科创板股票：{name} ({ticker})")
            continue

        # 剔除北交所（8/4 开头）
        if code.startswith(("8", "4")):
            logger.info(f"剔除北交所股票：{name} ({ticker})")
            continue

        # 只保留主板、中小板、创业板
        # 主板：600/601/603/605（沪）、000/001/002/003（深）
        # 创业板：300/301（深）
        allowed_prefixes = ("600", "601", "603", "605", "000", "001", "002", "003", "300", "301")
        if not code.startswith(allowed_prefixes):
            logger.debug(f"剔除非主板/中小/创业板股票：{name} ({ticker})")
            continue

        filtered.append(stock)

    logger.info(f"剔除 ST/科创板/北交所后剩余 {len(filtered)} 只")
    return filtered


if __name__ == "__main__":
    # 测试获取股票池
    stocks = get_csi300_stocks()
    print(f"股票池数量：{len(stocks)}")
    print("前 10 只股票：")
    for s in stocks[:10]:
        print(f"  {s['ticker']} - {s['name']}")
