import logging
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from api import get_prices, get_fear_greed, get_gas_price

DASH_COINS = ["BTC", "ETH", "BNB", "SOL"]

async def build_dashboard():
    """生成看板文本"""
    # 并发拿价格、情绪、gas
    prices_task = get_prices(DASH_COINS)
    fear_task = get_fear_greed()
    gas_task = get_gas_price()

    prices, fear, gas = await asyncio.gather(
        prices_task, fear_task, gas_task,
        return_exceptions=True
    )

    lines = ["📊 *市场看板*\n"]

    # 价格
    lines.append("💰 *主流币*")
    if isinstance(prices, dict):
        for sym in DASH_COINS:
            info = prices.get(sym)
            if info:
                emoji = "📈" if info["change"] >= 0 else "📉"
                lines.append(f"{emoji} {sym}: ${info['price']:,.2f} ({info['change']:+.2f}%)")
    else:
        lines.append("价格获取失败")

    # 市场情绪
    lines.append("\n😱 *市场情绪*")
    if isinstance(fear, dict):
        lines.append(f"恐惧贪婪: {fear['value']}/100 - {fear['classification']}")
    else:
        lines.append("情绪获取失败")

    # Gas
    lines.append("\n⛽ *以太坊Gas*")
    if isinstance(gas, (int, float)):
        lines.append(f"{gas:.2f} gwei")
    else:
        lines.append("Gas获取失败")

    lines.append("\n⚠️ 数据仅供参考，不构成投资建议")
    return "\n".join(lines)

async def dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📊 正在生成市场看板...")
    try:
        text = await build_dashboard()
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 刷新", callback_data="dash_refresh"),
             InlineKeyboardButton("📋 菜单", callback_data="menu_main")],
        ])
        await update.message.reply_text(text, reply_markup=kb, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"看板出错: {e}")
        await update.message.reply_text("生成失败，请稍后再试")
