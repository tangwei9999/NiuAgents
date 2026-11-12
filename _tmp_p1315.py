import time
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed

print("== P13 例1 ==")
df = pd.DataFrame(
    [["2026-11-03", "1450.0"], ["2026-11-04", "1462.5"], ["2026-11-05", "1455.0"],
     ["2026-11-06", "1470.2"], ["2026-11-09", "1481.0"], ["2026-11-10", "1476.8"]],
    columns=["date", "close"],
)
print("columns:", list(df.columns), "| len:", len(df), "| empty:", df.empty)
print("df['close'] 类型:", type(df["close"]).__name__)
close = df["close"].astype(float)
print("astype 后 dtype:", close.dtype)
print("diff:", list(close.diff()))
print("rolling(3).mean():", list(close.rolling(3).mean()))
print("rolling(20).mean().iloc[-1]:", close.rolling(20).mean().iloc[-1])
print("close.iloc[-1]:", float(close.iloc[-1]), "| tail(3):", list(close.tail(3)))
print("mean:", round(float(close.mean()), 4), "| max-min:", round(float(close.max() - close.min()), 4))
print("df.iloc[-1]:", dict(df.iloc[-1]))

print("\n== P13 例2 脏数据 ==")
raw = pd.DataFrame({"代码": ["600519.SH", "000001.SZ", "300750.SZ"],
                    "主力净流入-净额": ["1234.5", "1,234.5", None]})
flow = pd.to_numeric(raw["主力净流入-净额"], errors="coerce")
print("to_numeric:", list(flow))
print("mask >1000:", list(flow > 1000))
print("rows kept:", len(raw[flow > 1000]))
print("NaN>1000 是:", bool(flow.iloc[2] > 1000))
clean = raw.assign(_f=flow).dropna(subset=["_f"]).sort_values("_f", ascending=False)
print("assign+dropna+sort:", list(clean["代码"]), "| 原 raw 顺序未变:", list(raw["代码"]))
print("head(1) 取值:", clean.head(1)["代码"].tolist())
print("str 链:", list(raw["代码"].astype(str).str.split(".").str[0].str.zfill(6)))
print("列名字符串匹配:", [c for c in clean.columns if "主力净流入" in c and "净额" in c])
print("set 子集判断:", {"代码", "主力净流入-净额"} <= set(raw.columns),
      "| 缺列时:", {"代码", "nope"} <= set(raw.columns))

print("\n== P15 例1 IO 并发 ==")
def io_task(i):
    time.sleep(0.3)
    return i * 10
t0 = time.time(); serial = [io_task(i) for i in range(8)]; st = time.time() - t0
t0 = time.time()
with ThreadPoolExecutor(max_workers=16) as ex:
    par = list(ex.map(io_task, range(8)))
pt = time.time() - t0
print(f"串行 8×0.3s = {st:.2f}s | 16 线程 = {pt:.2f}s | 结果一致: {serial == par == [0,10,20,30,40,50,60,70]}")

print("\n== P15 例2 GIL：CPU 密集不提速 ==")
def cpu_task(n):
    return sum(i * i for i in range(n))
t0 = time.time(); r_serial = [cpu_task(2_000_000) for _ in range(4)]; serial_t = time.time() - t0
t0 = time.time()
with ThreadPoolExecutor(max_workers=4) as ex:
    r_thread = list(ex.map(cpu_task, [2_000_000] * 4))
thread_t = time.time() - t0
print(f"4×纯计算 串行 {serial_t:.2f}s | 4 线程 {thread_t:.2f}s | 结果一致: {r_serial == r_thread}")
print("结论：线程数对纯计算无效（GIL）；本项目 3 处线程池全是网络 IO，所以能提速。")

print("\n== P15 例3 失败隔离 ==")
def worker(i):
    time.sleep(0.05)
    if i == 2:
        raise ValueError("数据源限流")
    return {"i": i, "ok": True}
pool = ThreadPoolExecutor(max_workers=16)
futs = {pool.submit(worker, i): i for i in range(6)}
good, bad = [], []
for f in as_completed(futs):
    try:
        good.append(f.result())
    except Exception as e:
        bad.append((futs[f], type(e).__name__, str(e)))
print("as_completed 隔离: 成功", sorted(g["i"] for g in good), "失败", bad)
pool.shutdown()

def worker2(i):
    if i == 2:
        raise ValueError("boom")
    return i
print("map 惰性（迭代到第 3 个才抛）:", end=" ")
gen = ThreadPoolExecutor(max_workers=16).map(worker2, range(5))
got = []
try:
    for v in gen:
        got.append(v)
except ValueError as e:
    print(got, "-> 抛出", e)

print("\n== P15 submit 与 map 的异常时机 ==")
ex = ThreadPoolExecutor(max_workers=4)
f = ex.submit(worker2, 2)
print("submit 后不碰它，什么都不发生；.result() 才抛:", end=" ")
try:
    f.result()
except ValueError as e:
    print("ValueError:", e)
ex.shutdown()
