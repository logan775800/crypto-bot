"""5 分钟箱体破位扫描 —— 热门币里，箱体整理之后**带均线顺势**的那一下。

打法本身很朴素：先横盘（箱体整理），再放量突破箱体边界，而且 MA3/13/23 已经排好队。
三个条件缺一不可，缺了任何一个都是这套打法最典型的亏钱方式：

  • 没有箱体 → 那是趋势中继或者自由落体，"突破"这个词根本不成立；
  • 有箱体没均线顺势 → 三根均线还缠在一起，方向没定，破上破下都可能立刻打回来；
  • 有均线顺势没箱体收敛 → 追在半路上，止损无处可放（箱体边界才是天然止损位）。

所以这里不做"涨得多就报"。涨幅榜谁都会拉，报出来的必须是**能画出止损**的形态。

判定（全部在 5m 上，用**已收盘**的 K 线）：
  1. 箱体：最近 BOX_BARS 根里，(最高-最低)/中位价 ≤ BOX_MAX_PCT，
     且这段区间内价格来回穿过中轴（不是单边斜坡）；
  2. 破位：最新收盘价 收在箱体上沿之上（向上突破）或下沿之下（向下跌破），
     幅度要超过 BREAK_BUFFER，避免贴边算突破；
  3. 顺势：MA3/13/23 排列方向与破位方向一致（annotchart.ma_align）。

放量和止损距离是**硬条件**（见下面阈值处的实测数据）：缩量突破是假突破高发区，
止损超过 2.5% 的形态在 5 分钟级别赔率不够。
"""
import logging
import time

from telegram.ext import ContextTypes

from storage import data, save_data

log = logging.getLogger(__name__)

TF = "5m"
BOX_BARS = 24              # 箱体观察窗：24 根 5m = 2 小时
NEED_BARS = 60             # 至少要这么多根才算数（MA23 + 箱体窗口都要有余量）
# 这几个阈值是**量出来的**，不是拍的。拿 23 个热门币 × 3.3 天的 5m 历史逐根回放：
#   箱高≤4%  穿轴≥3  不要求放量  不限止损 → 55.8 次/天（推群里就是刷屏）
#   箱高≤2.5% 穿轴≥5  必须放量   止损≤2.5% → 17.5 次/天，止损中位 1.33%
# 后者才是能看的节奏。收紧的三条都有交易含义，不是为了少报而少报：
#   • 箱体越窄，突破的赔率越好，止损也越近；
#   • 穿轴次数是"真箱体 vs 慢速斜坡"的分界（BTC/ETH 这种缓慢漂移只穿 0~2 次）；
#   • 缩量突破是假突破的高发区；止损超过 2.5% 的形态，5 分钟级别不值得做。
BOX_MAX_PCT = 2.5          # 箱体高度上限（占中位价%）——太宽就不叫整理了
BOX_MIN_PCT = 0.4          # 太窄多半是没成交的僵尸盘，不是"整理"
BREAK_BUFFER = 0.15        # 收盘价要超出边界这么多百分比才算破位，贴边不算
MIN_CROSSES = 5            # 箱体内至少来回穿过中轴这么多次，否则是斜坡不是箱体
VOL_OK = 1.2               # 突破那根的量 ≥ 前段均量的这个倍数才叫放量
MAX_STOP_PCT = 2.5         # 止损（到箱体另一侧）超过这个数就不报——赔率不够

POOL = 40                  # 从热门币里扫多少个
CONCURRENCY = 5
COOLDOWN = 1800            # 同一个币同方向 30 分钟内不重复报
MIN_TURNOVER = 20_000_000  # 热门的门槛：24h 成交额


def box_of(rows, bars=BOX_BARS):
    """算箱体 → dict 或 None（不成箱体）。rows 是**已收盘**的 K 线，旧→新。"""
    if len(rows) < bars + 2:
        return None
    win = rows[-bars:]
    highs = [r[2] for r in win]
    lows = [r[3] for r in win]
    closes = [r[4] for r in win]
    top, bot = max(highs), min(lows)
    mid = (top + bot) / 2
    if mid <= 0:
        return None
    height = (top - bot) / mid * 100
    if not (BOX_MIN_PCT <= height <= BOX_MAX_PCT):
        return None
    # 来回穿过中轴的次数：斜坡只穿一次，真箱体会反复穿
    crosses = sum(1 for i in range(1, len(closes))
                  if (closes[i - 1] - mid) * (closes[i] - mid) < 0)
    if crosses < MIN_CROSSES:
        return None
    return {"top": top, "bot": bot, "mid": mid, "height_pct": height,
            "crosses": crosses, "bars": bars}


def detect(rows, fresh_only=True):
    """→ 信号 dict 或 None。rows 必须是**已收盘**的 5m K 线（旧→新）。

    fresh_only：只认**破出去那一根**。价格出箱后往往连着好几根都满足条件，
    不去重的话实测 23 个币 3.3 天触发 204 次（62 次/天）——推到群里就是刷屏，
    而且第 10 条提醒时那个入场点早就没了。
    判定方式是"上一根还没触发"，而不是"上一根收在箱体内"：
    箱体的上下沿本来就是含上一根算出来的，后者是同义反复，永远成立。
    """
    from handlers.annotchart import ma_align, _ma_series
    if len(rows) < NEED_BARS:
        return None
    closes = [r[4] for r in rows]
    # 箱体只看**突破那根之前**的区间，否则突破自己会把箱体撑大
    box = box_of(rows[:-1])
    if not box:
        return None

    last = rows[-1]
    close = last[4]
    up_line = box["top"] * (1 + BREAK_BUFFER / 100)
    dn_line = box["bot"] * (1 - BREAK_BUFFER / 100)
    if close > up_line:
        direction = 1
    elif close < dn_line:
        direction = -1
    else:
        return None

    if fresh_only and detect(rows[:-1], fresh_only=False):
        return None            # 上一根就已经破出去了，这根只是"还在外面"

    align = ma_align(closes)
    if align != direction:
        return None                 # 均线没顺势 —— 这正是要滤掉的那一半

    vols = [r[5] for r in rows[-(BOX_BARS + 1):-1] if r[5] > 0]
    avg_vol = sum(vols) / len(vols) if vols else 0
    vol_x = (last[5] / avg_vol) if avg_vol > 0 else None

    if not (vol_x and vol_x >= VOL_OK):
        return None            # 缩量突破是假突破的高发区
    stop = box["bot"] if direction > 0 else box["top"]
    stop_pct = abs(close - stop) / close * 100
    if stop_pct > MAX_STOP_PCT:
        return None            # 止损太远，这个形态 5 分钟级别不值得做

    mas = {n: _ma_series(closes, n)[-1] for n in (3, 13, 23)}
    return {
        "direction": direction,
        "close": close,
        "box": box,
        "vol_x": vol_x,
        "volume_ok": bool(vol_x and vol_x >= VOL_OK),
        # 止损天然放在箱体另一侧——这是这套打法能算仓位的原因
        "stop": stop,
        "stop_pct": stop_pct,
        "ma": mas,
        "ts": last[0],
    }


def render(sym, sig, src_label=""):
    d = sig["direction"]
    box = sig["box"]
    head = "🚀 *向上突破*" if d > 0 else "🔻 *向下跌破*"
    arrow = "上沿" if d > 0 else "下沿"
    from handlers import marketdata as md
    lines = [
        f"{head}　*{sym}*　{TF}",
        f"现价 {md.f(sig['close'])}　突破箱体{arrow} {md.f(box['top'] if d > 0 else box['bot'])}",
        f"箱体 {md.f(box['bot'])} ~ {md.f(box['top'])}"
        f"（高度 {box['height_pct']:.1f}%，{box['bars']} 根内来回 {box['crosses']} 次）",
        f"均线 {'多头排列 MA3>MA13>MA23' if d > 0 else '空头排列 MA3<MA13<MA23'}",
        f"　MA3 {md.f(sig['ma'][3])}　MA13 {md.f(sig['ma'][13])}　MA23 {md.f(sig['ma'][23])}",
    ]
    if sig["vol_x"] is not None:
        tag = "✅ 放量" if sig["volume_ok"] else "⚠️ 缩量——假突破多发生在这种时候"
        lines.append(f"成交量 {sig['vol_x']:.1f}× 前段均量　{tag}")
    lines.append(f"🛡 止损参考 {md.f(sig['stop'])}"
                 f"（箱体另一侧，距现价 {sig['stop_pct']:.2f}%）")
    if src_label:
        lines.append(f"数据源 {src_label}")
    lines.append("⚠️ 破位形态本身不保证方向，只是给了个能算仓位的止损位；不构成投资建议")
    return "\n".join(lines)


# ---------- 订阅 ----------
def subs():
    """{chat_id: {...}}。默认订阅的群在 bot 启动时种进来（见 seed_default）。"""
    return data.setdefault("breakout_subs", {})


def seed_default(chat_id):
    """把一个会话种成默认订阅。

    **只种一次**：记在 breakout_seeded 里，用户退订之后不会被下次重启塞回来。
    这条是必须的——"默认订阅"和"关不掉"之间只隔着这一个标记。
    """
    d = data.setdefault("breakout_seeded", {})
    key = str(chat_id)
    if d.get(key):
        return False
    d[key] = True
    subs().setdefault(key, {"on": True})
    save_data()
    return True


def seed_all():
    """启动时把已经在用本机器人的会话都种成默认订阅（和版本播报同一批目标）。"""
    from storage import subscribed_chats
    n = 0
    for cid in subscribed_chats():
        if seed_default(cid):
            n += 1
    if n:
        log.info(f"破位推送：默认订阅了 {n} 个会话")
    return n


def is_on(chat_id):
    rec = subs().get(str(chat_id))
    return bool(rec and rec.get("on"))


def toggle(chat_id, on):
    subs()[str(chat_id)] = {"on": bool(on)}
    save_data()
    return on


# ---------- 扫描 ----------
async def scan_once(source=None, limit=POOL):
    """扫一轮 → [(币, 信号)]。只用已收盘的 K 线。"""
    from handlers import klines as kl
    ex, market = ("bybit", "swap") if not source else kl.src_mod.split_label(source)
    if ex == kl.src_mod.AUTO:
        ex, market = "bybit", "swap"

    universe = await kl.universe(ex, market)
    pool = [x for x in universe
            if x["crypto"] and x["turnover"] >= MIN_TURNOVER][:limit]
    if not pool:
        return [], kl.src_mod.label_of(ex, market)

    import asyncio
    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(item):
        sym = item["symbol"]
        async with sem:
            rows, meta = await kl.fetch(sym, TF, NEED_BARS + 10, ex, market)
        if len(rows) < NEED_BARS + 1:
            return None
        # 最后一根是**还没收盘**的，扔掉——拿半根 K 线判突破，一根针就能骗过去
        closed = rows[:-1]
        sig = detect(closed)
        return (sym, sig) if sig else None

    res = await asyncio.gather(*[one(x) for x in pool], return_exceptions=True)
    hits = [r for r in res if r and not isinstance(r, Exception)]
    # 放量的排前面，其次止损近的（同样的形态，止损越近这单越划算）
    hits.sort(key=lambda x: (not x[1]["volume_ok"], x[1]["stop_pct"]))
    return hits, kl.src_mod.label_of(ex, market)


def _dedupe_ok(chat_id, sym, direction, now):
    """同一个币同方向 30 分钟内不重复报——箱体刚破的那几根会连着命中。"""
    seen = data.setdefault("breakout_seen", {})
    key = f"{chat_id}:{sym}:{direction}"
    if now - seen.get(key, 0) < COOLDOWN:
        return False
    seen[key] = now
    # 顺手清理超过一天的记录，别让它无限长大
    for k, ts in list(seen.items()):
        if now - ts > 86400:
            seen.pop(k, None)
    return True


async def job(context: ContextTypes.DEFAULT_TYPE):
    """定时任务：扫一轮，推给订阅的会话。"""
    targets = [cid for cid, rec in subs().items() if rec.get("on")]
    if not targets:
        return
    try:
        hits, label = await scan_once()
    except Exception as e:
        log.error(f"破位扫描失败: {e}")
        return
    if not hits:
        return
    now = time.time()
    changed = False
    for chat_id in targets:
        sent = 0
        for sym, sig in hits:
            if sent >= 3:            # 一轮最多推 3 条，破位常常成群出现
                break
            if not _dedupe_ok(chat_id, sym, sig["direction"], now):
                continue
            changed = True
            sent += 1
            try:
                await context.bot.send_message(
                    int(chat_id), render(sym.replace("USDT", ""), sig, label),
                    parse_mode="Markdown")
            except Exception as e:
                log.error(f"破位推送失败 {chat_id}: {e}")
    if changed:
        save_data()


# ---------- 命令 / 按钮 ----------
USAGE = (
    "🚀 *5分钟破位扫描*\n\n"
    "从热门币里找**箱体整理之后**、且 MA3/13/23 已经顺势的破位。\n\n"
    "三个条件缺一不可：\n"
    "• 先有箱体（横盘 2 小时、来回穿中轴）——没箱体就没有止损位\n"
    "• 收盘价破出箱体边界（贴边不算）\n"
    "• MA3/13/23 排列方向和破位方向一致\n\n"
    "`/breakout`　立即扫一次\n"
    "`/breakout on`　订阅（破位自动推这里）\n"
    "`/breakout off`　退订\n\n"
    "⚠️ 报的是**能画出止损**的形态，不是「会涨」。"
)


def kb(chat_id):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    on = is_on(chat_id)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 立即扫一次", callback_data="bo:scan")],
        [InlineKeyboardButton("🔕 关闭自动推送" if on else "🔔 开启自动推送",
                              callback_data=f"bo:{'off' if on else 'on'}")],
        [InlineKeyboardButton("⬅️ 返回主菜单", callback_data="menu_main")],
    ])


def render_list(hits, label, limit=5):
    if not hits:
        return ("🚀 *5分钟破位扫描*\n\n此刻没有符合的破位。\n"
                "三个条件（箱体 + 破边界 + 均线顺势）同时满足本来就不常见——"
                "扫不出来是正常的，硬凑出来的才危险。")
    from handlers.util import escape_md
    lines = [f"🚀 *5分钟破位*　命中 {len(hits)} 个　数据源 {label}", "━━━━━━━━━━━━━━"]
    for sym, sig in hits[:limit]:
        d = sig["direction"]
        icon = "🚀" if d > 0 else "🔻"
        vol = ("放量" if sig["volume_ok"]
               else (f"缩量{sig['vol_x']:.1f}×" if sig["vol_x"] else "量未知"))
        lines.append(f"{icon} *{escape_md(sym.replace('USDT', ''))}*"
                     f"　箱体高 {sig['box']['height_pct']:.1f}%"
                     f"　止损 {sig['stop_pct']:.2f}%　{vol}")
    if len(hits) > limit:
        lines.append(f"…还有 {len(hits) - limit} 个")
    lines.append("\n⚠️ 不构成投资建议")
    return "\n".join(lines)


async def breakout_cmd(update, context):
    from handlers.util import safe_reply
    chat_id = update.effective_chat.id
    arg = (context.args[0].lower() if context.args else "")
    if arg in ("on", "开", "订阅"):
        toggle(chat_id, True)
        await safe_reply(update.message, "🔔 已订阅 5 分钟破位推送（每 5 分钟扫一次）",
                         reply_markup=kb(chat_id))
        return
    if arg in ("off", "关", "退订"):
        toggle(chat_id, False)
        await safe_reply(update.message, "🔕 已关闭破位推送", reply_markup=kb(chat_id))
        return
    if arg in ("help", "帮助", "?"):
        await safe_reply(update.message, USAGE, reply_markup=kb(chat_id),
                         parse_mode="Markdown")
        return
    await safe_reply(update.message, "🔍 扫描热门币的 5 分钟破位…（约 10~20 秒）")
    try:
        from handlers.source import pref_label
        hits, label = await scan_once(source=pref_label(chat_id))
    except Exception as e:
        log.error(f"/breakout 失败: {e}")
        await safe_reply(update.message, f"扫描失败：{str(e)[:80]}")
        return
    await safe_reply(update.message, render_list(hits, label),
                     reply_markup=kb(chat_id), parse_mode="Markdown")


async def on_button(query, context):
    """处理 bo:* 回调。由 menu 转发。"""
    from handlers.util import safe_edit
    what = query.data.split(":", 1)[1]
    chat_id = query.message.chat_id if query.message else 0
    if what in ("on", "off"):
        toggle(chat_id, what == "on")
        await query.answer("已订阅" if what == "on" else "已关闭")
        await safe_edit(query,
                        ("🔔 已订阅 5 分钟破位推送（每 5 分钟扫一次）"
                         if what == "on" else "🔕 已关闭破位推送"),
                        reply_markup=kb(chat_id))
        return
    if what == "scan":
        await query.answer("扫描中…")
        await safe_edit(query, "🔍 扫描热门币的 5 分钟破位…（约 10~20 秒）")
        try:
            from handlers.source import pref_label
            hits, label = await scan_once(source=pref_label(chat_id))
        except Exception as e:
            log.error(f"破位扫描失败: {e}")
            await safe_edit(query, f"扫描失败：{str(e)[:80]}", reply_markup=kb(chat_id))
            return
        await safe_edit(query, render_list(hits, label), reply_markup=kb(chat_id),
                        parse_mode="Markdown")
        return
    await query.answer("不认识的操作")
