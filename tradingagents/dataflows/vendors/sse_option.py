"""Shanghai ETF option chain: the domestic stand-in for event probabilities.

China has no public prediction market, so nothing here answers "what does the
crowd price the odds of a Fed cut to". What the domestic market *does* price, in
liquid two-sided markets, is where a broad index will close on an expiry date --
the SSE ETF option chain. From adjacent strikes this module inverts the two
quantities that carry the same shape of information Polymarket's odds do:

- the expiry forward, from put-call parity on matched strikes;
- the risk-neutral probability of closing above (or below) a strike, from the
  slope of the call curve, cross-checked against the put curve.

Those are probabilities the market pays real money to hold, which is why the
report labels them as priced odds and not as a forecast: a risk-neutral number
carries the volatility premium and prices tails richer than the physical
measure does.

Data comes from AKShare's ``option_finance_board``, which reads the SSE quote
server (``yunhq.sse.com.cn``) -- measured here at 0.05-0.11s per expiry month,
keyless, and on a host a China network reaches. Boards whose payload does not
carry a strike/price pair (the SZSE and CFFEX layouts AKShare returns under
different column names) are reported as uncovered rather than guessed at.
"""

from __future__ import annotations

import datetime
import logging
import math
import re
from collections.abc import Callable

from tradingagents.dataflows.date_window import is_historical
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError

logger = logging.getLogger(__name__)

# Board symbol -> the index it prices. Only the SSE ETF boards, whose payload
# carries 合约交易代码 / 行权价 / 当前价 and therefore inverts to a probability.
BOARDS: dict[str, str] = {
    "华泰柏瑞沪深300ETF期权": "沪深300",
    "华夏上证50ETF期权": "上证50",
    "南方中证500ETF期权": "中证500",
    "华夏科创50ETF期权": "科创50",
}
DEFAULT_BOARD = "华泰柏瑞沪深300ETF期权"

# A topic that names a style or a sector points at the broad index that carries
# it; the default (whole-market) board is always included on top of that. Models
# ask in English as often as in Chinese, so each board lists both.
TOPIC_BOARDS: dict[str, tuple[str, ...]] = {
    "华夏科创50ETF期权": (
        "科创", "芯片", "半导体", "国产替代", "生物医药", "硬科技", "人工智能",
        "semiconductor", "chip", "artificial intelligence", "biotech",
        "star market", "kechuang", "ai infra",
    ),
    "华夏上证50ETF期权": (
        "上证50", "银行", "保险", "白酒", "权重", "红利", "央企",
        "bank", "insurance", "liquor", "dividend", "blue chip",
    ),
    "南方中证500ETF期权": (
        "中证500", "中小盘", "专精特新", "军工", "机械", "材料",
        "small cap", "small-cap", "mid cap", "defense", "military",
        "machinery", "materials",
    ),
}

# Narrative events an index option chain cannot price, whoever causes them -- a
# rate cut is unpriced whether the Fed or the PBoC is expected to deliver it. The
# index odds are still a read on market regime, so they are kept, but the caveat
# has to be the first thing a model reads, not a footnote after two tables.
UNPRICED_EVENT = re.compile(
    r"\b(fed|fomc|ecb|boj|opec|federal reserve|fed funds|tariffs?|election|"
    r"midterm|runoff|recession|shutdown|white house|congress|senate|payrolls|"
    r"rate cut|rate hike|rate cuts|rate hikes|cut rates|hiked rates|sanctions|"
    r"geopolitics|geopolitical)\b"
    r"|美联储|美国联储|降息|加息|关税|总统选举|中期选举|衰退|地缘",
    re.IGNORECASE,
)

# Money-market rate used to discount the parity and strike slopes. One and a
# half percent changes a two-month probability in the fourth decimal, so the
# number is a convention, not an input to be tuned.
RISK_FREE_RATE = 0.015

DEFAULT_LIMIT = 4  # strikes shown per expiry, nearest the money first

# Float slack for comparing two strike odds as equal, documented in _monotone.
_ODDS_TOL = 1e-9

# 10012498-style codes are SSE ETF option contract IDs; the tstyle list returns
# "<underlying><C|P><yymm><M|A|B><strike x1000>" (e.g. 510300C2612M04400).
_SIDE = re.compile(r"([CP])\d{4}[A-Z]")

# Parsed quotes live one day only: the chain reprices continuously, and a batch
# of A-share analyses asks the same two expiries once per stock.
_CACHE: dict[tuple[int, str, str], tuple[dict, dict]] = {}


def reset_quote_cache() -> None:
    """Drop parsed boards (tests, and anything that must see a fresh chain)."""
    _CACHE.clear()


def _fourth_wednesday(year: int, month: int) -> datetime.date:
    """SSE ETF options expire on the fourth Wednesday of the contract month."""
    day = datetime.date(year, month, 1)
    wednesdays = 0
    while True:
        if day.weekday() == 2:
            wednesdays += 1
            if wednesdays == 4:
                return day
        day += datetime.timedelta(days=1)


def _contract_months(today: datetime.date) -> list[tuple[str, datetime.date]]:
    """The two nearest contract months still ahead, as (month code, expiry)."""
    out = []
    for offset in (0, 1):
        month = today.month + offset
        year = today.year
        if month > 12:
            month -= 12
            year += 1
        expiry = _fourth_wednesday(year, month)
        if (expiry - today).days >= 1:
            out.append((f"{month:02d}", expiry))
    return out


def _boards_for(topic: str) -> list[str]:
    """The boards a topic points at, default whole-market board always first."""
    text = (topic or "").lower()
    boards = [DEFAULT_BOARD]
    boards += [
        board for board, keys in TOPIC_BOARDS.items()
        if board != DEFAULT_BOARD and any(key.lower() in text for key in keys)
    ]
    return boards


def _fetch_board(fetch: Callable[[], object], board: str, month: str, symbol: str):
    """Run one AKShare board call, mapping failures onto the vendor taxonomy."""
    try:
        return fetch()
    except KeyError as e:
        # A month the exchange does not list yet: coverage, not a broken call.
        raise NoMarketDataError(
            symbol, detail=f"{board} 无 {month} 月到期的挂牌合约"
        ) from e
    except Exception as e:  # network, parse, or an AKShare-side schema change
        raise VendorUnavailableError(f"上交所期权板获取失败（{board} {month}）: {e}") from e


def _quotes(frame, board: str, month: str, symbol: str) -> tuple[dict, dict]:
    """Split one board into {strike: last price} for calls and for puts."""
    columns = [str(name) for name in getattr(frame, "columns", ())]
    missing = {"合约交易代码", "行权价", "当前价"} - set(columns)
    if missing:
        # SZSE and CFFEX payloads AKShare returns under other column names carry
        # no usable strike/price pair; say so instead of guessing.
        raise NoMarketDataError(
            symbol, detail=f"{board} 的 {month} 月板缺少字段 {sorted(missing)}，无法反解概率"
        )
    calls: dict[float, float] = {}
    puts: dict[float, float] = {}
    for code, strike, price in zip(
        frame["合约交易代码"], frame["行权价"], frame["当前价"], strict=False
    ):
        match = _SIDE.search(str(code))
        if match is None:
            continue
        try:
            key, value = float(strike), float(price)
        except (TypeError, ValueError):
            continue
        (calls if match.group(1) == "C" else puts)[key] = value
    if not calls and not puts:
        raise NoMarketDataError(symbol, detail=f"{board} {month} 月板没有可读合约")
    return calls, puts


def _board_quotes(board: str, month: str, symbol: str) -> tuple[dict, dict]:
    """Parsed strikes for one board and month, cached for the day it was read."""
    key = (datetime.date.today().toordinal(), board, month)
    if key not in _CACHE:
        import akshare as ak

        frame = _fetch_board(
            lambda: ak.option_finance_board(symbol=board, end_month=month),
            board, month, symbol,
        )
        _CACHE[key] = _quotes(frame, board, month, symbol)
    return _CACHE[key]


def _discount(expiry: datetime.date, today: datetime.date) -> float:
    years = max((expiry - today).days, 1) / 365.0
    return math.exp(-RISK_FREE_RATE * years)


def _forward(calls: dict, puts: dict, discount: float) -> float | None:
    """Expiry forward from parity: C - P = DF * (F - K), median over strikes."""
    values = [
        k + (calls[k] - puts[k]) / discount
        for k in sorted(set(calls) & set(puts))
        if calls[k] > 0 and puts[k] > 0
    ]
    if not values:
        return None
    values.sort()
    mid = len(values) // 2
    if len(values) % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2.0


def _monotone(rows: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Drop strikes whose odds break the decay that P(close above K) must obey.

    A far out-of-the-money last trade is frequently a stale quote, so the slope
    it implies can jump back up: a ladder reading "0.2% above 4.75, 15.1% above
    4.85, 0.2% above 4.95" is self-contradictory. Such a row is dropped rather
    than smoothed, because clipping would pass noise off as a market number.

    The comparison carries a tolerance because the smallest difference the chain
    can express is about two tenths of a percent, while two equal slopes differ
    by 1e-17 of float noise -- without it, a run of identical tail odds would
    drop its own members at the mercy of the last bit.
    """
    kept: list[tuple[float, float]] = []
    for strike, prob in sorted(rows):
        if kept and prob > kept[-1][1] + _ODDS_TOL:
            continue
        kept.append((strike, prob))
    return kept


def _ladder(calls: dict, puts: dict, discount: float) -> list[tuple[float, float]]:
    """(strike, P(close above)) from the strike slope of the call curve.

    Adjacent calls price a bear spread, whose slope is the digital; the put side
    prices the same event from below, so where both are readable they are
    averaged, and a side that reads outside (0, 1] -- a stale or zero quote on a
    far strike -- is dropped rather than clipped.
    """
    rows: list[tuple[float, float]] = []
    strikes = sorted(calls)
    for low, high in zip(strikes, strikes[1:]):
        gap = high - low
        if gap <= 0:
            continue
        up = (calls[low] - calls[high]) / gap / discount
        valid_up = 0.0 < up <= 1.0
        down = None
        if low in puts and high in puts:
            candidate = (puts[high] - puts[low]) / gap / discount
            if 0.0 < candidate <= 1.0:
                down = candidate
        if valid_up and down is not None:
            rows.append(((low + high) / 2.0, (up + (1.0 - down)) / 2.0))
        elif valid_up:
            rows.append(((low + high) / 2.0, up))
        elif down is not None:
            rows.append(((low + high) / 2.0, 1.0 - down))
    return _monotone(rows)


def _digits(value: float) -> int:
    """Decimals that keep a strike readable: 4.13 for an ETF, 4133 for an index."""
    return 3 if value < 10 else 0


def _format_ladder(rows: list[tuple[float, float]], limit: int, forward: float) -> list[str]:
    """The ``limit`` strikes nearest the forward, as two odds lines."""
    picked = sorted(rows, key=lambda r: abs(r[0] - forward))[:limit]
    picked.sort()
    lines = []
    for above in (True, False):
        label = "收盘高于" if above else "收盘低于"
        cells = [
            f"{k:.{_digits(k)}f}：{(p if above else 1 - p) * 100:.1f}%"
            for k, p in picked
        ]
        lines.append(f"- {label}：" + "　".join(cells))
    return lines


def get_prediction_markets(
    topic: str, limit: int | None = None, as_of_date: str | None = None
) -> str:
    """Return option-implied probabilities for the nearest index expiries.

    Args:
        topic: Free text from the news analyst. A style or sector name picks an
            extra index board; a topic about an overseas event is reported as
            not covered rather than answered with Chinese index odds alone.
        limit: Strikes shown per expiry (nearest the money first).
        as_of_date: The analysis date; the chain is live-only, so a past date
            withholds it, as Polymarket does.

    Returns:
        A markdown report per expiry: the parity forward and the odds of closing
        above and below the shown strikes.
    """
    if is_historical(as_of_date):
        return (
            f"期权隐含概率对 {as_of_date} 不可用：上交所期权板只提供当前链条，"
            f"没有历史时点，给出会把决策后的信息带进 {as_of_date} 的分析。"
        )
    if limit is None:
        limit = DEFAULT_LIMIT

    today = datetime.date.today()
    months = _contract_months(today)
    if not months:
        raise NoMarketDataError(topic, detail="当前没有未到期且还有交易时间的合约月")

    header = (
        "## 上交所 ETF 期权隐含概率（市场定价，不是预测）\n"
        "国内没有公开的事件预测市场，最接近“市场对前瞻事件的定价”的是股指期权链。"
        "下列概率为期权链反解的风险中性概率（认购/认沽对行权价的价差分），"
        "含波动率溢价，通常把下行尾部定得比实际更重，按市场定价理解。\n\n"
    )
    lead = ""
    if UNPRICED_EVENT.search(topic or ""):
        lead = (
            f"先说明：本数据源只定价境内宽基指数的到期价位，读不出「{topic}」这类"
            "事件发生的概率。下面的数字可以当 A 股整体风险偏好的参考，但不要用它"
            "反推该事件。\n\n"
        )

    blocks: list[str] = []
    for board in _boards_for(topic):
        label = BOARDS[board]
        for month, expiry in months:
            calls, puts = _board_quotes(board, month, topic)
            discount = _discount(expiry, today)
            forward = _forward(calls, puts, discount)
            rows = _ladder(calls, puts, discount)
            if forward is None or not rows:
                continue
            months_ahead = "本月" if month == f"{today.month:02d}" else "下月"
            blocks.append(
                f"**{label}（{board}）{month} 月到期 {expiry.isoformat()}**"
                f"（{months_ahead}合约，隐含前向价 {forward:.{_digits(forward)}f}）\n"
                + "\n".join(_format_ladder(rows, limit, forward))
            )

    if not blocks:
        raise NoMarketDataError(topic, detail="期权板未能反解出可用的到期概率")

    return (
        header
        + lead
        + "\n\n".join(blocks)
        + "\n\n注：以上只覆盖宽基指数到期价位的定价。个股或行业事件"
        "（如某公司的订单、某项政策的出台）在国内没有对应的可交易概率市场，"
        f"话题「{topic}」本身未被此数据源定价。\n"
    )
