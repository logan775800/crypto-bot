import logging
import httpx
from telegram import Update
from telegram.ext import ContextTypes

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
YF = "https://query1.finance.yahoo.com/v8/finance/chart/"

# 三大指数代码
INDICES = [
    ("^IXIC", "纳斯达克"),
    ("^GSPC", "标普500"),
    ("^DJI", "道琼斯"),
]

async def _get_quote(symbol):
    """查Yahoo行情，返回 dict 或 None"""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(YF + symbol, headers={"User-Agent": UA})
            resp.raise_for_status()
            d = resp.json()
            m = d["chart"]["result"][0]["meta"]
            price = m.get("regularMarketPrice")
            prev = m.get("previousClose") or m.get("chartPreviousClose")
            if price is None or prev is None:
                return None
            change = (price - prev) / prev * 100 if prev else 0
            return {
                "symbol": m.get("symbol", symbol),
                "name": m.get("shortName") or m.get("longName") or symbol,
                "price": price,
                "change": change,
                "currency": m.get("currency", "USD"),
                "high": m.get("fiftyTwoWeekHigh"),
                "low": m.get("fiftyTwoWeekLow"),
                "day_high": m.get("regularMarketDayHigh"),
                "day_low": m.get("regularMarketDayLow"),
            }
    except Exception as e:
        logging.error(f"股票查询出错 {symbol}: {e}")
        return None

# /stock AAPL
async def stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "用法：/stock AAPL\n查美股个股\n例：AAPL苹果 NVDA英伟达 TSLA特斯拉 MSFT微软"
        )
        return
    symbol = context.args[0].upper()
    await update.message.reply_text(f"📈 查询 {symbol}...")
    q = await _get_quote(symbol)
    if not q:
        await update.message.reply_text(
            f"没查到 {symbol}\n(用美股代码，如 AAPL/NVDA/TSLA)"
        )
        return
    emoji = "📈" if q["change"] >= 0 else "📉"
    lines = [f"{emoji} *{q['name']}* ({q['symbol']})\n"]
    lines.append(f"现价: ${q['price']:,.2f} ({q['change']:+.2f}%)")
    if q.get("day_high") and q.get("day_low"):
        lines.append(f"日内高/低: ${q['day_high']:,.2f} / ${q['day_low']:,.2f}")
    if q.get("high") and q.get("low"):
        lines.append(f"52周高/低: ${q['high']:,.2f} / ${q['low']:,.2f}")
    lines.append("\n(美股数据，交易时段为美东时间)")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

# /index 三大指数
async def index(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📊 查询美股三大指数...")
    import asyncio
    results = await asyncio.gather(*[_get_quote(code) for code, _ in INDICES])
    lines = ["📊 *美股三大指数*\n"]
    for (code, name), q in zip(INDICES, results):
        if q:
            emoji = "📈" if q["change"] >= 0 else "📉"
            lines.append(f"{emoji} {name}: {q['price']:,.2f} ({q['change']:+.2f}%)")
        else:
            lines.append(f"• {name}: 获取失败")
    lines.append("\n(美东时间交易，北京时间晚上至凌晨开盘)")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
