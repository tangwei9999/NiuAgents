"""数据源适配器 - 多源回退机制，只使用境内数据源。

数据源优先级（按稳定性排序）：
1. BaoStock - 免费无限流，专为 A 股设计，最稳定
2. AKShare（腾讯财经）- stock_zh_a_hist_tx，限流少
3. AKShare（新浪财经）- stock_zh_a_daily，较稳定
4. AKShare（东方财富）- stock_zh_a_hist，数据最全但容易限流

四个源都落空就返回 None，不拿 Yahoo 兜底：海外源不可达同样只会返回 None，
两种失败分不开，只会无声丢候选。
"""

import functools
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from tradingagents.dataflows import net

logger = logging.getLogger(__name__)


def _bounded_baostock_session(func):
    """Give one BaoStock exchange a socket budget.

    The SDK opens a plain ``socket.socket()`` with no timeout and then reads it
    inside ``while True``, swallowing whatever comes back -- so a server that
    accepts the connection and stalls mid-message never gets given up on, and
    the screener would sit there instead of moving to the next source. The
    timeout is copied onto the socket when BaoStock creates it during login and
    that socket serves the whole session, so holding the budget for the body of
    the call covers connect and every read.
    """

    @functools.wraps(func)
    def wrapped(*args, **kwargs):
        with net.bounded_sockets():
            return func(*args, **kwargs)

    return wrapped


class DataSourceError(Exception):
    """数据源异常基类。"""
    pass


class DataSourceUnavailableError(DataSourceError):
    """数据源不可用（限流、网络错误等）。"""
    pass


class NoDataError(DataSourceError):
    """数据源可用但无数据。"""
    pass


# ============================================================
# 通用技术指标计算
# ============================================================

def _compute_technical_indicators(df: pd.DataFrame, ticker: str) -> dict | None:
    """
    从 DataFrame 计算技术指标。

    Args:
        df: 包含 'close' 和 'volume' 列的 DataFrame
        ticker: 股票代码

    Returns:
        技术指标字典
    """
    if df.empty:
        return None

    close = df["close"].astype(float)
    volume = df["volume"].astype(float)

    if len(close) < 5:
        return None

    # RSI (14 日)
    delta = close.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    current_rsi = float(rsi.iloc[-1]) if not rsi.empty else 50.0

    # 均线
    ma20 = float(close.rolling(window=20).mean().iloc[-1]) if len(close) >= 20 else float(close.iloc[-1])
    ma60 = float(close.rolling(window=60).mean().iloc[-1]) if len(close) >= 60 else float(close.iloc[-1])
    current_price = float(close.iloc[-1])

    # MACD
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    macd_value = float(macd.iloc[-1])
    signal_value = float(signal.iloc[-1])

    # 成交量
    avg_volume_5d = float(volume.tail(5).mean())
    avg_volume_20d = float(volume.tail(20).mean()) if len(volume) >= 20 else float(volume.mean())
    volume_ratio = avg_volume_5d / avg_volume_20d if avg_volume_20d > 0 else 1.0

    return {
        "ticker": ticker,
        "price": current_price,
        "rsi": current_rsi,
        "ma20": ma20,
        "ma60": ma60,
        "macd": macd_value,
        "macd_signal": signal_value,
        "volume_ratio": volume_ratio,
        "above_ma20": current_price > ma20,
        "above_ma60": current_price > ma60,
        "macd_golden_cross": macd_value > signal_value,
    }


# ============================================================
# 数据源 1: BaoStock（最稳定，免费无限流）
# ============================================================

def get_baostock_available() -> bool:
    """检查 BaoStock 是否可用。"""
    try:
        import baostock  # noqa: F401  # 可用性探测
        return True
    except ImportError:
        return False


@_bounded_baostock_session
def get_stock_list_baostock() -> list[dict[str, str]]:
    """
    从 BaoStock 获取 A 股股票列表。

    Returns:
        股票列表
    """
    try:
        import baostock as bs

        lg = bs.login()
        if lg.error_code != '0':
            raise DataSourceError(f"BaoStock 登录失败：{lg.error_msg}")

        # 获取股票列表
        rs = bs.query_stock_basic()
        stocks = []
        while (rs.error_code == '0') and rs.next():
            row = rs.get_row_data()
            code = row[0]  # 证券代码
            name = row[1]  # 证券名称
            delist_date = row[3]  # 退市日期（row[2] 上市日期、row[4] 证券类型用不到）

            # 跳过已退市
            if delist_date and delist_date != '':
                continue

            # 跳过 ST
            if "ST" in name:
                continue

            # 只保留主板和创业板
            if code.startswith("6"):
                ticker = f"{code}.SS"
            elif code.startswith(("0", "3")):
                ticker = f"{code}.SZ"
            else:
                continue

            stocks.append({
                "ticker": ticker,
                "name": name,
                "code": code,
                "market_cap": 0,  # BaoStock 不提供市值
            })

        bs.logout()
        logger.info(f"BaoStock 获取到 {len(stocks)} 只 A 股")
        return stocks

    except ImportError:
        raise DataSourceUnavailableError("BaoStock 未安装") from None
    except Exception as e:
        raise DataSourceError(f"BaoStock 获取失败：{e}") from e


@_bounded_baostock_session
def get_technical_data_baostock(ticker: str, days: int = 90) -> dict | None:
    """
    从 BaoStock 获取股票技术指标。

    Args:
        ticker: 股票代码（如 "600519.SS"）
        days: 获取天数

    Returns:
        技术指标字典
    """
    try:
        import baostock as bs

        # 转换代码格式（600519.SS -> sh.600519）
        code = ticker.split(".")[0]
        if ticker.endswith(".SS"):
            bs_code = f"sh.{code}"
        elif ticker.endswith(".SZ"):
            bs_code = f"sz.{code}"
        else:
            return None

        lg = bs.login()
        if lg.error_code != '0':
            logger.warning(f"BaoStock 登录失败：{lg.error_msg}")
            return None

        end_date = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

        rs = bs.query_history_k_data_plus(
            bs_code,
            "date,open,high,low,close,volume",
            start_date=start_date,
            end_date=end_date,
            frequency="d",
            adjustflag="2"  # 前复权
        )

        data_list = []
        while (rs.error_code == '0') and rs.next():
            data_list.append(rs.get_row_data())

        bs.logout()

        if not data_list:
            return None

        df = pd.DataFrame(data_list, columns=["date", "open", "high", "low", "close", "volume"])
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
        df = df.dropna(subset=["close", "volume"])

        return _compute_technical_indicators(df, ticker)

    except Exception as e:
        logger.debug(f"BaoStock 获取 {ticker} 失败：{e}")
        return None


# ============================================================
# 数据源 2: AKShare 腾讯财经（限流少）
# ============================================================

def get_technical_data_akshare_tx(ticker: str, days: int = 90) -> dict | None:
    """
    从 AKShare 腾讯财经接口获取股票技术指标。

    Args:
        ticker: 股票代码（如 "600519.SS"）
        days: 获取天数

    Returns:
        技术指标字典
    """
    try:
        import akshare as ak

        code = ticker.split(".")[0]

        end_date = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")

        df = ak.stock_zh_a_hist_tx(
            symbol=code,
            start_date=start_date,
            end_date=end_date,
            adjust="qfq"
        )

        if df.empty:
            return None

        # 腾讯接口列名不同，需要重命名
        df = df.rename(columns={
            "date": "date",
            "open": "open",
            "high": "high",
            "low": "low",
            "close": "close",
            "volume": "volume",
        })

        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
        df = df.dropna(subset=["close", "volume"])

        return _compute_technical_indicators(df, ticker)

    except Exception as e:
        err_str = str(e)
        if "限流" in err_str or "Rate" in err_str or "push2" in err_str:
            logger.debug(f"AKShare 腾讯限流：{e}")
            raise DataSourceUnavailableError(f"AKShare 腾讯限流：{e}") from e
        logger.debug(f"AKShare 腾讯获取 {ticker} 失败：{e}")
        return None


# ============================================================
# 数据源 3: AKShare 新浪财经（较稳定）
# ============================================================

def get_technical_data_akshare_sina(ticker: str, days: int = 90) -> dict | None:
    """
    从 AKShare 新浪财经接口获取股票技术指标。

    Args:
        ticker: 股票代码（如 "600519.SS"）
        days: 获取天数

    Returns:
        技术指标字典
    """
    try:
        import akshare as ak

        code = ticker.split(".")[0]

        # 新浪接口需要带后缀
        if ticker.endswith(".SS"):
            sina_code = f"sh{code}"
        elif ticker.endswith(".SZ"):
            sina_code = f"sz{code}"
        else:
            return None

        df = ak.stock_zh_a_daily(symbol=sina_code, adjust="qfq")

        if df.empty:
            return None

        # 只取最近 N 天
        df = df.tail(days)

        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
        df = df.dropna(subset=["close", "volume"])

        return _compute_technical_indicators(df, ticker)

    except Exception as e:
        err_str = str(e)
        if "限流" in err_str or "Rate" in err_str:
            logger.debug(f"AKShare 新浪限流：{e}")
            raise DataSourceUnavailableError(f"AKShare 新浪限流：{e}") from e
        logger.debug(f"AKShare 新浪获取 {ticker} 失败：{e}")
        return None


# ============================================================
# 数据源 4: AKShare 东方财富（数据最全但容易限流）
# ============================================================

def get_akshare_available() -> bool:
    """检查 AKShare 是否可用。"""
    try:
        import akshare  # noqa: F401  # 可用性探测
        return True
    except ImportError:
        return False


def get_stock_list_akshare() -> list[dict[str, str]]:
    """
    从 AKShare（东方财富）获取 A 股股票列表。

    Returns:
        股票列表
    """
    try:
        import akshare as ak

        df = ak.stock_zh_a_spot_em()

        if df.empty:
            raise NoDataError("AKShare 返回空数据")

        stocks = []
        for _, row in df.iterrows():
            code = row.get("代码", "")
            name = row.get("名称", "")
            market_cap = row.get("总市值", 0)
            price = row.get("最新价", 0)

            if "ST" in name:
                continue

            try:
                if float(price) <= 0 or float(market_cap) < 1e9:
                    continue
            except (ValueError, TypeError):
                continue

            if code.startswith("6"):
                ticker = f"{code}.SS"
            elif code.startswith(("0", "3")):
                ticker = f"{code}.SZ"
            else:
                continue

            stocks.append({
                "ticker": ticker,
                "name": name,
                "code": code,
                "market_cap": float(market_cap),
            })

        stocks.sort(key=lambda x: x.get("market_cap", 0), reverse=True)
        logger.info(f"AKShare 获取到 {len(stocks)} 只 A 股（剔除 ST/小盘/科创板后）")
        return stocks

    except ImportError:
        raise DataSourceUnavailableError("AKShare 未安装") from None
    except Exception as e:
        if "Too Many" in str(e) or "Rate" in str(e) or "限流" in str(e):
            raise DataSourceUnavailableError(f"AKShare 限流：{e}") from e
        raise DataSourceError(f"AKShare 获取失败：{e}") from e


def get_technical_data_akshare_em(ticker: str, days: int = 90) -> dict | None:
    """
    从 AKShare 东方财富接口获取股票技术指标。

    Args:
        ticker: 股票代码
        days: 获取天数

    Returns:
        技术指标字典
    """
    try:
        import akshare as ak

        code = ticker.split(".")[0]

        end_date = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")

        df = ak.stock_zh_a_hist(
            symbol=code,
            period="daily",
            start_date=start_date,
            end_date=end_date,
            adjust="qfq"
        )

        if df.empty:
            return None

        df = df.rename(columns={"收盘": "close", "成交量": "volume"})
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
        df = df.dropna(subset=["close", "volume"])

        return _compute_technical_indicators(df, ticker)

    except Exception as e:
        err_str = str(e)
        if "限流" in err_str or "Rate" in err_str or "push2" in err_str:
            logger.debug(f"AKShare 东财限流：{e}")
            raise DataSourceUnavailableError(f"AKShare 东财限流：{e}") from e
        logger.debug(f"AKShare 东财获取 {ticker} 失败：{e}")
        return None


# ============================================================
# 内置股票池（最后兜底）
# ============================================================

def get_stock_list_builtin() -> list[dict[str, str]]:
    """返回内置股票池。

    这个环节以前叫 get_stock_list_yahoo，但它一行 Yahoo 代码都没调过；继续挂
    这个名字，日志里的“Yahoo 失败”就会被当成一次海外取数失败，而它其实是本地兜底。
    """
    try:
        from tradingagents.screener.stock_universe import _get_builtin_stocks
        return _get_builtin_stocks()
    except Exception as e:
        raise DataSourceError(f"内置股票池读取失败：{e}") from e


# ============================================================
# 股票池本地缓存（在线源都失败时的兜底，成功取数时写回）
# ============================================================

# 仅在 AKShare 与 BaoStock 都取不到时读取，避免限流日被降级到 38 只内置池；
# 每次在线取数成功都会覆盖写回，正常每天跑时缓存基本就是昨天的快照。
# 次新股即使缓存几天没更新也几乎不受影响：技术面筛选要 ~20-60 根 K 线，
# 刚上市的股票本来就进不了最终推荐。
_STOCK_LIST_CACHE = Path.home() / ".tradingagents" / "cache" / "a_stock_list.json"
_STOCK_LIST_CACHE_WARN_DAYS = 7


def _cache_age_days(cached_date: str) -> int | None:
    """缓存快照距今天数；日期无效返回 None。"""
    try:
        cached = datetime.strptime(cached_date, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None
    return (datetime.now() - cached).days


def _save_stock_list_cache(stocks: list[dict]) -> None:
    """把一次成功取到的股票池落盘（尽力而为，失败不影响主流程）。"""
    try:
        _STOCK_LIST_CACHE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "count": len(stocks),
            "stocks": stocks,
        }
        tmp = _STOCK_LIST_CACHE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_STOCK_LIST_CACHE)  # 原子替换，避免读到写一半的文件
        logger.debug(f"股票池缓存已更新：{len(stocks)} 只 → {_STOCK_LIST_CACHE}")
    except Exception as e:
        logger.debug(f"写入股票池缓存失败（忽略）：{e}")


def _load_stock_list_cache() -> list[dict] | None:
    """读取最近一次成功取数的股票池快照；无缓存/损坏返回 None。"""
    try:
        if not _STOCK_LIST_CACHE.exists():
            return None
        payload = json.loads(_STOCK_LIST_CACHE.read_text(encoding="utf-8"))
        stocks = payload.get("stocks")
        if not stocks:
            return None
        cached_date = str(payload.get("date", ""))
        age_days = _cache_age_days(cached_date)
        if age_days is not None and age_days > _STOCK_LIST_CACHE_WARN_DAYS:
            logger.warning(
                f"使用 {cached_date} 的股票池缓存（{len(stocks)} 只，已 {age_days} 天），"
                "可能缺少此后新上市的股票"
            )
        else:
            logger.info(f"读取到股票池缓存：{len(stocks)} 只（{cached_date}）")
        return stocks
    except Exception as e:
        logger.warning(f"读取股票池缓存失败（忽略）：{e}")
        return None


# ============================================================
# 统一回退接口
# ============================================================

def get_stock_list_with_fallback() -> list[dict[str, str]]:
    """
    获取股票列表（带回退机制）。

    优先级：AKShare（东方财富）> BaoStock > 本地缓存 > 内置股票池。
    两个在线源取数成功时会写回本地缓存；都失败时优先用最近一次成功的
    缓存快照（避免限流日被降级到 38 只内置池），没有缓存才落到内置池。

    Returns:
        股票列表
    """
    errors = []

    # 1. 尝试 AKShare（东方财富）- 数据最全
    if get_akshare_available():
        try:
            logger.info("尝试从 AKShare（东方财富）获取股票列表...")
            stocks = get_stock_list_akshare()
            if stocks:
                logger.info(f"✓ AKShare 成功获取 {len(stocks)} 只股票")
                _save_stock_list_cache(stocks)
                return stocks
        except DataSourceError as e:
            logger.warning(f"AKShare 失败：{e}")
            errors.append(f"AKShare: {e}")

    # 2. 尝试 BaoStock
    if get_baostock_available():
        try:
            logger.info("尝试从 BaoStock 获取股票列表...")
            stocks = get_stock_list_baostock()
            if stocks:
                logger.info(f"✓ BaoStock 成功获取 {len(stocks)} 只股票")
                _save_stock_list_cache(stocks)
                return stocks
        except DataSourceError as e:
            logger.warning(f"BaoStock 失败：{e}")
            errors.append(f"BaoStock: {e}")

    # 3. 两个在线源都不可用：优先用上次成功取数的本地缓存（比内置 38 只全得多）
    cached = _load_stock_list_cache()
    if cached:
        logger.info(f"在线源均不可用，改用本地股票池缓存（{len(cached)} 只）")
        return cached

    # 4. 没有缓存才回退到内置股票池
    try:
        logger.info("回退到内置股票池...")
        stocks = get_stock_list_builtin()
        if stocks:
            logger.info(f"✓ 内置股票池成功获取 {len(stocks)} 只股票")
            return stocks
    except DataSourceError as e:
        logger.warning(f"内置股票池失败：{e}")
        errors.append(f"内置: {e}")

    raise DataSourceUnavailableError(f"所有数据源都失败：{'; '.join(errors)}")


def get_technical_data_with_fallback(ticker: str) -> dict | None:
    """
    获取技术指标（带回退机制）。

    优先级：
    1. BaoStock - 最稳定，无限流
    2. AKShare 腾讯 - 限流少
    3. AKShare 新浪 - 较稳定
    4. AKShare 东方财富 - 数据全但易限流

    四个境内源都落空就返回 None：不再拿 Yahoo 兜 A 股的底，因为海外源不可达
    时同样只会返回 None，两种失败分不开。

    Args:
        ticker: 股票代码

    Returns:
        技术指标字典
    """
    # 1. BaoStock（最稳定）
    if get_baostock_available():
        try:
            logger.info(f"[1/4] 尝试 BaoStock 获取 {ticker}...")
            data = get_technical_data_baostock(ticker)
            if data:
                logger.info(f"✓ BaoStock 成功获取 {ticker}")
                return data
            else:
                logger.info("BaoStock 返回空数据，尝试下一个源")
        except Exception as e:
            logger.info(f"BaoStock 获取 {ticker} 失败：{e}，尝试下一个源")

    # 2. AKShare 腾讯（限流少）
    if get_akshare_available():
        try:
            logger.info(f"[2/4] 尝试 AKShare 腾讯获取 {ticker}...")
            data = get_technical_data_akshare_tx(ticker)
            if data:
                logger.info(f"✓ AKShare 腾讯成功获取 {ticker}")
                return data
            else:
                logger.info("AKShare 腾讯返回空数据，尝试下一个源")
        except DataSourceUnavailableError:
            logger.info("AKShare 腾讯限流，跳过")
        except Exception as e:
            logger.info(f"AKShare 腾讯获取 {ticker} 失败：{e}，尝试下一个源")

    # 3. AKShare 新浪（较稳定）
    if get_akshare_available():
        try:
            logger.info(f"[3/4] 尝试 AKShare 新浪获取 {ticker}...")
            data = get_technical_data_akshare_sina(ticker)
            if data:
                logger.info(f"✓ AKShare 新浪成功获取 {ticker}")
                return data
            else:
                logger.info("AKShare 新浪返回空数据，尝试下一个源")
        except DataSourceUnavailableError:
            logger.info("AKShare 新浪限流，跳过")
        except Exception as e:
            logger.info(f"AKShare 新浪获取 {ticker} 失败：{e}，尝试下一个源")

    # 4. AKShare 东方财富（数据全但易限流）
    if get_akshare_available():
        try:
            logger.info(f"[4/4] 尝试 AKShare 东方财富获取 {ticker}...")
            data = get_technical_data_akshare_em(ticker)
            if data:
                logger.info(f"✓ AKShare 东财成功获取 {ticker}")
                return data
            else:
                logger.info("AKShare 东财返回空数据")
        except DataSourceUnavailableError:
            logger.info("AKShare 东财限流，跳过")
        except Exception as e:
            logger.info(f"AKShare 东财获取 {ticker} 失败：{e}")

    logger.warning(f"四个境内数据源都无数据：{ticker}")
    return None


if __name__ == "__main__":
    print("测试股票列表获取...")
    try:
        stocks = get_stock_list_with_fallback()
        print(f"获取到 {len(stocks)} 只股票")
        print("前 10 只：")
        for s in stocks[:10]:
            print(f"  {s['ticker']} - {s['name']}")
    except DataSourceUnavailableError as e:
        print(f"错误：{e}")
