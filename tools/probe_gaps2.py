"""上一轮四个候选没区分度。三个可能是我口径定错了，重验：
  ① 大单占比 —— 绝对 5 万美元的门槛对小市值币毫无意义（一笔都没有），换成相对口径
  ② 跨所持仓分歧 —— 哪家在加仓。三家人群不同，分歧本身可能才是信号
  ③ 爆仓量的历史分位 —— 「这段爆了 $11k」算多还是少？只有跟这个币自己的历史比才知道
"""
import asyncio, statistics, httpx
H={"User-Agent":"Mozilla/5.0"}; BN="https://fapi.binance.com"
BY="https://api.bybit.com"; G="https://api.gateio.ws/api/v4"

async def j(c,u,p=None):
    try:
        r=await c.get(u,params=p,headers=H,timeout=20)
        return r.json() if r.status_code==200 else None
    except Exception: return None

async def one(c,sym):
    base=sym[:-4]; out={"sym":base}
    # ① 大单：相对口径 —— 单笔金额 > 这一批成交中位数的 20 倍
    ag=await j(c,f"{BN}/fapi/v1/aggTrades",{"symbol":sym,"limit":1000})
    if ag and len(ag)>100:
        v=[float(x["p"])*float(x["q"]) for x in ag]
        med=statistics.median(v); tot=sum(v)
        if med>0 and tot>0:
            out["big_rel"]=sum(x for x in v if x>med*20)/tot*100
    # ② 跨所持仓分歧：三家 1h 变化率的极差
    ch={}
    b=await j(c,f"{BN}/futures/data/openInterestHist",{"symbol":sym,"period":"1h","limit":13})
    if b and len(b)>2:
        a0,a1=float(b[0]["sumOpenInterestValue"]),float(b[-1]["sumOpenInterestValue"])
        if a0>0: ch["bn"]=(a1-a0)/a0*100
    y=await j(c,f"{BY}/v5/market/open-interest",
              {"category":"linear","symbol":sym,"intervalTime":"1h","limit":13})
    lst=((y or {}).get("result") or {}).get("list") or []
    if len(lst)>2:
        a1,a0=float(lst[0]["openInterest"]),float(lst[-1]["openInterest"])
        if a0>0: ch["by"]=(a1-a0)/a0*100
    g=await j(c,f"{G}/futures/usdt/contract_stats",
              {"contract":f"{base}_USDT","interval":"1h","limit":13})
    if g and len(g)>2:
        a0,a1=float(g[0]["open_interest"]),float(g[-1]["open_interest"])
        if a0>0: ch["gate"]=(a1-a0)/a0*100
    if len(ch)>=2:
        out["venue_spread"]=max(ch.values())-min(ch.values())
        out["venues"]=ch
    # ③ 爆仓量分位：最近 12h 的每小时爆仓，在过去 30 天里排第几
    g30=await j(c,f"{G}/futures/usdt/contract_stats",
                {"contract":f"{base}_USDT","interval":"1h","limit":720})
    if g30 and len(g30)>200:
        liq=[float(x.get("long_liq_usd") or 0)+float(x.get("short_liq_usd") or 0)
             for x in g30]
        recent=statistics.mean(liq[-12:]); hist=sorted(liq[:-12])
        if hist:
            rank=sum(1 for v in hist if v<recent)/len(hist)*100
            out["liq_pct"]=rank
    return out

async def main():
    async with httpx.AsyncClient() as c:
        tk=await j(c,f"{BN}/fapi/v1/ticker/24hr")
        rows=[t for t in tk if t["symbol"].endswith("USDT")
              and float(t["quoteVolume"])>20_000_000]
        movers=sorted(rows,key=lambda t:-abs(float(t["priceChangePercent"])))[:18]
        base=sorted(rows,key=lambda t:-float(t["quoteVolume"]))[:25]
        got={}
        for name,grp in (("异动",movers),("基线",base)):
            got[name]=[g for g in await asyncio.gather(
                *[one(c,t["symbol"]) for t in grp]) if g]
    for k,label in [("big_rel","大单占比(相对口径)"),("venue_spread","跨所持仓分歧(极差%)"),
                    ("liq_pct","爆仓量30天分位")]:
        a=[g[k] for g in got["异动"] if g.get(k) is not None]
        b=[g[k] for g in got["基线"] if g.get(k) is not None]
        if len(a)<4 or len(b)<4:
            print(f"{label:24} 样本不足 异动{len(a)}/基线{len(b)}"); continue
        ma,mb=statistics.median(a),statistics.median(b)
        print(f"{label:24}异动 {ma:7.1f}　基线 {mb:7.1f}　能分开 {(ma/mb if mb else 0):.2f}x")
    print("\n跨所分歧明细（哪家在加仓）：")
    chg={t["symbol"][:-4]:float(t["priceChangePercent"]) for t in movers}
    for g in sorted([x for x in got["异动"] if x.get("venues")],
                    key=lambda g:-(g.get("venue_spread") or 0))[:8]:
        v=g["venues"]
        print(f"  {g['sym']:<10}{chg.get(g['sym'],0):+7.1f}%  " +
              "  ".join(f"{k}{val:+7.1f}%" for k,val in v.items()) +
              f"　极差 {g['venue_spread']:.0f}pt　爆仓分位 {g.get('liq_pct',-1):.0f}%")
asyncio.run(main())
