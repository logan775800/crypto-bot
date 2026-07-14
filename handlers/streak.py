"""连续上涨扫描 /upstreak —— 找连续 N 个完整交易日「日线收阳」的永续合约(OKX)。

定义：最近 N 根**已完成**的日线，每根都是阳线(收盘 > 开盘)。跳过当天未走完的日线。
为控制请求量：先按 24h 成交额过滤，再取成交额最高的前 MAX_SCAN 个来扫（会如实说明扫了多少）。
"""
import asyncio
import logging
import httpx
from telegram import Update
from telegram.ext import ContextTypes

OKX = "https://www.okx.com"
DEFAULT_DAYS = 3
DEFAULT_MIN_TURNOVER_M = 5      # 百万美元：过滤僵尸/微盘合约
MAX_SCAN = 150                  # 最多扫多少个（按成交额取前N），防止请求过多
CONCURRENCY = 8
TOP_SHOW = 25


async def _okx_swaps(client, min_turnover):
    """取 OKX USDT 永续，按 24h 成交额过滤并降序。返回 [(instId, turnover)]。"""
    r = await client.get(f"{OKX}/api/v5/market/tickers", params={"instType": "SWAP"})
    r.raise_for_status()
    d = r.json()
    out = []
    for t in d.get("data", []):
        iid = t.get("instId", "")
        if not iid.endswith("-USDT-SWAP"):
            continue
        try:
            last = float(t["last"])
            turnover = float(t.get("volCcy24h", 0) or 0) * last
            if turnover < min_turnover:
                continue
            out.append((iid, turnover))
        except (ValueError, KeyError):
            continue
    out.sort(key=lambda x: -x[1])
    return out


async def _check(client, sem, iid, days):
    """查该合约最近 days 根完整日线是否都收阳；命中返回详情，否则 None。"""
    async with sem:
        try:
            r = await client.get(f"{OKX}/api/v5/market/candles",
                                  params={"instId": iid, "bar": "1D", "limit": str(days + 1)})
            d = r.json()
            if d.get("code") != "0":
                return None
            rows = d.get("data", [])          # 新→旧，每行 [ts,o,h,l,c,...]
            comp = rows[1:days + 1]           # 跳过 index0(当天未完成)，取随后 days 根完整日线
            if len(comp) < days:
                return None
            legs = []
            for c in comp:
                o, cl = float(c[1]), float(c[4])
                if cl <= o:                   # 有一天不是阳线 → 淘汰
                    return None
                legs.append((cl - o) / o * 100)
            first_open, last_close = float(comp[-1][1]), float(comp[0][4])
            cum = (last_close - first_open) / first_open * 100
            return {"sym": iid[:-len("-USDT-SWAP")], "cum": cum,
                    "legs": list(reversed(legs)), "price": last_close}
        except Exception:
            return None


async def upstreak(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    days, min_m = DEFAULT_DAYS, DEFAULT_MIN_TURNOVER_M
    try:
        if len(args) >= 1:
            days = int(args[0])
        if len(args) >= 2:
            min_m = float(args[1])
    except ValueError:
        await update.message.reply_text("用法：/upstreak [天数=3] [最少成交额百万=5]\n例：/upstreak 3")
        return
    if days < 2 or days > 10:
        await update.message.reply_text("天数取 2~10")
        return

    min_turnover = min_m * 1_000_000
    await update.message.reply_text(
        f"⏳ 扫描 OKX 永续中…（连续 {days} 天日线收阳，成交额≥{min_m:g}M，约需十几秒）")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            swaps = await _okx_swaps(client, min_turnover)
            capped = swaps[:MAX_SCAN]
            sem = asyncio.Semaphore(CONCURRENCY)
            results = await asyncio.gather(*[_check(client, sem, iid, days) for iid, _ in capped])
    except Exception as e:
        logging.error(f"upstreak 扫描出错: {e}")
        await update.message.reply_text("扫描失败，稍后再试")
        return

    hits = sorted((r for r in results if r), key=lambda r: -r["cum"])
    note = (f"（扫描成交额前 {len(capped)}/{len(swaps)} 个）"
            if len(swaps) > len(capped) else f"（共扫描 {len(capped)} 个）")
    if not hits:
        await update.message.reply_text(f"没有连续 {days} 天都收阳的合约 {note}")
        return

    lines = [f"📈 *连续 {days} 天上涨* · OKX永续 · 日线收阳 {note}\n"]
    for r in hits[:TOP_SHOW]:
        legs = "｜".join(f"{x:+.1f}%" for x in r["legs"])
        lines.append(f"*{r['sym']}*  累计 {r['cum']:+.1f}%  (每日 {legs})")
    if len(hits) > TOP_SHOW:
        lines.append(f"\n…共 {len(hits)} 个，仅显示累计涨幅前 {TOP_SHOW}")
    lines.append("\n⚠️ 连涨≠必涨，注意追高风险，不构成投资建议")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
