import logging
import asyncio
import datetime
import time
import httpx
from telegram import Update
from telegram.ext import ContextTypes
from storage import data, save_data
from api import get_prices, get_fear_greed

OKX_BASE = "https://www.okx.com"

async def _okx_get(path, params=None):
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{OKX_BASE}{path}", params=params or {})
        resp.raise_for_status()
        return resp.json()

async def build_summary():
    """生成每日市场总结"""
    lines = [f"📊 *每日市场总结* ({datetime.datetime.now().strftime('%m-%d')})\n"]

    # 1. 主流币表现
    try:
        prices = await get_prices(["BTC", "ETH", "BNB", "SOL"])
        lines.append("💰 *主流币*")
        for sym in ["BTC", "ETH", "BNB", "SOL"]:
            info = prices.get(sym)
            if info:
                emoji = "📈" if info["change"] >= 0 else "📉"
                lines.append(f"{emoji} {sym}: ${info['price']:,.2f} ({info['change']:+.2f}%)")
    except Exception as e:
        logging.error(f"总结-主流币出错: {e}")

    # 2. 市场情绪
    try:
        fg = await get_fear_greed()
        lines.append(f"\n😱 *市场情绪*: {fg['value']}/100 {fg['classification']}")
    except Exception:
        pass

    # 3. 涨跌幅热点（OKX）
    try:
        d = await _okx_get("/api/v5/market/tickers", {"instType": "SPOT"})
        if d["code"] == "0":
            coins = []
            for t in d["data"]:
                if not t["instId"].endswith("-USDT"):
                    continue
                try:
                    last = float(t["last"]); op = float(t["open24h"]); vol = float(t["volCcy24h"])
                    if op <= 0 or vol < 1000000:
                        continue
                    change = (last - op) / op * 100
                    coins.append({"sym": t["instId"].replace("-USDT", ""), "change": change})
                except (ValueError, KeyError):
                    continue
            gainers = sorted(coins, key=lambda x: x["change"], reverse=True)[:3]
            losers = sorted(coins, key=lambda x: x["change"])[:3]
            lines.append("\n🔥 *今日热点*")
            lines.append("涨: " + " ".join(f"{c['sym']}+{c['change']:.0f}%" for c in gainers))
            lines.append("跌: " + " ".join(f"{c['sym']}{c['change']:.0f}%" for c in losers))
    except Exception as e:
        logging.error(f"总结-热点出错: {e}")

    # 4. 近期解锁预告（未来7天）
    try:
        from handlers.unlock import get_unlock_events, SYMBOL_MAP
        now = time.time()
        week = now + 7 * 86400
        upcoming = []
        async def chk(sym, proj):
            try:
                name, future, total = await get_unlock_events(proj)
                if not future or not total:
                    return None
                for e in future:
                    if now < e["timestamp"] <= week:
                        toks = e.get("noOfTokens", [])
                        pct = (sum(toks)/total*100) if toks and total else 0
                        if pct >= 1.0:
                            return {"sym": sym, "ts": e["timestamp"], "pct": pct}
                return None
            except Exception:
                return None
        res = await asyncio.gather(*[chk(s, p) for s, p in list(SYMBOL_MAP.items())[:15]])
        upcoming = [r for r in res if r]
        if upcoming:
            upcoming.sort(key=lambda x: x["ts"])
            lines.append("\n🔓 *近期解锁*")
            for u in upcoming[:3]:
                dt = datetime.datetime.fromtimestamp(u["ts"]).strftime("%m-%d")
                lines.append(f"{dt} {u['sym']} 解锁{u['pct']:.1f}%")
    except Exception as e:
        logging.error(f"总结-解锁出错: {e}")

    lines.append("\n⚠️ 数据仅供参考，不构成投资建议")
    return "\n".join(lines)

# /summary 手动查
async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📊 生成市场总结...")
    try:
        text = await build_summary()
        await update.message.reply_text(text, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"总结出错: {e}")
        await update.message.reply_text("生成失败")

# 订阅每日总结
async def sub_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    data.setdefault("summary_subs", [])
    if chat_id in data["summary_subs"]:
        await update.message.reply_text("已订阅每日总结 ✅")
        return
    data["summary_subs"].append(chat_id)
    save_data()
    await update.message.reply_text(
        "✅ 已订阅每日市场总结！\n每天早上推送市场全景\n(大盘+情绪+热点+解锁)\n取消用 /unsubsummary"
    )

async def unsub_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    data.setdefault("summary_subs", [])
    if chat_id in data["summary_subs"]:
        data["summary_subs"].remove(chat_id)
        save_data()
        await update.message.reply_text("已取消每日总结")
    else:
        await update.message.reply_text("你还没订阅")

# 定时推送（job调用）
async def daily_summary(context: ContextTypes.DEFAULT_TYPE):
    data.setdefault("summary_subs", [])
    if not data["summary_subs"]:
        return
    try:
        text = await build_summary()
    except Exception as e:
        logging.error(f"每日总结生成失败: {e}")
        return
    for chat_id in data["summary_subs"]:
        try:
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
        except Exception as e:
            logging.error(f"每日总结推送失败 {chat_id}: {e}")
