"""Bybit 全永续「短窗急涨/急跌」告警：默认 15 分钟内涨跌 ≥15% 就推送。

和现有告警的区别（别搞混）：
  • contract_alert.py 看的是 **24h 涨跌幅** 分档（20/30/…%）；
  • market_alert.py 看的是放量/新币；
  • 本模块看的是 **滚动 15 分钟窗口的涨跌幅**——抓的是"刚刚拉起来/砸下去"。

实现：每 60 秒拉一次 Bybit `/v5/market/tickers?category=linear`（一次返回全部
~500 个 U 本位永续），在内存里给每个币存一小段 (时间, 价格) 历史，用
**现价 vs 约 15 分钟前的价** 算滚动涨跌幅。历史只放内存、不写 pickle
（几千个元组，落盘不值当）；重启后 15 分钟内自行重建，期间不误报（没够 15 分钟
历史的币直接跳过）。

流动性过滤：24h 成交额 < MIN_TURNOVER 的币直接不纳入——微盘插针 15% 没意义还刷屏。
去重：同币同方向按「已报档位」记账，回落到阈值-迟滞以下才重新武装；继续同向再冲
高 REALERT_STEP 个点才升级重报。订阅：/watchpump（可带自定义百分比），/unwatchpump。
"""
import time
import logging
import httpx
from telegram import Update
from telegram.ext import ContextTypes
from storage import data, save_data
from handlers.util import escape_md

BYBIT_BASE = "https://api.bybit.com"

WINDOW = 900              # 滚动窗口秒数（15 分钟）
DEFAULT_PCT = 15.0        # 默认告警阈值（%）
MIN_TURNOVER = 3_000_000  # 24h 成交额下限（USDT），滤掉微盘/僵尸合约的插针噪音
HYSTERESIS = 3.0          # 迟滞（百分点）：回落到 阈值-迟滞 以下才重新武装，防边界抖动刷屏
REALERT_STEP = 10.0       # 同向继续冲高多少个点才升级重报（15%→25% 再报一次）
STATE_TTL = WINDOW * 2    # 单币告警记录 30 分钟没更新就作废，允许重新计
HIST_MARGIN = 240         # 历史多留 4 分钟，保证窗口边界两侧都有采样点
MAX_LINES = 40            # 单条消息最多行数，超出分条发
PCT_MIN, PCT_MAX = 3.0, 200.0   # 用户可设阈值范围

log = logging.getLogger(__name__)

# 价格历史：{symbol: [[ts, price], ...]}（按时间升序）。模块级、**不落盘**。
_price_hist = {}


def _rolling_change(hist, now):
    """现价相对约 WINDOW 秒前的涨跌幅（%）。历史不足 WINDOW 则返回 None（不误报）。

    基准取「时间 ≤ now-WINDOW 的最新一条」——即 15 分钟前那一刻的价。
    """
    if not hist:
        return None
    cur = hist[-1][1]
    ref = None
    cutoff = now - WINDOW
    for ts, p in hist:
        if ts <= cutoff:
            ref = p          # 一路推进到"仍≥15分钟前"的最新一条
        else:
            break
    if ref is None or ref <= 0:
        return None          # 还没攒够 15 分钟历史（新上线/刚重启）→ 跳过
    return (cur - ref) / ref * 100.0


async def _fetch_bybit_perps(client=None):
    """拉全部 U 本位永续，返回 [{sym, price, turnover}]（已过滤成交额下限）。

    单交易所、无需和别人共享连接池，默认自建 client（测试可注入 fake client）。
    """
    if client is None:
        async with httpx.AsyncClient(timeout=12) as c:
            return await _fetch_bybit_perps(c)
    r = await client.get(f"{BYBIT_BASE}/v5/market/tickers",
                         params={"category": "linear"})
    r.raise_for_status()
    d = r.json()
    if d.get("retCode") != 0:
        raise RuntimeError(f"Bybit tickers retCode={d.get('retCode')} {d.get('retMsg')}")
    out = []
    for t in d.get("result", {}).get("list", []):
        s = t.get("symbol", "")
        if not s.endswith("USDT"):        # 排除 USDC 永续 / 日期交割合约
            continue
        try:
            price = float(t["lastPrice"])
            turnover = float(t.get("turnover24h", 0) or 0)
        except (ValueError, KeyError, TypeError):
            continue
        if price <= 0 or turnover < MIN_TURNOVER:
            continue
        out.append({"sym": s[:-4], "price": price, "turnover": turnover})
    return out


def _ingest(perps, now):
    """把这一轮价格写进历史并按窗口裁剪；返回本轮在场的币集合。"""
    seen = set()
    for m in perps:
        sym = m["sym"]
        seen.add(sym)
        h = _price_hist.setdefault(sym, [])
        h.append([now, m["price"]])
        # 只保留窗口 + 余量内的采样点
        keep_from = now - WINDOW - HIST_MARGIN
        if h[0][0] < keep_from:
            idx = 0
            for i, (ts, _) in enumerate(h):
                if ts >= keep_from:
                    idx = i
                    break
            else:
                idx = len(h) - 1
            del h[:idx]
    # 淘汰这轮没出现（掉出成交额门槛/下架）且已很久没更新的币，防内存无限涨
    stale = [s for s, h in _price_hist.items()
             if s not in seen and (not h or now - h[-1][0] > WINDOW + HIST_MARGIN)]
    for s in stale:
        _price_hist.pop(s, None)
    return seen


def _compute_changes(seen, now):
    """本轮所有在场币的滚动涨跌幅。返回 {sym: (change, price)}。"""
    changes = {}
    for sym in seen:
        h = _price_hist.get(sym)
        if not h:
            continue
        ch = _rolling_change(h, now)
        if ch is not None:
            changes[sym] = (ch, h[-1][1])
    return changes


def _should_alert(recs, sym, direction, cabs, now):
    """同币同方向去重/升级判定。命中返回 True 并更新记录。

    首次达阈值 → 报；同向继续冲高 ≥REALERT_STEP 个点 → 升级重报；否则不报（续命时间戳）。
    """
    rec = recs.get(sym) or {}
    if rec and now - rec.get("ts", 0) > STATE_TTL:
        rec = {}
    prev = rec.get(direction, 0.0)
    rec["ts"] = now
    if cabs >= prev + (REALERT_STEP if prev else 0.0):
        rec[direction] = cabs
        recs[sym] = rec
        return True
    recs[sym] = rec
    return False


# market_alert 里已有 quiet 时段判断，直接复用，避免两份实现漂移
def _is_quiet(chat_id):
    from handlers.market_alert import is_quiet
    pref = data.get("user_prefs", {}).get(str(chat_id))
    return is_quiet(pref) if pref else False


async def scan_pump(context: ContextTypes.DEFAULT_TYPE):
    """后台任务：拉行情 → 更新历史 → 算滚动涨跌 → 按订阅推送。每 60 秒一次。"""
    watch = data.get("pump_watch") or {}
    if not watch:
        return
    try:
        perps = await _fetch_bybit_perps()
    except Exception as e:
        log.warning(f"急涨急跌扫描取数失败: {e}")
        return
    if not perps:
        return

    now = time.time()
    seen = _ingest(perps, now)
    changes = _compute_changes(seen, now)

    alerted = data.setdefault("pump_alerted", {})   # {chat_id: {sym: {up,down,ts}}}
    dirty = False

    for chat_id, cfg in list(watch.items()):
        pct = float((cfg or {}).get("pct", DEFAULT_PCT))
        recs = alerted.setdefault(str(chat_id), {})
        quiet = _is_quiet(chat_id)

        hits = []
        for sym, (ch, price) in changes.items():
            cabs = abs(ch)
            direction = "up" if ch > 0 else "down"
            if cabs >= pct:
                if _should_alert(recs, sym, direction, cabs, now):
                    dirty = True
                    hits.append({"sym": sym, "change": ch, "price": price,
                                 "direction": direction})
            elif cabs < pct - HYSTERESIS and sym in recs:
                recs.pop(sym, None)      # 回落到迟滞带以下 → 重新武装
                dirty = True

        # 清理本 chat 里过期的记录，避免无限增长
        for s in [s for s, r in recs.items() if now - r.get("ts", 0) > STATE_TTL]:
            recs.pop(s, None)
            dirty = True

        if hits and not quiet:
            await _push(context.bot, chat_id, hits, pct)

    if dirty:
        save_data()


async def _push(bot, chat_id, hits, pct):
    """把一个 chat 的命中推过去；涨在前、跌在后，各按幅度降序。"""
    hits.sort(key=lambda h: (h["direction"] != "up", -abs(h["change"])))
    body = []
    for h in hits:
        if h["direction"] == "up":
            body.append(f"🚀 {escape_md(h['sym'])} 拉升 *{h['change']:+.1f}%*"
                        f"（15m）现 ${h['price']:,.4g}")
        else:
            body.append(f"💥 {escape_md(h['sym'])} 跳水 *{h['change']:+.1f}%*"
                        f"（15m）现 ${h['price']:,.4g}")
    chunks = [body[i:i + MAX_LINES] for i in range(0, len(body), MAX_LINES)]
    for idx, chunk in enumerate(chunks):
        head = (f"⚡ *Bybit 急涨急跌*（15分钟 ≥{pct:g}%）\n" if idx == 0
                else "⚡ *Bybit 急涨急跌*（续）\n")
        text = head + "\n".join(chunk)
        if idx == len(chunks) - 1:
            text += "\n\n⚠️ 短时剧烈波动，追高风险大，不构成投资建议"
        try:
            await bot.send_message(chat_id=int(chat_id), text=text, parse_mode="Markdown")
        except Exception as e:
            log.error(f"急涨急跌推送失败 {chat_id}: {e}")


# ---------- 订阅命令 ----------
async def watch_pump(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/watchpump [百分比] —— 订阅 Bybit 全永续 15 分钟急涨急跌告警。"""
    chat_id = str(update.effective_chat.id)
    data.setdefault("pump_watch", {})
    pct = DEFAULT_PCT
    if context.args:
        try:
            pct = float(str(context.args[0]).replace("%", "").strip())
        except ValueError:
            await update.message.reply_text("百分比要是数字，例：`/watchpump 15`",
                                            parse_mode="Markdown")
            return
        if not PCT_MIN <= pct <= PCT_MAX:
            await update.message.reply_text(f"阈值请设在 {PCT_MIN:g}~{PCT_MAX:g}% 之间")
            return

    existed = chat_id in data["pump_watch"]
    data["pump_watch"][chat_id] = {"pct": pct}
    save_data()
    verb = "已更新" if existed else "已订阅"
    await update.message.reply_text(
        f"⚡ {verb}【Bybit 急涨急跌告警】\n\n"
        f"• 监控 **全部 U 本位永续**（~500 个）\n"
        f"• 触发：任一币 **15 分钟内涨跌 ≥ {pct:g}%**\n"
        f"• 涨🚀 跌💥 都报，基准=现价对比 15 分钟前\n"
        f"• 每 60 秒扫一次；同币同方向不重复刷屏，再冲高 {REALERT_STEP:g} 个点才升级\n"
        f"• 已滤掉 24h 成交额 < {MIN_TURNOVER/1e6:g}00 万 U 的微盘\n\n"
        f"改阈值：再发 `/watchpump 20`｜取消：`/unwatchpump`\n"
        f"（重启后需 ~15 分钟重新攒够历史才开始判涨跌，属正常）",
        parse_mode="Markdown")


async def unwatch_pump(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/unwatchpump —— 取消订阅。"""
    chat_id = str(update.effective_chat.id)
    data.setdefault("pump_watch", {})
    if chat_id in data["pump_watch"]:
        data["pump_watch"].pop(chat_id, None)
        data.get("pump_alerted", {}).pop(chat_id, None)
        save_data()
        await update.message.reply_text("已取消 Bybit 急涨急跌告警")
    else:
        await update.message.reply_text("本群还没订阅急涨急跌告警（用 /watchpump 开启）")
