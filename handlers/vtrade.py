"""虚拟合约交易（模拟盘）—— 用真实行情练手，不碰真钱。

面向永续杠杆玩法：开多/开空、指定保证金+杠杆、实时浮动盈亏、理论爆仓价、
平仓结算回账户、胜率/历史统计，后台自动监控爆仓。
纯记账，价格取自 CoinGecko 现货价做 mark（现货≈永续标记价，够练手用）。
"""
import time
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from api import get_price, get_prices
from config import COIN_IDS
from storage import data, save_data

START_BALANCE = 10000.0   # 初始虚拟本金（USDT）
FEE_RATE = 0.0005         # 单边吃单手续费 0.05%（开+平各扣一次，模拟真实磨损）
MAX_LEV = 125             # 杠杆上限


def is_group(update: Update):
    return update.effective_chat.type in ("group", "supergroup")


def fmt(p):
    """自适应价格精度：大币两位小数，小币多给几位有效数字。"""
    if p is None:
        return "?"
    ap = abs(p)
    if ap >= 100:
        return f"{p:,.2f}"
    if ap >= 1:
        return f"{p:,.4f}"
    if ap >= 0.01:
        return f"{p:.5f}"
    return f"{p:.8f}".rstrip("0").rstrip(".")


def _acct(uid):
    """取/建某用户的虚拟账户。"""
    data.setdefault("vtrade", {})
    a = data["vtrade"].get(uid)
    if a is None:
        a = {"balance": START_BALANCE, "positions": {}, "history": [], "chat_id": None}
        data["vtrade"][uid] = a
    a.setdefault("balance", START_BALANCE)
    a.setdefault("positions", {})
    a.setdefault("history", [])
    a.setdefault("chat_id", None)
    return a


def _pnl(pos, mark):
    """未实现盈亏（USDT）。多：(现价-入场)*张数；空反之。"""
    qty = pos["qty"]
    if pos["side"] == "long":
        return (mark - pos["entry"]) * qty
    return (pos["entry"] - mark) * qty


def _liq(pos):
    """理论爆仓价（逐仓，忽略维持保证金/手续费，实际会更早）。"""
    entry, lev = pos["entry"], pos["lev"]
    if pos["side"] == "long":
        return entry * (1 - 1 / lev)
    return entry * (1 + 1 / lev)


def _pos_line(sym, pos, mark):
    pnl = _pnl(pos, mark)
    roe = pnl / pos["margin"] * 100 if pos["margin"] else 0
    liq = _liq(pos)
    # 距爆仓还有多少（按现价到爆仓价的百分比）
    dist = (mark - liq) / mark * 100 if pos["side"] == "long" else (liq - mark) / mark * 100
    emoji = "🟢" if pnl >= 0 else "🔴"
    dir_txt = "多 📈" if pos["side"] == "long" else "空 📉"
    return (
        f"{emoji} *{sym}* {dir_txt} {pos['lev']}x\n"
        f"   入场 ${fmt(pos['entry'])} → 现价 ${fmt(mark)}\n"
        f"   保证金 ${pos['margin']:,.2f}｜仓位 ${pos['margin']*pos['lev']:,.2f}\n"
        f"   浮盈 {pnl:+,.2f} ({roe:+.1f}%)\n"
        f"   爆仓价 ${fmt(liq)}（距爆仓 {dist:+.1f}%）"
    )


# ============ 开仓 ============
async def vopen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_group(update):
        await update.message.reply_text("🔒 虚拟交易涉及你的账户，请私聊我使用")
        return
    args = context.args
    if len(args) < 4:
        await update.message.reply_text(
            "📝 *开仓用法*\n"
            "`/vopen 币 方向 保证金 杠杆 [入场价]`\n\n"
            "例：`/vopen BTC long 1000 10`\n"
            "　= 用 1000U 保证金 10 倍做多 BTC（入场取现价）\n"
            "`/vopen ETH short 500 20 3800`\n"
            "　= 500U 20 倍做空 ETH，指定入场价 3800\n\n"
            "方向：`long`/`多`　`short`/`空`",
            parse_mode="Markdown")
        return
    symbol = args[0].upper()
    if symbol not in COIN_IDS:
        await update.message.reply_text(f"不支持的币种：{symbol}")
        return
    side_raw = args[1].lower()
    if side_raw in ("long", "多", "buy", "l"):
        side = "long"
    elif side_raw in ("short", "空", "sell", "s"):
        side = "short"
    else:
        await update.message.reply_text("方向要填 long/多 或 short/空")
        return
    try:
        margin = float(args[2])
        lev = float(args[3])
    except ValueError:
        await update.message.reply_text("保证金和杠杆要是数字")
        return
    if margin <= 0:
        await update.message.reply_text("保证金要大于 0")
        return
    if not (1 <= lev <= MAX_LEV):
        await update.message.reply_text(f"杠杆范围 1~{MAX_LEV} 倍")
        return

    uid = str(update.effective_user.id)
    a = _acct(uid)
    a["chat_id"] = update.effective_chat.id
    if symbol in a["positions"]:
        await update.message.reply_text(
            f"你已有 {symbol} 的持仓，先 `/vclose {symbol}` 平掉再开", parse_mode="Markdown")
        return

    # 入场价：指定则用指定，否则取现价
    if len(args) >= 5:
        try:
            entry = float(args[4])
        except ValueError:
            await update.message.reply_text("入场价要是数字")
            return
        if entry <= 0:
            await update.message.reply_text("入场价要大于 0")
            return
    else:
        try:
            r = await get_price(symbol)
        except Exception as e:
            logging.error(f"vopen 查价出错: {e}")
            r = None
        if not r:
            await update.message.reply_text("取现价失败，稍后再试，或手动指定入场价")
            return
        entry = r["price"]

    notional = margin * lev
    fee = notional * FEE_RATE
    cost = margin + fee
    if cost > a["balance"] + 1e-9:
        await update.message.reply_text(
            f"💸 余额不足\n可用 ${a['balance']:,.2f}，本单需 ${cost:,.2f}"
            f"（保证金 ${margin:,.2f} + 手续费 ${fee:,.2f}）")
        return

    a["balance"] -= cost
    a["positions"][symbol] = {
        "side": side, "margin": margin, "lev": lev, "entry": entry,
        "qty": notional / entry, "open_ts": time.time(), "open_fee": fee,
    }
    save_data()
    liq = _liq(a["positions"][symbol])
    dir_txt = "做多 📈" if side == "long" else "做空 📉"
    await update.message.reply_text(
        f"✅ *虚拟开仓*\n"
        f"{symbol} {dir_txt} {lev:g}x\n"
        f"入场价 ${fmt(entry)}\n"
        f"保证金 ${margin:,.2f}｜仓位 ${notional:,.2f}\n"
        f"手续费 -${fee:,.2f}\n"
        f"理论爆仓价 ${fmt(liq)}\n"
        f"剩余可用 ${a['balance']:,.2f}\n\n"
        f"平仓 `/vclose {symbol}`｜查仓 `/vpos`\n"
        f"_模拟盘，不构成投资建议_",
        parse_mode="Markdown")


# ============ 平仓 ============
async def vclose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_group(update):
        await update.message.reply_text("🔒 请私聊使用")
        return
    if not context.args:
        await update.message.reply_text("用法：`/vclose BTC` 全平，`/vclose BTC 50` 平一半", parse_mode="Markdown")
        return
    symbol = context.args[0].upper()
    uid = str(update.effective_user.id)
    a = _acct(uid)
    pos = a["positions"].get(symbol)
    if not pos:
        await update.message.reply_text(f"你没有 {symbol} 的虚拟持仓")
        return
    # 平仓比例
    pct = 100.0
    if len(context.args) >= 2:
        try:
            pct = float(context.args[1])
        except ValueError:
            await update.message.reply_text("平仓比例要是数字（1~100）")
            return
        if not (0 < pct <= 100):
            await update.message.reply_text("平仓比例要在 1~100 之间")
            return
    try:
        r = await get_price(symbol)
    except Exception as e:
        logging.error(f"vclose 查价出错: {e}")
        r = None
    if not r:
        await update.message.reply_text("取现价失败，稍后再试")
        return
    mark = r["price"]

    frac = pct / 100.0
    close_margin = pos["margin"] * frac
    close_qty = pos["qty"] * frac
    # 平掉这部分的盈亏
    if pos["side"] == "long":
        pnl = (mark - pos["entry"]) * close_qty
    else:
        pnl = (pos["entry"] - mark) * close_qty
    close_fee = close_margin * pos["lev"] * FEE_RATE
    net = pnl - close_fee
    # 逐仓：亏损不超过这部分保证金（超了就是爆仓，balance 只退到 0）
    ret = max(0.0, close_margin + net)
    a["balance"] += ret
    roe = net / close_margin * 100 if close_margin else 0

    if pct >= 100:
        del a["positions"][symbol]
        remain_txt = ""
    else:
        pos["margin"] -= close_margin
        pos["qty"] -= close_qty
        remain_txt = f"\n剩余仓位 {100-pct:g}%（保证金 ${pos['margin']:,.2f}）"

    a["history"].append({
        "sym": symbol, "side": pos["side"], "lev": pos["lev"],
        "entry": pos["entry"], "exit": mark, "margin": close_margin,
        "pnl": net, "roe": roe, "ts": time.time(),
    })
    save_data()
    emoji = "🟢" if net >= 0 else "🔴"
    word = "止盈" if net >= 0 else "止损"
    await update.message.reply_text(
        f"{emoji} *虚拟平仓 {word}* {'(部分)' if pct<100 else ''}\n"
        f"{symbol} {'多' if pos['side']=='long' else '空'} {pos['lev']:g}x\n"
        f"入场 ${fmt(pos['entry'])} → 平仓 ${fmt(mark)}\n"
        f"实现盈亏 {net:+,.2f} ({roe:+.1f}%)（含手续费 -${close_fee:,.2f}）"
        f"{remain_txt}\n"
        f"账户可用 ${a['balance']:,.2f}",
        parse_mode="Markdown")


# ============ 查持仓 + 账户 ============
async def vpos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_group(update):
        await update.message.reply_text("🔒 请私聊使用")
        return
    uid = str(update.effective_user.id)
    a = _acct(uid)
    positions = a["positions"]
    if not positions:
        await update.message.reply_text(
            f"💼 *虚拟合约账户*\n"
            f"可用余额 ${a['balance']:,.2f}（初始 ${START_BALANCE:,.0f}）\n"
            f"当前无持仓。\n\n"
            f"开仓：`/vopen BTC long 1000 10`\n"
            f"历史：`/vhistory`",
            parse_mode="Markdown")
        return
    try:
        prices = await get_prices(list(positions.keys()))
    except Exception as e:
        logging.error(f"vpos 查价出错: {e}")
        await update.message.reply_text("查价失败，稍后再试")
        return

    lines = ["💼 *虚拟合约账户*\n"]
    total_pnl = 0.0
    locked_margin = 0.0
    for sym, pos in positions.items():
        info = prices.get(sym)
        if not info:
            lines.append(f"• {sym}: 取价失败")
            locked_margin += pos["margin"]
            continue
        mark = info["price"]
        total_pnl += _pnl(pos, mark)
        locked_margin += pos["margin"]
        lines.append(_pos_line(sym, pos, mark))
    equity = a["balance"] + locked_margin + total_pnl
    e = "🟢" if total_pnl >= 0 else "🔴"
    lines.append("─────────")
    lines.append(f"可用余额 ${a['balance']:,.2f}")
    lines.append(f"持仓保证金 ${locked_margin:,.2f}")
    lines.append(f"{e} 未实现盈亏 {total_pnl:+,.2f}")
    lines.append(f"💰 账户权益 ${equity:,.2f}（初始 ${START_BALANCE:,.0f}, {(equity/START_BALANCE-1)*100:+.1f}%）")
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 刷新", callback_data="vpos_refresh"),
        InlineKeyboardButton("📜 历史", callback_data="vhist_show"),
    ]])
    await update.message.reply_text("\n".join(lines), reply_markup=kb, parse_mode="Markdown")


# ============ 历史 + 胜率 ============
def _history_text(a):
    hist = a.get("history", [])
    if not hist:
        return "📜 *虚拟交易历史*\n还没有平仓记录。开一单试试：`/vopen BTC long 1000 10`"
    wins = [h for h in hist if h["pnl"] >= 0]
    total_pnl = sum(h["pnl"] for h in hist)
    win_rate = len(wins) / len(hist) * 100
    gross_win = sum(h["pnl"] for h in wins)
    gross_loss = -sum(h["pnl"] for h in hist if h["pnl"] < 0)
    pf = (gross_win / gross_loss) if gross_loss > 0 else 0
    lines = [
        "📜 *虚拟交易历史*\n",
        f"总交易 {len(hist)} 笔｜胜率 {win_rate:.0f}% ({len(wins)}胜{len(hist)-len(wins)}负)",
        f"累计盈亏 {total_pnl:+,.2f}"
        + (f"｜盈亏比 {pf:.2f}" if gross_loss > 0 else ""),
        "\n近 10 笔：",
    ]
    for h in reversed(hist[-10:]):
        emoji = "🟢" if h["pnl"] >= 0 else "🔴"
        dir_txt = "多" if h["side"] == "long" else "空"
        lines.append(
            f"{emoji} {h['sym']} {dir_txt}{h['lev']:g}x  "
            f"{h['pnl']:+,.2f} ({h['roe']:+.0f}%)")
    return "\n".join(lines)


async def vhistory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_group(update):
        await update.message.reply_text("🔒 请私聊使用")
        return
    uid = str(update.effective_user.id)
    a = _acct(uid)
    await update.message.reply_text(_history_text(a), parse_mode="Markdown")


# ============ 重置账户 ============
async def vreset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_group(update):
        await update.message.reply_text("🔒 请私聊使用")
        return
    uid = str(update.effective_user.id)
    # 二次确认
    if not context.args or context.args[0] != "confirm":
        await update.message.reply_text(
            "⚠️ 重置会清空当前所有虚拟持仓和历史，本金回到 "
            f"${START_BALANCE:,.0f}。\n确认请发 `/vreset confirm`", parse_mode="Markdown")
        return
    data.setdefault("vtrade", {})
    data["vtrade"][uid] = {
        "balance": START_BALANCE, "positions": {}, "history": [],
        "chat_id": update.effective_chat.id,
    }
    save_data()
    await update.message.reply_text(
        f"🔄 已重置虚拟账户，本金 ${START_BALANCE:,.0f}。开仓：`/vopen BTC long 1000 10`",
        parse_mode="Markdown")


# ============ 菜单按钮渲染（供 menu.button_handler 调用）============
async def render_vpos(query):
    uid = str(query.from_user.id)
    a = _acct(uid)
    positions = a["positions"]
    from handlers.menu import back_to
    if not positions:
        await query.edit_message_text(
            f"💼 *虚拟合约账户*\n可用余额 ${a['balance']:,.2f}（初始 ${START_BALANCE:,.0f}）\n"
            f"当前无持仓。\n\n用命令开仓：`/vopen BTC long 1000 10`",
            reply_markup=back_to("cat_vtrade"), parse_mode="Markdown")
        return
    try:
        prices = await get_prices(list(positions.keys()))
    except Exception:
        prices = {}
    lines = ["💼 *虚拟合约账户*\n"]
    total_pnl = 0.0
    locked = 0.0
    for sym, pos in positions.items():
        info = prices.get(sym)
        locked += pos["margin"]
        if not info:
            lines.append(f"• {sym}: 取价失败")
            continue
        total_pnl += _pnl(pos, info["price"])
        lines.append(_pos_line(sym, pos, info["price"]))
    equity = a["balance"] + locked + total_pnl
    e = "🟢" if total_pnl >= 0 else "🔴"
    lines += ["─────────", f"可用 ${a['balance']:,.2f}｜保证金 ${locked:,.2f}",
              f"{e} 浮盈 {total_pnl:+,.2f}",
              f"💰 权益 ${equity:,.2f}（{(equity/START_BALANCE-1)*100:+.1f}%）"]
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 刷新", callback_data="vpos_refresh"),
        InlineKeyboardButton("📜 历史", callback_data="vhist_show"),
    ], [InlineKeyboardButton("⬅️ 返回", callback_data="cat_vtrade")]])
    from handlers.util import safe_edit
    await safe_edit(query, "\n".join(lines), reply_markup=kb, parse_mode="Markdown")


async def render_vhist(query):
    uid = str(query.from_user.id)
    a = _acct(uid)
    from handlers.menu import back_to
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 刷新", callback_data="vhist_show"),
        InlineKeyboardButton("💼 持仓", callback_data="vpos_refresh"),
    ], [InlineKeyboardButton("⬅️ 返回", callback_data="cat_vtrade")]])
    from handlers.util import safe_edit
    await safe_edit(query, _history_text(a), reply_markup=kb, parse_mode="Markdown")


# ============ 后台自动爆仓监控（job，每 60s）============
async def check_liquidations(context: ContextTypes.DEFAULT_TYPE):
    accts = data.get("vtrade", {})
    if not accts:
        return
    # 汇总所有用户持仓的币，一次批量查价
    syms = set()
    for a in accts.values():
        syms.update(a.get("positions", {}).keys())
    if not syms:
        return
    try:
        prices = await get_prices(list(syms))
    except Exception as e:
        logging.error(f"爆仓监控查价出错: {e}")
        return

    changed = False
    for uid, a in accts.items():
        chat_id = a.get("chat_id")
        for sym, pos in list(a.get("positions", {}).items()):
            info = prices.get(sym)
            if not info:
                continue
            mark = info["price"]
            liq = _liq(pos)
            hit = (pos["side"] == "long" and mark <= liq) or \
                  (pos["side"] == "short" and mark >= liq)
            if not hit:
                continue
            # 爆仓：保证金归零，记历史，通知
            a["history"].append({
                "sym": sym, "side": pos["side"], "lev": pos["lev"],
                "entry": pos["entry"], "exit": mark, "margin": pos["margin"],
                "pnl": -pos["margin"], "roe": -100.0, "ts": time.time(),
            })
            del a["positions"][sym]
            changed = True
            if chat_id:
                dir_txt = "多" if pos["side"] == "long" else "空"
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=(f"💥 *虚拟爆仓*\n"
                              f"{sym} {dir_txt} {pos['lev']:g}x 触及爆仓价 ${fmt(liq)}\n"
                              f"现价 ${fmt(mark)}，保证金 ${pos['margin']:,.2f} 全损 (-100%)\n"
                              f"可用余额 ${a['balance']:,.2f}\n"
                              f"_模拟盘，高杠杆爆仓就是这么快_"),
                        parse_mode="Markdown")
                except Exception as e:
                    logging.error(f"爆仓通知失败: {e}")
    if changed:
        save_data()
