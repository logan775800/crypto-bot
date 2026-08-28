"""每条链每分钟出多少新池 —— 决定「每 N 分钟翻几页」够不够。
翻页量不够的话，扫描之间出的池子直接看不见，而这是**静默丢数据**：
计数会偏低，告警只是"没响"，没人会发现。
"""
import asyncio, time, httpx
from collections import defaultdict
H = {"User-Agent": "Mozilla/5.0"}
NETS = ["bsc", "solana", "base", "eth"]
seen = defaultdict(set)
t0 = time.time()

async def one(c, net):
    r = await c.get(f"https://api.geckoterminal.com/api/v2/networks/{net}/new_pools",
                    headers=H, timeout=20)
    for p in (r.json().get("data") or []):
        seen[net].add(p["attributes"].get("address") or p.get("id"))

async def main():
    async with httpx.AsyncClient() as c:
        while time.time() - t0 < 240:
            await asyncio.gather(*[one(c, n) for n in NETS], return_exceptions=True)
            await asyncio.sleep(15)
    mins = (time.time() - t0) / 60
    print(f"{mins:.1f} 分钟\n链      累计看到   每分钟   10分钟会出   现在每轮只翻 60 个")
    for n in NETS:
        rate = len(seen[n]) / mins
        print(f"{n:8}{len(seen[n]):6}  {rate:7.1f}  {rate*10:9.0f}   "
              f"{'⚠️ 不够' if rate*10 > 55 else '够'}")
