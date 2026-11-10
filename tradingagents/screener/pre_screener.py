"""预筛选器模块 - 基于技术面和基本面初步过滤股票。

K 线统一走 ``data_source`` 的境内多源链。筛选器不再向 Yahoo 询问 A 股：海外源
不可达时只会返回 None，到手里就成了“这只票没有数据”从而丢候选，和它本身不
可达分不开；而且直连 Yahoo 每只票要白付重试与超时的等待。
"""

import logging
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)


def get_technical_indicators_batch(
    stocks: List[Dict[str, str]],
) -> List[Dict]:
    """
    批量获取股票技术指标（境内多源回退）。

    回退顺序：BaoStock > AKShare 腾讯 > AKShare 新浪 > AKShare 东财，窗口统一
    90 个交易日（在 ``data_source`` 里定），调用方不再选周期。

    Args:
        stocks: 股票列表

    Returns:
        技术指标列表（取不到指标的票不在列内）
    """
    from tradingagents.screener.data_source import get_technical_data_with_fallback

    results = []
    total = len(stocks)

    for i, stock in enumerate(stocks, 1):
        ticker = stock["ticker"]
        name = stock.get("name", "")

        # 使用多源回退获取技术指标
        tech = get_technical_data_with_fallback(ticker)

        if tech is None:
            logger.warning(f"[{i}/{total}] {name} ({ticker}) - 所有数据源都失败")
            continue

        results.append(tech)
        logger.info(f"[{i}/{total}] {name} ({ticker}) - 获取成功")

    logger.info(f"批量获取完成：{len(results)}/{total} 只股票")
    return results


def pre_screen_stocks(
    stocks: List[Dict[str, str]],
    min_volume_ratio: float = 1.0,
    rsi_min: float = 40,
    rsi_max: float = 70,
    require_above_ma20: bool = True,
) -> List[Dict]:
    """
    预筛选股票（使用批量下载）。

    Args:
        stocks: 股票列表
        min_volume_ratio: 最小成交量放大倍数
        rsi_min: RSI 下限
        rsi_max: RSI 上限
        require_above_ma20: 是否要求股价在 20 日均线上方

    Returns:
        通过筛选的股票列表（含技术指标）
    """
    # 批量获取技术指标
    tech_list = get_technical_indicators_batch(stocks)

    screened = []
    for tech in tech_list:
        ticker = tech["ticker"]
        name = tech.get("name", "")

        # 筛选条件
        reasons = []

        # 1. RSI 在合理区间
        if not (rsi_min <= tech["rsi"] <= rsi_max):
            reasons.append(f"RSI={tech['rsi']:.1f} 超出 [{rsi_min},{rsi_max}]")

        # 2. 股价在 20 日均线上方
        if require_above_ma20 and not tech["above_ma20"]:
            reasons.append(f"股价低于 MA20")

        # 3. 成交量放大
        if tech["volume_ratio"] < min_volume_ratio:
            reasons.append(f"量比={tech['volume_ratio']:.2f} < {min_volume_ratio}")

        if reasons:
            logger.info(f"{name} ({ticker}) - 未通过：{', '.join(reasons)}")
        else:
            logger.info(f"{name} ({ticker}) - 通过筛选")
            screened.append(tech)

    logger.info(f"预筛选完成：{len(stocks)} -> {len(screened)} 只股票")
    return screened


if __name__ == "__main__":
    # 测试预筛选
    from stock_universe import get_csi300_stocks

    stocks = get_csi300_stocks()[:10]  # 只测试前 10 只
    print(f"测试股票池：{len(stocks)} 只")

    screened = pre_screen_stocks(stocks)
    print(f"\n通过筛选：{len(screened)} 只")
    for s in screened:
        print(f"  {s['ticker']} - {s['name']} - RSI={s['rsi']:.1f}")
