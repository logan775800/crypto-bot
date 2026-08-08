"""事件驱动预警 —— 「价格到了」这四个字是没用的。

现有的价格预警回答的是"到没到"，但你收到它的时候需要立刻决定做什么，
而"做什么"取决于**为什么到的**：放量突破着到的、缩量磨上去的、还是
BTC 拉着整个市场上去的——同一个价格，三种完全不同的处理。

所以这里的每条提醒都必须带上下文：触发了什么、当时的结构/资金/盘口是什么、
以及**这意味着什么**。没有上下文的提醒会训练用户忽略提醒。

监控的是永续特有的状态**切换**，不是绝对值：
  • OI 异常增仓 —— 短时间内持仓量跳升，有人在建仓
  • 价/OI 四象限切换 —— 从"新多进场"变成"多头平仓"，推动力换人了
  • 资金费跨阈值 —— 从常态进入拥挤区
  • 盘口失衡突变 —— 承接方向翻转
订阅：/events 开关面板。
"""
import logging
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from handlers import marketdata as md
from handlers.util import escape_md, safe_edit, safe_reply
from storage import data, save_data

log = logging.getLogger(__name__)

# 触发阈值。定得偏保守——预警的价值随误报率上升而**急速**归零，
# 一条没用的提醒会让用户开始忽略后面所有条。
OI_JUMP_PCT = 8.0          # 15分钟内 OI 跳升这么多 = 有人在建仓
FUNDING_CROSS = 0.0005     # 资金费跨过 ±0.05%/期 = 进入拥挤区
IMBALANCE_FLIP = 40.0      # 盘口失衡从 +X 翻到 -X（或反之）
COOLDOWN = 3600            # 同一(币,事件)冷却，防同一个状态反复报

QUADRANTS = {
    (True, True): "价涨+OI涨｜新多进场，趋势有资金推动",
    (True, False): "价涨+OI跌｜空头回补推的，缺新增买盘，追多要谨慎",
    (False, True): "价跌+OI涨｜新空堆积，可能延续，也可能酝酿轧空",
    (False, False): "价跌+OI跌｜多头在平仓/被清算，情绪释放，防反弹",
}


def _subs():
    return data.setdefault("event_subs", {})


def _state():
    return data.setdefault("event_state", {})


def quadrant(d_px, d_oi):
    return QUADRANTS[(d_px > 0, d_oi > 0)]


def detect(sym, cur, prev):
    """对比这一轮和上一轮的状态，返回 [(事件key, 标题, 上下文行)]。

    纯函数，方便测——预警的判定逻辑写错的代价是"用户被训练成忽略提醒"。
    prev 为空（第一次见到这个币）时一律不报：没有基线就没有"变化"。
    """
    out = []
    if not prev:
        return out

    # OI 异常增仓
    oi_now, oi_prev = cur.get("oi"), prev.get("oi")
    if oi_now and oi_prev and oi_prev > 0:
        jump = (oi_now - oi_prev) / oi_prev * 100
        if jump >= OI_JUMP_PCT:
            px_chg = cur.get("chg_15m") or 0
            out.append(("oi_jump",
                        f"⚡ OI 15分钟跳升 {jump:+.1f}%",
                        [f"持仓量 {oi_prev:,.0f} → {oi_now:,.0f}",
                         f"同期价格 {px_chg:+.2f}%",
                         quadrant(px_chg, jump),
                         "有人在建仓——方向由价/OI组合判断，别只看OI涨就当利多"]))

    # 四象限切换：推动力换人了
    q_now, q_prev = cur.get("quad"), prev.get("quad")
    if q_now and q_prev and q_now != q_prev:
        out.append(("quad_flip",
                    "🔄 价/OI 结构切换",
                    [f"由「{q_prev}」", f"变为「{q_now}」",
                     "推动这段行情的人换了，原来的持仓逻辑可能已经不成立"]))

    # 资金费跨阈值
    f_now, f_prev = cur.get("funding"), prev.get("funding")
    if f_now is not None and f_prev is not None:
        for sign in (1, -1):
            th = FUNDING_CROSS * sign
            crossed = (f_prev < th <= f_now) if sign > 0 else (f_prev > th >= f_now)
            if crossed:
                who = "多头" if sign > 0 else "空头"
                out.append(("funding_cross",
                            f"💵 资金费进入拥挤区 {f_now*100:+.3f}%/期",
                            [f"由 {f_prev*100:+.3f}% 跨过 {th*100:+.3f}%",
                             f"{who}正在为持仓付费，且付得越来越多",
                             f"持有{who}方向的仓位成本上升；反向仓位在收钱"]))

    # 盘口失衡翻转
    i_now, i_prev = cur.get("imb"), prev.get("imb")
    if i_now is not None and i_prev is not None:
        if (i_prev >= IMBALANCE_FLIP and i_now <= -IMBALANCE_FLIP) or \
           (i_prev <= -IMBALANCE_FLIP and i_now >= IMBALANCE_FLIP):
            side = "买盘转卖压" if i_prev > 0 else "卖压转买盘"
            out.append(("imb_flip",
                        f"📖 盘口承接翻转（{side}）",
                        [f"失衡 {i_prev:+.0f}% → {i_now:+.0f}%",
                         "挂单是可撤的，翻转可能是真换手也可能是撤单假象",
                         "配合逐笔成交确认，别单独据此进场"]))
    return out


async def snapshot(sym):
    """取一个币的当前状态。任何一项取不到就留 None —— 缺项不参与判定。"""
    cur = {"ts": time.time()}
    try:
        t = await md._get("/v5/market/tickers", {"category": md.CAT, "symbol": sym})
        tk = (t.get("list") or [{}])[0]
        cur["funding"] = float(tk.get("fundingRate") or 0)
        cur["price"] = float(tk.get("lastPrice") or 0)
    except Exception as e:
        log.debug(f"事件监控取 {sym} ticker 失败: {e}")
    try:
        r = await md._get("/v5/market/open-interest", {
            "category": md.CAT, "symbol": sym, "intervalTime": "15min", "limit": 2})
        rows = (r.get("list") or [])[::-1]
        if rows:
            cur["oi"] = float(rows[-1]["openInterest"])
    except Exception as e:
        log.debug(f"事件监控取 {sym} OI 失败: {e}")
    try:
        k = await md._get("/v5/market/kline", {
            "category": md.CAT, "symbol": sym,
            "interval": md.INTERVALS["15m"], "limit": 2})
        rows = (k.get("list") or [])[::-1]
        if len(rows) >= 1:
            o, c = float(rows[-1][1]), float(rows[-1][4])
            if o > 0:
                cur["chg_15m"] = (c - o) / o * 100
    except Exception as e:
        log.debug(f"事件监控取 {sym} K线失败: {e}")
    try:
        b = await md._get("/v5/market/orderbook",
                          {"category": md.CAT, "symbol": sym, "limit": 50})
        bv = sum(float(s) for _p, s in (b.get("b") or []))
        av = sum(float(s) for _p, s in (b.get("a") or []))
        if bv + av > 0:
            cur["imb"] = (bv - av) / (bv + av) * 100
    except Exception as e:
        log.debug(f"事件监控取 {sym} 盘口失败: {e}")
    if cur.get("chg_15m") is not None and cur.get("oi") is not None:
        cur["quad"] = None      # 需要 OI 变化率，下一轮才算得出
    return cur


def render(sym, title, ctx, price=None):
    short = sym.replace("USDT", "")
    lines = [f"🔔 *{escape_md(short)}*　{title}"]
    if price:
        lines.append(f"现价 {md.f(price)}")
    lines += [f"　{escape_md(c)}" for c in ctx]
    lines.append("事件提醒带上下文——没有上下文的「价格到了」没法据以决策")
    return "\n".join(lines)


# ── 后台任务 ────────────────────────────────────────────────────
async def check_events(context):
    subs = _subs()
    if not subs:
        return
    watch = {}
    for chat_id, cfg in subs.items():
        for s in (cfg.get("symbols") or []):
            watch.setdefault(s, []).append(chat_id)
    if not watch:
        return
    st = _state()
    now = time.time()
    cool = data.setdefault("event_cooldown", {})
    changed = False
    for sym, chats in watch.items():
        try:
            cur = await snapshot(sym)
        except Exception as e:
            log.warning(f"事件监控 {sym} 取数失败: {e}")
            continue
        prev = st.get(sym) or {}
        # 四象限要用相邻两轮的 OI 变化算，所以在这里补
        if prev.get("oi") and cur.get("oi") and prev["oi"] > 0:
            d_oi = (cur["oi"] - prev["oi"]) / prev["oi"] * 100
            cur["quad"] = quadrant(cur.get("chg_15m") or 0, d_oi)
        try:
            events = detect(sym, cur, prev)
        except Exception as e:
            log.error(f"事件判定出错 {sym}: {e}")
            events = []
        st[sym] = cur
        changed = True
        for key, title, ctx in events:
            ck = f"{sym}:{key}"
            if now - cool.get(ck, 0) < COOLDOWN:
                continue
            cool[ck] = now
            text = render(sym, title, ctx, cur.get("price"))
            for chat_id in chats:
                try:
                    await context.bot.send_message(chat_id=int(chat_id), text=text,
                                                   parse_mode="Markdown")
                except Exception as e:
                    log.error(f"事件提醒推送失败 {chat_id}: {e}")
    for k in [k for k, v in cool.items() if now - v > COOLDOWN * 4]:
        cool.pop(k, None)
    if changed:
        save_data()


# ── 命令 / 面板 ─────────────────────────────────────────────────
def _panel(chat_id):
    cfg = _subs().get(str(chat_id)) or {}
    syms = cfg.get("symbols") or []
    text = (
        "🔔 *事件驱动预警*\n"
        "━━━━━━━━━━━━━━\n"
        + (f"正在盯：*{'、'.join(s.replace('USDT','') for s in syms)}*\n"
           if syms else "还没盯任何币\n")
        + "\n盯的是**状态切换**，不是绝对价格：\n"
        "• OI 15分钟跳升 ≥8%（有人在建仓）\n"
        "• 价/OI 四象限切换（推动力换人了）\n"
        "• 资金费跨进拥挤区（±0.05%/期）\n"
        "• 盘口承接翻转\n"
        "━━━━━━━━━━━━━━\n"
        "每条提醒都带上下文和「这意味着什么」。\n"
        "加币：`/events BTC ETH`　清空：`/events off`"
    )
    rows = [[InlineKeyboardButton("🔄 刷新", callback_data="ev:panel")]]
    if syms:
        rows.append([InlineKeyboardButton("🔕 全部取消", callback_data="ev:off")])
    return text, InlineKeyboardMarkup(rows)


async def events_cmd(update, context):
    """/events [币...] —— 订阅事件驱动预警。"""
    chat_id = str(update.effective_chat.id)
    args = [a.upper() for a in (context.args or [])]
    subs = _subs()
    if args and args[0] in ("OFF", "CLEAR", "取消"):
        subs.pop(chat_id, None)
        save_data()
        await safe_reply(update.message, "已取消全部事件预警")
        return
    if args:
        syms = [md.norm(a) for a in args][:8]     # 每个币每轮 4 个请求，别开太多
        subs[chat_id] = {"symbols": syms}
        save_data()
    text, kb = _panel(update.effective_chat.id)
    await safe_reply(update.message, text, reply_markup=kb, parse_mode="Markdown")


async def on_button(query, context):
    chat_id = query.message.chat.id
    if query.data == "ev:off":
        _subs().pop(str(chat_id), None)
        save_data()
        await query.answer("已取消")
    else:
        await query.answer()
    text, kb = _panel(chat_id)
    await safe_edit(query, text, reply_markup=kb, parse_mode="Markdown")
