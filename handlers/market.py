import logging
from telegram import Update
from telegram.ext import ContextTypes
from api import get_fear_greed

async def fear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        d = await get_fear_greed()
    except Exception as e:
        logging.error(f"恐惧贪婪指数出错: {e}")
        await update.message.reply_text("获取失败，请稍后再试")
        return
    value = d["value"]
    cls = d["classification"]
    cls_map = {
        "Extreme Fear": ("极度恐惧", "😱"),
        "Fear": ("恐惧", "😨"),
        "Neutral": ("中性", "😐"),
        "Greed": ("贪婪", "😎"),
        "Extreme Greed": ("极度贪婪", "🤑"),
    }
    cn, emoji = cls_map.get(cls, (cls, "📊"))
    filled = round(value / 10)
    bar = "█" * filled + "░" * (10 - filled)
    if value <= 25:
        hint = "市场极度恐惧，常被视为潜在的关注区间（仅供参考）"
    elif value <= 45:
        hint = "市场偏恐惧"
    elif value <= 55:
        hint = "市场情绪中性"
    elif value <= 75:
        hint = "市场偏贪婪"
    else:
        hint = "市场极度贪婪，常被视为需谨慎的区间（仅供参考）"
    await update.message.reply_text(
        f"{emoji} 恐惧贪婪指数\n\n"
        f"{value}/100 - {cn}\n"
        f"[{bar}]\n\n"
        f"💡 {hint}\n\n"
        f"(指数反映市场整体情绪，不构成投资建议)"
    )


from api import get_gas_price, get_gas_multi
from storage import data, save_data

def _gas_level(gwei):
    if gwei < 10:
        return "🟢 很低，适合转账"
    elif gwei < 30:
        return "🟡 正常"
    elif gwei < 80:
        return "🟠 偏高"
    return "🔴 拥堵，建议等等"

# 功能16：Gas费查询 /gas（多链）
async def gas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_chat.send_action("typing")
    try:
        rows = await get_gas_multi()
    except Exception as e:
        logging.error(f"gas查询出错: {e}")
        await update.message.reply_text("获取失败，请稍后再试")
        return
    lines = ["⛽ 各链 Gas 费 (gwei)\n"]
    eth_gwei = None
    for name, g in rows:
        if g is None:
            lines.append(f"{name}: 获取失败")
        else:
            lines.append(f"{name}: {g:.3f}")
            if name == "ETH":
                eth_gwei = g
    if eth_gwei is not None:
        eth_cost = eth_gwei * 21000 / 1e9
        lines.append(f"\nETH主网: {_gas_level(eth_gwei)}")
        lines.append(f"普通转账(21000 gas)约 {eth_cost:.6f} ETH")
    lines.append("\n💡 设提醒: /gasalert 15 (ETH跌破15gwei时通知)")
    await update.message.reply_text("\n".join(lines))


# Gas 阈值提醒：/gasalert 15  或  /gasalert off
async def set_gas_alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    data.setdefault("gas_subs", {})
    if context.args and context.args[0].lower() in ("off", "取消", "关闭", "0"):
        if chat_id in data["gas_subs"]:
            del data["gas_subs"][chat_id]
            save_data()
        await update.message.reply_text("已关闭 Gas 提醒")
        return
    if not context.args:
        await update.message.reply_text("用法：/gasalert 15\n(ETH主网 gas 跌破 15 gwei 时提醒你；关闭用 /gasalert off)")
        return
    try:
        threshold = float(context.args[0])
    except ValueError:
        await update.message.reply_text("阈值要是数字，例如 /gasalert 15")
        return
    data["gas_subs"][chat_id] = {"threshold": threshold, "armed": True}
    save_data()
    await update.message.reply_text(
        f"✅ 已设 Gas 提醒：ETH 主网 gas 跌破 {threshold:g} gwei 时通知你\n"
        f"(触发一次后，等 gas 回升到阈值以上会自动重新武装；关闭用 /gasalert off)"
    )

# 后台检查 gas 提醒（job，边沿触发防刷屏）
async def check_gas_alerts(context: ContextTypes.DEFAULT_TYPE):
    subs = data.get("gas_subs", {})
    if not subs:
        return
    try:
        gwei = await get_gas_price()
    except Exception as e:
        logging.error(f"gas提醒查价出错: {e}")
        return
    changed = False
    for chat_id, cfg in list(subs.items()):
        th = cfg.get("threshold", 0)
        armed = cfg.get("armed", True)
        if gwei <= th and armed:
            try:
                await context.bot.send_message(
                    chat_id=int(chat_id),
                    text=f"⛽ Gas 提醒\nETH 主网 gas 已跌破 {th:g} gwei，当前 {gwei:.2f} gwei —— 适合转账/上链操作。")
            except Exception as e:
                logging.error(f"gas提醒推送失败 {chat_id}: {e}")
            cfg["armed"] = False
            changed = True
        elif gwei > th and not armed:
            cfg["armed"] = True   # 回升后重新武装
            changed = True
    if changed:
        save_data()
