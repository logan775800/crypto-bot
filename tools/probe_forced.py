# -*- coding: utf-8 -*-
"""持仓掉下去的那部分，有多少是被强平的、多少是自己跑的？

判据：窗口内 |ΔOI(美元)| 是"少了多少仓位"，同期爆仓金额是"其中被强制平掉的"。
两者同口径（都是名义金额），相除就是**强平占比**。
剩下的就是主动平仓——多头自己跑 和 多头被打爆是两种完全不同的行情。

先量分布，好定"算小还是算大"的界，别拍脑袋。
"""
import asyncio, statistics
import httpx

H = {"User-Agent": "Mozilla/5.0"}
G = "https://api.gateio.ws/api/v4"


async def j(c, u, p=None):
    try:
        r = await c.get(u, params=p, headers=H, timeout=25)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


async def rows(c, base):
    d = await j(c, f"{G}/futures/usdt/contract_stats",
                {"contract": f"{base}_USDT", "interval": "1h", "limit": 720})
    out = []
    for x in (d or []):
        try:
            out.append({"oi": float(x["open_interest_usd"]),
                        "liq": float(x.get("long_liq_usd") or 0)
                             + float(x.get("short_liq_usd") or 0),
                        "p": float(x["mark_price"])})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def windows(rs, h):
    """所有长度 h 的窗口里，OI 净减少的那些 → (强平占比, 价格变化, OI变化%)"""
    out = []
    for i in range(0, len(rs) - h, max(1, h // 4)):
        seg = rs[i:i + h]
        a, b = seg[0]["oi"], seg[-1]["oi"]
        if a <= 0 or b >= a:
            continue                      # 只看减仓的窗口
        drop = a - b
        if drop < a * 0.03:
            continue                      # 掉得太少，比值噪声大
        liq = sum(x["liq"] for x in seg)
        pa, pb = seg[0]["p"], seg[-1]["p"]
        out.append((liq / drop * 100, (pb - pa) / pa * 100 if pa else 0,
                    (b - a) / a * 100))
    return out


async def main():
    async with httpx.AsyncClient() as c:
        tk = await j(c, "https://fapi.binance.com/fapi/v1/ticker/24hr")
        syms = [x["symbol"][:-4] for x in sorted(
            [y for y in tk if y["symbol"].endswith("USDT")
             and float(y["quoteVolume"]) > 20_000_000],
            key=lambda y: -float(y["quoteVolume"]))[:35]]
        got = await asyncio.gather(*[rows(c, s) for s in syms])

    for h, lab in ((24, "24 小时"), (6, "6 小时")):
        allw = []
        for rs in got:
            if len(rs) > h + 20:
                allw.extend(windows(rs, h))
        if not allw:
            continue
        share = sorted(x[0] for x in allw)
        q = lambda p: share[min(len(share) - 1, int(len(share) * p))]
        print(f"\n═══ {lab}窗口，OI 净减少的样本 {len(allw)} 个 ═══")
        print(f"强平占比（爆仓额 ÷ 持仓减少额）：")
        for p in (0.10, 0.25, 0.50, 0.75, 0.90, 0.99):
            print(f"  {int(p*100):>2} 分位  {q(p):>7.1f}%")
        print(f"  中位数 {statistics.median(share):.1f}%")
        for lo, hi, name in ((0, 5, "几乎全是主动平仓"), (5, 20, "以主动平仓为主"),
                             (20, 50, "强平占相当一部分"), (50, 1e9, "以被强平为主")):
            n = sum(1 for x in share if lo <= x < hi)
            print(f"  {name:16}（{lo}~{hi if hi < 1e8 else '∞'}%）"
                  f"{n:>5} 个　{n/len(share)*100:>5.1f}%")
        # 强平占比高低，跟价格跌幅有没有关系
        hi_ = [x[1] for x in allw if x[0] >= 20]
        lo_ = [x[1] for x in allw if x[0] < 5]
        if len(hi_) > 10 and len(lo_) > 10:
            print(f"  强平占比≥20% 的窗口：价格中位 {statistics.median(hi_):+.2f}%"
                  f"（{len(hi_)} 个）")
            print(f"  强平占比 <5% 的窗口：价格中位 {statistics.median(lo_):+.2f}%"
                  f"（{len(lo_)} 个）")

asyncio.run(main())
