import logging
import asyncio
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from api import get_price, get_price_okx
from config import COIN_IDS


def fmt_price(p):
    """智能价格格式：大数字加逗号，小数字保留有效位"""
    if p >= 1:
        return f"{p:,.2f}"       # 1以上：58,940.00
    elif p >= 0.01:
        return f"{p:.4f}"        # 0.01-1：0.0378
    elif p >= 0.0001:
        return f"{p:.6f}"        # 很小：0.000123
    else:
        return f"{p:.8f}"        # 极小：0.00000012

def is_group(update):
    return update.effective_chat.type in ("group", "supergroup")

async def _okx_ticker(inst):
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get("https://www.okx.com/api/v5/market/ticker", params={"instId": inst})
            d = r.json()
            if d.get("code") == "0" and d.get("data"):
                t = d["data"][0]
                last = float(t["last"]); op = float(t["open24h"])
                ch = (last - op) / op * 100 if op > 0 else 0
                return {"price": last, "change": ch}
    except Exception:
        pass
    return None

async def _okx_funding(inst):
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get("https://www.okx.com/api/v5/public/funding-rate", params={"instId": inst})
            d = r.json()
            if d.get("code") == "0" and d.get("data"):
                return float(d["data"][0]["fundingRate"]) * 100
    except Exception:
        pass
    return None

async def quick_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    if text.startswith("/"):
        return
    if " " in text or len(text) > 12 or len(text) < 2:
        return
    symbol = text.upper()
    # 群里：靠"是否真币名"防误触发（大小写都接受，但必须是已知币种）
    if is_group(update):
        # 必须是纯字母 + 是已知币种（COIN_IDS里有），才响应
        # 这样 btc/BTC/Btc 都能查，但普通聊天词不会误触发
        if not text.isalpha() or symbol not in COIN_IDS:
            # 不在已知列表的，群里静默（避免把聊天词当币名）
            # 但允许：长度>=3的纯字母大写词尝试OKX（可能是新币）
            if not (text.isupper() and len(text) >= 2 and text.isalpha()):
                return

    try:
        spot_cg = await get_price(symbol)
        spot_okx = await get_price_okx(symbol) if spot_cg is None else None
        swap_tk = await _okx_ticker(f"{symbol}-USDT-SWAP")
        swap_fr = await _okx_funding(f"{symbol}-USDT-SWAP") if swap_tk else None

        if spot_cg is None and spot_okx is None and not swap_tk:
            if not is_group(update):
                await update.message.reply_text(f"没查到 {symbol}。试试 /price {symbol} 或检查币名")
            return

        lines = [f"💎 *{symbol}*\n"]
        spot = spot_cg or spot_okx
        if spot:
            e = "📈" if spot["change"] >= 0 else "📉"
            src = "" if spot_cg else " (OKX)"
            lines.append(f"{e} 现货: ${fmt_price(spot['price'])} ({spot['change']:+.2f}%){src}")
        if swap_tk:
            e2 = "📈" if swap_tk["change"] >= 0 else "📉"
            fr_text = f" | 费率{swap_fr:+.3f}%" if swap_fr is not None else ""
            lines.append(f"{e2} 合约: ${fmt_price(swap_tk['price'])} ({swap_tk['change']:+.2f}%){fr_text}")
        else:
            lines.append("(无永续合约)")

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("📋 详情", callback_data=f"getinfo:{symbol}"),
            InlineKeyboardButton("📈 分析", callback_data=f"doanalyze:{symbol}"),
        ]])
        await update.message.reply_text("\n".join(lines), reply_markup=kb, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"快捷查价出错: {e}")
