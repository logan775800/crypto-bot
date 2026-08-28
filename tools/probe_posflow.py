"""量一下：大户持仓比 / 人数多空比 在 24h 里到底会动多少，好定「算不算变化」的门槛。"""
import asyncio, httpx, statistics
BN = "https://fapi.binance.com"

async def series(c, ep, sym, n=25):
    r = await c.get(f"{BN}/futures/data/{ep}",
                    params={"symbol": sym, "period": "1h", "limit": n})
    d = r.json()
    return d if isinstance(d, list) else []

async def one(c, sym):
    top, ret, oi = await asyncio.gather(
        series(c, "topLongShortPositionRatio", sym),
        series(c, "globalLongShortAccountRatio", sym),
        series(c, "openInterestHist", sym))
    if len(top) < 2 or len(ret) < 2 or len(oi) < 2:
        return None
    f = lambda s, k: (float(s[0][k]), float(s[-1][k]))
    t0, t1 = f(top, "longShortRatio"); r0, r1 = f(ret, "longShortRatio")
    o0, o1 = f(oi, "sumOpenInterestValue")
    return {"sym": sym,
            "top_pct": (t1 - t0) / t0 * 100 if t0 else None,
            "ret_pct": (r1 - r0) / r0 * 100 if r0 else None,
            "oi_pct": (o1 - o0) / o0 * 100 if o0 else None}

async def main():
    async with httpx.AsyncClient(timeout=20) as c:
        tk = (await c.get(f"{BN}/fapi/v1/ticker/24hr")).json()
        rows = [t for t in tk if t["symbol"].endswith("USDT")
                and float(t["quoteVolume"]) > 20_000_000]
        movers = sorted(rows, key=lambda t: -abs(float(t["priceChangePercent"])))[:25]
        base = sorted(rows, key=lambda t: -float(t["quoteVolume"]))[:40]
        for name, grp in (("涨跌最大 25 个", movers), ("成交额最大 40 个（基线）", base)):
            got = [g for g in await asyncio.gather(
                *[one(c, t["symbol"]) for t in grp]) if g]
            for k in ("top_pct", "ret_pct", "oi_pct"):
                v = sorted(abs(g[k]) for g in got if g[k] is not None)
                if not v:
                    continue
                q = lambda p: v[min(len(v) - 1, int(len(v) * p))]
                print(f"{name:22} |Δ{k:8}| 中位 {statistics.median(v):5.1f}%  "
                      f"75分位 {q(.75):5.1f}%  90分位 {q(.90):5.1f}%  最大 {v[-1]:6.1f}%")
            print()
        # 大涨的币里，大户和散户方向是不是真的常常相反？
        got = [g for g in await asyncio.gather(*[one(c, t["symbol"]) for t in movers]) if g]
        chg = {t["symbol"]: float(t["priceChangePercent"]) for t in movers}
        opp = sum(1 for g in got if g["top_pct"] and g["ret_pct"]
                  and g["top_pct"] * g["ret_pct"] < 0)
        print(f"涨跌最大的 {len(got)} 个里，大户与散户方向相反的：{opp} 个")
        for g in sorted(got, key=lambda g: -abs(chg[g["sym"]]))[:10]:
            print(f"  {g['sym']:<14}{chg[g['sym']]:+7.1f}%  大户{g['top_pct']:+7.1f}%  "
                  f"散户{g['ret_pct']:+7.1f}%  OI{g['oi_pct']:+7.1f}%")

asyncio.run(main())
