"""全交易所合约涨跌幅分级告警。

两条触发路径共用本模块的判档去重 + 推送逻辑：
  • WebSocket 实时（handlers/contract_ws.py，OKX/Bybit）：价格穿过阈值秒级触发。
  • REST 轮询（本文件 scan_contracts，覆盖三家，含 Binance）：定时兜底 / 安全网。
两者写同一份 data["contract_tiers"] 分档记录，所以不会重复告警。

当 |涨跌幅| 突破台阶（20/30/40%…到 400%）时向订阅群推送，每条标注交易所来源，
同一个币在多个所同时命中会分别成行、各标来源。

订阅：/watchcontract 订阅，/unwatchcontract 取消。
"""
import time
import logging
import asyncio
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from storage import data, save_data
from handlers.util import escape_md, safe_edit

OKX_BASE = "https://www.okx.com"
FAPI = "https://fapi.binance.com"          # 币安 USDT 本位合约
BYBIT_BASE = "https://api.bybit.com"

# 告警台阶：20% 起，每 10% 一档，封顶 400%
TIERS = list(range(20, 401, 10))
# 迟滞带（百分点）：涨跌幅须回落到 (最低档-迟滞) 以下才重新武装，
# 杜绝币在 20% 边界上下抖动被反复当成"首次穿越"而刷屏
HYSTERESIS = 3
# 最小 24h 成交额（USDT），滤掉僵尸/微盘合约的噪音；按需调整
MIN_TURNOVER = 1_000_000
TIER_RESET = 86400                          # 记录 24h 后过期，允许重新计档
LEV_SUFFIX = ("UP", "DOWN", "BULL", "BEAR")  # 币安杠杆代币，排除
MAX_LINES = 40                              # 单条消息最多多少行，超出分条发
# 推送冷却：同一(币,方向,档位)在此秒数内只推一次。台阶记录跨所/跨路径抖动时
# （如 KORU 深跌被反复当"新穿档"），这是最终兜底，杜绝同一异动每轮重报。
PUSH_COOLDOWN = 6 * 3600


def get_tier(change_abs):
    """返回 |涨跌幅| 命中的最高台阶；不足 20% 返回 0，超 400% 封顶 400。"""
    if change_abs < TIERS[0]:
        return 0
    tier = TIERS[0]
    for t in TIERS:
        if change_abs >= t:
            tier = t
        else:
            break
    return tier


def _upgrade_rec(rec):
    """兼容旧格式 {tier,dir,ts} → 新格式 {up:最高档, down:最高档, ts}。
    旧格式只记单一方向，方向一翻就得整条作废，是之前反复重报的根因。"""
    if not rec:
        return {}
    if "tier" in rec and "dir" in rec:
        return {rec["dir"]: rec["tier"], "ts": rec.get("ts", 0)}
    return rec


def eval_tier_cross(ex_name, sym, change, now=None):
    """判断某币当前涨跌幅是否升到了**该方向上**更高的台阶。

    命中返回要告警的台阶(int)并更新 data["contract_tiers"]；否则返回 None。
    WS 实时与 REST 轮询共用此函数 → 同一套去重。

    记录格式 {sym: {"up": 最高档, "down": 最高档, "ts": 时间}}：
    **按方向各自记最高档**，所以某个源瞬时报出反向读数时，不会把原方向的记录清掉
    （旧实现会 pop 整条 → 下一轮又被当成"首次穿档"重报，KORU 那次刷屏就是这么来的）。
    """
    if now is None:
        now = time.time()
    data.setdefault("contract_tiers", {})
    tiers = data["contract_tiers"]
    change_abs = abs(change)
    direction = "up" if change > 0 else "down"
    key = sym                    # 只按币去重：同一个币在多所同时异动＝一个事件，只报一次
    rec = _upgrade_rec(tiers.get(key))

    # 过期(24h)：整条作废，允许重新计档
    if rec and now - rec.get("ts", 0) > TIER_RESET:
        rec = {}

    # 明显回落到迟滞带以下(< 最低档-迟滞) → 解除武装，清记录，之后重新穿越才再报
    if change_abs < TIERS[0] - HYSTERESIS:
        tiers.pop(key, None)
        return None

    # 处于迟滞带或未达最低档(如 17~20%) → 不报；有记录则续命时间戳，别过期
    if change_abs < TIERS[0]:
        if rec:
            rec["ts"] = now
            tiers[key] = rec
        return None

    tier = get_tier(change_abs)
    prev = rec.get(direction, 0)
    rec["ts"] = now
    if tier > prev:                       # 仅在该方向升到更高台阶才报（同档/反向抖动不再重复）
        rec[direction] = tier
        tiers[key] = rec
        return tier

    tiers[key] = rec                      # 同档/回落但仍在高位：续命，不报
    return None


async def push_to_subscribers(bot, alerts):
    """把一批告警(dict: ex/sym/change/price/tier/direction)推给所有订阅群，每条标来源。"""
    subs = data.get("contract_watch", [])
    if not subs or not alerts:
        return
    # 同一(币,方向)去重（跨交易所也算重复），保留最高档，只留首个交易所来源
    dedup = {}
    for a in alerts:
        k = (a["sym"], a["direction"])
        if k not in dedup or a["tier"] > dedup[k]["tier"]:
            dedup[k] = a

    # 推送冷却兜底：同一(币,方向,档位) 6h 内已推过就跳过，杜绝台阶记录抖动导致的刷屏。
    # 升到更高档位是不同 key，仍会照常推。
    now = time.time()
    cooled = data.setdefault("contract_alerted", {})
    fresh = []
    for a in dedup.values():
        ck = f"{a['sym']}:{a['direction']}:{a['tier']}"
        if now - cooled.get(ck, 0) < PUSH_COOLDOWN:
            continue
        cooled[ck] = now
        fresh.append(a)
    # 清理过期冷却记录，避免无限增长
    for k in [k for k, v in cooled.items() if now - v > PUSH_COOLDOWN * 2]:
        cooled.pop(k, None)
    if not fresh:
        save_data()
        return
    save_data()
    alerts = sorted(fresh, key=lambda a: (-a["tier"], a["ex"], a["sym"]))
    # 按群的「最低告警档」各自过滤：有人只想看 ≥30%/≥50% 的大动作，少刷屏。
    # 默认 MIN_ALERT_TIER(=20)，即全收。
    for chat_id in subs:
        mt = _min_tier(chat_id)
        chat_alerts = [a for a in alerts if a["tier"] >= mt]
        if not chat_alerts:
            continue
        body = [_render_alert(a) for a in chat_alerts]
        chunks = [body[i:i + MAX_LINES] for i in range(0, len(body), MAX_LINES)]
        for idx, chunk in enumerate(chunks):
            head = "🚨 *合约异动告警*（全交易所）\n" if idx == 0 else "🚨 *合约异动告警*（续）\n"
            text = head + "\n".join(chunk)
            if idx == len(chunks) - 1:
                text += "\n\n⚠️ 合约杠杆风险高，异动剧烈，不构成投资建议"
            try:
                await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
            except Exception as e:
                logging.error(f"合约告警推送失败 {chat_id}: {e}")


def _render_alert(a):
    emoji = "🚀" if a["direction"] == "up" else "💥"
    arrow = "涨破" if a["direction"] == "up" else "跌破"
    return (f"{emoji} *{a['ex']}* {escape_md(a['sym'])} {arrow} {a['tier']}%！"
            f"现 {a['change']:+.2f}% (${a['price']:,.4g})")


MIN_ALERT_TIER = 20        # 默认最低告警档（全收）
MIN_TIER_PRESETS = [20, 30, 50, 100]


def _min_tier(chat_id):
    """该群的最低告警档；没设过就返回默认 20（全收）。contract_watch 仍是纯列表，
    最低档单独存 data['contract_min_tier'] = {chat_id: tier}，零迁移。"""
    return int((data.get("contract_min_tier") or {}).get(str(chat_id), MIN_ALERT_TIER))


# ---------- 各交易所合约行情抓取（REST，统一返回 [{sym, change, price, turnover}]）----------
async def _okx_swap(client):
    r = await client.get(f"{OKX_BASE}/api/v5/market/tickers", params={"instType": "SWAP"})
    r.raise_for_status()
    d = r.json()
    if d.get("code") != "0":
        return []
    out = []
    for t in d.get("data", []):
        iid = t.get("instId", "")
        if not iid.endswith("-USDT-SWAP"):
            continue
        try:
            last = float(t["last"]); op = float(t["open24h"])
            if op <= 0:
                continue
            change = (last - op) / op * 100
            # OKX SWAP 的 volCcy24h 以基础币计价，× 现价 ≈ USD 成交额
            turnover = float(t.get("volCcy24h", 0) or 0) * last
            if turnover < MIN_TURNOVER:
                continue
            out.append({"sym": iid[:-len("-USDT-SWAP")], "change": change,
                        "price": last, "turnover": turnover})
        except (ValueError, KeyError):
            continue
    return out


async def _binance_swap(client):
    r = await client.get(f"{FAPI}/fapi/v1/ticker/24hr")
    r.raise_for_status()
    out = []
    for t in r.json():
        s = t.get("symbol", "")
        if not s.endswith("USDT"):          # 排除交割合约(带日期)/USDC 等
            continue
        base = s[:-4]
        if any(base.endswith(x) for x in LEV_SUFFIX):
            continue
        try:
            last = float(t["lastPrice"]); ch = float(t["priceChangePercent"])
            turnover = float(t.get("quoteVolume", 0) or 0)   # 已是 USDT
            if turnover < MIN_TURNOVER:
                continue
            out.append({"sym": base, "change": ch, "price": last, "turnover": turnover})
        except (ValueError, KeyError):
            continue
    return out


async def _bybit_swap(client):
    r = await client.get(f"{BYBIT_BASE}/v5/market/tickers", params={"category": "linear"})
    r.raise_for_status()
    d = r.json()
    if d.get("retCode") != 0:
        return []
    out = []
    for t in d.get("result", {}).get("list", []):
        s = t.get("symbol", "")
        if not s.endswith("USDT"):          # 排除 USDC 永续/日期交割
            continue
        base = s[:-4]
        try:
            last = float(t["lastPrice"]); ch = float(t["price24hPcnt"]) * 100
            turnover = float(t.get("turnover24h", 0) or 0)   # 已是 USDT
            if turnover < MIN_TURNOVER:
                continue
            out.append({"sym": base, "change": ch, "price": last, "turnover": turnover})
        except (ValueError, KeyError):
            continue
    return out


EXCHANGES = [("OKX", _okx_swap), ("币安", _binance_swap), ("Bybit", _bybit_swap)]


# ---------- 订阅命令 ----------
async def watch_contract(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    data.setdefault("contract_watch", [])
    if chat_id in data["contract_watch"]:
        await update.message.reply_text("本群已订阅合约异动告警 ✅")
        return
    data["contract_watch"].append(chat_id)
    save_data()
    await update.message.reply_text(
        "✅ 已订阅【全交易所合约异动告警】\n\n"
        "• 覆盖 OKX / 币安 / Bybit 永续合约\n"
        "• |涨跌幅| 突破 20% / 30% / … / 400% 分级告警\n"
        "• 每条标注交易所来源，多所同时命中都会发\n"
        "• OKX/Bybit 秒级实时(WebSocket)，币安约1分钟兜底\n"
        "• 同币同方向升档才再报\n\n"
        "取消订阅：/unwatchcontract"
    )


async def unwatch_contract(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    data.setdefault("contract_watch", [])
    if chat_id in data["contract_watch"]:
        data["contract_watch"].remove(chat_id)
        save_data()
        await update.message.reply_text("已取消合约异动告警")
    else:
        await update.message.reply_text("本群还没订阅合约异动告警")


# ---------- 按钮面板（不用记命令）----------
def _is_sub(chat_id):
    subs = data.get("contract_watch", [])
    return chat_id in subs or str(chat_id) in [str(s) for s in subs]


def _panel(chat_id):
    """返回 (文本, 键盘)：订阅状态 + 最低档选择 + 看榜/取消。"""
    subbed = _is_sub(chat_id)
    mt = _min_tier(chat_id)
    if subbed:
        status = f"✅ *已订阅*　最低告警档：*{mt}%*（≥{mt}% 才推）"
    else:
        status = "⬜️ *未订阅*　点下面「开启订阅」即可"

    text = (
        "📊 *全交易所合约异动告警*\n"
        "━━━━━━━━━━━━━━\n"
        f"{status}\n\n"
        "• 覆盖 OKX / 币安 / Bybit 永续\n"
        "• |涨跌幅| 突破 20%/30%/…/400% 分级告警（24h口径）\n"
        "• OKX/Bybit 秒级实时，币安约1分钟兜底\n"
        "• 同币同方向升档才再报（防刷屏）\n"
        "━━━━━━━━━━━━━━\n"
        "选最低档（越高越少、只留大动作）："
    )
    rows = []
    row = []
    for p in MIN_TIER_PRESETS:
        mark = "✅" if (subbed and p == mt) else ""
        label = f"{mark}≥{p}%" + ("(全部)" if p == 20 else "")
        row.append(InlineKeyboardButton(label, callback_data=f"ctr:tier:{p}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("📊 看合约涨跌榜", callback_data="ctr:top"),
                 InlineKeyboardButton("🔔 立即补报当前", callback_data="ctr:now")])
    rows.append([InlineKeyboardButton("🩺 自检(为啥没告警)", callback_data="ctr:diag"),
                 InlineKeyboardButton("🔄 刷新", callback_data="ctr:panel")])
    if subbed:
        rows.append([InlineKeyboardButton("🔕 取消订阅", callback_data="ctr:off")])
    else:
        rows.append([InlineKeyboardButton("⚡ 开启订阅", callback_data="ctr:on")])
    return text, InlineKeyboardMarkup(rows)


async def contract_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/contract —— 合约异动告警按钮面板。"""
    text, kb = _panel(update.effective_chat.id)
    await update.message.reply_text(text, reply_markup=kb, parse_mode="Markdown")


async def _fetch_all_movers():
    """拉三所永续，返回按 (币,方向) 去重后、达到最低档≥20% 的异动列表（不改任何全局状态）。"""
    async with httpx.AsyncClient(timeout=12) as client:
        results = await asyncio.gather(
            *[fn(client) for _, fn in EXCHANGES], return_exceptions=True)
    dedup = {}
    for (ex_name, _), res in zip(EXCHANGES, results):
        if isinstance(res, Exception):
            continue
        for m in res:
            t = get_tier(abs(m["change"]))
            if t < 20:
                continue
            direction = "up" if m["change"] > 0 else "down"
            key = (m["sym"], direction)
            a = {"ex": ex_name, "sym": m["sym"], "change": m["change"],
                 "price": m["price"], "tier": t, "direction": direction}
            if key not in dedup or t > dedup[key]["tier"]:
                dedup[key] = a
    return list(dedup.values())


async def _do_alert_now(chat_id, reply):
    """立即补推当前异动的核心。reply 是发消息的协程函数（命令用 message.reply_text，
    按钮回调也用 message.reply_text，各自传进来）。"""
    mt = _min_tier(chat_id)
    try:
        movers = await _fetch_all_movers()
    except Exception as e:
        await reply(f"取数失败：{str(e)[:80]}")
        return
    movers = [a for a in movers if a["tier"] >= mt]
    if not movers:
        await reply(f"✅ 通道正常，但当前**没有** ≥{mt}% 的合约异动（24h口径）。\n"
                    f"想放宽用 /contract 把最低档调低。", parse_mode="Markdown")
        return
    movers.sort(key=lambda a: (-a["tier"], a["ex"], a["sym"]))
    body = [_render_alert(a) for a in movers]
    chunks = [body[i:i + MAX_LINES] for i in range(0, len(body), MAX_LINES)]
    for idx, chunk in enumerate(chunks):
        head = (f"🚨 *当前合约异动*（补推，≥{mt}%，共{len(movers)}个）\n" if idx == 0
                else "🚨 *当前合约异动*（续）\n")
        await reply(head + "\n".join(chunk), parse_mode="Markdown")


async def alert_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/alertnow —— 立即把当前所有合约异动补推一遍（无视去重，一次性）。

    解决「怎么一个告警都没有」：正常告警对已报过的币会去重，安静≠坏了。
    """
    await update.message.reply_text("🔍 正在拉取当前合约异动…")
    await _do_alert_now(update.effective_chat.id, update.message.reply_text)


async def _do_alert_diag(chat_id, reply):
    """告警自检核心：订阅状态 + 为什么安静。reply 同上。"""
    import time
    cid = str(chat_id)

    def sub(key, is_dict=False):
        v = data.get(key, {} if is_dict else [])
        return (cid in [str(x) for x in v]) if not is_dict else (cid in v)

    ctr = sub("contract_watch")
    pmp = cid in (data.get("pump_watch") or {})
    mkt = sub("market_watch")
    lines = [
        "📋 *告警自检*",
        "━━━━━━━━━━━━━━",
        f"本群 chat\\_id：`{chat_id}`",
        f"📊 合约异动：{'✅ 已订阅' if ctr else '⬜ 未订阅'}"
        + (f"（最低档 {_min_tier(chat_id)}%）" if ctr else "　→ /contract 开启"),
        f"⚡ 急涨急跌：{'✅ 已订阅' if pmp else '⬜ 未订阅'}"
        + (f"（阈值 {(data['pump_watch'][cid]).get('pct',15):g}%）" if pmp else "　→ /pump 开启"),
        f"📈 市场异动：{'✅ 已订阅' if mkt else '⬜ 未订阅'}",
        "━━━━━━━━━━━━━━",
    ]

    # 合约现状：当前有多少币达标、多少被去重、多少是新的
    try:
        movers = await _fetch_all_movers()
        mt = _min_tier(chat_id)
        movers = [a for a in movers if a["tier"] >= mt]
        now = time.time()
        tiers = data.get("contract_tiers", {})
        deduped = fresh = 0
        for a in movers:
            rec = _upgrade_rec(tiers.get(a["sym"]))
            if rec and now - rec.get("ts", 0) > TIER_RESET:
                rec = {}
            prev = rec.get(a["direction"], 0)
            if a["tier"] > prev:
                fresh += 1
            else:
                deduped += 1
        lines += [
            f"当前 ≥{mt}% 的合约异动：*{len(movers)}* 个",
            f"　├ 已报过·去重中：{deduped} 个 ← 安静的正常原因",
            f"　└ 新的·下轮会推：{fresh} 个",
            f"去重记录数：{len(tiers)} 条",
            "━━━━━━━━━━━━━━",
            "✅ 你能看到这条 = *推送通道正常*",
            "想立刻把当前异动全看一遍 → /alertnow",
        ]
    except Exception as e:
        lines.append(f"（拉取现状失败：{str(e)[:60]}）")

    await reply("\n".join(lines), parse_mode="Markdown")


async def alert_diag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/alertdiag —— 告警自检：订阅状态 + 为什么安静 + 投递测试（这条消息本身就是测试）。"""
    await _do_alert_diag(update.effective_chat.id, update.message.reply_text)


async def _live_board():
    """当前各所永续 24h 涨跌榜（涨/跌各前 8，≥10% 才列），面板「看榜」用。"""
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            results = await asyncio.gather(
                *[fn(client) for _, fn in EXCHANGES], return_exceptions=True)
    except Exception as e:
        return f"取数失败：{str(e)[:60]}"
    rows = []
    for (ex_name, _), res in zip(EXCHANGES, results):
        if isinstance(res, Exception):
            continue
        for m in res:
            rows.append((ex_name, m["sym"], m["change"], m["price"]))
    if not rows:
        return "暂时没取到行情，稍后再试。"
    ups = sorted([r for r in rows if r[2] > 0], key=lambda x: -x[2])[:8]
    downs = sorted([r for r in rows if r[2] < 0], key=lambda x: x[2])[:8]
    ups = [r for r in ups if r[2] >= 10]
    downs = [r for r in downs if r[2] <= -10]

    def fmt(r):
        ex, sym, ch, price = r
        return f"  {escape_md(sym)} {ch:+.1f}% [{ex}]（${price:,.4g}）"
    lines = ["📊 *合约 24h 涨跌榜*（≥10%）"]
    lines.append("\n🚀 *涨幅前列*")
    lines += [fmt(r) for r in ups] or ["  （暂无≥10%）"]
    lines.append("\n💥 *跌幅前列*")
    lines += [fmt(r) for r in downs] or ["  （暂无≤-10%）"]
    return "\n".join(lines)


async def on_button(query, context):
    """处理 ctr: 开头的回调（合约面板）。由 menu.button_handler 转发。"""
    d = query.data
    chat_id = query.message.chat.id
    data.setdefault("contract_watch", [])

    if d == "ctr:panel":
        text, kb = _panel(chat_id)
        await safe_edit(query, text, reply_markup=kb, parse_mode="Markdown")
        return

    if d == "ctr:on":
        if not _is_sub(chat_id):
            data["contract_watch"].append(chat_id)
            save_data()
        await query.answer("已开启订阅")
        text, kb = _panel(chat_id)
        await safe_edit(query, text, reply_markup=kb, parse_mode="Markdown")
        return

    if d == "ctr:off":
        data["contract_watch"] = [s for s in data["contract_watch"]
                                  if str(s) != str(chat_id)]
        save_data()
        await query.answer("已取消订阅")
        text, kb = _panel(chat_id)
        await safe_edit(query, text, reply_markup=kb, parse_mode="Markdown")
        return

    if d == "ctr:top":
        await query.answer("拉取中…")
        board = await _live_board()
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("⬅️ 返回", callback_data="ctr:panel"),
            InlineKeyboardButton("🔄 刷新榜", callback_data="ctr:top")]])
        await safe_edit(query, board, reply_markup=kb, parse_mode="Markdown")
        return

    if d == "ctr:now":
        await query.answer("拉取当前异动中…")
        await _do_alert_now(chat_id, query.message.reply_text)
        return

    if d == "ctr:diag":
        await query.answer("自检中…")
        await _do_alert_diag(chat_id, query.message.reply_text)
        return

    if d.startswith("ctr:tier:"):
        try:
            tier = int(d.split(":")[2])
        except (ValueError, IndexError):
            await query.answer("参数错误")
            return
        # 选档即视为订阅（还没订就一起订上）
        if not _is_sub(chat_id):
            data["contract_watch"].append(chat_id)
        data.setdefault("contract_min_tier", {})[str(chat_id)] = tier
        save_data()
        await query.answer(f"已设为只推 ≥{tier}%")
        text, kb = _panel(chat_id)
        await safe_edit(query, text, reply_markup=kb, parse_mode="Markdown")
        return


# ---------- 后台扫描（REST 轮询，安全网 + 币安主路）----------
async def scan_contracts(context: ContextTypes.DEFAULT_TYPE):
    if not data.get("contract_watch"):
        return
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            results = await asyncio.gather(
                *[fn(client) for _, fn in EXCHANGES], return_exceptions=True
            )
    except Exception as e:
        logging.error(f"合约扫描取数出错: {e}")
        return

    now = time.time()
    alerts = []
    for (ex_name, _), res in zip(EXCHANGES, results):
        if isinstance(res, Exception):
            logging.warning(f"合约扫描 {ex_name} 失败: {res}")
            continue
        for m in res:
            tier = eval_tier_cross(ex_name, m["sym"], m["change"], now)
            if tier:
                alerts.append({"ex": ex_name, "sym": m["sym"], "change": m["change"],
                               "price": m["price"], "tier": tier,
                               "direction": "up" if m["change"] > 0 else "down"})

    # 清理过期记录，避免无限增长
    tiers = data.get("contract_tiers", {})
    data["contract_tiers"] = {k: v for k, v in tiers.items() if now - v["ts"] < TIER_RESET * 2}
    save_data()

    await push_to_subscribers(context.bot, alerts)
