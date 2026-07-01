import logging
from telegram import Update
from telegram.ext import ContextTypes
from config import COIN_IDS
from storage import data, save_data

def get_pref(chat_id):
    """获取用户偏好，没有则返回默认"""
    key = str(chat_id)
    data.setdefault("user_prefs", {})
    if key not in data["user_prefs"]:
        data["user_prefs"][key] = {"follows": [], "threshold": 20, "quiet": None}
    return data["user_prefs"][key]

# /follow BTC ETH SOL
async def follow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("用法：/follow BTC ETH SOL\n(关注后，告警和播报可只推关注的币)")
        return
    chat_id = update.effective_chat.id
    pref = get_pref(chat_id)
    added = []
    for sym in context.args:
        sym = sym.upper()
        if sym in COIN_IDS and sym not in pref["follows"]:
            pref["follows"].append(sym)
            added.append(sym)
    save_data()
    if added:
        await update.message.reply_text(
            f"✅ 已关注：{', '.join(added)}\n"
            f"当前关注：{', '.join(pref['follows'])}"
        )
    else:
        await update.message.reply_text(f"当前关注：{', '.join(pref['follows']) or '无'}")

# /unfollow BTC
async def unfollow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("用法：/unfollow BTC")
        return
    chat_id = update.effective_chat.id
    pref = get_pref(chat_id)
    removed = []
    for sym in context.args:
        sym = sym.upper()
        if sym in pref["follows"]:
            pref["follows"].remove(sym)
            removed.append(sym)
    save_data()
    await update.message.reply_text(
        f"已取消关注：{', '.join(removed) or '无'}\n"
        f"当前关注：{', '.join(pref['follows']) or '无'}"
    )

# /myfollows
async def my_follows(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    pref = get_pref(chat_id)
    if not pref["follows"]:
        await update.message.reply_text("你还没关注任何币。用 /follow BTC ETH 关注")
        return
    await update.message.reply_text(f"⭐ 你的关注：\n{', '.join(pref['follows'])}")

# /setalert 15
async def set_alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("用法：/setalert 15\n(设置你的异动告警阈值%，默认20)")
        return
    try:
        threshold = float(context.args[0])
        if threshold < 1 or threshold > 100:
            await update.message.reply_text("阈值范围 1-100")
            return
    except ValueError:
        await update.message.reply_text("请输入数字，如 /setalert 15")
        return
    chat_id = update.effective_chat.id
    pref = get_pref(chat_id)
    pref["threshold"] = threshold
    save_data()
    await update.message.reply_text(f"✅ 你的异动告警阈值已设为 {threshold:g}%")

# /myalert
async def my_alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    pref = get_pref(chat_id)
    follows = ', '.join(pref["follows"]) if pref["follows"] else "全市场"
    quiet = f"{pref['quiet'][0]}-{pref['quiet'][1]}" if pref.get("quiet") else "无"
    await update.message.reply_text(
        f"⚙️ 你的告警设置\n\n"
        f"异动阈值: {pref['threshold']:g}%\n"
        f"关注范围: {follows}\n"
        f"静音时段: {quiet}\n\n"
        f"/setalert 改阈值 | /follow 加关注 | /quiet 设静音"
    )

# /quiet 23:00 8:00
async def set_quiet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text(
            "用法：/quiet 23:00 8:00\n(设置免打扰时段，此时段不推送告警)\n"
            "取消静音：/quiet off"
        )
        return
    chat_id = update.effective_chat.id
    pref = get_pref(chat_id)
    if context.args[0].lower() == "off":
        pref["quiet"] = None
        save_data()
        await update.message.reply_text("已取消静音时段")
        return
    pref["quiet"] = [context.args[0], context.args[1]]
    save_data()
    await update.message.reply_text(
        f"✅ 静音时段设为 {context.args[0]} - {context.args[1]}\n这段时间不推送告警"
    )
