import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

async def welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 有新成员进群
    for member in update.message.new_chat_members:
        # 跳过 bot 自己被拉进群的情况
        if member.is_bot:
            continue
        name = member.first_name or "朋友"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 打开功能菜单", callback_data="menu_main")],
        ])
        await update.message.reply_text(
            f"👋 欢迎 {name} 加入！\n\n"
            f"我是 *加密货币助手* 🤖，能帮大家：\n"
            f"📊 查币价、涨跌榜\n"
            f"📈 技术分析 + AI解读\n"
            f"🔔 价格预警提醒\n"
            f"🛠 多交易所比价、市场情绪、巨鲸监控\n\n"
            f"👇 点下方按钮开始，或随时发 /menu",
            reply_markup=kb, parse_mode="Markdown"
        )
