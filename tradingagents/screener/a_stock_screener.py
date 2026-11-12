"""A 股选股器主脚本 - 自动从全市场扫描、筛选、分析并推荐 Top 3 股票。

使用方法：
    python -m tradingagents.screener.a_stock_screener [--top N] [--date YYYY-MM-DD] [--save] [--html]

示例：
    python -m tradingagents.screener.a_stock_screener --top 3 --save --html
"""

import argparse
import logging
from datetime import date
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.screener.batch_analyzer import batch_analyze
from tradingagents.screener.pre_screener import get_technical_indicators_batch
from tradingagents.screener.pre_screener_v2 import pre_screen_stocks_multi_factor
from tradingagents.screener.price_levels import (
    attach_price_levels,
    format_level_line,
    level_basis,
)
from tradingagents.screener.scorer import print_ranking, rank_stocks
from tradingagents.screener.stock_universe import filter_st_stocks, get_csi300_stocks

logger = logging.getLogger(__name__)
console = Console()


def run_screener(
    top_n: int = 3,
    analysis_date: str = None,
    save_reports: bool = True,
    generate_html: bool = True,
    max_candidates: int = 50,
    max_workers: int = 3,
    assume_yes: bool = False,
):
    """
    运行 A 股选股器。

    Args:
        top_n: 推荐股票数量（默认 3）
        analysis_date: 分析日期（默认今天）
        save_reports: 是否保存报告（摘要 + 每股完整报告树）
        generate_html: 完整报告是否同时渲成单页 HTML
        max_candidates: 最大候选股数量
        max_workers: 最大并发分析数
        assume_yes: 跳过深度分析前的确认（无 tty 的定时/批跑场景）
    """
    # 配置日志（确保通过 CLI 调用时也能看到日志）
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )

    console.print(Panel.fit(
        "[bold blue]A 股选股器 v0.1.0[/bold blue]\n"
        "自动从全市场扫描、筛选、分析并推荐股票",
        border_style="blue",
    ))

    # 设置分析日期
    if not analysis_date:
        analysis_date = date.today().strftime("%Y-%m-%d")

    console.print(f"\n[bold]分析日期：[/bold] {analysis_date}")
    console.print(f"[bold]推荐数量：[/bold] Top {top_n}\n")

    # 第 1 步：获取股票池
    console.print("[bold yellow]第 1 步：获取股票池...[/bold yellow]")
    stocks = get_csi300_stocks()
    console.print(f"  获取到 {len(stocks)} 只股票")

    # 第 2 步：基础过滤（剔除 ST 股）
    console.print("\n[bold yellow]第 2 步：基础过滤...[/bold yellow]")
    stocks = filter_st_stocks(stocks)
    console.print(f"  过滤后剩余 {len(stocks)} 只股票")

    # 第 3 步：资金面预筛选（快速从全市场筛选主力买入的股票）
    console.print("\n[bold yellow]第 3 步：资金面预筛选...[/bold yellow]")
    console.print("  从全市场筛选主力净流入 > 0 的股票（取前 100 只）...")

    from tradingagents.screener.pre_screener_v2 import (
        filter_by_news_sector,
        get_last_window_note,
        get_positive_news_sectors,
        get_stock_list_by_fund_flow,
    )
    # universe=stocks：盘前/限流时，若全市场资金流排行不可用，用已获取的股票池
    # 兜底做候选池精查最近交易日净流入，避免“没有股票满足资金面条件”直接中断。
    fund_flow_stocks = get_stock_list_by_fund_flow(min_inflow=0, max_stocks=100, universe=stocks)

    # 盘前/非交易时段东财“今日”资金流尚未生成，已自动改用最近一个交易日
    window = get_last_window_note()
    if window and window != "今日":
        console.print(f"  [dim]盘前/非交易时段：今日资金流尚未生成，改用最近交易日（{window}）的主力净流入[/dim]")
    console.print(f"  主力净流入 > 0 的股票：{len(fund_flow_stocks)} 只")

    if not fund_flow_stocks:
        console.print("[bold red]没有股票满足资金面条件[/bold red]")
        return

    # 对资金面结果再次应用板块过滤（确保剔除科创板/北交所/ST）
    fund_flow_stocks = filter_st_stocks(fund_flow_stocks)
    console.print(f"  剔除科创板/北交所/ST 后剩余：{len(fund_flow_stocks)} 只")

    if not fund_flow_stocks:
        console.print("[bold red]没有股票满足条件[/bold red]")
        return

    # 第 4 步：消息面筛选（近一周利好板块）
    console.print("\n[bold yellow]第 4 步：消息面筛选（近一周利好板块）...[/bold yellow]")
    console.print("  获取资金流入前 20 的热门板块，评估近 7 天消息面...")
    positive_sectors = get_positive_news_sectors(days=7, min_score=30, sector_top_n=20)

    if not positive_sectors:
        console.print("[bold red]没有板块近一周有利好消息，请放宽条件或稍后再试[/bold red]")
        return

    console.print(f"  利好板块 {len(positive_sectors)} 个：{', '.join(positive_sectors.keys())}")

    news_stocks = filter_by_news_sector(fund_flow_stocks, positive_sectors)
    console.print(f"  属于利好板块的股票：{len(news_stocks)} 只")

    if not news_stocks:
        console.print("[bold red]没有股票属于利好板块[/bold red]")
        return

    # 第 5 步：技术指标计算 + 技术面筛选
    console.print("\n[bold yellow]第 5 步：技术指标筛选...[/bold yellow]")
    console.print(f"  正在对 {len(news_stocks)} 只股票计算 RSI/MACD/MA/量比...")
    tech_data = get_technical_indicators_batch(news_stocks)
    console.print(f"  成功获取 {len(tech_data)} 只股票的技术指标")

    if not tech_data:
        console.print("[bold red]无法获取技术指标，请检查数据源[/bold red]")
        return

    console.print("  技术面条件：RSI 30-65（早期上升趋势）、股价>MA20、MACD 金叉、量比≥0.8")
    screened = pre_screen_stocks_multi_factor(
        news_stocks,  # 已通过资金面 + 消息面的股票
        tech_data,
        min_main_force_inflow=0,  # 要求主力净流入
        require_hot_sector=False,  # 板块热度已在第4步消息面筛选中体现
        rsi_min=30,
        rsi_max=65,
        require_above_ma20=True,
        require_macd_golden_cross=True,
        min_volume_ratio=0.8,
        find_early_uptrend=True,
    )
    console.print(f"  通过技术面筛选：{len(screened)} 只股票")

    if not screened:
        console.print("[bold red]没有股票通过预筛选，请放宽筛选条件[/bold red]")
        return

    # 深度分析上限。以前这个参数收了不生效，全部潜力股都进第 6 步，几十只
    # × 每只 3-5 分钟就是几小时；列表已按潜力评分降序，截断即取最高分。
    if len(screened) > max_candidates:
        console.print(
            f"  潜力股 {len(screened)} 只，超过 --max-candidates {max_candidates}："
            f"只对潜力评分最高的前 {max_candidates} 只做深度分析"
        )
        screened = screened[:max_candidates]

    # 打印潜力股列表
    console.print("\n[bold green]═══════════════════════════════════════════════════════════[/bold green]")
    console.print("[bold green]  🎯 第一步筛选完成！找到以下潜力股（按潜力评分降序）：[/bold green]")
    console.print("[bold green]═══════════════════════════════════════════════════════════[/bold green]\n")

    for i, stock in enumerate(screened, 1):
        ticker = stock.get("ticker", "")
        name = stock.get("name", "")
        score = stock.get("potential_score", 0)
        rsi = stock.get("rsi", 0)
        inflow = stock.get("main_force_net_inflow", 0) / 10000  # 转换为万
        sector = stock.get("sector", "未知")
        news_score = stock.get("sector_news_score", 0)

        console.print(f"  [bold cyan]{i:2d}.[/bold cyan] {name} ({ticker})")
        console.print(f"      潜力评分: [bold yellow]{score}[/bold yellow] | RSI: {rsi:.1f} | 主力净流入: {inflow:.0f}万 | 板块: {sector} | 消息面: {news_score}分")

    console.print("\n[bold green]═══════════════════════════════════════════════════════════[/bold green]")
    console.print(f"[bold green]  共找到 {len(screened)} 只潜力股[/bold green]")
    console.print("[bold green]═══════════════════════════════════════════════════════════[/bold green]\n")

    # 询问是否继续深度分析。--yes 时不问：无人值守的终端没有 tty 可回答，
    # 卡在确认上等于整次跑掉什么也没留下。
    if assume_yes:
        console.print(f"  [dim]--yes：跳过确认，直接深度分析 {len(screened)} 只[/dim]")
    else:
        import questionary
        continue_analysis = questionary.confirm(
            f"是否对这 {len(screened)} 只潜力股进行深度分析（每只约 3-5 分钟）？",
            default=True
        ).ask()

        if not continue_analysis:
            console.print("\n[bold yellow]✅ 已保留潜力股列表，分析完成！[/bold yellow]")
            console.print("[dim]如需稍后深度分析，可重新运行选股器[/dim]\n")
            return

    # 第 6 步和第 8 步共用同一个目录：完整报告树与摘要挨在一起
    output_dir = Path.home() / ".tradingagents" / "logs" / "screener" / analysis_date

    # 第 6 步：批量深度分析
    console.print(f"\n[bold yellow]第 6 步：批量深度分析（{len(screened)} 只股票）...[/bold yellow]")
    console.print("  使用 TradingAgents 多智能体分析引擎")
    console.print("  预计耗时：每只 3-5 分钟\n")

    results = batch_analyze(
        screened,
        analysis_date=analysis_date,
        max_workers=max_workers,
        config=DEFAULT_CONFIG,
        report_dir=str(output_dir) if save_reports else None,
        html=generate_html,
    )

    # 过滤成功的结果
    successful = [r for r in results if r.get("success", False)]
    if not successful:
        console.print("[bold red]所有股票分析失败，请检查配置和网络[/bold red]")
        return

    console.print(f"\n[bold green]成功分析 {len(successful)} 只股票[/bold green]")

    # 第 7 步：综合评分排序
    console.print("\n[bold yellow]第 7 步：综合评分排序...[/bold yellow]")
    top_stocks = rank_stocks(successful, top_n=top_n, config=DEFAULT_CONFIG)

    # 打印排名
    print_ranking(top_stocks)

    # 交易点位：阻力位 / 支撑位 / 建议买入价 / 建议卖出价（每只 1 次 K 线请求）
    console.print("  计算交易点位（阻力位 / 支撑位 / 建议买卖价）…")
    attach_price_levels(top_stocks)

    # 最终推荐：输出股票代码、股票名称、上涨潜力分数
    final_table = Table(
        title=f"🏆 最终推荐 Top {len(top_stocks)}",
        show_header=True,
        header_style="bold magenta",
        border_style="green",
    )
    final_table.add_column("排名", justify="center", style="bold cyan")
    final_table.add_column("股票代码", justify="center", style="bold")
    final_table.add_column("股票名称", justify="left", style="bold")
    final_table.add_column("上涨潜力分数", justify="center", style="bold yellow")
    final_table.add_column("综合评级", justify="center", style="green")

    for i, stock in enumerate(top_stocks, 1):
        final_table.add_row(
            f"{i}",
            stock["ticker"],
            stock["name"],
            f"{stock.get('final_score', 0):.1f}",
            stock["rating"],
        )

    console.print("\n[bold green]═══════════════ 🏆 最终推荐结果 ═══════════════[/bold green]")
    console.print(final_table)
    console.print("[dim]上涨潜力分数 = 深度分析综合评分（LLM 评级 40% + 技术面 30% + 资金面 20% + 动量 10%，满分 100）[/dim]")

    # 每只股票一行的交易点位建议
    console.print("\n[bold green]═══════════════ 📌 交易点位建议 ═══════════════[/bold green]")
    for i, stock in enumerate(top_stocks, 1):
        console.print(f"  [cyan]{i:2d}. {format_level_line(stock)}[/cyan]")
    res_basis, sup_basis = level_basis()
    console.print(
        f"[dim]口径：阻力位 = {res_basis}（避开单根长上影）；支撑位 = {sup_basis}（避开单根长下影）；"
        "建议买入价 = 支撑位上方 2% 且不低于现价 8%（回调不足 2% 时取现价下方 3%）；"
        "建议卖出价 = 阻力位下方 2% 且不高于现价 12%（获利不足 3% 时取现价上方 3%）[/dim]\n"
    )

    # 第 8 步：保存报告
    if save_reports:
        console.print("\n[bold yellow]第 8 步：保存报告...[/bold yellow]")

        # 创建输出目录
        output_dir.mkdir(parents=True, exist_ok=True)

        for i, stock in enumerate(top_stocks, 1):
            # 保存 Markdown 报告
            report_file = output_dir / f"top{i}_{stock['ticker']}_{stock['name']}.md"
            with open(report_file, "w", encoding="utf-8") as f:
                f.write(f"# {stock['name']} ({stock['ticker']}) 分析报告\n\n")
                f.write(f"**分析日期**: {analysis_date}\n\n")
                f.write(f"**综合评分**: {stock.get('final_score', 0):.1f}\n\n")
                f.write(f"**评级**: {stock['rating']}\n\n")
                levels = stock.get("price_levels") or {}
                if levels:
                    res_desc, sup_desc = level_basis(levels.get("bars"))
                    f.write("## 交易点位\n\n")
                    f.write(f"- 现价：{levels.get('current_price', 0):.2f}\n")
                    f.write(f"- 阻力位：{levels.get('resistance', 0):.2f}（{res_desc}）\n")
                    f.write(f"- 支撑位：{levels.get('support', 0):.2f}（{sup_desc}）\n")
                    f.write(f"- 建议买入价：{levels.get('buy_price', 0):.2f}\n")
                    f.write(f"- 建议卖出价：{levels.get('sell_price', 0):.2f}\n\n")
                f.write("## 细分得分\n\n")
                scores = stock.get("scores", {})
                f.write(f"- LLM 评级：{scores.get('rating', 0):.1f}\n")
                f.write(f"- 技术面：{scores.get('technical', 0):.1f}\n")
                f.write(f"- 资金面：{scores.get('volume', 0):.1f}\n")
                f.write(f"- 动量：{scores.get('momentum', 0):.1f}\n\n")
                f.write("## 交易决策\n\n")
                f.write(stock.get("decision", "无") + "\n")

            console.print(f"  ✓ {stock['name']} 摘要已保存：{report_file}")
            if stock.get("report_path"):
                # 深度分析当时就写好了：同一目录下的 complete_report.md（+.html）
                # 是分析师到风控的全正文，摘要里只有评级与决策
                console.print(f"    完整报告：{Path(stock['report_path']).parent}")

        console.print(f"\n[bold green]报告已保存到：{output_dir}[/bold green]")

    # 最终总结
    console.print(Panel.fit(
        "[bold green]选股完成！[/bold green]\n\n"
        + "\n".join([
            f"{i}. {s['name']} ({s['ticker']}) - 综合得分：{s.get('final_score', 0):.1f}"
            for i, s in enumerate(top_stocks, 1)
        ]),
        border_style="green",
    ))

    console.print("\n[bold]免责声明：[/bold] 本结果仅供研究参考，不构成投资建议。投资有风险，入市需谨慎。")


def main():
    """命令行入口。"""
    parser = argparse.ArgumentParser(
        description="A 股选股器 - 自动从全市场扫描、筛选、分析并推荐股票",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python -m tradingagents.screener.a_stock_screener --top 3
  python -m tradingagents.screener.a_stock_screener --top 5 --date 2026-11-07
  python -m tradingagents.screener.a_stock_screener --top 3 --save --html
        """,
    )

    parser.add_argument(
        "--top",
        type=int,
        default=3,
        help="推荐股票数量（默认 3）",
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="分析日期，格式 YYYY-MM-DD（默认今天）",
    )
    parser.add_argument(
        "--save",
        action="store_true",
        default=True,
        help="保存报告（默认开启）",
    )
    parser.add_argument(
        "--no-save",
        action="store_true",
        default=False,
        help="不保存报告",
    )
    parser.add_argument(
        "--html",
        action="store_true",
        default=True,
        help="每股完整报告额外渲成单页 complete_report.html（默认开启）",
    )
    parser.add_argument(
        "--no-html",
        action="store_true",
        default=False,
        help="不生成 HTML，只保留 Markdown",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        default=False,
        help="跳过深度分析前的确认（定时/无人值守运行）",
    )
    parser.add_argument(
        "--max-candidates",
        type=int,
        default=50,
        help="最大候选股数量（默认 50）",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=3,
        help="最大并发分析数（默认 3）",
    )

    args = parser.parse_args()

    # 配置日志
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # 运行选股器
    run_screener(
        top_n=args.top,
        analysis_date=args.date,
        save_reports=not args.no_save,
        generate_html=not args.no_html,
        max_candidates=args.max_candidates,
        max_workers=args.workers,
        assume_yes=args.yes,
    )


if __name__ == "__main__":
    main()
