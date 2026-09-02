# -*- coding: utf-8 -*-
"""相对分位 + 绝对下限 两道闸一起用，各组合的胜率和频率。

只有相对分位是不够的：实测 32 个币里 27 个的 90 分位门槛低于 2 万美元，
而 $1 万以下那一档抄底胜率 49.5%（基线 49.6%）——**零边际**。
所以档位要同时管两个数，这里把组合逐个量出来，好把卡片上印的数字换掉。
"""
import asyncio, statistics
import httpx

H = {"User-Agent": "Mozilla/5.0"}
G = "https://api.gateio.ws/api/v4"
SIDE_TH = 0.80
FWD = {"long": 12, "short": 48}          # 抄底看 1h、摸顶看 4h
COMBOS = [("宽", 0.90, 10_000), ("标准", 0.95, 30_000), ("严", 0.98, 100_000)]
BASE = {"long": 49.6, "short": 52.2}


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
    async with httpx.AsyncClient() as c:
        tk = await j(c, "https://fapi.binance.com/fapi/v1/ticker/24hr")
        rows = sorted([x for x in tk if x["symbol"].endswith("USDT")
                       and float(x["quoteVolume"]) > 5_000_000],
                      key=lambda x: -float(x["quoteVolume"]))[:40]
        got = await asyncio.gather(*[bars(c, x["symbol"][:-4]) for x in rows])
    data = [(x["symbol"][:-4], r) for x, r in zip(rows, got) if len(r) >= 200]
    days = 2000 * 5 / 60 / 24

    print(f"{len(data)} 个币 · {days:.1f} 天\n")
    print(f"{'档':4}{'分位':>6}{'绝对下限':>10}"
          f"{'抄底样本':>9}{'1h涨':>8}{'中位':>9}"
          f"{'摸顶样本':>9}{'4h涨':>8}{'中位':>9}{'每天条数':>9}")
    for name, q, floor in COMBOS:
        ev = {"long": [], "short": []}
        for _sym, rs in data:
            liq = sorted(r["l"] + r["s"] for r in rs if r["l"] + r["s"] > 0)
            if len(liq) < 30:
                continue
            cut = max(liq[int(len(liq) * q)], floor)     # 两道闸取严的
            for i, r in enumerate(rs):
                tot = r["l"] + r["s"]
                if tot < cut:
                    continue
                side = ("short" if r["s"] / tot >= SIDE_TH else
                        "long" if r["l"] / tot >= SIDE_TH else None)
                if not side:
                    continue
                n = FWD[side]
                if i + n >= len(rs):
                    continue
                a, b = rs[i]["p"], rs[i + n]["p"]
                if a:
                    ev[side].append((b - a) / a * 100)
        cells = []
        for side in ("long", "short"):
            v = ev[side]
            if len(v) < 15:
                cells.append((len(v), None, None))
            else:
                cells.append((len(v),
                              sum(1 for x in v if x > 0) / len(v) * 100,
                              statistics.median(v)))
        per_day = (len(ev["long"]) + len(ev["short"])) / days
        row = f"{name:4}{q*100:>5.0f}%{floor:>10,}"
        for n_, up, med in cells:
            row += (f"{n_:>9}" +
                    (f"{up:>7.1f}%{med:>+8.2f}%" if up is not None
                     else f"{'样本少':>8}{'':>9}"))
        print(row + f"{per_day:>8.1f}")
    print(f"\n（基线：抄底 {BASE['long']}% / 摸顶 {BASE['short']}%）")

asyncio.run(main())
