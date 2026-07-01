import logging
import asyncio
import httpx
from telegram import Update
from telegram.ext import ContextTypes

# 各交易所获取价格（公开API，无需密钥）
async def get_binance(client, symbol):
    try:
        r = await client.get(f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}USDT")
        return float(r.json()["price"])
    except Exception:
        return None

async def get_okx(client, symbol):
    try:
        r = await client.get(f"https://www.okx.com/api/v5/market/ticker?instId={symbol}-USDT")
        return float(r.json()["data"][0]["last"])
    except Exception:
        return None

async def get_coinbase(client, symbol):
    try:
        r = await client.get(f"https://api.coinbase.com/v2/prices/{symbol}-USD/spot")
        return float(r.json()["data"]["amount"])
    except Exception:
        return None

async def arb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = context.args[0].upper() if context.args else "BTC"

    await update.message.reply_text(f"💱 查询 {symbol} 各交易所价格...")

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # 并发查三家
            binance, okx, coinbase = await asyncio.gather(
                get_binance(client, symbol),
                get_okx(client, symbol),
                get_coinbase(client, symbol),
            )

        prices = {}
        if binance: prices["币安 Binance"] = binance
        if okx: prices["欧易 OKX"] = okx
        if coinbase: prices["Coinbase"] = coinbase

        if not prices:
            await update.message.reply_text(f"未能获取 {symbol} 的交易所价格")
            return

        # 排序显示
        sorted_ex = sorted(prices.items(), key=lambda x: x[1])
        lines = [f"💱 {symbol} 多交易所比价\n"]
        for name, price in sorted_ex:
            lines.append(f"{name}: ${price:,.2f}")

        # 价差
        lowest = sorted_ex[0]
        highest = sorted_ex[-1]
        spread = highest[1] - lowest[1]
        spread_pct = spread / lowest[1] * 100
        lines.append("─────────")
        lines.append(f"最低: {lowest[0]} ${lowest[1]:,.2f}")
        lines.append(f"最高: {highest[0]} ${highest[1]:,.2f}")
        lines.append(f"价差: ${spread:,.2f} ({spread_pct:.3f}%)")

        if spread_pct > 0.5:
            lines.append("\n⚠️ 价差较大（注意：实际套利需考虑手续费、提币费、滑点、时间差，价差常被吃掉）")
        else:
            lines.append("\n💡 价差较小，正常范围")

        lines.append("\n⚠️ 仅供参考，不构成投资建议")
        await update.message.reply_text("\n".join(lines))

    except Exception as e:
        logging.error(f"比价出错: {e}")
        await update.message.reply_text("查询失败，请稍后再试")


async def build_arb_text(symbol):
    """返回比价文本（供按钮调用）"""
    async with httpx.AsyncClient(timeout=10) as client:
        binance, okx, coinbase = await asyncio.gather(
            get_binance(client, symbol), get_okx(client, symbol), get_coinbase(client, symbol),
        )
    prices = {}
    if binance: prices["币安"] = binance
    if okx: prices["OKX"] = okx
    if coinbase: prices["Coinbase"] = coinbase
    if not prices:
        return f"未获取到 {symbol} 价格"
    s = sorted(prices.items(), key=lambda x: x[1])
    lines = [f"💱 *{symbol} 比价*\n"]
    for name, p in s:
        lines.append(f"{name}: ${p:,.2f}")
    spread = s[-1][1] - s[0][1]
    pct = spread / s[0][1] * 100
    lines.append(f"━━━━━━\n价差: ${spread:,.2f} ({pct:.3f}%)")
    lines.append("⚠️ 实际套利需扣手续费等，不构成投资建议")
    return "\n".join(lines)
