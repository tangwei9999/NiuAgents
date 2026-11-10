"""评分排序模块 - 综合多维度评分并排序股票。"""

import logging

logger = logging.getLogger(__name__)


# 评级权重映射
RATING_SCORE_MAP = {
    "Buy": 5,
    "Overweight": 4,
    "Hold": 3,
    "Underweight": 2,
    "Sell": 1,
    "REVIEW": 0,
    "ERROR": -1,
}

# 默认评分维度权重（可被配置覆盖）
DEFAULT_WEIGHTS = {
    "rating": 0.40,        # LLM 评级权重 40%
    "technical": 0.30,     # 技术面权重 30%
    "volume": 0.20,        # 资金面权重 20%
    "momentum": 0.10,      # 动量权重 10%
}


def get_weights(config=None):
    """
    获取评分权重（从配置或默认值）。

    Args:
        config: 配置字典（可选）

    Returns:
        权重的字典
    """
    if config is None:
        return DEFAULT_WEIGHTS.copy()

    return {
        "rating": config.get("screener_weight_rating", DEFAULT_WEIGHTS["rating"]),
        "technical": config.get("screener_weight_technical", DEFAULT_WEIGHTS["technical"]),
        "volume": config.get("screener_weight_volume", DEFAULT_WEIGHTS["volume"]),
        "momentum": config.get("screener_weight_momentum", DEFAULT_WEIGHTS["momentum"]),
    }


def calculate_technical_score(tech: dict) -> float:
    """
    计算技术面得分（0-100）。

    Args:
        tech: 技术指标字典

    Returns:
        技术面得分
    """
    score = 50.0  # 基础分

    # RSI 得分（40-60 为最佳区间）
    rsi = tech.get("rsi", 50)
    if 40 <= rsi <= 60:
        score += 20
    elif 30 <= rsi < 40 or 60 < rsi <= 70:
        score += 10
    else:
        score -= 10

    # MACD 金叉
    if tech.get("macd_golden_cross", False):
        score += 15

    # 股价在均线上方
    if tech.get("above_ma20", False):
        score += 10
    if tech.get("above_ma60", False):
        score += 5

    return min(100, max(0, score))


def calculate_volume_score(tech: dict) -> float:
    """
    计算资金面得分（0-100）。

    Args:
        tech: 技术指标字典

    Returns:
        资金面得分
    """
    score = 50.0  # 基础分

    # 成交量放大
    volume_ratio = tech.get("volume_ratio", 1.0)
    if volume_ratio >= 1.5:
        score += 25
    elif volume_ratio >= 1.2:
        score += 15
    elif volume_ratio >= 1.0:
        score += 5
    else:
        score -= 10

    return min(100, max(0, score))


def calculate_momentum_score(tech: dict) -> float:
    """
    计算动量得分（0-100）。

    Args:
        tech: 技术指标字典

    Returns:
        动量得分
    """
    score = 50.0  # 基础分

    # MACD 值
    macd = tech.get("macd", 0)
    macd_signal = tech.get("macd_signal", 0)
    if macd > 0 and macd > macd_signal:
        score += 20
    elif macd > 0:
        score += 10

    # 价格在均线上方
    price = tech.get("price", 0)
    ma20 = tech.get("ma20", 0)
    if price > 0 and ma20 > 0:
        pct_above_ma20 = (price - ma20) / ma20 * 100
        if 0 < pct_above_ma20 <= 5:
            score += 15  # 刚突破均线，最佳
        elif 5 < pct_above_ma20 <= 10:
            score += 10
        elif pct_above_ma20 > 10:
            score += 5  # 涨太多，风险增加

    return min(100, max(0, score))


def score_stock(result: dict, config=None) -> dict:
    """
    综合评分单只股票。

    Args:
        result: 分析结果字典（含技术指标）
        config: 配置字典（可选）

    Returns:
        评分后的结果字典
    """
    weights = get_weights(config)

    # LLM 评级得分
    rating = result.get("rating", "ERROR")
    rating_score = RATING_SCORE_MAP.get(rating, 0)
    rating_normalized = (rating_score / 5.0) * 100  # 归一化到 0-100

    # 技术面得分
    technical_score = calculate_technical_score(result)

    # 资金面得分
    volume_score = calculate_volume_score(result)

    # 动量得分
    momentum_score = calculate_momentum_score(result)

    # 综合得分
    total_score = (
        rating_normalized * weights["rating"]
        + technical_score * weights["technical"]
        + volume_score * weights["volume"]
        + momentum_score * weights["momentum"]
    )

    return {
        **result,
        "scores": {
            "rating": rating_normalized,
            "technical": technical_score,
            "volume": volume_score,
            "momentum": momentum_score,
            "total": total_score,
        },
        "final_score": total_score,
    }


def rank_stocks(results: list[dict], top_n: int = 3, config=None) -> list[dict]:
    """
    对股票进行评分排序。

    Args:
        results: 分析结果列表
        top_n: 返回前 N 只
        config: 配置字典（可选）

    Returns:
        排序后的结果列表
    """
    # 评分
    scored = [score_stock(r, config) for r in results]

    # 排序（按综合得分降序）
    scored.sort(key=lambda x: x["final_score"], reverse=True)

    # 返回 Top N
    return scored[:top_n]


def print_ranking(results: list[dict]):
    """
    打印排名结果。

    Args:
        results: 评分排序后的结果列表
    """
    print("\n" + "=" * 70)
    print("A 股选股器 - 综合评分排名")
    print("=" * 70)

    for i, r in enumerate(results, 1):
        scores = r.get("scores", {})
        print(f"\n{i}. {r['name']} ({r['ticker']})")
        print(f"   综合得分：{scores.get('total', 0):.1f}")
        print(f"   评级：{r['rating']}")
        print("   细分得分：")
        print(f"     - LLM 评级：{scores.get('rating', 0):.1f}")
        print(f"     - 技术面：{scores.get('technical', 0):.1f}")
        print(f"     - 资金面：{scores.get('volume', 0):.1f}")
        print(f"     - 动量：{scores.get('momentum', 0):.1f}")

        if r.get("decision"):
            decision = r["decision"][:200]
            print(f"   决策摘要：{decision}...")

    print("\n" + "=" * 70)


if __name__ == "__main__":
    # 测试评分
    test_results = [
        {
            "ticker": "600519.SS",
            "name": "贵州茅台",
            "rating": "Buy",
            "rsi": 55,
            "above_ma20": True,
            "above_ma60": True,
            "macd_golden_cross": True,
            "volume_ratio": 1.3,
            "price": 1800,
            "ma20": 1750,
            "ma60": 1700,
            "macd": 10,
            "macd_signal": 8,
            "decision": "建议买入，技术面良好...",
        },
        {
            "ticker": "000858.SZ",
            "name": "五粮液",
            "rating": "Overweight",
            "rsi": 45,
            "above_ma20": True,
            "above_ma60": False,
            "macd_golden_cross": False,
            "volume_ratio": 1.1,
            "price": 150,
            "ma20": 145,
            "ma60": 155,
            "macd": -2,
            "macd_signal": -1,
            "decision": "建议增持，基本面稳健...",
        },
    ]

    ranked = rank_stocks(test_results, top_n=2)
    print_ranking(ranked)
