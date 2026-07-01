import logging
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from api import get_market_leaders, get_fear_greed, get_gas_price

# 看板显示市值前 N（过滤掉稳定币后）
DASH_TOP_N = 15
# 稳定币不参与看板展示（价格恒为1、涨跌≈0，占位无意义）
STABLES = {"USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE", "USDS", "PYUSD", "BUSD"}

def _fmt_price(p):
    """按价格量级智能保留小数，避免小价币显示成 $0.00。"""
    if p >= 1:
        return f"${p:,.2f}"
    elif p >= 0.01:
        return f"${p:.4f}"
    elif p >= 0.0001:
        return f"${p:.6f}"
    else:
        return f"${p:.8f}"

async def build_dashboard():
    """生成看板文本"""
    # 并发拿市值榜、情绪、gas
    leaders_task = get_market_leaders(DASH_TOP_N + 8)
    fear_task = get_fear_greed()
    gas_task = get_gas_price()

    leaders, fear, gas = await asyncio.gather(
        leaders_task, fear_task, gas_task,
        return_exceptions=True
    )

    lines = ["📊 *市场看板*\n"]

    # 价格（市值前 N，跳过稳定币）
    lines.append(f"💰 *市值 Top {DASH_TOP_N}*")
    if isinstance(leaders, list) and leaders:
        shown = 0
        for c in leaders:
            if c["symbol"] in STABLES:
                continue
            emoji = "📈" if c["change"] >= 0 else "📉"
            lines.append(f"{emoji} {c['symbol']}: {_fmt_price(c['price'])} ({c['change']:+.2f}%)")
            shown += 1
            if shown >= DASH_TOP_N:
                break
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
