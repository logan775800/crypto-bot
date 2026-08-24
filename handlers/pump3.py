"""极端拉升告警 `/pump3` —— 15m 放量暴拉 **且** 多日累计已经涨了一大截，才推。

他的原话：「15m 放量巨幅拉升 40%。3日累计涨幅达到 50% 这个能做吗」。

## 为什么做成订阅，不做成命令

先量了池子（`tools\\probe_pump3d.py`）：**过去 24 小时、112 个成交额≥500万的
永续里，符合「15m ≥40% 且 3日 ≥50%」的是 0 个。**

    15m 门槛   只看15m   +3日≥50%   +放量≥2倍
      ≥10%        4          0          0
      ≥40%        1          0          0

这不是条件写错了，是这个组合**本来就稀有**。稀有的东西做成命令 = 点开永远空白；
做成订阅 = 一个月响几次，每次都值得看一眼。所以这里只有订阅。

## 为什么 3 日涨幅不只是个门槛

同一根 15m 暴涨 K 线，配上不同的 3 日涨幅，意思完全相反。真机实测那天唯一
15m 涨过 40% 的币：

    VELVET   15m +54.9%   量比 3.2x   3日 -79.2%

它不是"拉升"，是砸了 79% 之后的超跌反弹。所以每条告警都带**位置标签**
（超跌反弹 / 横盘启动 / 顺势加速 / 末端逼空），而不是只甩一个百分比——
数字一样，该不该碰完全不一样。

## 取数：先闸便宜的，再拉贵的

15m 涨幅**白拿**（复用 pumpalert 每 60 秒那次全市场 ticker，不多打一轮接口）。
3 日涨幅和量比要各拉一次 K 线，**只对过了 15m 闸的币拉**——实测一天 0~1 个，
等于几乎不产生额外请求。反过来做（先给几百个币拉日线再筛）纯属浪费。

## 量比的口径

**只用已收盘的 K 线**（v1.33.1 的教训）：未收盘那根成交量只累积了一部分，
拿它比均量会系统性偏低，周期越短压得越狠。
这里取最近 3 根**已收盘**的 5m（正好 15 分钟）之和，比前 36 根 5m 的同长度均值。
"""
import logging
import time

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from storage import data, save_data
from handlers.util import safe_reply, safe_edit, escape_md

log = logging.getLogger(__name__)

BYBIT = "https://api.bybit.com"

DEF_M15 = 40.0        # 15 分钟滚动涨幅（%）
DEF_D3 = 50.0         # 3 日累计涨幅（%）
DEF_VOL = 2.0         # 量比（倍），0 = 不看量
DAYS = 3              # 「3 日」是几根已收盘日线
COOLDOWN = 6 * 3600   # 同币再报的冷却：这种形态一天内反复推没有信息量
VOL_BARS = 3          # 3 根 5m = 15 分钟
VOL_BASE = 36         # 基准：前 36 根 5m（3 小时）

M15_PRESETS = (20, 30, 40, 50)
D3_PRESETS = (0, 30, 50, 80)
VOL_PRESETS = (0, 1.5, 2, 3)


def _cfg(chat_id):
    return (data.get("pump3") or {}).get(str(chat_id))


def _defaults():
    return {"m15": DEF_M15, "d3": DEF_D3, "vol": DEF_VOL}


# ── 取数（只对过了 15m 闸的币调用）────────────────────────────
async def _context_for(client, sym):
    """→ (3日涨幅%, 量比) 或 None。sym 是基名，接口要 XXXUSDT。"""
    inst = f"{sym}USDT"
    try:
        d = await client.get(f"{BYBIT}/v5/market/kline", params={
            "category": "linear", "symbol": inst, "interval": "D",
            "limit": DAYS + 2})
        drows = (d.json().get("result") or {}).get("list") or []   # 新→旧
        if len(drows) < DAYS + 1:
            return None
        now_px = float(drows[0][4])
        base = float(drows[DAYS][4])
        if base <= 0:
            return None
        d3 = (now_px - base) / base * 100

        v = await client.get(f"{BYBIT}/v5/market/kline", params={
            "category": "linear", "symbol": inst, "interval": "5",
            "limit": VOL_BARS + VOL_BASE + 2})
        vrows = (v.json().get("result") or {}).get("list") or []
        # rows[0] 还在走，跳掉——用它算量比会系统性偏低
        closed = vrows[1:]
        if len(closed) < VOL_BARS + VOL_BASE:
            return d3, 0.0
        recent = sum(float(k[5]) for k in closed[:VOL_BARS])
        base_bars = closed[VOL_BARS:VOL_BARS + VOL_BASE]
        avg = sum(float(k[5]) for k in base_bars) / len(base_bars) * VOL_BARS
        return d3, (recent / avg if avg > 0 else 0.0)
    except Exception as e:
        log.debug(f"极端拉升取上下文失败 {sym}: {e}")
        return None


def position_tag(d3):
    """同一根暴涨 K 线，3 日涨幅不同意思完全不同——标签比数字更该被读到。"""
    if d3 <= -30:
        return "超跌反弹", "砸下来之后的反抽，不是新趋势"
    if d3 < 15:
        return "横盘启动", "之前一直趴着，这是第一波"
    if d3 < 50:
        return "顺势加速", "已经在涨，这一下是加速"
    return "末端逼空", "涨了三天还在加速，最容易在追进去之后见顶"


# ── 检查（由 pumpalert 的 60 秒任务调用，共用它取的那批行情）──
async def check(context, changes):
    """changes: {sym: (15m涨幅%, 现价)}，来自 pumpalert._compute_changes。"""
    subs = data.get("pump3") or {}
    if not subs or not changes:
        return
    # 便宜的闸：谁都过不了就直接收工，一个接口都不打
    floor = min(float((c or {}).get("m15", DEF_M15)) for c in subs.values())
    cand = {s: v for s, v in changes.items() if v[0] >= floor}
    if not cand:
        return

    ctx = {}
    async with httpx.AsyncClient(timeout=15) as client:
        for sym in list(cand)[:20]:        # 封顶，防极端行情下几十个币一起冲
            got = await _context_for(client, sym)
            if got:
                ctx[sym] = got

    now = time.time()
    recs = data.setdefault("pump3_alerted", {})
    dirty = False
    for chat_id, cfg in list(subs.items()):
        cfg = cfg or _defaults()
        m15_th = float(cfg.get("m15", DEF_M15))
        d3_th = float(cfg.get("d3", DEF_D3))
        vol_th = float(cfg.get("vol", DEF_VOL))
        seen = recs.setdefault(str(chat_id), {})
        hits = []
        for sym, (ch, px) in cand.items():
            if ch < m15_th or sym not in ctx:
                continue
            d3, vr = ctx[sym]
            if d3 < d3_th:
                continue
            if vol_th > 0 and vr < vol_th:
                continue
            if now - seen.get(sym, 0) < COOLDOWN:
                continue
            seen[sym] = now
            dirty = True
            hits.append((sym, ch, px, d3, vr))
        for s in [s for s, t in seen.items() if now - t > COOLDOWN * 2]:
            seen.pop(s, None)
            dirty = True
        if hits:
            try:
                await context.bot.send_message(
                    chat_id=int(chat_id), text=_msg(hits, cfg),
                    parse_mode="Markdown")
            except Exception as e:
                log.error(f"极端拉升推送失败 {chat_id}: {e}")
    if dirty:
        save_data()


def _msg(hits, cfg):
    lines = ["🚨 *极端拉升*　15m 暴拉 + 多日已涨一大截"]
    for sym, ch, px, d3, vr in sorted(hits, key=lambda h: -h[1]):
        tag, why = position_tag(d3)
        lines.append("")
        lines.append(f"*{escape_md(sym)}*　现价 {px:,.6g}")
        lines.append(f"　15m {ch:+.1f}%" + (f"　量比 {vr:.1f}×" if vr else ""))
        lines.append(f"　{DAYS}日累计 {d3:+.1f}%")
        lines.append(f"　位置：*{tag}* —— {why}")
    lines.append("")
    lines.append(f"你的门槛：15m≥{cfg.get('m15', DEF_M15):g}%"
                 f"　{DAYS}日≥{cfg.get('d3', DEF_D3):g}%"
                 + (f"　量比≥{cfg.get('vol', DEF_VOL):g}×"
                    if float(cfg.get("vol", DEF_VOL)) > 0 else "　不看量"))
    lines.append("同一个币 6 小时内不重复报。⚠️ 不构成投资建议")
    return "\n".join(lines)


# ── 自检：这种告警一个月响几次，"没响"和"坏了"看起来一模一样 ──
async def selftest(chat_id):
    """拿当前门槛现扫一遍，报"现在有没有"。返回给用户看的文本。

    这个功能必须有：命中率本来就低，没有自检的话，他分不清是行情没到
    还是功能坏了——静默失效是这个项目最贵的 bug 类型。
    """
    from handlers import pumpalert as PA
    cfg = _cfg(chat_id) or _defaults()
    try:
        perps = await PA._fetch_bybit_perps()
    except Exception as e:
        return f"取行情失败：{str(e)[:80]}"
    if not perps:
        return "取行情失败：没拿到永续列表"
    now = time.time()
    seen = PA._ingest(perps, now)
    changes = PA._compute_changes(seen, now)
    if not changes:
        ago = int(now - (PA._started_at or now))
        return ("⏳ 还在攒 15 分钟价格历史（进程重启后需要 15 分钟才有滚动涨幅）。\n"
                f"已运行 {ago} 秒，再等等。这期间不会误报。")
    m15_th = float(cfg.get("m15", DEF_M15))
    top = sorted(changes.items(), key=lambda kv: -kv[1][0])[:5]
    cand = {s: v for s, v in changes.items() if v[0] >= m15_th}

    lines = [f"🔍 *极端拉升自检*　（在场 {len(changes)} 个币）",
             f"你的门槛：15m≥{m15_th:g}%　{DAYS}日≥{cfg.get('d3', DEF_D3):g}%"
             + (f"　量比≥{cfg.get('vol', DEF_VOL):g}×"
                if float(cfg.get("vol", DEF_VOL)) > 0 else "　不看量")]
    lines.append("")
    lines.append("*当前 15m 涨幅最大的 5 个*")
    for s, (ch, px) in top:
        lines.append(f"　{escape_md(s)}　{ch:+.1f}%")
    lines.append("")
    if not cand:
        lines.append(f"过 15m 门槛的：*0 个*。功能是好的，只是行情没到——"
                     f"这个组合实测一天也就 0~1 个币碰得到。")
        return "\n".join(lines)

    ctx = {}
    async with httpx.AsyncClient(timeout=15) as client:
        for sym in list(cand)[:10]:
            got = await _context_for(client, sym)
            if got:
                ctx[sym] = got
    lines.append(f"过 15m 门槛的 *{len(cand)} 个*，逐个看它们的 {DAYS} 日和量比：")
    fired = 0
    for sym, (ch, px) in cand.items():
        if sym not in ctx:
            lines.append(f"　{escape_md(sym)}　15m {ch:+.1f}%　（取不到日线）")
            continue
        d3, vr = ctx[sym]
        ok_d3 = d3 >= float(cfg.get("d3", DEF_D3))
        ok_v = float(cfg.get("vol", DEF_VOL)) <= 0 or vr >= float(cfg.get("vol", DEF_VOL))
        mark = "✅" if (ok_d3 and ok_v) else "❌"
        fired += 1 if (ok_d3 and ok_v) else 0
        tag, _why = position_tag(d3)
        lines.append(f"　{mark} {escape_md(sym)}　15m {ch:+.1f}%　"
                     f"{DAYS}日 {d3:+.1f}%　量比 {vr:.1f}×　{tag}")
    lines.append("")
    lines.append(f"会触发推送的：*{fired} 个*")
    return "\n".join(lines)


# ── 面板 ────────────────────────────────────────────────────
def panel(chat_id):
    cfg = _cfg(chat_id)
    on = cfg is not None
    c = cfg or _defaults()
    vol = float(c.get("vol", DEF_VOL))
    txt = (
        "🚨 *极端拉升告警*\n\n"
        f"15 分钟内暴拉 **且** {DAYS} 日累计已经涨了一大截——两个条件**同时**满足才推。\n\n"
        f"当前：{'✅ 已开启' if on else '⭕️ 未开启'}\n"
        f"　15m 涨幅　≥ *{c.get('m15', DEF_M15):g}%*\n"
        f"　{DAYS}日累计　≥ *{c.get('d3', DEF_D3):g}%*\n"
        f"　量比　　　{'≥ *' + format(vol, 'g') + '×*' if vol > 0 else '*不看*'}\n\n"
        "这个组合**很稀有**——实测一天也就 0~1 个币碰得到，"
        "所以它平时是安静的。想确认功能没坏，点【🔍 现在有没有】。\n"
        "同一个币 6 小时内不重复报。"
    )
    rows = [
        [InlineKeyboardButton(f"{'✅' if x == c.get('m15', DEF_M15) else ''}15m≥{x}%",
                              callback_data=f"p3:m15:{x}") for x in M15_PRESETS],
        [InlineKeyboardButton(
            f"{'✅' if x == c.get('d3', DEF_D3) else ''}"
            + (f"{DAYS}日不限" if x == 0 else f"{DAYS}日≥{x}%"),
            callback_data=f"p3:d3:{x}") for x in D3_PRESETS],
        [InlineKeyboardButton(
            f"{'✅' if x == vol else ''}" + ("不看量" if x == 0 else f"量比≥{x:g}×"),
            callback_data=f"p3:vol:{x}") for x in VOL_PRESETS],
        [InlineKeyboardButton("🔴 关闭告警" if on else "🟢 开启告警",
                              callback_data="p3:toggle")],
        [InlineKeyboardButton("🔍 现在有没有（自检）", callback_data="p3:test")],
        [InlineKeyboardButton("⬅️ 返回", callback_data="cat_notify")],
    ]
    return txt, InlineKeyboardMarkup(rows)


async def pump3_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/pump3 —— 极端拉升告警（15m 暴拉 + 多日已涨），全按钮设置。"""
    chat_id = update.effective_chat.id
    args = context.args or []
    if args:
        # /pump3 on|off 和 /pump3 40 50 [2] 也认——他有时候懒得点
        low = [a.lower() for a in args]
        if "off" in low or "关" in low:
            (data.get("pump3") or {}).pop(str(chat_id), None)
            save_data()
            await safe_reply(update.message, "⭕️ 极端拉升告警已关闭")
            return
        nums = []
        for a in args:
            try:
                nums.append(float(a))
            except ValueError:
                pass
        if nums:
            cfg = _defaults()
            cfg["m15"] = nums[0]
            if len(nums) > 1:
                cfg["d3"] = nums[1]
            if len(nums) > 2:
                cfg["vol"] = nums[2]
            data.setdefault("pump3", {})[str(chat_id)] = cfg
            save_data()
        elif "on" in low or "开" in low:
            data.setdefault("pump3", {}).setdefault(str(chat_id), _defaults())
            save_data()
    txt, kb = panel(chat_id)
    await safe_reply(update.message, txt, reply_markup=kb, parse_mode="Markdown")


async def on_button(query, context):
    chat_id = query.message.chat_id if query.message else query.from_user.id
    bits = (query.data or "").split(":")
    act = bits[1] if len(bits) > 1 else ""
    subs = data.setdefault("pump3", {})
    key = str(chat_id)

    if act == "toggle":
        if key in subs:
            subs.pop(key)
            await query.answer("已关闭")
        else:
            subs[key] = _defaults()
            await query.answer("已开启")
        save_data()
    elif act in ("m15", "d3", "vol"):
        cfg = subs.setdefault(key, _defaults())   # 调门槛即视为开启
        cfg[act] = float(bits[2])
        save_data()
        await query.answer("已设置")
    elif act == "test":
        await query.answer("现扫一遍…")
        try:
            txt = await selftest(chat_id)
        except Exception as e:
            log.error(f"极端拉升自检出错: {e}")
            txt = f"自检失败：{str(e)[:100]}"
        await safe_reply(query.message, txt, parse_mode="Markdown")
        return
    txt, kb = panel(chat_id)
    await safe_edit(query, txt, reply_markup=kb, parse_mode="Markdown")
