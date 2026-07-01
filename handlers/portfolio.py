import logging
from telegram import Update
from telegram.ext import ContextTypes
from api import get_prices_usd as get_prices
from config import COIN_IDS
from storage import data, save_data

def is_group(update: Update):
    return update.effective_chat.type in ("group", "supergroup")

async def add_holding(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_group(update):
        await update.message.reply_text("🔒 持仓功能涉及隐私，请私聊我使用")
        return
    if len(context.args) < 3:
        await update.message.reply_text("用法：/add BTC 0.5 60000")
        return
    symbol = context.args[0].upper()
    if symbol not in COIN_IDS:
        await update.message.reply_text(f"不支持的币种：{symbol}")
        return
    try:
        amount = float(context.args[1])
        cost = float(context.args[2])
    except ValueError:
        await update.message.reply_text("数量和成本价要是数字")
        return
    uid = str(update.effective_user.id)
    data["holdings"].setdefault(uid, {})
    data["holdings"][uid][symbol] = {"amount": amount, "cost": cost}
    save_data()
    await update.message.reply_text(f"✅ 已记录：{symbol} {amount}个，成本价 ${cost:,.2f}")

async def portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_group(update):
        await update.message.reply_text("🔒 持仓功能涉及隐私，请私聊我使用")
        return
    uid = str(update.effective_user.id)
    holdings = data["holdings"].get(uid, {})
    if not holdings:
        await update.message.reply_text("你还没有持仓。用 /add BTC 0.5 60000 记录")
        return
    try:
        prices = await get_prices(list(holdings.keys()))
    except Exception as e:
        logging.error(f"组合查价出错: {e}")
        await update.message.reply_text("查询失败")
        return
    lines = ["💼 你的投资组合\n"]
    total_cost = total_value = 0.0
    for sym, h in holdings.items():
        info = prices.get(sym)
        if not info:
            continue
        cur = info["usd"]
        cost_total = h["amount"] * h["cost"]
        value_total = h["amount"] * cur
        pnl = value_total - cost_total
        pnl_pct = (pnl / cost_total * 100) if cost_total else 0
        total_cost += cost_total
        total_value += value_total
        emoji = "🟢" if pnl >= 0 else "🔴"
        lines.append(f"{emoji} {sym}: {h['amount']}个\n   成本 ${cost_total:,.2f} → 现值 ${value_total:,.2f}\n   盈亏 {pnl:+,.2f} ({pnl_pct:+.2f}%)")
    total_pnl = total_value - total_cost
    total_pct = (total_pnl / total_cost * 100) if total_cost else 0
    e = "🟢" if total_pnl >= 0 else "🔴"
    lines += ["─────────", f"总成本: ${total_cost:,.2f}", f"总现值: ${total_value:,.2f}", f"{e} 总盈亏: {total_pnl:+,.2f} ({total_pct:+.2f}%)"]
    await update.message.reply_text("\n".join(lines))

async def holdings_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_group(update):
        await update.message.reply_text("🔒 持仓功能涉及隐私，请私聊我使用")
        return
    uid = str(update.effective_user.id)
    holdings = data["holdings"].get(uid, {})
    if not holdings:
        await update.message.reply_text("你还没有持仓。用 /add 记录")
        return
    lines = ["你的持仓："]
    for sym, h in holdings.items():
        lines.append(f"{sym}: {h['amount']}个，成本价 ${h['cost']:,.2f}")
    lines.append("\n用 /delhold BTC 删除")
    await update.message.reply_text("\n".join(lines))

async def del_holding(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_group(update):
        await update.message.reply_text("🔒 持仓功能涉及隐私，请私聊我使用")
        return
    if not context.args:
        await update.message.reply_text("用法：/delhold BTC")
        return
    symbol = context.args[0].upper()
    uid = str(update.effective_user.id)
    holdings = data["holdings"].get(uid, {})
    if symbol not in holdings:
        await update.message.reply_text(f"你没有 {symbol} 的持仓")
        return
    del holdings[symbol]
    save_data()
    await update.message.reply_text(f"已删除 {symbol} 持仓")


# 功能12：加仓 /buy BTC 0.5 60000（自动算平均成本）
async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_group(update):
        await update.message.reply_text("🔒 持仓功能涉及隐私，请私聊我使用")
        return
    if len(context.args) < 3:
        await update.message.reply_text("用法：/buy BTC 0.5 60000\n(币种 数量 买入价，多次买入自动算均价)")
        return
    symbol = context.args[0].upper()
    if symbol not in COIN_IDS:
        await update.message.reply_text(f"不支持的币种：{symbol}")
        return
    try:
        amount = float(context.args[1])
        buy_price = float(context.args[2])
    except ValueError:
        await update.message.reply_text("数量和价格要是数字")
        return
    if amount <= 0:
        await update.message.reply_text("数量要大于0")
        return

    uid = str(update.effective_user.id)
    data["holdings"].setdefault(uid, {})
    h = data["holdings"][uid].get(symbol)

    if h:
        # 已有持仓：算加权平均成本
        old_amount = h["amount"]
        old_cost = h["cost"]
        new_amount = old_amount + amount
        # 平均成本 = (旧量*旧成本 + 新量*新价) / 总量
        new_cost = (old_amount * old_cost + amount * buy_price) / new_amount
        data["holdings"][uid][symbol] = {"amount": new_amount, "cost": new_cost}
        save_data()
        await update.message.reply_text(
            f"✅ 加仓 {symbol} {amount}个 @${buy_price:,.2f}\n"
            f"持仓: {old_amount}→{new_amount}个\n"
            f"平均成本: ${old_cost:,.2f}→${new_cost:,.2f}"
        )
    else:
        # 新建持仓
        data["holdings"][uid][symbol] = {"amount": amount, "cost": buy_price}
        save_data()
        await update.message.reply_text(
            f"✅ 买入 {symbol} {amount}个 @${buy_price:,.2f}"
        )

# 功能12：减仓 /sell BTC 0.3
async def sell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_group(update):
        await update.message.reply_text("🔒 持仓功能涉及隐私，请私聊我使用")
        return
    if len(context.args) < 2:
        await update.message.reply_text("用法：/sell BTC 0.3\n(卖出数量，成本价不变)")
        return
    symbol = context.args[0].upper()
    try:
        amount = float(context.args[1])
    except ValueError:
        await update.message.reply_text("数量要是数字")
        return

    uid = str(update.effective_user.id)
    h = data["holdings"].get(uid, {}).get(symbol)
    if not h:
        await update.message.reply_text(f"你没有 {symbol} 的持仓")
        return
    if amount <= 0:
        await update.message.reply_text("数量要大于0")
        return
    if amount > h["amount"]:
        await update.message.reply_text(f"持仓不足，你只有 {h['amount']}个 {symbol}")
        return

    new_amount = h["amount"] - amount
    if new_amount <= 0:
        # 清仓
        del data["holdings"][uid][symbol]
        save_data()
        await update.message.reply_text(f"✅ 已清仓 {symbol}（卖出{amount}个）")
    else:
        data["holdings"][uid][symbol]["amount"] = new_amount
        save_data()
        await update.message.reply_text(
            f"✅ 卖出 {symbol} {amount}个\n剩余: {new_amount}个 (成本价 ${h['cost']:,.2f} 不变)"
        )


# 功能13：盈亏排行 /ranking
async def ranking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_group(update):
        await update.message.reply_text("🔒 持仓功能涉及隐私，请私聊我使用")
        return
    uid = str(update.effective_user.id)
    holdings = data["holdings"].get(uid, {})
    if not holdings:
        await update.message.reply_text("你还没有持仓")
        return
    try:
        prices = await get_prices(list(holdings.keys()))
    except Exception as e:
        logging.error(f"排行查价出错: {e}")
        await update.message.reply_text("查询失败")
        return

    # 算每个币的盈亏
    items = []
    for sym, h in holdings.items():
        info = prices.get(sym)
        if not info:
            continue
        cur = info["usd"]
        cost_total = h["amount"] * h["cost"]
        value_total = h["amount"] * cur
        pnl = value_total - cost_total
        pnl_pct = (pnl / cost_total * 100) if cost_total else 0
        items.append({"sym": sym, "pnl": pnl, "pct": pnl_pct})

    if not items:
        await update.message.reply_text("无法获取价格")
        return

    # 按盈亏金额排序（高到低）
    items.sort(key=lambda x: x["pnl"], reverse=True)

    lines = ["🏆 持仓盈亏排行\n"]
    for i, it in enumerate(items, 1):
        emoji = "🟢" if it["pnl"] >= 0 else "🔴"
        medal = ["🥇", "🥈", "🥉"][i-1] if i <= 3 else f"{i}."
        lines.append(f"{medal} {emoji} {it['sym']}: {it['pnl']:+,.2f} ({it['pct']:+.2f}%)")

    # 最赚/最亏
    best = items[0]
    worst = items[-1]
    lines.append("─────────")
    lines.append(f"最赚: {best['sym']} {best['pnl']:+,.2f}")
    lines.append(f"最亏: {worst['sym']} {worst['pnl']:+,.2f}")

    await update.message.reply_text("\n".join(lines))


# D-3：持仓异动提醒开关 + 检查
from storage import data as _pdata, save_data as _psave

async def watch_holdings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_group(update):
        await update.message.reply_text("🔒 请私聊使用")
        return
    uid = str(update.effective_user.id)
    _pdata.setdefault("holding_watch", {})
    chat_id = update.effective_chat.id
    _pdata["holding_watch"][uid] = chat_id
    _psave()
    await update.message.reply_text(
        "✅ 已开启持仓异动提醒\n"
        "你持仓的币单日涨/跌超10%时主动通知你\n"
        "关闭用 /unwatchhold"
    )

async def unwatch_holdings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if _pdata.get("holding_watch", {}).get(uid):
        del _pdata["holding_watch"][uid]
        _psave()
        await update.message.reply_text("已关闭持仓异动提醒")
    else:
        await update.message.reply_text("你还没开启")

# 后台检查持仓异动（job调用）
async def check_holding_moves(context: ContextTypes.DEFAULT_TYPE):
    watch = _pdata.get("holding_watch", {})
    if not watch:
        return
    from api import get_prices
    _pdata.setdefault("holding_alerted", {})

    for uid, chat_id in list(watch.items()):
        holdings = _pdata["holdings"].get(uid, {})
        if not holdings:
            continue
        try:
            prices = await get_prices(list(holdings.keys()))
        except Exception as e:
            logging.error(f"持仓异动查价出错: {e}")
            continue

        alerts = []
        for sym in holdings:
            info = prices.get(sym)
            if not info:
                continue
            change = info["change"]  # 24h涨跌
            if abs(change) >= 10:  # 超10%
                # 冷却：同币同方向6小时内不重复
                import time
                key = f"{uid}_{sym}"
                last = _pdata["holding_alerted"].get(key, 0)
                if time.time() - last >= 21600:
                    alerts.append({"sym": sym, "change": change, "price": info["price"]})
                    _pdata["holding_alerted"][key] = time.time()

        if alerts:
            lines = ["💼 *持仓异动提醒*\n"]
            for a in alerts:
                emoji = "🚀" if a["change"] > 0 else "💥"
                lines.append(f"{emoji} {a['sym']}: {a['change']:+.2f}% (${a['price']:,.4g})")
            lines.append("\n你的持仓有较大波动，注意关注")
            try:
                await context.bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode="Markdown")
            except Exception as e:
                logging.error(f"持仓异动推送失败: {e}")
    _psave()
