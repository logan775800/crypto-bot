import time
import logging
from telegram import Update
from telegram.ext import ContextTypes
from api import get_prices_usd as get_prices, get_price
from config import COIN_IDS
from storage import data, save_data

COOLDOWN = 300  # 持续预警冷却秒数（5分钟），避免刷屏

# 固定价预警（一次性）
async def alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 3:
        await update.message.reply_text(
            "用法：\n"
            "/alert BTC 60000 above - 涨破(一次性)\n"
            "/alert BTC 50000 below - 跌破(一次性)\n"
            "/alertpct BTC 5 - 涨跌超5%(一次性)\n"
            "/watch BTC 60000 above - 持续监控(反复提醒)"
        )
        return
    symbol = context.args[0].upper()
    if symbol not in COIN_IDS:
        await update.message.reply_text(f"不支持的币种：{symbol}")
        return
    try:
        target = float(context.args[1])
    except ValueError:
        await update.message.reply_text("目标价格要是数字")
        return
    direction = context.args[2].lower()
    if direction not in ("above", "below"):
        await update.message.reply_text("方向只能是 above 或 below")
        return
    data["alerts"].append({
        "type": "fixed", "chat_id": update.effective_chat.id,
        "symbol": symbol, "target": target, "direction": direction,
        "set_by": update.effective_user.first_name,
    })
    save_data()
    arrow = "涨破" if direction == "above" else "跌破"
    await update.message.reply_text(f"✅ 预警已设置：{symbol} {arrow} ${target:,.2f}")

# 百分比预警（一次性）
async def alert_pct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("用法：/alertpct BTC 5")
        return
    symbol = context.args[0].upper()
    if symbol not in COIN_IDS:
        await update.message.reply_text(f"不支持的币种：{symbol}")
        return
    try:
        pct = float(context.args[1])
    except ValueError:
        await update.message.reply_text("百分比要是数字")
        return
    try:
        result = await get_price(symbol)
        base = result["price"]
    except Exception as e:
        logging.error(f"获取基准价出错: {e}")
        await update.message.reply_text("获取当前价格失败")
        return
    data["alerts"].append({
        "type": "pct", "chat_id": update.effective_chat.id,
        "symbol": symbol, "pct": pct, "base_price": base,
        "set_by": update.effective_user.first_name,
    })
    save_data()
    await update.message.reply_text(f"✅ 百分比预警：{symbol} 从 ${base:,.2f} 涨跌超 ±{pct}% 提醒")

# 功能10：持续监控预警 /watch BTC 60000 above
async def watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 3:
        await update.message.reply_text("用法：/watch BTC 60000 above\n(持续监控，满足条件反复提醒，5分钟冷却)")
        return
    symbol = context.args[0].upper()
    if symbol not in COIN_IDS:
        await update.message.reply_text(f"不支持的币种：{symbol}")
        return
    try:
        target = float(context.args[1])
    except ValueError:
        await update.message.reply_text("目标价格要是数字")
        return
    direction = context.args[2].lower()
    if direction not in ("above", "below"):
        await update.message.reply_text("方向只能是 above 或 below")
        return
    data["alerts"].append({
        "type": "watch", "chat_id": update.effective_chat.id,
        "symbol": symbol, "target": target, "direction": direction,
        "set_by": update.effective_user.first_name,
        "last_notified": 0,
    })
    save_data()
    arrow = "涨破" if direction == "above" else "跌破"
    await update.message.reply_text(f"👁 持续监控：{symbol} {arrow} ${target:,.2f} 时反复提醒(5分钟冷却)")

async def list_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    my = [(i, a) for i, a in enumerate(data["alerts"]) if a["chat_id"] == chat_id]
    if not my:
        await update.message.reply_text("还没有预警")
        return
    lines = ["预警列表："]
    for idx, (i, a) in enumerate(my, 1):
        t = a.get("type")
        if t == "pct":
            lines.append(f"{idx}. [一次] {a['symbol']} 涨跌±{a['pct']}% (基准${a['base_price']:,.2f})")
        elif t == "watch":
            arrow = "涨破" if a["direction"] == "above" else "跌破"
            lines.append(f"{idx}. [持续] {a['symbol']} {arrow} ${a['target']:,.2f}")
        else:
            arrow = "涨破" if a["direction"] == "above" else "跌破"
            lines.append(f"{idx}. [一次] {a['symbol']} {arrow} ${a['target']:,.2f}")
    lines.append("\n用 /delalert 序号 删除")
    await update.message.reply_text("\n".join(lines))

async def del_alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("用法：/delalert 1")
        return
    try:
        num = int(context.args[0])
    except ValueError:
        await update.message.reply_text("请输入序号数字")
        return
    chat_id = update.effective_chat.id
    my = [(i, a) for i, a in enumerate(data["alerts"]) if a["chat_id"] == chat_id]
    if num < 1 or num > len(my):
        await update.message.reply_text("序号不存在")
        return
    removed = data["alerts"].pop(my[num - 1][0])
    save_data()
    await update.message.reply_text(f"已删除：{removed['symbol']} 预警")

# 后台检查（三种类型）
async def check_alerts(context: ContextTypes.DEFAULT_TYPE):
    if not data["alerts"]:
        return
    symbols = set(a["symbol"] for a in data["alerts"])
    try:
        prices = await get_prices(list(symbols))
    except Exception as e:
        logging.error(f"预警查价出错: {e}")
        return
    now = time.time()
    to_remove = []
    for a in data["alerts"]:
        info = prices.get(a["symbol"])
        if not info:
            continue
        cur = info["usd"]
        t = a.get("type")

        if t == "pct":
            change = (cur - a["base_price"]) / a["base_price"] * 100
            if abs(change) >= a["pct"]:
                arrow = "涨" if change >= 0 else "跌"
                await _send(context, a, f"🔔 百分比预警！\n{a['symbol']} 已{arrow} {abs(change):.2f}%\n基准 ${a['base_price']:,.2f} → 当前 ${cur:,.2f}")
                to_remove.append(a)

        elif t == "watch":
            hit = (a["direction"] == "above" and cur >= a["target"]) or \
                  (a["direction"] == "below" and cur <= a["target"])
            if hit and (now - a.get("last_notified", 0)) >= COOLDOWN:
                arrow = "涨破" if a["direction"] == "above" else "跌破"
                await _send(context, a, f"👁 持续监控！\n{a['symbol']} 当前 ${cur:,.2f}，已{arrow} ${a['target']:,.2f}")
                a["last_notified"] = now  # 更新冷却，不删除

        else:  # fixed
            hit = (a["direction"] == "above" and cur >= a["target"]) or \
                  (a["direction"] == "below" and cur <= a["target"])
            if hit:
                arrow = "涨破" if a["direction"] == "above" else "跌破"
                await _send(context, a, f"🔔 预警触发！\n{a['symbol']} 已{arrow} ${a['target']:,.2f}\n当前价格 ${cur:,.2f}")
                to_remove.append(a)

    for a in to_remove:
        if a in data["alerts"]:
            data["alerts"].remove(a)
    save_data()  # watch更新了last_notified，也要存

async def _send(context, a, text):
    try:
        await context.bot.send_message(chat_id=a["chat_id"], text=text)
    except Exception as e:
        logging.error(f"推送失败: {e}")
