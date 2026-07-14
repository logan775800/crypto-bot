"""连续涨/跌扫描 —— 找连续 N 个完整交易日同向的永续合约（OKX / Bybit）。

/upstreak   连续 N 天日线收阳（涨）
/downstreak 连续 N 天日线收阴（跌，抄底参考）

支持 okx / bybit / all（默认 all，按币去重）。定义：最近 N 根**已完成**日线全部同向，
跳过当天未走完的日线。控请求量：先按 24h 成交额过滤，再取成交额最高的前 MAX_SCAN 个扫。
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
MAX_SCAN = 150                  # 每个交易所最多扫多少个（按成交额取前N）
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


async def _okx_streak(client, sem, iid, base, days, direction):
    async with sem:
        try:
            r = await client.get(f"{OKX}/api/v5/market/candles",
                                  params={"instId": iid, "bar": "1D", "limit": str(days + 1)})
            d = r.json()
            if d.get("code") != "0":
                return None
            return _eval_candles(d.get("data", []), days, base, "OKX", 1, 4, True, direction)
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


async def _bybit_streak(client, sem, symbol, base, days, direction):
    async with sem:
        try:
            r = await client.get(f"{BYBIT}/v5/market/kline",
                                  params={"category": "linear", "symbol": symbol,
                                          "interval": "D", "limit": str(days + 1)})
            d = r.json()
            if d.get("retCode") != 0:
                return None
            return _eval_candles(d.get("result", {}).get("list", []), days, base, "Bybit", 1, 4, True, direction)
        except Exception:
            return None


# ---------- 通用：最近 days 根完整日线是否都同向 ----------
def _eval_candles(rows, days, base, ex, o_idx, c_idx, skip_live, direction):
    comp = rows[1:days + 1] if skip_live else rows[:days]   # 新→旧，跳过当天未完成
    if len(comp) < days:
        return None
    legs = []
    for c in comp:
        try:
            o, cl = float(c[o_idx]), float(c[c_idx])
        except (ValueError, IndexError):
            return None
        if direction == "up" and cl <= o:      # 有一天不是阳线 → 淘汰
            return None
        if direction == "down" and cl >= o:     # 有一天不是阴线 → 淘汰
            return None
        legs.append((cl - o) / o * 100)
    first_open, last_close = float(comp[-1][o_idx]), float(comp[0][c_idx])
    cum = (last_close - first_open) / first_open * 100
    return {"sym": base, "ex": ex, "cum": cum, "legs": list(reversed(legs)), "price": last_close}


# ---------- 扫描 + 组装文本（命令与菜单按钮共用）----------
async def build_streak_text(direction, ex="all", days=DEFAULT_DAYS, min_m=DEFAULT_MIN_TURNOVER_M):
    """执行扫描并返回结果 Markdown 文本。网络异常向上抛。"""
    word = "上涨" if direction == "up" else "下跌"
    kline = "收阳" if direction == "up" else "收阴"
    emoji = "📈" if direction == "up" else "📉"
    min_turnover = min_m * 1_000_000
    ex_label = {"okx": "OKX", "bybit": "Bybit", "all": "OKX+Bybit"}[ex]

    async with httpx.AsyncClient(timeout=15) as client:
        sem = asyncio.Semaphore(CONCURRENCY)
        tasks, scanned = [], 0
        if ex in ("okx", "all"):
            uni = (await _okx_universe(client, min_turnover))[:MAX_SCAN]
            scanned += len(uni)
            tasks += [_okx_streak(client, sem, iid, base, days, direction) for iid, base, _ in uni]
        if ex in ("bybit", "all"):
            uni = (await _bybit_universe(client, min_turnover))[:MAX_SCAN]
            scanned += len(uni)
            tasks += [_bybit_streak(client, sem, sym, base, days, direction) for sym, base, _ in uni]
        results = await asyncio.gather(*tasks)

    # 按币去重：涨保留累计最高、跌保留累计最低（跌得最狠）
    best = {}
    for r in results:
        if not r:
            continue
        cur = best.get(r["sym"])
        if cur is None or (r["cum"] > cur["cum"] if direction == "up" else r["cum"] < cur["cum"]):
            best[r["sym"]] = r
    hits = sorted(best.values(), key=lambda r: r["cum"], reverse=(direction == "up"))

    if not hits:
        return f"没有连续 {days} 天都{kline}的合约（扫描 {scanned} 个）"

    show_ex = ex == "all"
    lines = [f"{emoji} *连续 {days} 天{word}* · {ex_label}永续 · 日线{kline}（扫描 {scanned} 个）\n"]
    for r in hits[:TOP_SHOW]:
        legs = "｜".join(f"{x:+.1f}%" for x in r["legs"])
        tag = f"[{r['ex']}] " if show_ex else ""
        lines.append(f"{tag}*{r['sym']}*  累计 {r['cum']:+.1f}%  (每日 {legs})")
    if len(hits) > TOP_SHOW:
        lines.append(f"\n…共 {len(hits)} 个，仅显示前 {TOP_SHOW}")
    tail = "连涨≠必涨，注意追高" if direction == "up" else "连跌≠见底，别接飞刀"
    lines.append(f"\n⚠️ {tail}，不构成投资建议")
    return "\n".join(lines)


def _parse_args(args):
    days, min_m, ex = DEFAULT_DAYS, DEFAULT_MIN_TURNOVER_M, "all"
    nums = []
    for a in args:
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
    return days, min_m, ex


# ---------- 命令入口 ----------
async def upstreak(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _run_scan(update, context, "up")


async def downstreak(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _run_scan(update, context, "down")


async def _run_scan(update, context, direction):
    days, min_m, ex = _parse_args(context.args)
    cmd = "upstreak" if direction == "up" else "downstreak"
    if days < 2 or days > 10:
        await update.message.reply_text(f"天数取 2~10。用法：/{cmd} [天数=3] [成交额百万=5] [okx|bybit|all]")
        return
    kline = "收阳" if direction == "up" else "收阴"
    ex_label = {"okx": "OKX", "bybit": "Bybit", "all": "OKX+Bybit"}[ex]
    await update.message.reply_text(
        f"⏳ 扫描 {ex_label} 永续中…（连续 {days} 天日线{kline}，成交额≥{min_m:g}M，约需十几秒）")
    try:
        txt = await build_streak_text(direction, ex, days, min_m)
    except Exception as e:
        logging.error(f"{cmd} 扫描出错: {e}")
        await update.message.reply_text("扫描失败，稍后再试")
        return
    await update.message.reply_text(txt, parse_mode="Markdown")
