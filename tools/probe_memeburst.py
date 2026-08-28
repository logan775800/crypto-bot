"""量「同一个梗在多短时间内被抄几次」。
这才是"热度第一时间"的真正定义——热梗的信号不是某一个池子够大，
是**抄袭速度**。抄要花钱建池子，一小时被抄 7 次说明这个梗正在被讨论。
"""
import asyncio, time, re, httpx
from collections import defaultdict

CJK = re.compile(r"[\u4e00-\u9fff]")
H = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
RUN_SEC = 900
TICK = 25

seen = {}                       # addr -> (name, ts)
t0 = time.time()

def base_name(n):
    n = re.split(r"\s*/\s*", n or "")[0]
    return re.sub(r"\s*\d+(\.\d+)?%\s*$", "", n).strip()

async def gt(c, net):
    r = await c.get(f"https://api.geckoterminal.com/api/v2/networks/{net}/new_pools",
                    headers=H, timeout=20)
    for p in (r.json().get("data") or []):
        a = p.get("relationships", {}).get("base_token", {}).get("data", {}).get("id", "")
        addr = a.split("_", 1)[1] if "_" in a else ""
        if addr and addr not in seen:
            seen[addr] = (base_name(p["attributes"].get("name", "")), time.time(), net)

async def main():
    async with httpx.AsyncClient(follow_redirects=True) as c:
        while time.time() - t0 < RUN_SEC:
            await asyncio.gather(gt(c, "bsc"), gt(c, "solana"),
                                 gt(c, "base"), return_exceptions=True)
            await asyncio.sleep(TICK)

    grace = t0 + TICK + 2
    fresh = {a: v for a, v in seen.items() if v[1] >= grace}
    mins = (time.time() - grace) / 60
    by = defaultdict(list)
    for a, (n, ts, net) in fresh.items():
        if n:
            by[n].append(ts)
    cn = {n: v for n, v in by.items() if CJK.search(n)}
    print(f"净观察 {mins:.1f} 分钟，新增 {len(fresh)} 个池子，"
          f"不同名字 {len(by)} 个，中文名 {len(cn)} 个\n")

    print("同名出现 N 次的名字有几个（换算成每小时）：")
    for k in (2, 3, 4, 5, 7, 10):
        allk = [n for n, v in by.items() if len(v) >= k]
        cnk = [n for n, v in cn.items() if len(v) >= k]
        print(f"  ≥{k:2} 次：全部 {len(allk):3} 个 → {len(allk)/mins*60:5.1f} 个/小时"
              f"　｜中文名 {len(cnk):3} 个 → {len(cnk)/mins*60:5.1f} 个/小时")

    print("\n被抄最多的（这就是「正在热」的梗）：")
    for n, v in sorted(by.items(), key=lambda x: -len(x[1]))[:15]:
        span = (max(v) - min(v)) / 60
        flag = "🀄" if CJK.search(n) else "  "
        print(f"  {flag} {n[:26]:28} {len(v):2} 次 / {span:5.1f} 分钟")

asyncio.run(main())
