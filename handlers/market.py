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


from api import get_gas_price

# 功能16：Gas费查询 /gas
async def gas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        gwei = await get_gas_price()
    except Exception as e:
        logging.error(f"gas查询出错: {e}")
        await update.message.reply_text("获取失败，请稍后再试")
        return

    # 简单分级提示
    if gwei < 10:
        level = "🟢 很低，适合转账"
    elif gwei < 30:
        level = "🟡 正常"
    elif gwei < 80:
        level = "🟠 偏高"
    else:
        level = "🔴 拥堵，建议等等"

    # 估算一笔普通转账成本（21000 gas）
    eth_cost = gwei * 21000 / 1e9  # ETH

    await update.message.reply_text(
        f"⛽ 以太坊 Gas 费\n\n"
        f"当前: {gwei:.2f} gwei\n"
        f"{level}\n\n"
        f"普通转账(21000 gas)约: {eth_cost:.6f} ETH"
    )
