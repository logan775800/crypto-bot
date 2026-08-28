"""候选数据维度的区分度排序。**判据和之前一样**：
同一时刻取两组币（涨跌最大的 vs 成交额最大的作基线），看这个指标
在两组之间能不能分开。分不开的字段加了也是噪音，只会挤走真信号。
"""
import asyncio, statistics, httpx
H = {"User-Agent": "Mozilla/5.0"}
BN = "https://fapi.binance.com"
G = "https://api.gateio.ws/api/v4"

async def j(c, u, p=None):
    r = await c.get(u, params=p, headers=H, timeout=20)
    return r.json() if r.status_code == 200 else None

async def one(c, sym):
    base = sym[:-4]
    out = {"sym": base}
    # 主动买卖盘（币安，24 根小时）
    tk = await j(c, f"{BN}/futures/data/takerlongshortRatio",
                 {"symbol": sym, "period": "1h", "limit": 25})
    if tk and len(tk) > 2:
        v = [float(x["buySellRatio"]) for x in tk]
        out["taker_now"] = v[-1]
        out["taker_pct"] = (v[-1] - v[0]) / v[0] * 100 if v[0] else None
        out["taker_dev"] = abs(v[-1] - 1) * 100          # 离平衡多远
    # 现货 vs 合约成交额（同 24h）
    sp = await j(c, "https://api.binance.com/api/v3/ticker/24hr", {"symbol": sym})
    pf = await j(c, f"{BN}/fapi/v1/ticker/24hr", {"symbol": sym})
    if sp and pf:
        s, p = float(sp["quoteVolume"]), float(pf["quoteVolume"])
        if s > 0:
            out["perp_spot"] = p / s                      # 合约是现货的几倍
    # 现货主动买入占比（CVD 方向）
    kl = await j(c, "https://api.binance.com/api/v3/klines",
                 {"symbol": sym, "interval": "1h", "limit": 24})
    if kl and len(kl) > 2:
        tot = sum(float(k[7]) for k in kl)
        buy = sum(float(k[10]) for k in kl)
        if tot > 0:
            out["spot_buy_share"] = buy / tot * 100
    # 大单占比
    ag = await j(c, f"{BN}/fapi/v1/aggTrades", {"symbol": sym, "limit": 1000})
    if ag:
        tot = sum(float(x["p"]) * float(x["q"]) for x in ag)
        big = sum(float(x["p"]) * float(x["q"]) for x in ag
                  if float(x["p"]) * float(x["q"]) > 50_000)
        if tot > 0:
            out["big_share"] = big / tot * 100
    return out

async def main():
    async with httpx.AsyncClient() as c:
        tk = await j(c, f"{BN}/fapi/v1/ticker/24hr")
        rows = [t for t in tk if t["symbol"].endswith("USDT")
                and float(t["quoteVolume"]) > 20_000_000]
        movers = sorted(rows, key=lambda t: -abs(float(t["priceChangePercent"])))[:20]
        base = sorted(rows, key=lambda t: -float(t["quoteVolume"]))[:30]
        got = {}
        for name, grp in (("异动", movers), ("基线", base)):
            got[name] = [g for g in await asyncio.gather(
                *[one(c, t["symbol"]) for t in grp]) if g]
    keys = [("taker_dev", "主动盘失衡度(离1有多远)"), ("taker_pct", "主动盘24h变化"),
            ("perp_spot", "合约/现货成交额倍数"), ("spot_buy_share", "现货主动买入占比"),
            ("big_share", "大单(>5万)占比")]
    print(f"{'指标':26}{'异动组':>10}{'基线组':>10}{'能分开':>9}")
    for k, label in keys:
        a = [abs(g[k]) for g in got["异动"] if g.get(k) is not None]
        b = [abs(g[k]) for g in got["基线"] if g.get(k) is not None]
        if len(a) < 5 or len(b) < 5:
            print(f"{label:26}{'样本不足':>10}")
            continue
        ma, mb = statistics.median(a), statistics.median(b)
        print(f"{label:26}{ma:10.1f}{mb:10.1f}{(ma/mb if mb else 0):8.2f}x")
    print("\n异动组明细（按涨跌幅排）：")
    chg = {t["symbol"][:-4]: float(t["priceChangePercent"]) for t in movers}
    for g in sorted(got["异动"], key=lambda g: -abs(chg.get(g["sym"], 0)))[:10]:
        print(f"  {g['sym']:<12}{chg.get(g['sym'],0):+7.1f}%  "
              f"主动买卖 {g.get('taker_now', 0):.2f}  "
              f"合约/现货 {g.get('perp_spot', 0):5.1f}x  "
              f"现货主买 {g.get('spot_buy_share', 0):4.1f}%  "
              f"大单 {g.get('big_share', 0):4.1f}%")

asyncio.run(main())
