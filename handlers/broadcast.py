import logging
from telegram import Update
from telegram.ext import ContextTypes
from api import get_prices_usd as get_prices
from config import BROADCAST_COINS
from storage import data, save_data

# 订阅定时播报（在群或私聊里发 /subscribe）
async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in data["broadcast_chats"]:
        await update.message.reply_text("这个对话已经订阅了每日播报 ✅")
        return
    data["broadcast_chats"].append(chat_id)
    save_data()
    await update.message.reply_text(
        "✅ 已订阅每日行情播报！\n每天早上会在这里推送主流币行情。\n取消用 /unsubscribe"
    )

async def unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in data["broadcast_chats"]:
        data["broadcast_chats"].remove(chat_id)
        save_data()
        await update.message.reply_text("已取消每日播报订阅")
    else:
        await update.message.reply_text("这个对话还没订阅")

# 立即播报一次（测试用 /broadcast）
async def broadcast_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = await build_broadcast_text()
    await update.message.reply_text(text)

# 生成播报内容
async def build_broadcast_text():
    try:
        prices = await get_prices(BROADCAST_COINS)
    except Exception as e:
        logging.error(f"播报查价出错: {e}")
        return "行情获取失败"
    lines = ["📊 每日行情播报\n"]
    for sym in BROADCAST_COINS:
        info = prices.get(sym)
        if not info:
            continue
        emoji = "📈" if info["change"] >= 0 else "📉"
        lines.append(f"{emoji} {sym}: ${info['usd']:,.2f} ({info['change']:+.2f}%)")
    return "\n".join(lines)

# 定时任务：给所有订阅的对话推送（被 job_queue 调用）
async def daily_broadcast(context: ContextTypes.DEFAULT_TYPE):
    if not data["broadcast_chats"]:
        return
    text = await build_broadcast_text()
    for chat_id in data["broadcast_chats"]:
        try:
            await context.bot.send_message(chat_id=chat_id, text=text)
        except Exception as e:
            logging.error(f"播报推送失败 {chat_id}: {e}")


# D-1：每日技术分析推送
from storage import data as _data2, save_data as _save2

async def sub_analysis(update, context):
    chat_id = update.effective_chat.id
    if chat_id in _data2["analysis_subs"]:
        await update.message.reply_text("已订阅每日分析推送 ✅")
        return
    _data2["analysis_subs"].append(chat_id)
    _save2()
    await update.message.reply_text(
        "✅ 已订阅每日技术分析推送！\n每天定时推送 BTC 技术分析+AI解读\n取消用 /unsubanalysis"
    )

async def unsub_analysis(update, context):
    chat_id = update.effective_chat.id
    if chat_id in _data2["analysis_subs"]:
        _data2["analysis_subs"].remove(chat_id)
        _save2()
        await update.message.reply_text("已取消每日分析推送")
    else:
        await update.message.reply_text("你还没订阅")

# 定时推送（job调用）
async def daily_analysis(context):
    if not _data2["analysis_subs"]:
        return
    from handlers.analysis import build_analysis_text
    from handlers.ai import build_ai_text
    try:
        tech = await build_analysis_text("BTC")
        ai = await build_ai_text("BTC")
        text = f"📅 *每日分析* (BTC)\n\n{tech}\n\n━━━━━━\n{ai}"
    except Exception as e:
        logging.error(f"每日分析生成失败: {e}")
        return
    for chat_id in _data2["analysis_subs"]:
        try:
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
        except Exception as e:
            logging.error(f"每日分析推送失败 {chat_id}: {e}")
