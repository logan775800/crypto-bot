# -*- coding: utf-8 -*-
"""绝对金额到底重不重要？

他反馈「5106 U 的爆仓有啥意思」。当前门槛只有相对口径（该币自己的分位），
没有绝对下限。这里把命中事件按**绝对爆仓额**分桶，看胜率是不是随金额单调
——如果是，就该加绝对闸；如果不是，那小额命中也一样有效，是我想多了。
"""
import asyncio, statistics, sys
import httpx

H = {"User-Agent": "Mozilla/5.0"}
G = "https://api.gateio.ws/api/v4"
SIDE_TH = 0.80
FWD = {"long": (12, "1h"), "short": (48, "4h")}   # 各自的有效窗口


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
            out.append({"p": float(x["mark_price"]),
                        "l": float(x.get("long_liq_usd") or 0),
                        "s": float(x.get("short_liq_usd") or 0)})
        except (KeyError, TypeError, ValueError):
            continue
    return out


async def main():
    q = float(sys.argv[1]) if len(sys.argv) > 1 else 0.90
    n_coins = int(sys.argv[2]) if len(sys.argv) > 2 else 40
    async with httpx.AsyncClient() as c:
        tk = await j(c, "https://fapi.binance.com/fapi/v1/ticker/24hr")
        rows = sorted([x for x in tk if x["symbol"].endswith("USDT")
                       and float(x["quoteVolume"]) > 5_000_000],
                      key=lambda x: -float(x["quoteVolume"]))[:n_coins]
        syms = [x["symbol"][:-4] for x in rows]
        got = await asyncio.gather(*[bars(c, s) for s in syms])

    cuts, ev = [], []
    for sym, rs in zip(syms, got):
        if len(rs) < 200:
            continue
        liq = sorted(r["l"] + r["s"] for r in rs if r["l"] + r["s"] > 0)
        if len(liq) < 30:
            continue
        cut = liq[int(len(liq) * q)]
        cuts.append((sym, cut))
        for i, r in enumerate(rs):
            tot = r["l"] + r["s"]
            if tot < cut:
                continue
            side = ("short" if r["s"] / tot >= SIDE_TH else
                    "long" if r["l"] / tot >= SIDE_TH else None)
            if not side:
                continue
            n, _lab = FWD[side]
            if i + n >= len(rs):
                continue
            a, b = rs[i]["p"], rs[i + n]["p"]
            if a:
                ev.append((side, tot, (b - a) / a * 100))

    cuts.sort(key=lambda x: x[1])
    print(f"扫 {len(cuts)} 个币　分位 {q*100:.0f}%\n")
    print("各币的门槛（绝对金额）——最低的 8 个和最高的 5 个：")
    for s, v in cuts[:8]:
        print(f"  {s:<10}${v:>12,.0f}")
    print("  ...")
    for s, v in cuts[-5:]:
        print(f"  {s:<10}${v:>12,.0f}")
    med = statistics.median(v for _s, v in cuts)
    lo = sum(1 for _s, v in cuts if v < 20_000)
    print(f"\n门槛中位数 ${med:,.0f}　"
          f"门槛低于 2 万美元的币：{lo}/{len(cuts)} 个")

    print(f"\n命中事件 {len(ev)} 次，按**绝对爆仓额**分桶看胜率：")
    BUCKETS = [(0, 10_000), (10_000, 30_000), (30_000, 100_000),
               (100_000, 500_000), (500_000, 10**12)]
    for side, want in (("long", "多头被爆→抄底(1h) 上涨"),
                       ("short", "空头被爆→摸顶(4h) 上涨")):
        print(f"\n  {want}（越偏离 50% 越有效）")
        for lo_, hi in BUCKETS:
            v = [r for s, t, r in ev if s == side and lo_ <= t < hi]
            if len(v) < 10:
                print(f"    ${lo_:>7,}~{hi if hi < 10**11 else '∞':>9}　"
                      f"样本 {len(v):>4}　太少")
                continue
            up = sum(1 for x in v if x > 0) / len(v) * 100
            print(f"    ${lo_:>7,}~{hi if hi < 10**11 else '∞':>9}　"
                  f"样本 {len(v):>4}　上涨 {up:>5.1f}%　"
                  f"中位 {statistics.median(v):>+7.3f}%")

asyncio.run(main())
