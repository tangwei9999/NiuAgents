"""批量分析引擎模块 - 复用 TradingAgentsGraph 进行批量股票分析。"""

import logging
from typing import List, Dict, Optional
from datetime import date
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskID
from rich.console import Console

from tradingagents.dataflows.symbols import safe_ticker_component
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

logger = logging.getLogger(__name__)
console = Console()


def analyze_single_stock(
    ticker: str,
    name: str,
    analysis_date: str,
    config: Dict,
    report_dir: Optional[str] = None,
    html: bool = True,
) -> Optional[Dict]:
    """
    分析单只股票。

    Args:
        ticker: 股票代码
        name: 股票名称
        analysis_date: 分析日期（YYYY-MM-DD）
        config: 配置字典
        report_dir: 给了就把完整报告写到这里下面的一股一目录，否则只回结果字典
        html: 是否同时渲染单页 HTML（complete_report.html）

    Returns:
        分析结果字典，包含评级、决策文本等；落盘成功时多一个 report_path
    """
    try:
        # 创建分析引擎实例
        ta = TradingAgentsGraph(debug=False, config=config)

        # 执行分析
        state, signal = ta.propagate(ticker, analysis_date)

        # 提取结果
        rating = signal  # 5 级评级
        decision = state.get("final_trade_decision", "")
        market_report = state.get("market_report", "")
        fundamentals_report = state.get("fundamentals_report", "")

        result = {
            "ticker": ticker,
            "name": name,
            "rating": rating,
            "decision": decision,
            "market_report": market_report,
            "fundamentals_report": fundamentals_report,
            "success": True,
        }

        if report_dir:
            # 报告树交给框架自己写（与 CLI 同一个入口），这样 --save/--html
            # 交出的是分析师正文全文；失败只降级为“没有留档”，不影响已算好的评级。
            try:
                saved = ta.save_reports(
                    state,
                    ticker,
                    save_path=Path(report_dir) / safe_ticker_component(ticker),
                    html=html,
                )
                result["report_path"] = str(saved)
            except Exception as e:
                logger.warning(f"{name} ({ticker}) 报告落盘失败：{e}")

        return result

    except Exception as e:
        logger.error(f"分析 {name} ({ticker}) 失败：{e}")
        return {
            "ticker": ticker,
            "name": name,
            "rating": "ERROR",
            "decision": str(e),
            "success": False,
        }


def batch_analyze(
    stocks: List[Dict],
    analysis_date: Optional[str] = None,
    max_workers: int = 3,
    config: Optional[Dict] = None,
    report_dir: Optional[str] = None,
    html: bool = True,
) -> List[Dict]:
    """
    批量分析股票。

    Args:
        stocks: 股票列表（含技术指标）
        analysis_date: 分析日期（默认今天）
        max_workers: 最大并发数
        config: 配置字典
        report_dir: 完整报告的写入目录（None ＝不落盘）
        html: 每只股是否同时生成 HTML 报告

    Returns:
        分析结果列表
    """
    if not analysis_date:
        analysis_date = date.today().strftime("%Y-%m-%d")

    if config is None:
        config = DEFAULT_CONFIG.copy()
        config["output_language"] = "Chinese"
        config["max_debate_rounds"] = 1
        config["max_risk_discuss_rounds"] = 1

    # A 股选股场景：数据源只用国内源。Yahoo Finance 对 A 股覆盖差且易限流，
    # 每次限流重试（2s/4s/8s）会显著拖慢批量分析。浅拷贝避免污染 DEFAULT_CONFIG。
    config = dict(config)
    config["data_vendors"] = {
        **config.get("data_vendors", {}),
        "core_stock_apis": "akshare",
        "technical_indicators": "akshare",
        "fundamental_data": "akshare",
        "news_data": "akshare",
        # FRED（美国宏观）是消息面分析师的海外补充源：国内网络下永远连不上，
        # 却会每只股票、每个话题付一次连接超时并刷 WARNING，置为 "none" 后
        # 该工具直接返回哨兵字符串，一次请求也不发，也不等。需要时改回 "fred"。
        # Polymarket 的事件概率国内没有对应物，换成上交所期权链反解的隐含概率
        # （sse_option）：同样是市场对前瞻事件的定价，国内 0.1s 可达。
        "macro_data": "none",
        "prediction_markets": "sse_option",
    }
    # 全局新闻在默认配置里给 Yahoo（美股不该读到上证快讯），但 A 股批量不回退
    # Yahoo：市场面同样走东方财富口径。
    config["tool_vendors"] = {
        **config.get("tool_vendors", {}),
        "get_global_news": "akshare",
    }
    console.print("[dim]数据源：仅使用 AKShare 国内数据源（A 股场景，不回退 Yahoo）[/dim]")
    console.print(
        "[dim]海外可选源 FRED 已关闭（避免连接超时），Polymarket 换为上交所期权"
        "隐含概率；news 分析师用国内新闻、公告与期权定价[/dim]"
    )

    results = []
    total = len(stocks)

    console.print(f"\n[bold blue]开始批量分析 {total} 只股票...[/bold blue]")
    console.print(f"分析日期：{analysis_date}")
    console.print(f"并发数：{max_workers}\n")

    # 使用线程池并发分析
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        console=console,
    ) as progress:
        task = progress.add_task("分析进度", total=total)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # 提交任务
            future_to_stock = {}
            for stock in stocks:
                future = executor.submit(
                    analyze_single_stock,
                    stock["ticker"],
                    stock.get("name", ""),
                    analysis_date,
                    config,
                    report_dir,
                    html,
                )
                future_to_stock[future] = stock

            # 收集结果
            for future in as_completed(future_to_stock):
                stock = future_to_stock[future]
                try:
                    result = future.result()
                    results.append(result)

                    if result["success"]:
                        progress.update(
                            task,
                            advance=1,
                            description=f"✓ {stock['name']} ({stock['ticker']}) - {result['rating']}",
                        )
                    else:
                        progress.update(
                            task,
                            advance=1,
                            description=f"✗ {stock['name']} ({stock['ticker']}) - 失败",
                        )

                except Exception as e:
                    logger.error(f"处理 {stock['ticker']} 结果时出错：{e}")
                    results.append({
                        "ticker": stock["ticker"],
                        "name": stock.get("name", ""),
                        "rating": "ERROR",
                        "decision": str(e),
                        "success": False,
                    })
                    progress.update(task, advance=1)

    console.print(f"\n[bold green]批量分析完成！成功：{sum(1 for r in results if r['success'])}/{total}[/bold green]")

    return results


if __name__ == "__main__":
    # 测试批量分析（只用 2 只股票测试）
    test_stocks = [
        {"ticker": "600519.SS", "name": "贵州茅台"},
        {"ticker": "000858.SZ", "name": "五粮液"},
    ]

    results = batch_analyze(test_stocks, max_workers=1)
    for r in results:
        print(f"\n{r['name']} ({r['ticker']})")
        print(f"  评级：{r['rating']}")
        print(f"  决策：{r['decision'][:100]}...")
