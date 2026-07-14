"""连续上涨扫描 /upstreak —— 找连续 N 个完整交易日「日线收阳」的永续合约。

支持 OKX / Bybit，默认两个都扫并按币去重（同币在多所都连涨，保留累计涨幅高的那个）。
定义：最近 N 根**已完成**的日线，每根都是阳线(收盘 > 开盘)，跳过当天未走完的日线。
为控请求量：先按 24h 成交额过滤，再取成交额最高的前 MAX_SCAN 个扫（会如实说明扫了多少）。
"""
import asyncio
import logging
import httpx
from telegram import Update
from telegram.ext import ContextTypes

OKX = "https://www.okx.com"
BYBIT = "https://api.bybit.com"
DEFAULT_DAYS = 3
DEFAULT_MIN_TURNOVER_M = 5      # 百万美元：过滤僵尸/微盘合约
MAX_SCAN = 150                  # 每个交易所最多扫多少个（按成交额取前N），防请求过多
CONCURRENCY = 8
TOP_SHOW = 25


# ---------- OKX ----------
async def _okx_universe(client, min_turnover):
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
            if turnover >= min_turnover:
                out.append((iid, iid[:-len("-USDT-SWAP")], turnover))
        except (ValueError, KeyError):
            continue
    out.sort(key=lambda x: -x[2])
    return out


async def _okx_streak(client, sem, iid, base, days):
    async with sem:
        try:
            r = await client.get(f"{OKX}/api/v5/market/candles",
                                  params={"instId": iid, "bar": "1D", "limit": str(days + 1)})
            d = r.json()
            if d.get("code") != "0":
                return None
            return _eval_candles(d.get("data", []), days, base, "OKX",
                                 o_idx=1, c_idx=4, skip_live=True)
        except Exception:
            return None


# ---------- Bybit ----------
async def _bybit_universe(client, min_turnover):
    r = await client.get(f"{BYBIT}/v5/market/tickers", params={"category": "linear"})
    r.raise_for_status()
    d = r.json()
    if d.get("retCode") != 0:
        return []
    out = []
    for t in d.get("result", {}).get("list", []):
        s = t.get("symbol", "")
        if not s.endswith("USDT"):
            continue
        try:
            turnover = float(t.get("turnover24h", 0) or 0)
            if turnover >= min_turnover:
                out.append((s, s[:-4], turnover))
        except (ValueError, KeyError):
            continue
    out.sort(key=lambda x: -x[2])
    return out


async def _bybit_streak(client, sem, symbol, base, days):
    async with sem:
        try:
            r = await client.get(f"{BYBIT}/v5/market/kline",
                                  params={"category": "linear", "symbol": symbol,
                                          "interval": "D", "limit": str(days + 1)})
            d = r.json()
            if d.get("retCode") != 0:
                return None
            return _eval_candles(d.get("result", {}).get("list", []), days, base, "Bybit",
                                 o_idx=1, c_idx=4, skip_live=True)
        except Exception:
            return None


# ---------- 通用：判断最近 days 根完整日线是否都收阳 ----------
def _eval_candles(rows, days, base, ex, o_idx, c_idx, skip_live):
    comp = rows[1:days + 1] if skip_live else rows[:days]   # 新→旧，跳过当天未完成
    if len(comp) < days:
        return None
    legs = []
    for c in comp:
        try:
            o, cl = float(c[o_idx]), float(c[c_idx])
        except (ValueError, IndexError):
            return None
        if cl <= o:
            return None
        legs.append((cl - o) / o * 100)
    first_open, last_close = float(comp[-1][o_idx]), float(comp[0][c_idx])
    cum = (last_close - first_open) / first_open * 100
    return {"sym": base, "ex": ex, "cum": cum, "legs": list(reversed(legs)), "price": last_close}


async def upstreak(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 参数：数字按顺序=天数/成交额；okx|bybit|all 选交易所（默认 all）
    days, min_m, ex = DEFAULT_DAYS, DEFAULT_MIN_TURNOVER_M, "all"
    nums = []
    for a in context.args:
        al = a.lower()
        if al in ("okx", "bybit", "all"):
            ex = al
        else:
            try:
                nums.append(float(a))
            except ValueError:
                pass
    if len(nums) >= 1:
        days = int(nums[0])
    if len(nums) >= 2:
        min_m = nums[1]
    if days < 2 or days > 10:
        await update.message.reply_text("天数取 2~10。用法：/upstreak [天数=3] [成交额百万=5] [okx|bybit|all]")
        return

    min_turnover = min_m * 1_000_000
    ex_label = {"okx": "OKX", "bybit": "Bybit", "all": "OKX+Bybit"}[ex]
    await update.message.reply_text(
        f"⏳ 扫描 {ex_label} 永续中…（连续 {days} 天日线收阳，成交额≥{min_m:g}M，约需十几秒）")

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            sem = asyncio.Semaphore(CONCURRENCY)
            tasks, scanned, total = [], 0, 0
            if ex in ("okx", "all"):
                uni = (await _okx_universe(client, min_turnover))[:MAX_SCAN]
                total += len(uni)  # 近似：过滤后总数（capped 前）
                scanned += len(uni)
                tasks += [_okx_streak(client, sem, iid, base, days) for iid, base, _ in uni]
            if ex in ("bybit", "all"):
                uni = (await _bybit_universe(client, min_turnover))[:MAX_SCAN]
                scanned += len(uni)
                tasks += [_bybit_streak(client, sem, sym, base, days) for sym, base, _ in uni]
            results = await asyncio.gather(*tasks)
    except Exception as e:
        logging.error(f"upstreak 扫描出错: {e}")
        await update.message.reply_text("扫描失败，稍后再试")
        return

    # 按币去重（多所都命中保留累计涨幅高的），再按累计涨幅降序
    best = {}
    for r in results:
        if r and (r["sym"] not in best or r["cum"] > best[r["sym"]]["cum"]):
            best[r["sym"]] = r
    hits = sorted(best.values(), key=lambda r: -r["cum"])

    if not hits:
        await update.message.reply_text(f"没有连续 {days} 天都收阳的合约（扫描 {scanned} 个）")
        return

    show_ex = ex == "all"
    lines = [f"📈 *连续 {days} 天上涨* · {ex_label}永续 · 日线收阳（扫描 {scanned} 个）\n"]
    for r in hits[:TOP_SHOW]:
        legs = "｜".join(f"{x:+.1f}%" for x in r["legs"])
        tag = f"[{r['ex']}] " if show_ex else ""
        lines.append(f"{tag}*{r['sym']}*  累计 {r['cum']:+.1f}%  (每日 {legs})")
    if len(hits) > TOP_SHOW:
        lines.append(f"\n…共 {len(hits)} 个，仅显示累计涨幅前 {TOP_SHOW}")
    lines.append("\n⚠️ 连涨≠必涨，注意追高风险，不构成投资建议")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
