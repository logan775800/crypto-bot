import logging
import httpx
from telegram import Update
from telegram.ext import ContextTypes
from config import COIN_IDS, AI_API_KEY, AI_BASE_URL, AI_MODEL
from api import get_daily_prices, get_price
from indicators import analyze as do_analyze, macd

async def ask_ai(prompt: str):
    """调用中转站AI"""
    url = AI_BASE_URL.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {AI_API_KEY}", "Content-Type": "application/json"}
    body = {
        "model": AI_MODEL,
        "messages": [
            {"role": "system", "content": "你是加密货币行情分析助手。基于提供的技术指标数据，给出简洁客观的中文分析（200字内）。必须说明这不构成投资建议。不要编造数据。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.7,
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(url, headers=headers, json=body)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]

async def ask_ai_messages(messages, system=None, temperature=0.7):
    """多轮对话版：messages 是 [{role,content}...]，可选 system。用于群内@对话。"""
    url = AI_BASE_URL.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {AI_API_KEY}", "Content-Type": "application/json"}
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.extend(messages)
    body = {"model": AI_MODEL, "messages": msgs, "temperature": temperature}
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(url, headers=headers, json=body)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]


async def ai_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not AI_API_KEY or not AI_BASE_URL:
        await update.message.reply_text("AI 功能未配置（缺少密钥或URL）")
        return
    if not context.args:
        await update.message.reply_text("用法：/ai BTC")
        return
    symbol = context.args[0].upper()
    if symbol not in COIN_IDS:
        await update.message.reply_text(f"不支持的币种：{symbol}")
        return

    await update.message.reply_text(f"🤖 AI 正在分析 {symbol}...")

    try:
        # 收集技术指标数据
        prices = await get_daily_prices(symbol, 35)
        cur = await get_price(symbol)
        if not prices or not cur:
            await update.message.reply_text("数据获取失败")
            return
        r = do_analyze(prices)
        macd_line, macd_sig = macd(prices)

        # 组织数据给AI
        data_text = (
            f"币种: {symbol}\n"
            f"当前价: ${cur['price']:,.2f}\n"
            f"24h涨跌: {cur['change']:+.2f}%\n"
            f"RSI(14): {r.get('rsi', 0):.1f}\n"
            f"MA7: ${r.get('ma7', 0):,.2f}\n"
            f"MA30: ${r.get('ma30', 0):,.2f}\n"
            f"MACD: {macd_sig}\n"
            f"近期价格: {[round(p) for p in prices[-7:]]}"
        )
        prompt = f"请分析以下加密货币技术指标数据：\n{data_text}\n\n给出简洁的趋势解读。"

        ai_reply = await ask_ai(prompt)

        await update.message.reply_text(
            f"🤖 {symbol} AI 分析\n\n{ai_reply}\n\n"
            f"━━━━━━━━\n⚠️ AI分析仅供参考，不构成投资建议"
        )
    except Exception as e:
        logging.error(f"AI分析出错: {e}")
        await update.message.reply_text(f"AI分析失败：{str(e)[:100]}")


async def build_ai_text(symbol):
    """返回AI分析文本（供按钮调用）"""
    if not AI_API_KEY or not AI_BASE_URL:
        return "AI未配置"
    prices = await get_daily_prices(symbol, 35)
    cur = await get_price(symbol)
    if not prices or not cur:
        return "数据获取失败"
    r = do_analyze(prices)
    ml, ms = macd(prices)
    data_text = (f"币种:{symbol} 价${cur['price']:,.2f} 24h{cur['change']:+.2f}% "
                 f"RSI:{r.get('rsi',0):.1f} MA7:${r.get('ma7',0):,.0f} MA30:${r.get('ma30',0):,.0f} MACD:{ms}")
    reply = await ask_ai(f"分析这些技术指标：{data_text}，给简洁趋势解读")
    # AI 输出是自由文本，可能含 _ * ` 等字符，转义后再嵌入 Markdown，避免整条消息渲染失败
    from handlers.util import escape_md
    return f"🤖 *{symbol} AI分析*\n\n{escape_md(reply)}\n\n⚠️ 不构成投资建议"
