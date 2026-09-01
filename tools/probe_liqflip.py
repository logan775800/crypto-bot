# -*- coding: utf-8 -*-
"""「5 分钟内爆仓一边倒 → 反转」这个假设，先回测再决定做不做。

判据：找出所有「总爆仓够大 且 某一侧占比 ≥80%」的 5 分钟窗口，
看它们之后 15m / 1h / 4h 的价格走向，和**同一批币的全体窗口**比。
一边倒爆仓天天都有，关键是它之后的走势和平均情况有没有差别——
没差别就是个好看的指标，不是信号。

门槛必须**按币自适应**：$1 万爆仓对 BTC 是零头、对小币是天量。
所以用「这个币自己 7 天爆仓分布的分位数」当闸。
"""
import asyncio, statistics, sys
import httpx

H = {"User-Agent": "Mozilla/5.0"}
G = "https://api.gateio.ws/api/v4"
SIDE_TH = 0.80        # 一边占比门槛（他要的 80%）
FWD = [(3, "15m"), (12, "1h"), (48, "4h")]


async def j(c, u, p=None):
    try:
        r = await c.get(u, params=p, headers=H, timeout=25)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


async def bars(c, base):
    d = await j(c, f"{G}/futures/usdt/contract_stats",
                {"contract": f"{base}_USDT", "interval": "5m", "limit": 2000})
    out = []
    for x in (d or []):
        try:
            out.append({
                "t": int(x["time"]),
                "p": float(x["mark_price"]),
                "l": float(x.get("long_liq_usd") or 0),
                "s": float(x.get("short_liq_usd") or 0)})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def fwd(rows, i, n):
    if i + n >= len(rows):
        return None
    a, b = rows[i]["p"], rows[i + n]["p"]
    return (b - a) / a * 100 if a else None


def run(rows, pct_cut):
    """→ {窗口: (一边倒空头样本, 一边倒多头样本, 全体样本)}"""
    liq = sorted(r["l"] + r["s"] for r in rows if r["l"] + r["s"] > 0)
    if len(liq) < 30:
        return None
    cut = liq[int(len(liq) * pct_cut)]
    out = {lab: ([], [], []) for _n, lab in FWD}
    for i, r in enumerate(rows):
        tot = r["l"] + r["s"]
        for n, lab in FWD:
            f = fwd(rows, i, n)
            if f is None:
                continue
            out[lab][2].append(f)
            if tot < cut:
                continue
            if r["s"] / tot >= SIDE_TH:
                out[lab][0].append(f)      # 爆的全是空头 → 刚被轧空
            elif r["l"] / tot >= SIDE_TH:
                out[lab][1].append(f)      # 爆的全是多头 → 刚被砸穿
    return out, cut


async def main():
    pct_cut = float(sys.argv[1]) if len(sys.argv) > 1 else 0.80
    async with httpx.AsyncClient() as c:
        tk = await j(c, "https://fapi.binance.com/fapi/v1/ticker/24hr")
        syms = [t["symbol"][:-4] for t in sorted(
            [x for x in tk if x["symbol"].endswith("USDT")],
            key=lambda x: -float(x["quoteVolume"]))[:30]]
        got = await asyncio.gather(*[bars(c, s) for s in syms])

    agg = {lab: ([], [], []) for _n, lab in FWD}
    used = 0
    for s, rows in zip(syms, got):
        if len(rows) < 200:
            continue
        r = run(rows, pct_cut)
        if not r:
            continue
        res, _cut = r
        used += 1
        for lab in agg:
            for k in range(3):
                agg[lab][k].extend(res[lab][k])

    print(f"币数 {used}　总爆仓额门槛=该币 7 天分布的 {pct_cut*100:.0f} 分位　"
          f"一边占比 ≥{SIDE_TH*100:.0f}%\n")
    print(f"{'窗口':6}{'情形':22}{'样本':>7}{'平均':>9}{'中位':>9}{'上涨占比':>10}")
    for _n, lab in FWD:
        sh, lo, allv = agg[lab]
        for name, v in (("空头一边倒被爆(刚轧空)", sh),
                        ("多头一边倒被爆(刚砸穿)", lo),
                        ("全体窗口(基线)", allv)):
            if len(v) < 20:
                print(f"{lab:6}{name:22}{len(v):>7}　样本太少")
                continue
            up = sum(1 for x in v if x > 0) / len(v) * 100
            print(f"{lab:6}{name:22}{len(v):>7}{statistics.mean(v):>8.3f}%"
                  f"{statistics.median(v):>8.3f}%{up:>9.1f}%")
        print()

asyncio.run(main())
