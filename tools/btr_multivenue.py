# -*- coding: utf-8 -*-
"""BTR 全网持仓量 + 清算图重算（跨交易所）。

## 为什么要跨所

单看一家会得出相反的结论。真机就撞到过：龙虾涨 82% 时币安持仓 +117%
而 Gate 只有 +2.5%——「这波是谁推的」在两家眼里完全不是一回事。

## 合约乘数不一样，不能直接相加

    币安 / Bybit   openInterest 直接就是币数
    Gate           open_interest 是**张**，quanto_multiplier = 10
    KuCoin         openInterest 是**张**，multiplier = 10
    MEXC           holdVol 是**张**，contractSize = 10

不归一化的话 Gate 的 321 万会被当成 321 万个币（实际是 3210 万），
差一个数量级。
"""
import asyncio, sys, time
import httpx

sys.path.insert(0, ".")
H = {"User-Agent": "Mozilla/5.0"}
SYM = "BTR"


async def j(c, u, p=None):
    try:
        r = await c.get(u, params=p, headers=H, timeout=25)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


# ── 现值：五家 ──────────────────────────────────────────────
async def snapshot(c):
    out = {}
    d = await j(c, "https://fapi.binance.com/fapi/v1/openInterest", {"symbol": "BTRUSDT"})
    if d and "openInterest" in d:
        out["币安"] = float(d["openInterest"])
    d = await j(c, "https://api.bybit.com/v5/market/open-interest",
                {"category": "linear", "symbol": "BTRUSDT", "intervalTime": "5min", "limit": 1})
    lst = ((d or {}).get("result") or {}).get("list") or []
    if lst:
        out["Bybit"] = float(lst[0]["openInterest"])
    d = await j(c, "https://api.gateio.ws/api/v4/futures/usdt/contract_stats",
                {"contract": "BTR_USDT", "interval": "5m", "limit": 1})
    if d:
        out["Gate"] = float(d[0]["open_interest"]) * 10
    d = await j(c, "https://api-futures.kucoin.com/api/v1/contracts/BTRUSDTM")
    if d and d.get("data"):
        out["KuCoin"] = float(d["data"]["openInterest"]) * float(d["data"]["multiplier"])
    d = await j(c, "https://contract.mexc.com/api/v1/contract/ticker", {"symbol": "BTR_USDT"})
    if d and d.get("data"):
        out["MEXC"] = float(d["data"]["holdVol"]) * 10
    return out


# ── 历史：只有三家给（KuCoin/MEXC 的历史接口返回空）──────────
async def hist_bn(c, n):
    d = await j(c, "https://fapi.binance.com/futures/data/openInterestHist",
                {"symbol": "BTRUSDT", "period": "1h", "limit": n})
    return {int(x["timestamp"]): float(x["sumOpenInterest"]) for x in (d or [])}


async def hist_by(c, n):
    out = {}
    cur = None
    while len(out) < n:
        p = {"category": "linear", "symbol": "BTRUSDT", "intervalTime": "1h", "limit": 200}
        if cur:
            p["cursor"] = cur
        d = await j(c, "https://api.bybit.com/v5/market/open-interest", p)
        res = (d or {}).get("result") or {}
        lst = res.get("list") or []
        if not lst:
            break
        for x in lst:
            out[int(x["timestamp"])] = float(x["openInterest"])
        cur = res.get("nextPageCursor")
        if not cur:
            break
    return out


async def hist_gate(c, n):
    d = await j(c, "https://api.gateio.ws/api/v4/futures/usdt/contract_stats",
                {"contract": "BTR_USDT", "interval": "1h", "limit": n})
    return {int(x["time"]) * 1000: float(x["open_interest"]) * 10 for x in (d or [])}


async def klines(c, n):
    d = await j(c, "https://fapi.binance.com/fapi/v1/klines",
                {"symbol": "BTRUSDT", "interval": "1h", "limit": n})
    return d or []


def pct(a, b):
    return (b - a) / a * 100 if a else 0.0


async def main():
    hours = 168
    async with httpx.AsyncClient() as c:
        snap, bn, by, gt, kl = await asyncio.gather(
            snapshot(c), hist_bn(c, hours), hist_by(c, hours),
            hist_gate(c, hours), klines(c, hours))
        px = await j(c, "https://fapi.binance.com/fapi/v1/ticker/24hr", {"symbol": "BTRUSDT"})
    last = float(px["lastPrice"]); chg24 = float(px["priceChangePercent"])

    print(f"BTR ${last:.4f}　24h {chg24:+.1f}%\n")
    print("═══ 全网持仓量（已按各家合约乘数归一化到 BTR 币数）═══")
    tot = sum(snap.values())
    for k, v in sorted(snap.items(), key=lambda x: -x[1]):
        print(f"  {k:8}{v/1e6:8.1f}M BTR　${v*last/1e6:7.2f}M　占 {v/tot*100:5.1f}%")
    print(f"  {'合计':8}{tot/1e6:8.1f}M BTR　${tot*last/1e6:7.2f}M")

    print("\n═══ 持仓量变化方向 ═══")
    print("（KuCoin / MEXC 不提供历史 OI 接口，返回空——只有现值，无方向）")
    series = {"币安": bn, "Bybit": by, "Gate": gt}
    windows = [("1 小时", 1), ("4 小时", 4), ("12 小时", 12), ("24 小时", 24)]
    kmap = {int(k[0]): k for k in kl}
    ks = sorted(kmap)

    def price_at(ts):
        c_ = min(ks, key=lambda t: abs(t - ts))
        return float(kmap[c_][4])

    print(f"\n{'窗口':10}" + "".join(f"{v:>12}" for v in series) + f"{'加权合计':>12}{'价格':>10}{'判读':>22}")
    for label, hh in windows:
        row, w_now, w_old = f"{label:10}", 0.0, 0.0
        for v, s in series.items():
            ts = sorted(s)
            if len(ts) < hh + 1:
                row += f"{'—':>12}"; continue
            a, b = s[ts[-1 - hh]], s[ts[-1]]
            row += f"{pct(a,b):>11.1f}%"
            w_now += b; w_old += a
        ts = sorted(bn)
        if len(ts) < hh + 1:
            print(row); continue
        p_old = price_at(ts[-1 - hh]); p_chg = pct(p_old, last)
        oi_chg = pct(w_old, w_now)
        if p_chg > 2 and oi_chg < 1:
            verdict = "涨+仓平 空头回补推的"
        elif p_chg > 2 and oi_chg >= 1:
            verdict = "涨+仓增 新资金进场"
        elif p_chg < -2 and oi_chg > 1:
            verdict = "跌+仓增 新空在推"
        elif p_chg < -2:
            verdict = "跌+仓减 多头认赔离场"
        else:
            verdict = "价格没怎么动"
        print(row + f"{oi_chg:>11.1f}%{p_chg:>9.1f}%   {verdict:>20}")

    # ── 用合成的全网 OI 重建清算图 ──
    from handlers import liqmap
    common = sorted(set(bn) & set(gt))
    rows = []
    for ts in common:
        coins = bn[ts] + gt[ts] + (by.get(ts) or 0)
        k = kmap.get(ts)
        if not k:
            continue
        p = (float(k[2]) + float(k[3]) + float(k[4])) / 3
        rows.append({"timestamp": ts, "sumOpenInterestValue": coins * p})
    cover = "币安+Gate+Bybit" if any(by.get(t) for t in common) else "币安+Gate"
    share = sum(snap.get(x, 0) for x in ("币安", "Gate", "Bybit")) / tot * 100
    print(f"\n═══ 清算图（{cover}，覆盖全网 OI 的 {share:.0f}%，"
          f"{len(rows)} 根小时线）═══")
    m = liqmap.build_map(rows, kl, last, levs=liqmap.TIER_LEVS)
    print(f"窗口内新增持仓合计 ${m['added']/1e6:.1f}M（这是清算簇金额的来源）\n")
    for side, cn in (("short", "上方空头"), ("long", "下方多头")):
        t = liqmap.totals(m, side, last)
        print(f"{cn}：合计 {liqmap._money(t['all'])} U　"
              f"3%内 {liqmap._money(t['d3'])}　5%内 {liqmap._money(t['d5'])}　"
              f"10%内 {liqmap._money(t['d10'])}"
              + (f"　最近一档距现价 {t['near']:.1f}%" if t.get('near') is not None else ""))
        for lev, _w, _c in liqmap.TIER_LEVS:
            cl = liqmap.clusters(m, side, lev)
            if not cl:
                print(f"  {lev:2}x  —— 已清空")
                continue
            s = sum(z["amount"] for z in cl)
            top = max(cl, key=lambda z: z["amount"])
            mid = (top["lo"] + top["hi"]) / 2
            print(f"  {lev:2}x  {liqmap._money(s)} U　最厚一叠 "
                  f"{liqmap._px(top['lo'])}~{liqmap._px(top['hi'])}"
                  f"（{pct(last, mid):+.1f}%）{liqmap._money(top['amount'])} U")
        print()

asyncio.run(main())
