"""同一个代币，各条源谁先看见、早多久。**"第一时间"只能这么量**：
拿单次快照比时间戳会被各家自己的"创建时间"口径骗，只有并发轮询记首见才算数。
"""
import asyncio, time, re, json, httpx
from collections import defaultdict

CJK = re.compile(r"[\u4e00-\u9fff]")
H = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
RUN_SEC = 480
TICK = 20

first = {}                      # (chain, addr) -> {src: ts}
meta = {}                       # (chain, addr) -> name
t0 = time.time()

def mark(src, chain, addr, name=""):
    if not addr:
        return
    k = (chain, str(addr).lower())
    first.setdefault(k, {}).setdefault(src, time.time())
    if name and k not in meta:
        meta[k] = name

async def gt(c, net):
    r = await c.get(f"https://api.geckoterminal.com/api/v2/networks/{net}/new_pools",
                    headers=H, timeout=20)
    for p in (r.json().get("data") or []):
        a = p.get("relationships", {}).get("base_token", {}).get("data", {}).get("id", "")
        addr = a.split("_", 1)[1] if "_" in a else ""
        mark("GeckoTerminal新池", net, addr, p["attributes"].get("name", ""))

async def ds_profiles(c):
    r = await c.get("https://api.dexscreener.com/token-profiles/latest/v1",
                    headers=H, timeout=20)
    for x in r.json() or []:
        mark("DS档案(有社交)", x.get("chainId"), x.get("tokenAddress"),
             (x.get("description") or "")[:24])

async def ds_boosts(c):
    r = await c.get("https://api.dexscreener.com/token-boosts/latest/v1",
                    headers=H, timeout=20)
    for x in r.json() or []:
        mark("DS付费推广", x.get("chainId"), x.get("tokenAddress"),
             (x.get("description") or "")[:24])

async def pumpfun(c):
    r = await c.get("https://frontend-api-v3.pump.fun/coins"
                    "?offset=0&limit=50&sort=created_timestamp&order=DESC",
                    headers=H, timeout=20)
    for x in r.json() or []:
        mark("pump.fun内盘", "solana", x.get("mint"), x.get("name") or "")

async def tick(c):
    await asyncio.gather(gt(c, "bsc"), gt(c, "solana"), ds_profiles(c),
                         ds_boosts(c), pumpfun(c), return_exceptions=True)

async def main():
    async with httpx.AsyncClient(follow_redirects=True) as c:
        while time.time() - t0 < RUN_SEC:
            await tick(c)
            await asyncio.sleep(TICK)

    seen = defaultdict(int)
    for v in first.values():
        for s in v:
            seen[s] += 1
    print(f"跑了 {int(time.time()-t0)} 秒，共见到 {len(first)} 个代币\n")
    print("各源见到多少个（含开跑那一刻的存量）：")
    for s, n in sorted(seen.items(), key=lambda x: -x[1]):
        print(f"  {s:18} {n}")

    # 只看开跑之后才出现的（新增），存量没有先后可言
    grace = t0 + TICK + 2
    lead = defaultdict(list)
    both = 0
    for k, v in first.items():
        if len(v) < 2 or min(v.values()) < grace:
            continue
        both += 1
        w = min(v, key=v.get)
        for s, ts in v.items():
            if s != w:
                lead[(w, s)].append(ts - v[w])
    print(f"\n开跑后新增且被两条以上源看到的：{both} 个")
    for (w, l), gaps in sorted(lead.items(), key=lambda x: -len(x[1])):
        gaps.sort()
        print(f"  {w} 比 {l} 早：{len(gaps)} 次，中位 {gaps[len(gaps)//2]:.0f} 秒")

    cn = [(k, v) for k, v in first.items() if CJK.search(meta.get(k, ""))]
    print(f"\n中文名代币 {len(cn)} 个：")
    for k, v in cn[:15]:
        srcs = ", ".join(f"{s}" for s in sorted(v, key=v.get))
        print(f"  {meta.get(k,'')[:20]:22} {k[0]:8} {srcs}")

asyncio.run(main())
