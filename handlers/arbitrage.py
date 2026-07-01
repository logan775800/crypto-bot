import logging
import asyncio
import time
import httpx
from telegram import Update
from telegram.ext import ContextTypes
from config import COIN_IDS
from storage import data, save_data

TAKER_ROUNDTRIP = 0.2   # 买+卖两次 taker 手续费约 0.2%(不含提币费)
STABLES = {"USDT", "USDC", "DAI", "TUSD", "FDUSD", "USDE", "USDS"}

# ================= 单币各所现价（/arb 用）=================
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

async def get_bybit(client, symbol):
    try:
        r = await client.get(f"https://api.bybit.com/v5/market/tickers?category=spot&symbol={symbol}USDT")
        return float(r.json()["result"]["list"][0]["lastPrice"])
    except Exception:
        return None

async def get_gate(client, symbol):
    try:
        r = await client.get(f"https://api.gateio.ws/api/v4/spot/tickers?currency_pair={symbol}_USDT")
        return float(r.json()[0]["last"])
    except Exception:
        return None

EXCHANGES = [("币安", get_binance), ("OKX", get_okx), ("Coinbase", get_coinbase),
             ("Bybit", get_bybit), ("Gate", get_gate)]

async def _all_prices(symbol):
    async with httpx.AsyncClient(timeout=10) as client:
        results = await asyncio.gather(*[fn(client, symbol) for _, fn in EXCHANGES])
    return {name: p for (name, _), p in zip(EXCHANGES, results) if p and p > 0}

def _arb_lines(symbol, prices):
    s = sorted(prices.items(), key=lambda x: x[1])
    lines = [f"💱 *{symbol} 多所比价*\n"]
    for name, p in s:
        lines.append(f"{name}: ${p:,.6g}")
    low, high = s[0], s[-1]
    gross = (high[1] - low[1]) / low[1] * 100 if low[1] else 0
    net = gross - TAKER_ROUNDTRIP
    lines.append("━━━━━━")
    lines.append(f"最低 {low[0]} → 最高 {high[0]}")
    lines.append(f"毛价差: {gross:.3f}%")
    lines.append(f"扣手续费(约0.2%)后净: {net:.3f}%")
    if net > 0.3:
        lines.append("⚠️ 仍需扣提币费/滑点/时间差，实际常被吃掉")
    else:
        lines.append("💡 扣费后基本无套利空间")
    lines.append("⚠️ 不构成投资建议")
    return "\n".join(lines)

async def arb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = context.args[0].upper() if context.args else "BTC"
    await update.effective_chat.send_action("typing")
    try:
        prices = await _all_prices(symbol)
        if not prices:
            await update.message.reply_text(f"未获取到 {symbol} 的交易所价格")
            return
        await update.message.reply_text(_arb_lines(symbol, prices), parse_mode="Markdown")
    except Exception as e:
        logging.error(f"比价出错: {e}")
        await update.message.reply_text("查询失败，请稍后再试")

async def build_arb_text(symbol):
    prices = await _all_prices(symbol)
    if not prices:
        return f"未获取到 {symbol} 价格"
    return _arb_lines(symbol, prices)


# ================= 套利监控（后台扫描 + 订阅告警）=================
async def _bulk_okx(client):
    try:
        r = await client.get("https://www.okx.com/api/v5/market/tickers", params={"instType": "SPOT"})
        return {t["instId"][:-5]: float(t["last"])
                for t in r.json().get("data", []) if t.get("instId", "").endswith("-USDT")}
    except Exception:
        return {}

async def _bulk_binance(client):
    try:
        r = await client.get("https://api.binance.com/api/v3/ticker/price")
        return {t["symbol"][:-4]: float(t["price"])
                for t in r.json() if t.get("symbol", "").endswith("USDT")}
    except Exception:
        return {}

async def _bulk_bybit(client):
    try:
        r = await client.get("https://api.bybit.com/v5/market/tickers", params={"category": "spot"})
        return {t["symbol"][:-4]: float(t["lastPrice"])
                for t in r.json().get("result", {}).get("list", []) if t.get("symbol", "").endswith("USDT")}
    except Exception:
        return {}

async def _bulk_gate(client):
    try:
        r = await client.get("https://api.gateio.ws/api/v4/spot/tickers")
        return {t["currency_pair"][:-5]: float(t["last"])
                for t in r.json() if t.get("currency_pair", "").endswith("_USDT")}
    except Exception:
        return {}

BULK = [("币安", _bulk_binance), ("OKX", _bulk_okx), ("Bybit", _bulk_bybit), ("Gate", _bulk_gate)]
ARB_COOLDOWN = 1800   # 同币30分钟内不重复告警

# /arbwatch 0.8  或  /arbwatch off
async def set_arb_watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    data.setdefault("arb_subs", {})
    if context.args and context.args[0].lower() in ("off", "取消", "关闭"):
        if chat_id in data["arb_subs"]:
            del data["arb_subs"][chat_id]
            save_data()
        await update.message.reply_text("已关闭套利监控")
        return
    th = 0.8
    if context.args:
        try:
            th = float(context.args[0])
        except ValueError:
            pass
    data["arb_subs"][chat_id] = {"threshold": th}
    save_data()
    await update.message.reply_text(
        f"✅ 已开启套利监控：跨所净价差 ≥ {th:g}% 时通知你\n"
        f"(每5分钟扫主流币，同币30分钟冷却)\n"
        f"调阈值 /arbwatch 1.5，关闭 /arbwatch off\n"
        f"⚠️ 净价差已扣约0.2%手续费，但未含提币费/滑点，实际常更低"
    )

# 后台扫描（job）
async def scan_arb(context: ContextTypes.DEFAULT_TYPE):
    subs = data.get("arb_subs", {})
    if not subs:
        return
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            maps = await asyncio.gather(*[fn(client) for _, fn in BULK])
    except Exception as e:
        logging.error(f"套利扫描取价出错: {e}")
        return
    books = {BULK[i][0]: maps[i] for i in range(len(BULK)) if maps[i]}
    if len(books) < 2:   # 至少两个所才能比
        return
    now = time.time()
    data.setdefault("arb_alerted", {})

    opps = []
    for sym in COIN_IDS:
        if sym in STABLES:
            continue
        pts = [(ex, m[sym]) for ex, m in books.items() if sym in m and m[sym] > 0]
        if len(pts) < 2:
            continue
        pts.sort(key=lambda x: x[1])
        low, high = pts[0], pts[-1]
        gross = (high[1] - low[1]) / low[1] * 100 if low[1] else 0
        net = gross - TAKER_ROUNDTRIP
        if net > 0:
            opps.append((sym, net, low[0], low[1], high[0], high[1]))

    for chat_id, cfg in list(subs.items()):
        th = cfg.get("threshold", 0.8)
        hit = [o for o in opps if o[1] >= th and now - data["arb_alerted"].get(o[0], 0) >= ARB_COOLDOWN]
        hit.sort(key=lambda x: x[1], reverse=True)
        hit = hit[:5]
        if not hit:
            continue
        lines = [f"💱 *套利机会*（净价差≥{th:g}%）\n"]
        for sym, net, le, lp, he, hp in hit:
            lines.append(f"{sym}: {le} ${lp:,.6g} → {he} ${hp:,.6g}  净 {net:.2f}%")
            data["arb_alerted"][sym] = now
        lines.append("\n⚠️ 未含提币费/滑点，实际可能无利可图。不构成投资建议")
        try:
            await context.bot.send_message(chat_id=int(chat_id), text="\n".join(lines), parse_mode="Markdown")
        except Exception as e:
            logging.error(f"套利告警推送失败 {chat_id}: {e}")
    save_data()
