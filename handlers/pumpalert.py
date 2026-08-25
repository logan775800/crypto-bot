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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from storage import data, save_data
from handlers.util import escape_md, safe_edit

BYBIT_BASE = "https://api.bybit.com"

WINDOW = 900              # 滚动窗口秒数（15 分钟）
DEFAULT_PCT = 15.0        # 默认告警阈值（%）
MIN_TURNOVER = 1_000_000  # 24h 成交额下限（USDT），滤掉纯僵尸盘。放低到100万，
                          # 因为真正15分钟暴涨15%的常是中小盘，卡太高会漏掉正主
HYSTERESIS = 3.0          # 迟滞（百分点）：回落到 阈值-迟滞 以下才重新武装，防边界抖动刷屏
REALERT_STEP = 10.0       # 同向继续冲高多少个点才升级重报（15%→25% 再报一次）
STATE_TTL = WINDOW * 2    # 单币告警记录 30 分钟没更新就作废，允许重新计
HIST_MARGIN = 240         # 历史多留 4 分钟，保证窗口边界两侧都有采样点
MAX_LINES = 40            # 单条消息最多行数，超出分条发
PCT_MIN, PCT_MAX = 3.0, 200.0   # 用户可设阈值范围
PRESETS = [3, 5, 8, 10, 15, 20]   # 面板上的阈值快捷档

log = logging.getLogger(__name__)

# 价格历史：{symbol: [[ts, price], ...]}（按时间升序）。模块级、**不落盘**。
# 每次进程重启（部署）都会清空，需重新攒 15 分钟——这是「刚部署完没告警」的常见原因。
_price_hist = {}
_started_at = None       # 本次进程首次扫描的时间戳，用于在 /pumptop 显示已运行多久


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


def _change_and_span(hist, now):
    """给榜单用：算涨跌幅 + 实际参照跨度秒数。

    和 _rolling_change 的区别：历史不足 15 分钟时**不返回 None**，改用手头最老的
    采样点算，并把真实跨度一并返回——这样刚启动/热身期也能立刻给出反馈，
    让用户看到「监控在跑，只是还没到 15 分钟」。
    """
    if not hist or len(hist) < 2:
        return None
    cur = hist[-1][1]
    cutoff = now - WINDOW
    ref_ts = ref_p = None
    for ts, p in hist:
        if ts <= cutoff:
            ref_ts, ref_p = ts, p     # 有满 15 分钟的点就优先用
    if ref_p is None:
        ref_ts, ref_p = hist[0]       # 否则用手头最老的
    if ref_p <= 0:
        return None
    return (cur - ref_p) / ref_p * 100.0, now - ref_ts


BN_FAPI = "https://fapi.binance.com"


async def _bybit_perps(client):
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


async def _binance_perps(client):
    r = await client.get(f"{BN_FAPI}/fapi/v1/ticker/24hr")
    r.raise_for_status()
    out = []
    for t in r.json() or []:
        s = t.get("symbol", "")
        # "_" 是交割合约（BTCUSDT_240329），不是永续
        if not s.endswith("USDT") or "_" in s:
            continue
        try:
            price = float(t["lastPrice"])
            turnover = float(t.get("quoteVolume", 0) or 0)
        except (ValueError, KeyError, TypeError):
            continue
        if price <= 0 or turnover < MIN_TURNOVER:
            continue
        out.append({"sym": s[:-4], "price": price, "turnover": turnover})
    return out


async def _fetch_bybit_perps(client=None):
    """拉全部 U 本位永续，返回 [{sym, price, turnover}]（已过滤成交额下限）。

    名字沿用旧的（很多地方引用了），实际是**币安 + Bybit 取并集**：
    实测两家各有独占——只有币安有且成交额≥500万的 19 个（PROM/PUMP/FET…），
    只有 Bybit 有的 6 个（MNT/AGI/CASHCAT…）。只扫一家等于固定看漏一批。

    同一个币两家都有时留**成交额大**的那家，理由和涨跌榜一样：
    那是流动性更好、更有代表性的报价。

    **一家挂了不影响另一家**：两家都取不到才抛异常。急涨急跌是每 60 秒的
    实时告警，不能因为一家抽风就整条哑掉。
    """
    if client is None:
        async with httpx.AsyncClient(timeout=12) as c:
            return await _fetch_bybit_perps(c)
    got, errs, ok = {}, [], 0
    for name, fn in (("币安", _binance_perps), ("Bybit", _bybit_perps)):
        try:
            for m in await fn(client):
                cur = got.get(m["sym"])
                if cur is None or m["turnover"] > cur["turnover"]:
                    got[m["sym"]] = m
            ok += 1
        except Exception as e:
            errs.append(f"{name}: {str(e)[:60]}")
    # 只有**两家都出错**才算取数失败。
    # 按 `not got` 判是错的：那会把「取到了，但没有一个币过成交额门槛」
    # 也当成故障报出去——"没有合格的币"和"取不到数据"是两回事，
    # 混在一起的话，日志里的错误就再也不能信了
    if not ok:
        raise RuntimeError("；".join(errs) or "两家都没取到永续行情")
    if errs:
        log.warning(f"急涨急跌取数部分失败（另一家仍在用）：{'；'.join(errs)}")
    return list(got.values())


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
    # 极端拉升告警（pump3）复用这一轮的行情和滚动历史——它只多一个"3日+量比"的闸。
    # ⚠️ 早退条件必须把它算进来：只看 pump_watch 的话，**只订了 pump3 的人
    # 这个任务一次都不会跑**，告警永远不触发而且日志干净（同 v1.35.0 那个坑）。
    p3 = data.get("pump3") or {}
    if not watch and not p3:
        return
    try:
        perps = await _fetch_bybit_perps()
    except Exception as e:
        log.warning(f"急涨急跌扫描取数失败: {e}")
        return
    if not perps:
        return

    global _started_at
    now = time.time()
    if _started_at is None:
        _started_at = now
    seen = _ingest(perps, now)
    changes = _compute_changes(seen, now)

    # 极端拉升：15m 涨幅在这里是白拿的，它只对过闸的那几个再去拉日线和量
    try:
        from handlers import pump3
        await pump3.check(context, changes)
    except Exception as e:
        log.error(f"极端拉升检查出错: {e}")

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


# ---------- 按钮面板（不用记命令）----------
def _panel(chat_id):
    """返回 (文本, 键盘)。展示订阅状态 + 阈值快捷档 + 涨跌榜/开关按钮。"""
    sub = (data.get("pump_watch") or {}).get(str(chat_id))
    if sub:
        pct = float(sub.get("pct", DEFAULT_PCT))
        status = f"✅ *已订阅*　告警线：涨跌 ≥ *{pct:g}%*"
    else:
        pct = None
        status = "⬜️ *未订阅*　点下面任一阈值即可开启"

    text = (
        "⚡ *Bybit 急涨急跌监控*\n"
        "━━━━━━━━━━━━━━\n"
        f"{status}\n\n"
        "• 监控全部 U 本位永续（~500个）\n"
        "• 15 分钟内涨跌到阈值就推送，涨🚀跌💥都报\n"
        f"• 已滤掉 24h 成交额 < {MIN_TURNOVER/1e4:g}万U 的僵尸盘\n"
        "━━━━━━━━━━━━━━\n"
        "选阈值（越小越灵敏、告警越多）："
    )

    # 阈值档按钮：当前档打勾
    row = []
    rows = []
    for p in PRESETS:
        mark = "✅" if (pct is not None and abs(pct - p) < 0.01) else ""
        row.append(InlineKeyboardButton(f"{mark}{p}%", callback_data=f"pump:set:{p}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    rows.append([InlineKeyboardButton("📊 看涨跌榜", callback_data="pump:top"),
                 InlineKeyboardButton("🔄 刷新", callback_data="pump:panel")])
    if sub:
        rows.append([InlineKeyboardButton("🔕 取消订阅", callback_data="pump:off")])
    return text, InlineKeyboardMarkup(rows)


async def pump_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/pump —— 打开按钮面板（订阅/调阈值/看榜，全点按钮）。"""
    text, kb = _panel(update.effective_chat.id)
    await update.message.reply_text(text, reply_markup=kb, parse_mode="Markdown")


def _leaderboard_text(chat_id):
    """涨跌榜文本（面板里点「看涨跌榜」用，复用 pumptop 的算法）。"""
    watch = data.get("pump_watch") or {}
    sub = watch.get(str(chat_id))
    pct = float(sub.get("pct", DEFAULT_PCT)) if sub else DEFAULT_PCT
    if not _price_hist:
        if not watch:
            return "📭 还没人订阅，后台监控没在跑。先在面板点个阈值开启，等约15分钟再看。"
        return "⏳ 监控刚启动，还没拉到第一轮行情，几十秒后再看。"
    now = time.time()
    ranked = []
    for sym, h in _price_hist.items():
        r = _change_and_span(h, now)
        if r is not None:
            ranked.append((sym, r[0], r[1], h[-1][1]))
    if not ranked:
        return "⏳ 历史还在攒（不足2个采样点），稍等。"
    max_span = max(r[2] for r in ranked)
    ups = sorted([r for r in ranked if r[1] > 0], key=lambda x: -x[1])[:6]
    downs = sorted([r for r in ranked if r[1] < 0], key=lambda x: x[1])[:6]
    lines = [f"📊 *15m 滚动涨跌榜*（监控 {len(ranked)} 个）"]
    if max_span < WINDOW - 60:
        lines.append(f"⏳ 热身中：当前最长仅 ~{max_span/60:.0f} 分钟")

    def fmt(r):
        sym, ch, span, price = r
        tag = "" if span >= WINDOW - 60 else f" [{span/60:.0f}m]"
        hit = " 🔔" if abs(ch) >= pct else ""
        return f"  {escape_md(sym)} {ch:+.1f}%{tag}（${price:,.4g}）{hit}"

    lines.append("\n🚀 *涨幅前列*")
    lines += [fmt(r) for r in ups] or ["  （暂无）"]
    lines.append("\n💥 *跌幅前列*")
    lines += [fmt(r) for r in downs] or ["  （暂无）"]
    top = max((abs(r[1]) for r in ranked), default=0)
    if top < pct:
        lines.append(f"\n此刻最大波动 {top:.1f}%，还没到 {pct:g}% 线——没推送是正常的")
    return "\n".join(lines)


async def on_button(query, context):
    """处理 pump: 开头的回调。由 menu.button_handler 转发进来。"""
    d = query.data
    chat_id = query.message.chat.id

    if d == "pump:panel":
        text, kb = _panel(chat_id)
        await safe_edit(query, text, reply_markup=kb, parse_mode="Markdown")
        return

    if d == "pump:off":
        cid = str(chat_id)
        (data.get("pump_watch") or {}).pop(cid, None)
        (data.get("pump_alerted") or {}).pop(cid, None)
        save_data()
        await query.answer("已取消订阅")
        text, kb = _panel(chat_id)
        await safe_edit(query, text, reply_markup=kb, parse_mode="Markdown")
        return

    if d == "pump:top":
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("⬅️ 返回", callback_data="pump:panel"),
            InlineKeyboardButton("🔄 刷新榜", callback_data="pump:top")]])
        await safe_edit(query, _leaderboard_text(chat_id), reply_markup=kb,
                        parse_mode="Markdown")
        return

    if d.startswith("pump:set:"):
        try:
            pct = float(d.split(":")[2])
        except (ValueError, IndexError):
            await query.answer("参数错误")
            return
        data.setdefault("pump_watch", {})[str(chat_id)] = {"pct": pct}
        save_data()
        await query.answer(f"已设为 ≥{pct:g}%，开始监控")
        text, kb = _panel(chat_id)
        await safe_edit(query, text, reply_markup=kb, parse_mode="Markdown")
        return


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
        f"• 已滤掉 24h 成交额 < {MIN_TURNOVER/1e4:g} 万 U 的僵尸盘\n\n"
        f"改阈值：再发 `/watchpump 20`｜取消：`/unwatchpump`\n"
        f"（重启后需 ~15 分钟重新攒够历史才开始判涨跌，属正常）",
        parse_mode="Markdown")


async def pump_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/pumptop —— 当前 15 分钟滚动涨跌榜，用来确认监控在跑、看离阈值多远。

    没告警时最常见的原因就是「真没币动到那么多」——这条命令让你直接看到
    此刻最猛的币涨跌多少，一目了然。
    """
    chat_id = str(update.effective_chat.id)
    watch = data.get("pump_watch") or {}
    sub = watch.get(chat_id)
    pct = float(sub.get("pct", DEFAULT_PCT)) if sub else DEFAULT_PCT

    # 历史为空：要么没人订阅（扫描任务根本没跑），要么刚重启还没拉第一轮
    if not _price_hist:
        if not watch:
            await update.message.reply_text(
                "📭 还没有人订阅急涨急跌，后台监控**没在跑**（省 API）。\n"
                "先发 `/watchpump` 开启，等约 15 分钟攒够历史后再看 `/pumptop`。",
                parse_mode="Markdown")
        else:
            await update.message.reply_text(
                "⏳ 监控刚启动，还没拉到第一轮行情，几十秒后再试 `/pumptop`。",
                parse_mode="Markdown")
        return

    now = time.time()
    ranked = []
    for sym, h in _price_hist.items():
        r = _change_and_span(h, now)
        if r is not None:
            ranked.append((sym, r[0], r[1], h[-1][1]))   # sym, 涨跌%, 跨度s, 现价

    if not ranked:
        await update.message.reply_text("⏳ 历史还在攒（不足 2 个采样点），稍等再看。")
        return

    max_span = max(r[2] for r in ranked)
    warming = max_span < WINDOW - 60      # 最长跨度还没到 ~15 分钟＝仍在热身
    ups = sorted([r for r in ranked if r[1] > 0], key=lambda x: -x[1])[:8]
    downs = sorted([r for r in ranked if r[1] < 0], key=lambda x: x[1])[:8]

    lines = [f"📊 *Bybit 15m 滚动涨跌榜*（共监控 {len(ranked)} 个永续）"]
    if sub:
        lines.append(f"你的告警线：涨跌 ≥ *{pct:g}%*")
    else:
        lines.append("⚠️ 本群*未订阅*（下面是全局历史）；`/watchpump` 才会推送")
    if _started_at:
        lines.append(f"⏱ 本次运行 {(time.time()-_started_at)/60:.0f} 分钟"
                     "（每次部署会清空历史、重新攒15分钟）")
    if warming:
        lines.append(f"⏳ *仍在热身*：当前最长仅覆盖 ~{max_span/60:.0f} 分钟，"
                     f"满 15 分钟后判定才完整——**这期间不会告警是正常的**")

    def fmt(r):
        sym, ch, span, price = r
        span_tag = "" if span >= WINDOW - 60 else f" [{span/60:.0f}m]"
        hit = " 🔔" if abs(ch) >= pct else ""
        return f"  {escape_md(sym)} {ch:+.1f}%{span_tag}（${price:,.4g}）{hit}"

    lines.append("\n🚀 *涨幅前列*")
    lines += [fmt(r) for r in ups] or ["  （暂无上涨的）"]
    lines.append("\n💥 *跌幅前列*")
    lines += [fmt(r) for r in downs] or ["  （暂无下跌的）"]

    top = max((abs(r[1]) for r in ranked), default=0)
    if top < pct:
        lines.append(f"\n此刻最大波动 {top:.1f}%，还没到你 {pct:g}% 的线——"
                     f"所以没推送是**正常的**。想更灵敏就 `/watchpump {max(int(top),5)}`")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


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
