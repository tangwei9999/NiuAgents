"""数据源限流自查：用对照隔离法判断"是不是真的被东方财富限流"。

用法：
    python -m tradingagents.screener.diagnose_sources [示例股票代码]

思路（对照隔离）：
  1) 非东财对照（腾讯 / 新浪 / baidu）——排除"本机断网/代理问题"。
  2) 东财 push2 轻量单请求——看单次轻请求能否漏过（限流是抖动的，轻请求偶尔能过）。
  3) akshare 真实接口（排行 / 个股历史资金流）——东财限流时这里会抛 Push2RateLimitError。

判读：
  - 对照全失败           → 先查本机网络 / 代理，不是东财的锅。
  - 对照 OK 但 akshare 报 Push2RateLimitError → 确认是东财 push2 系限流（按滚动窗口请求量封 IP）。
  - 全部 OK              → 当前未被限流。

默认只做几个轻量请求，不拉全市场快照（59 页），以免自己把限流顶起来。
"""

from __future__ import annotations

import sys
import time
from urllib.parse import urlsplit

import requests

_UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}


def _is_push2_rate_limit(exc: BaseException) -> bool:
    """akshare 的限流异常类不在本项目里，按类名匹配避免耦合其内部路径。"""
    return type(exc).__name__ == "Push2RateLimitError"


def _probe_http(label: str, url: str, headers: dict, timeout=(5, 15)) -> str:
    t = time.perf_counter()
    host = urlsplit(url).hostname or ""
    tag = "东财" if host.endswith("eastmoney.com") else "对照"
    try:
        r = requests.get(url, headers=headers, timeout=timeout)
        return f"[{tag}] {label:<16} HTTP {r.status_code}  {len(r.text)}B  {time.perf_counter()-t:.2f}s"
    except Exception as e:  # noqa: BLE001 - 诊断脚本需要吞掉并展示任意底层异常
        return f"[{tag}] {label:<16} 异常 {type(e).__name__}: {str(e)[:80]}  {time.perf_counter()-t:.2f}s"


def _probe_akshare(label: str, fn) -> tuple[str, bool]:
    """返回 (展示行, 是否命中东财限流)。"""
    t = time.perf_counter()
    try:
        df = fn()
        n = 0 if df is None else len(df)
        return f"[东财] {label:<16} OK 行数={n}  {time.perf_counter()-t:.2f}s", False
    except Exception as e:  # noqa: BLE001
        hit = _is_push2_rate_limit(e)
        tag = "限流" if hit else "异常"
        return f"[东财] {label:<16} {tag} {type(e).__name__}: {str(e)[:60]}  {time.perf_counter()-t:.2f}s", hit


def main(argv: list[str]) -> int:
    code = argv[1] if len(argv) > 1 else "002428"
    market = "sh" if code.startswith(("5", "6", "9")) else "sz"

    print(f"数据源限流自查（示例代码 {code}.{market}，仅轻量请求）\n" + "-" * 64)

    # 1) 非东财对照
    ctrl_fail = 0
    for label, url, headers in (
        ("腾讯对照", f"https://qt.gtimg.cn/q={market}{code}", {"User-Agent": "Mozilla/5.0"}),
        ("新浪对照", f"https://hq.sinajs.cn/list={market}{code}",
         {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"}),
        ("baidu对照", "https://www.baidu.com", {"User-Agent": "Mozilla/5.0"}),
    ):
        line = _probe_http(label, url, headers)
        print(line)
        if "HTTP 2" not in line:
            ctrl_fail += 1

    print("-" * 64)

    # 2) 东财 push2 轻量单请求（排行第 1 页 5 条）
    print(_probe_http(
        "push2轻GET",
        "https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=5&po=1&np=1"
        "&fltt=2&invt=2&fid=f62&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23&fields=f12,f14,f62",
        _UA,
    ))

    # 3) akshare 真实接口（限流时这里最可靠）
    try:
        import akshare as ak
    except ImportError:
        print("[akshare] 未安装，跳过真实接口测试（pip install \"tradingagents[a-share]\"）")
        ak = None

    banned = False
    if ak is not None:
        for label, fn in (
            ("ak排行今日", lambda: ak.stock_individual_fund_flow_rank(indicator="今日")),
            ("ak个股历史", lambda: ak.stock_individual_fund_flow(stock=code, market=market)),
        ):
            line, hit = _probe_akshare(label, fn)
            print(line)
            banned = banned or hit

    # 判读
    print("-" * 64)
    if ctrl_fail == 3:
        print("结论：非东财对照全部失败 → 先排查本机网络 / 代理 / VPN，这不是东财限流。")
    elif banned:
        print("结论：对照正常，但 akshare 命中 Push2RateLimitError → 确认是东方财富 push2 系限流。")
        print("处置：① 停止连续重跑（每次全市场快照都在续命封禁），等约 5–40 分钟；"
              "② 资金流可配 TUSHARE_TOKEN 绕开 push2；③ 消息面/板块无免费第二源，限流时只能降级。")
    else:
        print("结论：各源均正常 → 当前未被东财限流，可以正常跑选股器。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
