import logging
from telegram import Update
from telegram.ext import ContextTypes
from api import get_price, get_prices, get_market_data, get_top_movers, get_price_okx
from config import COIN_IDS
from suggest import suggest_symbol

# 法币符号
FIAT = {"usd": "$", "cny": "¥", "eur": "€", "jpy": "¥"}

# /price BTC [cny]  —— 支持法币
async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("用法：/price BTC 或 /price BTC cny")
        return
    symbol = context.args[0].upper()
    vs = context.args[1].lower() if len(context.args) > 1 else "usd"
    if vs not in FIAT:
        vs = "usd"
    await update.effective_chat.send_action("typing")
    try:
        result = await get_price(symbol, vs)
        if result is None:
            # CoinGecko查不到，尝试OKX（覆盖OKX上的新币/小币）
            okx_result = await get_price_okx(symbol)
            if okx_result:
                emoji = "📈" if okx_result["change"] >= 0 else "📉"
                await update.message.reply_text(
                    f"{emoji} {symbol}\n价格: ${okx_result['price']:,.4g}\n"
                    f"24h涨跌: {okx_result['change']:+.2f}%\n"
                    f"(数据来自 OKX)"
                )
                return
            suggestions = suggest_symbol(symbol)
            if suggestions:
                await update.message.reply_text(
                    f"找不到 {symbol}，你是不是想查：{', '.join(suggestions)}？"
                )
            else:
                await update.message.reply_text(f"找不到币种：{symbol}")
            return
        sign = FIAT.get(vs, "$")
        emoji = "📈" if result["change"] >= 0 else "📉"
        await update.message.reply_text(
            f"{emoji} {symbol}\n价格: {sign}{result['price']:,.2f}\n24h涨跌: {result['change']:+.2f}%"
        )
    except Exception as e:
        logging.error(f"查询出错: {e}")
        await update.message.reply_text("查询失败，请稍后再试")

# 功能2：/top 涨跌榜
async def top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_chat.send_action("typing")
    try:
        gainers, losers = await get_top_movers(5)
    except Exception as e:
        logging.error(f"涨跌榜出错: {e}")
        await update.message.reply_text("查询失败，请稍后再试")
        return
    lines = ["🚀 24h 涨幅榜 TOP5\n"]
    for i, c in enumerate(gainers, 1):
        lines.append(f"{i}. {c['symbol']}: ${c['price']:,.4g} (+{c['change']:.2f}%)")
    lines.append("\n📉 24h 跌幅榜 TOP5\n")
    for i, c in enumerate(losers, 1):
        lines.append(f"{i}. {c['symbol']}: ${c['price']:,.4g} ({c['change']:.2f}%)")
    await update.message.reply_text("\n".join(lines))

# 功能3：/compare BTC ETH SOL 多币对比
async def compare(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("用法：/compare BTC ETH SOL")
        return
    symbols = [s.upper() for s in context.args]
    valid = [s for s in symbols if s in COIN_IDS]
    if not valid:
        await update.message.reply_text("没有支持的币种")
        return
    await update.effective_chat.send_action("typing")
    try:
        prices = await get_prices(valid)
    except Exception as e:
        logging.error(f"对比出错: {e}")
        await update.message.reply_text("查询失败")
        return
    lines = ["📊 多币对比\n"]
    for sym in valid:
        info = prices.get(sym)
        if not info:
            continue
        emoji = "📈" if info["change"] >= 0 else "📉"
        lines.append(f"{emoji} {sym}: ${info['price']:,.2f} ({info['change']:+.2f}%)")
    await update.message.reply_text("\n".join(lines))

# 功能4 & 6：/info BTC 详细信息（市值、成交量、7d/30d涨跌）
async def info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("用法：/info BTC")
        return
    symbol = context.args[0].upper()
    if symbol not in COIN_IDS:
        await update.message.reply_text(f"不支持的币种：{symbol}")
        return
    await update.effective_chat.send_action("typing")
    try:
        md = await get_market_data([symbol])
    except Exception as e:
        logging.error(f"详情出错: {e}")
        await update.message.reply_text("查询失败")
        return
    d = md.get(symbol)
    if not d:
        await update.message.reply_text("没有数据")
        return
    await update.message.reply_text(
        f"📋 {symbol} 详细信息\n\n"
        f"价格: ${d['price']:,.2f}\n"
        f"市值排名: #{d['market_cap_rank']}\n"
        f"市值: ${d['market_cap']:,.0f}\n"
        f"24h成交量: ${d['volume']:,.0f}\n"
        f"24h最高/最低: ${d['high_24h']:,.2f} / ${d['low_24h']:,.2f}\n\n"
        f"涨跌幅:\n"
        f"  24h: {d['change_24h']:+.2f}%\n"
        f"  7天: {d['change_7d']:+.2f}%\n"
        f"  30天: {d['change_30d']:+.2f}%"
    )


# 功能18：价格计算器 /calc 0.5 BTC  或  /calc 1000 usd BTC
async def calc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "用法：\n"
            "/calc 0.5 BTC - 0.5个BTC值多少钱\n"
            "/calc 1000 usd BTC - 1000美元能买多少BTC"
        )
        return

    # 模式1：/calc 数量 币种  → 算总价值
    # 模式2：/calc 金额 usd 币种 → 算能买多少币
    try:
        num = float(args[0])
    except ValueError:
        await update.message.reply_text("第一个参数要是数字")
        return

    # 判断模式
    if len(args) >= 3 and args[1].lower() == "usd":
        # 模式2：美元换币
        symbol = args[2].upper()
        if symbol not in COIN_IDS:
            await update.message.reply_text(f"不支持的币种：{symbol}")
            return
        try:
            result = await get_price(symbol)
            price_usd = result["price"]
            coins = num / price_usd
            await update.message.reply_text(
                f"💰 ${num:,.2f} ≈ {coins:.8g} {symbol}\n"
                f"({symbol} 现价 ${price_usd:,.2f})"
            )
        except Exception as e:
            logging.error(f"计算出错: {e}")
            await update.message.reply_text("查询失败")
    else:
        # 模式1：币换美元
        symbol = args[1].upper()
        if symbol not in COIN_IDS:
            await update.message.reply_text(f"不支持的币种：{symbol}")
            return
        try:
            result = await get_price(symbol)
            price_usd = result["price"]
            value = num * price_usd
            await update.message.reply_text(
                f"💰 {num:g} {symbol} ≈ ${value:,.2f}\n"
                f"({symbol} 现价 ${price_usd:,.2f})"
            )
        except Exception as e:
            logging.error(f"计算出错: {e}")
            await update.message.reply_text("查询失败")
