"""风险守护 /risk —— 比爆仓预警更早一步的四道闸 + BTC 联动。

和 rtrade.check_liq_alerts（距爆仓 ≤X% 才叫）的区别：那是最后一道，这里是提前量。
五项检查，各自独立开关、独立冷却：
  1) 保证金率  账户维持保证金率越界（整个账户级别，比单币爆仓价更早反映风险）
  2) 同向集中  N 个仓全同向 / 山寨敞口过大 → BTC 一破位全灭
  3) 当日熔断  当日权益回撤达阈值 → 「今天到此为止」（连亏时最救命的一条）
  4) 裸奔仓位  有持仓但没设止损
  5) BTC 联动  BTC 短周期急跌 + 你手里有山寨多单 → 提示降仓

全部只读，不会自动平仓——只提醒，动手的是人。
"""
import time
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from storage import data, save_data
from handlers.util import safe_reply, safe_edit

log = logging.getLogger(__name__)

# 默认阈值
DEF = {
    "mmr": 40.0,        # 账户维持保证金率 ≥40% 就叫（100% = 强平）
    "daily": 5.0,       # 当日权益回撤 ≥5% 触发熔断提醒
    "conc": 80.0,       # 单方向名义占比 ≥80% 且 ≥3 个仓 → 集中度告警
    "btc_drop": 1.5,    # BTC 15m 跌幅 ≥1.5% 且持有山寨多单 → 联动提醒
}
COOLDOWN = {
    "mmr": 1800,        # 30分钟
    "conc": 7200,       # 2小时（结构性问题，别刷屏）
    "nosl": 7200,       # 2小时
    "btc": 1800,        # 30分钟
}
MAJORS = ("BTCUSDT", "ETHUSDT")


def _cfg():
    return data.setdefault("riskguard", {})


def _on(key):
    """某项检查是否开启。默认全开（用户开了总开关就是要它管事）。"""
    return _cfg().get("checks", {}).get(key, True)


def _cool_ok(key, sec):
    """冷却判断 + 打点。返回 True 表示可以发。"""
    cd = _cfg().setdefault("cooldown", {})
    now = time.time()
    if now - cd.get(key, 0) < sec:
        return False
    cd[key] = now
    return True


async def _notify(context, text):
    cid = _cfg().get("chat_id")
    if not cid:
        return
    try:
        await context.bot.send_message(chat_id=cid, text=text, parse_mode="Markdown")
    except Exception as e:
        log.error(f"风险守护推送失败: {e}")


def _fnum(x, d=2):
    try:
        return f"{float(x):,.{d}f}"
    except (TypeError, ValueError):
        return str(x)


# ── 1) 账户维持保证金率 ─────────────────────────────────────────────
def check_mmr(bal):
    """Bybit 统一账户返回 accountMMRate（0~1 的比率，1=触发强平）。
    比逐币爆仓价更早：多个仓一起亏时它先涨起来。返回告警文本或 None。"""
    raw = bal.get("accountMMRate")
    if raw in (None, ""):
        return None
    try:
        mmr = float(raw) * 100
    except (TypeError, ValueError):
        return None
    thr = _cfg().get("mmr", DEF["mmr"])
    if mmr < thr:
        return None
    return (f"🚨 *保证金率告警*\n"
            f"账户维持保证金率 *{mmr:.1f}%*（阈值 {thr:g}%，到 100% 强平）\n"
            f"权益 {_fnum(bal.get('totalEquity'))}｜可用 {_fnum(bal.get('totalAvailableBalance'))} USDT\n"
            f"这是**账户级**风险，比单币爆仓价更早预警。\n"
            f"处理：减仓 / 加保证金 / 挪止损。`/trade` 一键操作")


# ── 2) 同向集中度 ──────────────────────────────────────────────────
def check_concentration(positions):
    """全押一个方向 + 全是山寨 = BTC 一破位全灭。返回告警文本或 None。"""
    if len(positions) < 3:
        return None
    longs = [p for p in positions if p.get("side") == "Buy"]
    shorts = [p for p in positions if p.get("side") == "Sell"]

    def notional(ps):
        return sum(float(p.get("positionValue") or 0) for p in ps)

    ln, sn = notional(longs), notional(shorts)
    tot = ln + sn
    if tot <= 0:
        return None
    thr = _cfg().get("conc", DEF["conc"])
    if ln / tot * 100 >= thr:
        side, ps, share = "多", longs, ln / tot * 100
    elif sn / tot * 100 >= thr:
        side, ps, share = "空", shorts, sn / tot * 100
    else:
        return None
    alts = [p for p in ps if p.get("symbol") not in MAJORS]
    alt_share = notional(alts) / notional(ps) * 100 if notional(ps) > 0 else 0
    syms = "、".join(p.get("symbol", "?").replace("USDT", "") for p in ps[:6])
    extra = ""
    if alt_share >= 70 and len(alts) >= 2:
        extra = (f"\n其中 *{alt_share:.0f}%* 是山寨 —— BTC 一破位，这些会一起走，"
                 f"等于一个放大版的单一仓位。")
    return (f"⚠️ *同向集中度告警*\n"
            f"{len(ps)} 个仓全是*做{side}*，占总名义 {share:.0f}%（${tot:,.0f}）\n"
            f"　{syms}{extra}\n"
            f"处理：砍掉相关性最高的那几个，或对冲一部分。")


# ── 3) 当日亏损熔断 ────────────────────────────────────────────────
def _today():
    """容器 TZ=Asia/Shanghai，localtime 即北京时间。"""
    return time.strftime("%Y-%m-%d")


def check_daily(equity):
    """记录每日起始权益，回撤达阈值提醒收工。每天只叫一次。"""
    c = _cfg()
    day = c.setdefault("day", {})
    today = _today()
    if day.get("date") != today:
        # 新的一天：重置基准，当天的熔断也一并复位
        day.clear()
        day.update({"date": today, "start": equity, "fired": False})
        return None
    start = day.get("start") or 0
    if start <= 0 or day.get("fired"):
        return None
    dd = (start - equity) / start * 100
    thr = _cfg().get("daily", DEF["daily"])
    if dd < thr:
        return None
    day["fired"] = True
    return (f"🛑 *当日亏损熔断*\n"
            f"今日权益 {_fnum(start)} → {_fnum(equity)}，回撤 *-{dd:.2f}%*（阈值 {thr:g}%）\n"
            f"━━━━━━━━━━━━━━\n"
            f"建议今天到此为止。连亏之后的下一单，通常是想把钱赢回来的那一单——"
            f"那也是最容易加倍亏的一单。\n"
            f"明天 0 点自动复位。看复盘：`/rstats 7`")


# ── 4) 裸奔仓位（没设止损）──────────────────────────────────────────
def check_no_sl(positions):
    naked = [p for p in positions
             if str(p.get("stopLoss") or "0") in ("0", "0.0", "", "None")]
    if not naked:
        return None
    lines = ["🔓 *有仓位没设止损*"]
    for p in naked:
        sym = p.get("symbol", "?")
        side = "多" if p.get("side") == "Buy" else "空"
        lines.append(f"　{sym.replace('USDT','')} {side} {p.get('leverage','?')}x"
                     f"｜名义 ${_fnum(p.get('positionValue'), 0)}"
                     f"｜浮盈 {_fnum(p.get('unrealisedPnl'))}")
    lines.append("\n一键设：`/trade` → 点该仓的【改止损】"
                 "\n或 `/rtpsl BTC sl=60000`")
    return "\n".join(lines)


# ── 5) BTC 破位 → 山寨持仓联动 ──────────────────────────────────────
async def check_btc_linkage(positions):
    """BTC 短周期急跌且你手里有山寨多单 → 提示降仓。BTC 自己的仓不算。"""
    alt_longs = [p for p in positions
                 if p.get("side") == "Buy" and p.get("symbol") not in MAJORS]
    if not alt_longs:
        return None
    from handlers import marketdata as md
    try:
        r = await md._get("/v5/market/kline",
                          {"category": "linear", "symbol": "BTCUSDT",
                           "interval": "15", "limit": 5})
        rows = (r.get("list") or [])[::-1]      # Bybit 返回新→旧
        closes = [float(x[4]) for x in rows]
    except Exception as e:
        log.warning(f"BTC 联动检查取K线失败: {e}")
        return None
    if len(closes) < 3:
        return None
    # 近 2 根 15m（约半小时）的跌幅
    drop = (closes[-1] - closes[-3]) / closes[-3] * 100
    thr = _cfg().get("btc_drop", DEF["btc_drop"])
    if drop > -thr:
        return None
    upnl = sum(float(p.get("unrealisedPnl") or 0) for p in alt_longs)
    notional = sum(float(p.get("positionValue") or 0) for p in alt_longs)
    syms = "、".join(p.get("symbol", "?").replace("USDT", "") for p in alt_longs[:6])
    return (f"🔗 *BTC 破位 → 山寨多单预警*\n"
            f"BTC 近 30 分钟 *{drop:.2f}%*（现价 {_fnum(closes[-1], 0)}）\n"
            f"你有 {len(alt_longs)} 个山寨多单｜名义 ${notional:,.0f}｜浮盈 {upnl:+,.2f}\n"
            f"　{syms}\n"
            f"山寨在 BTC 急跌时跌幅通常是 BTC 的 1.5~3 倍。考虑先降仓或收紧止损。")


# ── 后台 job ───────────────────────────────────────────────────────
async def check_risk(context: ContextTypes.DEFAULT_TYPE):
    """每 60 秒跑一遍五项检查。未开启/无密钥/无仓位则静默跳过。"""
    c = _cfg()
    if not c.get("enabled") or not c.get("chat_id"):
        return
    from handlers.rtrade import _client
    try:
        client = _client()
    except RuntimeError:
        return          # 没配密钥，静默
    try:
        bal = await client.wallet_balance("USDT")
        positions = await client.positions_all()
    except Exception as e:
        log.warning(f"风险守护取数失败: {e}")
        return

    fired = False
    try:
        equity = float(bal.get("totalEquity") or 0)
    except (TypeError, ValueError):
        equity = 0.0

    # 当日熔断：即使空仓也要跑（要维护每日基准），且不吃冷却——本来每天只叫一次
    if _on("daily") and equity > 0:
        msg = check_daily(equity)
        if msg:
            await _notify(context, msg)
        fired = True                     # day 基准有更新，需要落盘

    if _on("mmr"):
        msg = check_mmr(bal)
        if msg and _cool_ok("mmr", COOLDOWN["mmr"]):
            await _notify(context, msg)
            fired = True

    if positions:
        if _on("conc"):
            msg = check_concentration(positions)
            if msg and _cool_ok("conc", COOLDOWN["conc"]):
                await _notify(context, msg)
                fired = True
        if _on("nosl"):
            msg = check_no_sl(positions)
            if msg and _cool_ok("nosl", COOLDOWN["nosl"]):
                await _notify(context, msg)
                fired = True
        if _on("btc"):
            try:
                msg = await check_btc_linkage(positions)
            except Exception as e:
                log.warning(f"BTC 联动检查出错: {e}")
                msg = None
            if msg and _cool_ok("btc", COOLDOWN["btc"]):
                await _notify(context, msg)
                fired = True

    if fired:
        save_data()


# ── 面板 ───────────────────────────────────────────────────────────
CHECK_LABELS = [
    ("mmr", "保证金率告警"),
    ("conc", "同向集中度"),
    ("daily", "当日亏损熔断"),
    ("nosl", "裸奔仓位提醒"),
    ("btc", "BTC破位→山寨联动"),
]


def panel_content():
    c = _cfg()
    on = c.get("enabled")
    lines = [
        f"🛡 *风险守护*　{'✅ 已开启' if on else '⬜ 未开启'}",
        "",
        "比爆仓预警更早的四道闸 + BTC 联动。只提醒，不自动平仓。",
        "",
        f"• 保证金率 ≥ *{c.get('mmr', DEF['mmr']):g}%* 告警（100%=强平）",
        f"• 当日权益回撤 ≥ *{c.get('daily', DEF['daily']):g}%* 熔断提醒",
        f"• 单方向名义占比 ≥ *{c.get('conc', DEF['conc']):g}%* 且≥3仓 → 集中度告警",
        f"• BTC 30分钟跌 ≥ *{c.get('btc_drop', DEF['btc_drop']):g}%* 且持山寨多单 → 联动提醒",
        "• 持仓没设止损 → 提醒（每2小时一次）",
    ]
    day = c.get("day") or {}
    if day.get("date") == _today() and day.get("start"):
        lines.append(f"\n今日基准权益 {_fnum(day['start'])} USDT"
                     + ("　🛑 今日已熔断" if day.get("fired") else ""))
    rows = [[InlineKeyboardButton("🛑 关闭守护" if on else "✅ 开启守护",
                                  callback_data="rgtog")]]
    for key, label in CHECK_LABELS:
        mark = "✅" if _on(key) else "⬜"
        rows.append([InlineKeyboardButton(f"{mark} {label}", callback_data=f"rgc:{key}")])
    rows.append([InlineKeyboardButton("当日熔断 3%", callback_data="rgset:daily:3"),
                 InlineKeyboardButton("5%", callback_data="rgset:daily:5"),
                 InlineKeyboardButton("10%", callback_data="rgset:daily:10")])
    rows.append([InlineKeyboardButton("保证金率 30%", callback_data="rgset:mmr:30"),
                 InlineKeyboardButton("40%", callback_data="rgset:mmr:40"),
                 InlineKeyboardButton("60%", callback_data="rgset:mmr:60")])
    rows.append([InlineKeyboardButton("🎛 交易台", callback_data="tpanel"),
                 InlineKeyboardButton("📊 复盘", callback_data="rsd:30")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def risk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/risk 风险守护面板。"""
    from handlers.rtrade import _guard
    if not await _guard(update):
        return
    # 面板在哪个会话打开，告警就推到哪个会话
    _cfg().setdefault("chat_id", update.effective_chat.id)
    save_data()
    text, kb = panel_content()
    await safe_reply(update.message, text, reply_markup=kb, parse_mode="Markdown")


# ── 按钮回调（由 menu.button_handler 分发）──────────────────────────
async def toggle(query, context):
    from handlers.rtrade import _btn_admin_ok
    if not _btn_admin_ok(query):
        await query.answer("仅管理员", show_alert=True)
        return
    c = _cfg()
    c["enabled"] = not c.get("enabled")
    c["chat_id"] = query.message.chat_id
    if c["enabled"]:
        # 重新开启时清掉旧冷却，别让上次的冷却压住第一条告警
        c["cooldown"] = {}
    save_data()
    await query.answer("已开启风险守护" if c["enabled"] else "已关闭")
    text, kb = panel_content()
    await safe_edit(query, text, reply_markup=kb, parse_mode="Markdown")


async def toggle_check(query, context, key):
    from handlers.rtrade import _btn_admin_ok
    if not _btn_admin_ok(query):
        await query.answer("仅管理员", show_alert=True)
        return
    checks = _cfg().setdefault("checks", {})
    checks[key] = not checks.get(key, True)
    save_data()
    await query.answer(("已开启 " if checks[key] else "已关闭 ")
                       + dict(CHECK_LABELS).get(key, key))
    text, kb = panel_content()
    await safe_edit(query, text, reply_markup=kb, parse_mode="Markdown")


async def set_threshold(query, context, key, val):
    from handlers.rtrade import _btn_admin_ok
    if not _btn_admin_ok(query):
        await query.answer("仅管理员", show_alert=True)
        return
    try:
        _cfg()[key] = float(val)
    except ValueError:
        await query.answer("阈值无效", show_alert=True)
        return
    save_data()
    await query.answer(f"已设 {val}%")
    text, kb = panel_content()
    await safe_edit(query, text, reply_markup=kb, parse_mode="Markdown")
