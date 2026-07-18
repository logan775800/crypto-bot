"""交易计划 —— 分析到执行的闭环脊柱。

为什么是一个「对象」而不是一段文本：用户要的三件事其实是同一个东西的三个面——
  • 一屏能执行的交易卡（不用从长文里翻价格）
  • 计划直接预填到下单页（不用手抄）
  • 计划自动失效（昨晚的计划今天可能已经害人）
它们都需要计划是**持久化、有状态、可被后台盯着**的。所以先有 Plan，再有那三个面。

状态机（只允许这些流转，别的都是 bug）：
    waiting  ──触发价成交──▶ triggered ──TP1──▶ partial ──▶ done
       │                        │                  │
       └──失效条件/过期──▶ invalid / expired ◀──────┘
  archived 是人手动归档，任何状态都能进。

「有效跌破」一律用**收盘价**判断，不用插针的影线——这是这类计划最容易假触发的地方。
"""
import time
import logging

from storage import data, save_data
from handlers.util import safe_reply, safe_edit, escape_md
from handlers import marketdata as md

log = logging.getLogger(__name__)

DEFAULT_TTL = 48 * 3600      # 计划默认 48 小时过期——行情早变了
MAX_PER_CHAT = 15
CHECK_IV = "5m"              # 触发/失效判定用的收盘周期

STATUS = {
    "waiting": ("⏳", "等待触发"),
    "triggered": ("🎯", "已触发"),
    "partial": ("💰", "部分止盈"),
    "invalid": ("❌", "已失效"),
    "expired": ("🕘", "已过期"),
    "done": ("✅", "已完成"),
    "archived": ("📁", "已归档"),
}
LIVE = ("waiting", "triggered", "partial")     # 还需要后台盯的状态


# ── 存取 ───────────────────────────────────────────────────────────
def _all():
    return data.setdefault("plans", [])


def get(pid):
    for p in _all():
        if p.get("id") == pid:
            return p
    return None


def mine(chat_id, include_dead=False):
    ps = [p for p in _all() if p.get("chat_id") == chat_id]
    if not include_dead:
        ps = [p for p in ps if p.get("status") in LIVE]
    return sorted(ps, key=lambda x: -x.get("created", 0))


def new_id():
    n = data.get("plan_seq", 0) + 1
    data["plan_seq"] = n
    return f"p{n}"


def save(plan):
    plan["updated"] = time.time()
    if not get(plan.get("id")):
        _all().append(plan)
    save_data()
    return plan


# ── 渲染：一屏能执行的交易卡 ────────────────────────────────────────
def _fmt_zone(z):
    if not z:
        return "—"
    if isinstance(z, (list, tuple)):
        if len(z) >= 2 and z[0] != z[1]:
            return f"{md.f(min(z))} – {md.f(max(z))}"
        return md.f(z[0])
    return md.f(z)


def _temp_bar(t):
    """风险温度 1-10 → 直观的条。数字本身没有实感。"""
    try:
        t = int(t)
    except (TypeError, ValueError):
        return ""
    t = max(1, min(t, 10))
    return "🟥" * (t // 2) + ("🟧" if t % 2 else "") + "⬜" * ((10 - t) // 2)


def card(p, with_meta=True):
    """交易卡。目标：不用往上翻、不用再算，照着就能下单。"""
    emoji, label = STATUS.get(p.get("status", "waiting"), ("❔", "?"))
    short = p["symbol"].replace("USDT", "")
    side_txt = "做空 📉" if p["side"] == "short" else "做多 📈"
    lines = [
        f"*{escape_md(short)}｜当前：{label}* {emoji}",
        f"{side_txt}｜风险温度 *{p.get('risk_temp','?')}/10* {_temp_bar(p.get('risk_temp'))}"
        + (f"｜{escape_md(p['note'])}" if p.get("note") else ""),
        "━━━━━━━━━━━━━━",
    ]
    tr = p.get("trigger") or {}
    lines.append(f"*触发*　{escape_md(tr.get('desc') or '—')}")
    lines.append(f"*入场*　{_fmt_zone(p.get('entry'))}")
    lines.append(f"*止损*　{md.f(p.get('stop'))}"
                 + (f"（距入场 {p['stop_pct']:.2f}%）" if p.get("stop_pct") else ""))
    lo, hi = min(p["entry"]), max(p["entry"])
    mid = (lo + hi) / 2
    for i, tp in enumerate(p.get("tps") or [], 1):
        extra = []
        r = rr(mid, p.get("stop"), tp.get("price"))
        if r is not None:
            extra.append(f"R:R {r:.2f}")
        if tp.get("pct"):
            extra.append(f"平 {tp['pct']:g}%")
        if tp.get("note"):
            extra.append(escape_md(tp["note"]))
        lines.append(f"*TP{i}*　{md.f(tp.get('price'))}"
                     + (f"（{'，'.join(extra)}）" if extra else ""))
    inv = p.get("invalid") or {}
    lines.append(f"*失效*　{escape_md(inv.get('desc') or '—')}")

    # 盈亏比是「这单值不值得做」最直接的数字，差就要刺眼
    if p.get("rr_final") is not None and p["rr_final"] < LOW_RR:
        lines.append("")
        lines.append(f"⚠️ *末段盈亏比只有 {p['rr_final']:.2f}* —— 冒 "
                     f"{md.f(abs(mid - p['stop']))} 去赚 "
                     f"{md.f(abs(p['tps'][-1]['price'] - mid))}。")
        lines.append("_即使胜率 60% 这单的期望也是负的。要么等更好的入场位，要么放弃。_")

    if p.get("status") == "invalid":
        lines.append("━━━━━━━━━━━━━━")
        lines.append(f"❌ *此计划已失效*：{escape_md(p.get('invalid_reason') or '条件已破坏')}")
        lines.append("_不要再按这份计划挂单_")
    elif p.get("status") == "expired":
        lines.append("━━━━━━━━━━━━━━")
        lines.append("🕘 *已过期*（超过有效期，行情已变）。要用请 `/replan` 重新生成")

    if with_meta:
        lines.append("━━━━━━━━━━━━━━")
        age = time.time() - p.get("created", time.time())
        lines.append(f"`{p['id']}`｜生成于 {time.strftime('%m-%d %H:%M', time.localtime(p.get('created', 0)))}"
                     f"（{age/3600:.1f} 小时前）")
        dm = p.get("data_meta") or {}
        if dm.get("completeness") is not None and dm["completeness"] < 100:
            lines.append(f"⚠️ 生成时数据完整度 {dm['completeness']:.0f}%"
                         f"（缺：{'、'.join(dm.get('missing') or [])}）—— 该计划的精确度已打折")
    lines.append("\n⚠️ 不构成投资建议")
    return "\n".join(lines)


def kb(p):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    pid = p["id"]
    rows = []
    if p.get("status") in LIVE:
        rows.append([
            InlineKeyboardButton("🔔 设触发提醒", callback_data=f"pl:alert:{pid}"),
            InlineKeyboardButton("🧮 生成下单参数", callback_data=f"pl:size:{pid}"),
        ])
        rows.append([
            InlineKeyboardButton("📐 看图", callback_data=f"pl:chart:{pid}"),
            InlineKeyboardButton("🔄 刷新校验", callback_data=f"pl:refresh:{pid}"),
        ])
        rows.append([
            InlineKeyboardButton("🎛 交易台", callback_data="tpanel"),
            InlineKeyboardButton("📁 归档", callback_data=f"pl:arch:{pid}"),
        ])
    else:
        rows.append([
            InlineKeyboardButton("🔁 重新生成", callback_data=f"pl:re:{pid}"),
            InlineKeyboardButton("📋 我的计划", callback_data="pl:list"),
        ])
    return InlineKeyboardMarkup(rows)


# ── AI 生成：强制结构化输出 ─────────────────────────────────────────
PLAN_TOOL = [{
    "type": "function",
    "function": {
        "name": "submit_plan",
        "description": "提交一份可执行的交易计划。所有价位必须来自你实际取到的数据，不得编造。",
        "parameters": {
            "type": "object",
            "properties": {
                "side": {"type": "string", "enum": ["long", "short"],
                         "description": "计划方向"},
                "risk_temp": {"type": "integer", "minimum": 1, "maximum": 10,
                              "description": "风险温度1-10。逆势/高波动/数据不全都要调高"},
                "note": {"type": "string",
                         "description": "一句话定性，如「逆势空，轻仓」「顺势回踩，可正常仓」"},
                "trigger_desc": {"type": "string",
                                 "description": "触发条件的人话描述，要具体到周期和价位，"
                                                "如「5m 有效跌破 0.0806，反抽不能站回 0.0815」"},
                "trigger_price": {"type": "number",
                                  "description": "触发的关键价位（用于后台自动盯）"},
                "trigger_mode": {"type": "string",
                                 "enum": ["breakdown", "breakout", "zone", "retest"],
                                 "description": "breakdown=跌破确认 breakout=突破确认 "
                                                "zone=进入区间 retest=回踩确认"},
                "entry_low": {"type": "number", "description": "入场区间下沿"},
                "entry_high": {"type": "number", "description": "入场区间上沿"},
                "stop": {"type": "number", "description": "止损价，必须在结构失效位之外"},
                "tps": {
                    "type": "array",
                    "description": "分段止盈，1~3 个，按先后顺序",
                    "items": {"type": "object", "properties": {
                        "price": {"type": "number"},
                        "pct": {"type": "number", "description": "该段平仓百分比"},
                        "note": {"type": "string", "description": "如「止损保本」"},
                    }, "required": ["price"]},
                },
                "invalid_desc": {"type": "string",
                                 "description": "计划失效条件的人话描述"},
                "invalid_price": {"type": "number",
                                  "description": "失效的关键价位（用于后台自动判失效）"},
                "reasoning": {"type": "string",
                              "description": "200字内的理由：结构+数据依据。数据有缺失必须在这说明"},
            },
            "required": ["side", "risk_temp", "trigger_desc", "trigger_price",
                         "trigger_mode", "entry_low", "entry_high", "stop",
                         "tps", "invalid_desc", "invalid_price", "reasoning"],
        },
    },
}]

SYSTEM = (
    "你是加密永续合约的交易计划生成器。用户是做杠杆的活跃交易者。"
    "根据给你的数据，产出一份**可直接执行**的条件计划，并调用 submit_plan 提交。\n\n"
    "硬规则：\n"
    "1. 所有价位**必须来自给你的数据**（结构位、前高前低、EMA、订单簿、ATR）。"
    "一个编造的价位比没有计划危险得多——用户会照着它挂单。\n"
    "2. 止损必须放在**结构失效位之外**（用摆动高低点或 1.5×ATR），不是拍脑袋的整数。\n"
    "3. 触发条件要具体到**周期 + 价位 + 确认方式**，别写「跌破就空」这种没法执行的话。\n"
    "4. 失效条件必须给，且要能用价格判定——它是这份计划的保质期。\n"
    "5. 数据状态里说某维度缺失时：在 reasoning 里明说，并把 risk_temp 调高；"
    "缺得多就直接说这单只能给方向不给精确位。\n"
    "6. 逆势单、BTC 方向不利、资金费拥挤、数据不全 → risk_temp 往高调（7-10）。\n"
    "7. 只给一个方向的计划——用户已经指定了方向就按那个方向做；"
    "如果那个方向明显是逆势，照做但把 risk_temp 拉满并在 note 里写「逆势」。\n"
    "8. **盈亏比**：最后一个 TP 相对入场中值的盈亏比（赚的距离÷亏的距离）要 ≥1.5。"
    "如果按结构位算下来盈亏比不足 1，说明这个位置根本不该进——"
    "那就把入场区挪到更好的位置（等回踩/等反抽），而不是硬凑一个近的 TP。\n"
    "9. **分段止盈要拉开**：两个 TP 至少相差 0.5%，否则就是同一个位置，分段没有意义。"
    "TP 要落在有意义的位置上（前高前低、流动性密集区、结构位），不是随手取的整数。\n"
    "10. 止损距离别太窄：至少 1×ATR，否则正常波动就会把你扫掉。"
)


async def generate(symbol, side, chat_id, uid):
    """拉数 → AI 出结构化计划 → 校验 → 落库。返回 (plan, 数据报告) 或抛异常。"""
    from config import AI_API_KEY, AI_BASE_URL
    if not AI_API_KEY or not AI_BASE_URL:
        raise RuntimeError("AI 未配置（缺 AI_API_KEY / AI_BASE_URL）")
    from handlers import datameta

    sym = md.norm(symbol)
    rep = await datameta.probe(sym)
    if rep.invalid_symbol:
        raise ValueError(f"Bybit 没有 {sym} 这个永续合约")

    parts = [rep.for_ai()]
    for iv in ("4h", "1h", "15m", "5m"):
        if rep.klines.get(iv, (False, ""))[0]:
            try:
                parts.append(await md.klines_analysis(sym, iv))
            except Exception as e:
                log.warning(f"plan {sym} {iv} K线失败: {e}")
    for fn, name in ((md.market_context(), "市场联动"),
                     (md.funding_analysis(sym), "资金费"),
                     (md.oi_analysis(sym, "15m"), "OI"),
                     (md.orderbook_analysis(sym), "订单簿")):
        try:
            parts.append(await fn)
        except Exception as e:
            log.warning(f"plan {sym} {name} 失败: {e}")
            parts.append(f"⚠️ {name}本次取数失败，不可据此下结论")

    from handlers.ai import ask_ai_struct
    args = await ask_ai_struct(
        [{"role": "user", "content":
          f"给我一份 {sym} 的{'做空' if side == 'short' else '做多'}计划。\n\n"
          + "\n\n".join(parts)}],
        PLAN_TOOL, "submit_plan", system=SYSTEM)

    p = _from_ai(args, sym, side, chat_id, uid, rep)
    return save(p), rep


MIN_TP_GAP_PCT = 0.15    # 两个止盈位挨得比这还近就是同一个位置，分段没有意义
LOW_RR = 1.0             # 末段盈亏比低于它 = 这单的数学期望本身就不划算
# 止损相对入场的最小距离。理想下限是 1×ATR，但校验层没有K线数据算不了 ATR，
# 所以用一个「无论如何都太窄了」的百分比兜底：实测模型给过 0.11% 的止损，
# 正常盘口噪声就能扫掉。真正的 ATR 约束交给 SYSTEM 提示。
MIN_STOP_PCT = 0.3


def rr(entry_mid, stop, tp):
    """盈亏比 = 赚的距离 / 亏的距离。计划好不好，这个数比任何指标都直接。"""
    risk = abs(entry_mid - stop)
    if risk <= 0:
        return None
    return abs(tp - entry_mid) / risk


def _clean_tps(raw, side, entry_lo, entry_hi, stop, mid):
    """清洗 AI 给的止盈位。实测发现模型会给出 63,683 和 63,698 这种只差 0.02%
    的两个「分段」止盈——等于同一个价位，分段止盈就成了摆设。这里：
      1) 丢掉方向反了的（做空的止盈却在入场之上）
      2) 按离场顺序排好（做空从高到低，做多从低到高）
      3) 挨太近的合并——保留先到的那个
    """
    out = []
    for t in (raw or []):
        try:
            px = float(t["price"])
        except (KeyError, TypeError, ValueError):
            continue
        if side == "short" and px >= entry_lo:
            continue
        if side == "long" and px <= entry_hi:
            continue
        out.append({"price": px, "pct": t.get("pct"), "note": t.get("note")})
    # 离场顺序：做空先到的是价格高的，做多先到的是价格低的
    out.sort(key=lambda x: x["price"], reverse=(side == "short"))
    merged = []
    for t in out:
        if merged and abs(t["price"] - merged[-1]["price"]) / merged[-1]["price"] * 100 < MIN_TP_GAP_PCT:
            continue     # 和上一个几乎同价，丢掉
        merged.append(t)
    return merged[:3]


def _from_ai(a, sym, side, chat_id, uid, rep=None):
    """AI 返回的参数 → Plan。这里做校验：模型可能给出自相矛盾的价位。"""
    entry_lo = float(a["entry_low"])
    entry_hi = float(a["entry_high"])
    if entry_lo > entry_hi:
        entry_lo, entry_hi = entry_hi, entry_lo
    stop = float(a["stop"])
    mid = (entry_lo + entry_hi) / 2
    # 方向自洽：做空的止损必须在入场之上，做多必须在之下。模型偶尔会写反，
    # 写反的话止损变成止盈，是会真亏钱的错误——宁可拒绝也不能放过去。
    if side == "short" and stop <= entry_hi:
        raise ValueError(f"AI 给的止损 {md.f(stop)} 不在入场区 {md.f(entry_hi)} 之上（做空止损必须在上方），已拒绝")
    if side == "long" and stop >= entry_lo:
        raise ValueError(f"AI 给的止损 {md.f(stop)} 不在入场区 {md.f(entry_lo)} 之下（做多止损必须在下方），已拒绝")
    # 止损太窄：正常波动就会被扫掉，是一份注定「被止损又看对方向」的计划
    stop_pct = abs(stop - mid) / mid * 100 if mid else 0
    if stop_pct < MIN_STOP_PCT:
        raise ValueError(
            f"AI 给的止损距入场只有 {stop_pct:.2f}%（{md.f(mid)}→{md.f(stop)}），"
            f"太窄，正常波动就会打掉。已拒绝——发 /replan 重出，或手动放宽止损。")

    tps = _clean_tps(a.get("tps"), side, entry_lo, entry_hi, stop, mid)
    if not tps:
        raise ValueError("AI 没给出方向自洽的止盈位，已拒绝")

    now = time.time()
    return {
        "id": new_id(), "chat_id": chat_id, "uid": uid,
        "symbol": sym, "side": side, "status": "waiting",
        "created": now, "updated": now, "expires": now + DEFAULT_TTL,
        "risk_temp": int(a.get("risk_temp") or 5),
        "note": a.get("note") or "",
        "reasoning": a.get("reasoning") or "",
        "trigger": {"desc": a["trigger_desc"], "price": float(a["trigger_price"]),
                    "mode": a.get("trigger_mode") or "zone"},
        "entry": [entry_lo, entry_hi],
        "stop": stop,
        "stop_pct": abs(stop - mid) / mid * 100 if mid else None,
        "tps": tps,
        "rr_final": rr(mid, stop, tps[-1]["price"]),   # 末段盈亏比：这单的数学期望
        "rr_first": rr(mid, stop, tps[0]["price"]),
        "invalid": {"desc": a["invalid_desc"], "price": float(a["invalid_price"]),
                    # 失效方向：做空计划被「站回上方」证伪，做多被「跌破下方」证伪
                    "dir": "above" if side == "short" else "below"},
        "data_meta": ({"completeness": rep.completeness, "missing": rep.missing}
                      if rep else {}),
        "hit_tps": [],
    }


# ════════════════════════════════════════════════════════════════════
#  生命周期 —— 「昨晚的计划今天可能已经害人」的解药
# ════════════════════════════════════════════════════════════════════
def evaluate(p, close, high, low, now=None):
    """给定最新一根收盘K线，判断这份计划该转成什么状态。

    纯函数（不碰网络不碰存储），因为状态机判错的代价是「用户按已失效的计划下单」。
    返回 (新状态|None, 推送文案|None)。None 表示不变。

    「有效跌破/站回」一律用**收盘价**判定，不用影线——插针假触发是这类计划最大的坑。
    """
    now = now or time.time()
    st = p.get("status")
    if st not in LIVE:
        return None, None

    short = p["symbol"].replace("USDT", "")
    side = p["side"]
    inv = p.get("invalid") or {}
    inv_px = inv.get("price")

    # 1) 失效优先于一切——已经证伪的计划不该再谈触发或止盈
    if inv_px:
        broken = (close > inv_px) if inv.get("dir") == "above" else (close < inv_px)
        if broken:
            return "invalid", (
                f"❌ *{escape_md(short)} 计划已失效*　`{p['id']}`\n"
                f"{escape_md(inv.get('desc') or '')}\n"
                f"现价 {md.f(close)} 已{'站稳' if inv.get('dir') == 'above' else '跌破'} "
                f"{md.f(inv_px)}（{CHECK_IV} 收盘确认），原逻辑不成立。\n"
                f"*请勿继续按旧计划挂单。* 要重做发 `/replan {p['id']}`")

    # 2) 过期
    if p.get("expires") and now > p["expires"]:
        return "expired", (
            f"🕘 *{escape_md(short)} 计划已过期*　`{p['id']}`\n"
            f"生成于 {(now - p.get('created', now))/3600:.0f} 小时前，行情大概率已变。\n"
            f"要用请 `/replan {p['id']}` 重新校验。")

    # 3) 止盈（已触发的计划才有意义）
    if st in ("triggered", "partial"):
        for i, tp in enumerate(p.get("tps") or []):
            if i in (p.get("hit_tps") or []):
                continue
            px = tp.get("price")
            if px is None:
                continue
            hit = (low <= px) if side == "short" else (high >= px)
            if hit:
                p.setdefault("hit_tps", []).append(i)
                last = len(p.get("hit_tps", [])) >= len(p.get("tps") or [])
                extra = f"　计划动作：平 {tp['pct']:g}%" if tp.get("pct") else ""
                if tp.get("note"):
                    extra += f"，{escape_md(tp['note'])}"
                return ("done" if last else "partial"), (
                    f"💰 *{escape_md(short)} TP{i+1} 触及*　`{p['id']}`\n"
                    f"{md.f(px)} 已到（现价 {md.f(close)}）。{extra}\n"
                    + ("全部止盈位已走完。" if last else "记得把止损挪到保本。"))

    # 4) 触发（等待中的计划）
    if st == "waiting":
        tr = p.get("trigger") or {}
        tpx = tr.get("price")
        mode = tr.get("mode")
        lo, hi = min(p["entry"]), max(p["entry"])
        fired = False
        if mode == "breakdown" and tpx:
            fired = close < tpx
        elif mode == "breakout" and tpx:
            fired = close > tpx
        else:                       # zone / retest：价格进入入场区
            fired = lo <= close <= hi
        if fired:
            return "triggered", (
                f"🎯 *{escape_md(short)} 计划已触发*　`{p['id']}`\n"
                f"{escape_md(tr.get('desc') or '')}\n"
                f"现价 {md.f(close)}（{CHECK_IV} 收盘）。\n"
                f"入场区 {_fmt_zone(p['entry'])}｜止损 {md.f(p['stop'])}\n"
                f"⚠️ 是否入场由你定——触发≠必须做。")
    return None, None


async def check_plans(context):
    """后台 job：按币聚合取一次 5m K线，驱动所有活计划的状态机。"""
    live = [p for p in _all() if p.get("status") in LIVE]
    if not live:
        return
    now = time.time()
    changed = False
    bars = {}
    for sym in {p["symbol"] for p in live}:
        try:
            r = await md._get("/v5/market/kline", {
                "category": md.CAT, "symbol": sym,
                "interval": md.INTERVALS[CHECK_IV], "limit": 2})
            rows = r.get("list") or []
            # Bybit 新→旧；[0] 是**进行中**那根，用 [1] 已收盘的那根做判定
            if len(rows) >= 2:
                x = rows[1]
                bars[sym] = (float(x[4]), float(x[2]), float(x[3]))
        except Exception as e:
            log.warning(f"计划盯盘取 {sym} K线失败: {e}")

    for p in live:
        b = bars.get(p["symbol"])
        if not b:
            # 取不到数据时只判过期（过期不需要价格），绝不猜价格
            if p.get("expires") and now > p["expires"]:
                p["status"] = "expired"
                changed = True
            continue
        close, high, low = b
        try:
            new_st, msg = evaluate(p, close, high, low, now)
        except Exception as e:
            log.error(f"计划 {p.get('id')} 状态机出错: {e}")
            continue
        if not new_st:
            continue
        p["status"] = new_st
        p["updated"] = now
        if new_st == "invalid":
            p["invalid_reason"] = (p.get("invalid") or {}).get("desc") or "条件已破坏"
        changed = True
        if msg:
            try:
                await context.bot.send_message(
                    chat_id=p["chat_id"], text=msg, parse_mode="Markdown",
                    reply_markup=kb(p))
            except Exception as e:
                log.error(f"计划状态推送失败 {p.get('chat_id')}: {e}")
    if changed:
        save_data()


# ════════════════════════════════════════════════════════════════════
#  命令层
# ════════════════════════════════════════════════════════════════════
USAGE = (
    "📋 *交易计划* —— 一屏能执行，且会自己失效\n\n"
    "`/plan BANK short`　生成空头条件计划\n"
    "`/plan BTC long`　多头计划\n\n"
    "给出：触发条件 / 入场区 / 止损 / 分段止盈 / **失效条件**，"
    "并在后台按 5m 收盘盯着——触发、止盈触及、失效、过期都会主动推给你。\n\n"
    "`/plans` 我的计划　`/replan p3` 重新校验　`/delplan p3` 删除\n"
    "计划默认 48 小时过期（行情早变了）。"
)


async def plan_cmd(update, context):
    """/plan BANK short"""
    args = context.args or []
    if len(args) < 1:
        await safe_reply(update.message, USAGE, parse_mode="Markdown")
        return
    symbol = args[0].upper().replace("USDT", "")
    side = "long"
    if len(args) > 1:
        s = args[1].lower()
        if s in ("short", "空", "做空", "s"):
            side = "short"
        elif s in ("long", "多", "做多", "l"):
            side = "long"
        else:
            await safe_reply(update.message, "方向填 long/多 或 short/空")
            return
    chat_id = update.effective_chat.id
    if len(mine(chat_id)) >= MAX_PER_CHAT:
        await safe_reply(update.message,
            f"活跃计划已达 {MAX_PER_CHAT} 个，先 /plans 归档几个")
        return
    await safe_reply(update.message,
        f"📋 生成 {symbol} {'空头' if side == 'short' else '多头'}计划中…"
        f"（体检数据 + 多周期结构 + 资金费/OI/盘口，约 30~60 秒）")
    try:
        p, rep = await generate(symbol, side, chat_id,
                               update.effective_user.id if update.effective_user else 0)
    except ValueError as e:      # 校验没过：把原因如实说清，不给半成品
        await safe_reply(update.message, f"❌ {e}")
        return
    except Exception as e:
        log.error(f"生成计划出错 {symbol}: {e}")
        await safe_reply(update.message, f"❌ 生成失败：{str(e)[:140]}")
        return
    head = rep.header() + "\n\n" if rep else ""
    await safe_reply(update.message, head + card(p), reply_markup=kb(p),
                     parse_mode="Markdown")
    if p.get("reasoning"):
        await safe_reply(update.message, f"🧠 *理由*\n{escape_md(p['reasoning'])}",
                         parse_mode="Markdown")


async def plans_cmd(update, context):
    """/plans —— 我的计划（默认只列活的）。"""
    chat_id = update.effective_chat.id
    show_all = bool(context.args and context.args[0].lower() in ("all", "全部"))
    ps = mine(chat_id, include_dead=show_all)
    if not ps:
        await safe_reply(update.message,
            "还没有交易计划。\n\n" + USAGE, parse_mode="Markdown")
        return
    lines = [f"📋 *我的计划*（{'全部' if show_all else '活跃'} {len(ps)} 份）\n"]
    for p in ps:
        emoji, label = STATUS.get(p.get("status"), ("❔", "?"))
        short = p["symbol"].replace("USDT", "")
        side = "空" if p["side"] == "short" else "多"
        age = (time.time() - p.get("created", 0)) / 3600
        lines.append(f"{emoji} `{p['id']}` {escape_md(short)} {side}"
                     f"｜{label}｜温度{p.get('risk_temp','?')}/10｜{age:.0f}h前")
    lines.append(f"\n看详情：`/plan` 后点按钮，或 `/replan <id>` 重新校验")
    if not show_all:
        lines.append("`/plans all` 看含已失效/过期的全部")
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    rows = [[InlineKeyboardButton(f"{STATUS.get(p['status'],('',''))[0]} {p['id']} "
                                  f"{p['symbol'].replace('USDT','')}",
                                  callback_data=f"pl:show:{p['id']}")]
            for p in ps[:8]]
    await safe_reply(update.message, "\n".join(lines),
                     reply_markup=InlineKeyboardMarkup(rows) if rows else None,
                     parse_mode="Markdown")


async def replan_cmd(update, context):
    """/replan p3 —— 用同币同方向重新生成一份（旧的归档）。"""
    if not context.args:
        await safe_reply(update.message, "用法：`/replan p3`（id 看 /plans）",
                         parse_mode="Markdown")
        return
    old = get(context.args[0])
    if not old or old.get("chat_id") != update.effective_chat.id:
        await safe_reply(update.message, "没找到这份计划（id 看 /plans）")
        return
    old["status"] = "archived"
    save_data()
    context.args = [old["symbol"].replace("USDT", ""), old["side"]]
    await plan_cmd(update, context)


async def delplan_cmd(update, context):
    if not context.args:
        await safe_reply(update.message, "用法：`/delplan p3`　全删：`/delplan all`",
                         parse_mode="Markdown")
        return
    chat_id = update.effective_chat.id
    ps = _all()
    if context.args[0].lower() in ("all", "全部"):
        n = len([p for p in ps if p.get("chat_id") == chat_id])
        ps[:] = [p for p in ps if p.get("chat_id") != chat_id]
        save_data()
        await safe_reply(update.message, f"已删除全部 {n} 份计划")
        return
    p = get(context.args[0])
    if not p or p.get("chat_id") != chat_id:
        await safe_reply(update.message, "没找到这份计划")
        return
    ps.remove(p)
    save_data()
    await safe_reply(update.message, f"✅ 已删除 `{p['id']}`", parse_mode="Markdown")


# ── 按钮 ───────────────────────────────────────────────────────────
async def button(query, context, action, pid=None):
    p = get(pid) if pid else None
    if pid and not p:
        await query.answer("计划不存在（可能已删除）", show_alert=True)
        return

    if action == "show":
        await safe_edit(query, card(p), reply_markup=kb(p), parse_mode="Markdown")

    elif action == "list":
        chat_id = query.message.chat_id
        ps = mine(chat_id)
        if not ps:
            await safe_edit(query, "没有活跃计划。`/plan BANK short` 生成一份",
                            parse_mode="Markdown")
            return
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        rows = [[InlineKeyboardButton(
            f"{STATUS.get(x['status'],('',''))[0]} {x['id']} {x['symbol'].replace('USDT','')}",
            callback_data=f"pl:show:{x['id']}")] for x in ps[:8]]
        await safe_edit(query, f"📋 *我的活跃计划*（{len(ps)}）",
                        reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown")

    elif action == "arch":
        p["status"] = "archived"
        save_data()
        await query.answer("已归档")
        await safe_edit(query, card(p), reply_markup=kb(p), parse_mode="Markdown")

    elif action == "alert":
        # 把计划的触发价接到已有的条件提醒上——不另造一套盯盘
        from handlers.condalert import parse_cond, rule_text
        tr = p.get("trigger") or {}
        px = tr.get("price")
        if not px:
            await query.answer("这份计划没有可盯的触发价", show_alert=True)
            return
        op = "<" if p["side"] == "short" else ">"
        rules = data.setdefault("cond_alerts", [])
        rule = {"chat_id": query.message.chat_id, "symbol": p["symbol"].replace("USDT", ""),
                "conds": [parse_cond(f"{op}{px:g}")],
                "set_by": "计划 " + p["id"], "last_ts": 0}
        rules.append(rule)
        save_data()
        await query.answer("已设触发提醒")
        await safe_edit(query,
            card(p) + f"\n\n🔔 已加条件提醒：{escape_md(rule_text(rule))}\n"
                      f"（`/conds` 管理）",
            reply_markup=kb(p), parse_mode="Markdown")

    elif action == "size":
        # 计划 → 仓位：入场取区间中值，止损取计划的止损
        from handlers import sizing
        lo, hi = min(p["entry"]), max(p["entry"])
        entry = (lo + hi) / 2
        equity, pos = await sizing._account(query)
        if not equity:
            await query.answer("拿不到账户权益（未配 Bybit 密钥？）", show_alert=True)
            return
        s = sizing.plan_size(equity, entry, p["stop"], 0.5)
        if not s:
            await query.answer("这份计划的入场/止损算不出仓位", show_alert=True)
            return
        exp = sizing.exposure(pos, p["side"])
        from handlers.rtrade import _env_tag
        await safe_edit(query,
            sizing.build_text(s, exp, p["symbol"].replace("USDT", ""), _env_tag())
            + f"\n\n_按计划 `{p['id']}` 的入场中值 {md.f(entry)} 和止损 {md.f(p['stop'])} 算_",
            reply_markup=sizing._kb(entry, p["stop"], p["symbol"].replace("USDT", "")),
            parse_mode="Markdown")

    elif action == "chart":
        from handlers import annotchart
        await query.answer("出图中…")
        await annotchart._send(query.message, p["symbol"].replace("USDT", ""), "15m")

    elif action == "refresh":
        # 立即用最新收盘重新判一次状态——不用等后台那 2 分钟
        await query.answer("校验中…")
        try:
            r = await md._get("/v5/market/kline", {
                "category": md.CAT, "symbol": p["symbol"],
                "interval": md.INTERVALS[CHECK_IV], "limit": 2})
            rows = r.get("list") or []
            if len(rows) < 2:
                await query.answer("取不到K线", show_alert=True)
                return
            x = rows[1]
            new_st, msg = evaluate(p, float(x[4]), float(x[2]), float(x[3]))
        except Exception as e:
            log.error(f"计划刷新出错: {e}")
            await query.answer("校验失败", show_alert=True)
            return
        if new_st:
            p["status"] = new_st
            if new_st == "invalid":
                p["invalid_reason"] = (p.get("invalid") or {}).get("desc") or "条件已破坏"
            save(p)
        await safe_edit(query,
            card(p) + ("\n\n" + msg if msg else "\n\n_刚校验过：状态未变_"),
            reply_markup=kb(p), parse_mode="Markdown")

    elif action == "re":
        await query.answer("重新生成中，约 30~60 秒…")
        p["status"] = "archived"
        save_data()
        try:
            np, rep = await generate(p["symbol"], p["side"], p["chat_id"], p.get("uid", 0))
        except Exception as e:
            log.error(f"重新生成计划出错: {e}")
            await query.answer(f"生成失败：{str(e)[:60]}", show_alert=True)
            return
        await query.message.reply_text(rep.header() + "\n\n" + card(np),
                                       reply_markup=kb(np), parse_mode="Markdown")
