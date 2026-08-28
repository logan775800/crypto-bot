"""量各条「新币源」到底能提前多少、免费能不能打通。
判据只有一个：**从代币创建到我这边能看见，隔多久**。
"""
import time, json, httpx

now = time.time()
H = {"User-Agent": "Mozilla/5.0"}

def show(name, ok, note=""):
    print(f"{'✅' if ok else '❌'} {name:38} {note}")

# 1. DexScreener 最新代币档案（有社交/官网的才会进这个表）
try:
    r = httpx.get("https://api.dexscreener.com/token-profiles/latest/v1",
                  headers=H, timeout=15)
    d = r.json()
    show("DexScreener token-profiles", r.status_code == 200,
         f"{r.status_code} 条数={len(d) if isinstance(d,list) else '?'}")
    if isinstance(d, list) and d:
        x = d[0]
        print("     样本:", x.get("chainId"), str(x.get("description"))[:40],
              "links=", len(x.get("links") or []))
except Exception as e:
    show("DexScreener token-profiles", False, str(e)[:60])

# 2. DexScreener 付费推广（项目方自己掏钱买曝光 = 有预算 = 认真做的）
for ep in ("token-boosts/latest/v1", "token-boosts/top/v1"):
    try:
        r = httpx.get(f"https://api.dexscreener.com/{ep}", headers=H, timeout=15)
        d = r.json()
        show(f"DexScreener {ep}", r.status_code == 200,
             f"{r.status_code} 条数={len(d) if isinstance(d,list) else '?'}")
        if isinstance(d, list) and d:
            print("     样本:", d[0].get("chainId"), "amount=", d[0].get("amount"),
                  "total=", d[0].get("totalAmount"))
    except Exception as e:
        show(f"DexScreener {ep}", False, str(e)[:60])

# 3. four.meme —— BSC 上中文梗币的主战场，比池子早一个阶段（内盘/绑定曲线）
for url in ("https://four.meme/meme-api/v1/private/token/query?orderBy=New&pageIndex=1&pageSize=10&listedPancake=false",
            "https://four.meme/meme-api/v1/private/token/query?orderBy=New&pageIndex=1&pageSize=10"):
    try:
        r = httpx.get(url, headers=H, timeout=15)
        ok = r.status_code == 200
        body = r.json() if ok else None
        n = len((body or {}).get("data") or []) if isinstance(body, dict) else 0
        show("four.meme 新币(内盘)", ok and n > 0, f"{r.status_code} 条数={n}")
        if n:
            t = (body["data"][0])
            print("     样本:", t.get("name"), "|", t.get("shortName"),
                  "| createDate=", t.get("createDate"))
            break
    except Exception as e:
        show("four.meme 新币(内盘)", False, str(e)[:70])

# 4. pump.fun（Solana）
for url in ("https://frontend-api.pump.fun/coins?offset=0&limit=5&sort=created_timestamp&order=DESC",
            "https://frontend-api-v3.pump.fun/coins?offset=0&limit=5&sort=created_timestamp&order=DESC"):
    try:
        r = httpx.get(url, headers=H, timeout=15)
        show(f"pump.fun {url.split('//')[1][:22]}", r.status_code == 200,
             f"{r.status_code} {str(r.text)[:60]}")
    except Exception as e:
        show("pump.fun", False, str(e)[:60])

# 5. GeckoTerminal 新池（现在用的）——量一下滞后
try:
    r = httpx.get("https://api.geckoterminal.com/api/v2/networks/bsc/new_pools",
                  headers=H, timeout=20)
    d = r.json().get("data") or []
    lags = []
    from datetime import datetime, timezone
    for p in d[:20]:
        ts = p["attributes"].get("pool_created_at")
        if ts:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            lags.append((now - dt.timestamp()) / 60)
    lags.sort()
    show("GeckoTerminal new_pools (现用)", True,
         f"{len(d)} 个；最新那个建池到现在 {lags[0]:.1f} 分钟" if lags else "无时间戳")
except Exception as e:
    show("GeckoTerminal new_pools", False, str(e)[:60])
