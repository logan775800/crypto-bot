"""持续波动监控：盯住指定币，价格从基准涨跌超阈值就提醒，报后以新价为基准继续盯。

与 /alertpct（一次性）的区别：本功能是持续的，报警后自动重设基准，长期盯盘。
价格从多所取（OKX/币安/Bybit，现货+永续都试），兼容 KORU/RAM 这类小盘/合约币，
不受主流币列表 COIN_IDS 限制。

命令：
  /watchpct DOGE 5        盯 DOGE，每从基准涨跌超 ±5% 提醒
  /watchpcts              查看我在盯的币
  /unwatchpct DOGE        取消盯 DOGE（/unwatchpct all 全部取消）
后台 check_watchpct 每 60s 轮询。
"""
import time
import logging
import httpx
from telegram import Update
from telegram.ext import ContextTypes
from storage import data, save_data

OKX = "https://www.okx.com"
BN = "https://api.binance.com"
BYBIT = "https://api.bybit.com"

COOLDOWN = 180          # 同一币两次提醒最短间隔秒，防急涨急跌时刷屏
MAX_PER_CHAT = 30       # 每个会话最多盯多少个币


def fmt(p):
    """价格显示：大数保留2位，小数按量级保留有效位。"""
    if p >= 1:
        return f"{p:,.2f}"
    elif p >= 0.01:
        return f"{p:.4f}"
    elif p >= 0.0001:
        return f"{p:.6f}"
    return f"{p:.8f}"


async def resolve_price(symbol):
    """多所取现价，兼容小盘/合约币。返回 (price, source) 或 (None, None)。"""
    s = symbol.upper()
    async with httpx.AsyncClient(timeout=8) as c:
        # OKX 现货 → OKX 永续
        for inst, label in ((f"{s}-USDT", "OKX"), (f"{s}-USDT-SWAP", "OKX永续")):
            try:
                r = await c.get(f"{OKX}/api/v5/market/ticker", params={"instId": inst})
                d = r.json()
                if d.get("code") == "0" and d.get("data"):
                    return float(d["data"][0]["last"]), label
            except Exception:
                pass
        # 币安现货
        try:
            r = await c.get(f"{BN}/api/v3/ticker/price", params={"symbol": f"{s}USDT"})
            if r.status_code == 200 and "price" in r.json():
                return float(r.json()["price"]), "Binance"
        except Exception:
            pass
        # Bybit 现货 → Bybit 永续
        for cat, label in (("spot", "Bybit"), ("linear", "Bybit永续")):
            try:
                r = await c.get(f"{BYBIT}/v5/market/tickers",
                                params={"category": cat, "symbol": f"{s}USDT"})
                d = r.json()
                lst = d.get("result", {}).get("list") or []
                if d.get("retCode") == 0 and lst:
                    return float(lst[0]["lastPrice"]), label
            except Exception:
                pass
    return None, None


# ---------- 命令 ----------
async def watchpct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "用法：/watchpct 币 百分比\n"
            "例：/watchpct DOGE 5  （DOGE 每涨跌超 ±5% 提醒，报后以新价继续盯）\n"
            "支持小盘/合约币（如 KORU、RAM）。取消：/unwatchpct 币")
        return
    symbol = args[0].upper()
    try:
        pct = float(args[1])
    except ValueError:
        await update.message.reply_text("百分比要是数字，例：/watchpct DOGE 5")
        return
    if pct <= 0:
        await update.message.reply_text("百分比要大于 0")
        return

    price, src = await resolve_price(symbol)
    if price is None:
        await update.message.reply_text(
            f"没查到 {symbol} 的价格。用交易所里的交易对基名试试（如 KORU、RAM、DOGE）")
        return

    chat_id = update.effective_chat.id
    lst = data.setdefault("watchpct", [])
    mine = [w for w in lst if w["chat_id"] == chat_id]
    # 去重：同 (会话,币) 覆盖
    existed = any(w["symbol"] == symbol for w in mine)
    if not existed and len(mine) >= MAX_PER_CHAT:
        await update.message.reply_text(f"最多同时盯 {MAX_PER_CHAT} 个币，先 /unwatchpct 取消几个")
        return
    lst[:] = [w for w in lst if not (w["chat_id"] == chat_id and w["symbol"] == symbol)]
    lst.append({
        "chat_id": chat_id, "symbol": symbol, "pct": pct,
        "base": price, "src": src, "last_ts": 0,
        "set_by": update.effective_user.first_name,
    })
    save_data()
    verb = "已更新" if existed else "已开启"
    await update.message.reply_text(
        f"👁 {verb}持续波动监控：*{symbol}* 每涨跌超 *±{pct}%* 提醒\n"
        f"当前基准 ${fmt(price)}（{src}）\n"
        f"报警后自动以新价为基准继续盯（{COOLDOWN//60}分钟冷却）。\n"
        f"查看 /watchpcts　取消 /unwatchpct {symbol}",
        parse_mode="Markdown")


async def unwatchpct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    lst = data.setdefault("watchpct", [])
    if not context.args:
        await update.message.reply_text("用法：/unwatchpct 币　或　/unwatchpct all 全部取消")
        return
    arg = context.args[0].upper()
    before = len(lst)
    if arg == "ALL":
        lst[:] = [w for w in lst if w["chat_id"] != chat_id]
    else:
        lst[:] = [w for w in lst if not (w["chat_id"] == chat_id and w["symbol"] == arg)]
    save_data()
    removed = before - len(lst)
    await update.message.reply_text(f"已取消 {removed} 个波动监控" if removed else "没找到对应的监控")


async def watchpcts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    mine = [w for w in data.get("watchpct", []) if w["chat_id"] == chat_id]
    if not mine:
        await update.message.reply_text("你还没盯任何币。/watchpct DOGE 5 开一个")
        return
    lines = ["👁 *持续波动监控*"]
    for w in mine:
        lines.append(f"• {w['symbol']}  ±{w['pct']}%  基准 ${fmt(w['base'])}（{w.get('src','?')}）")
    lines.append("\n取消 /unwatchpct 币")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ---------- 后台轮询 ----------
async def check_watchpct(context: ContextTypes.DEFAULT_TYPE):
    lst = data.get("watchpct", [])
    if not lst:
        return
    now = time.time()
    # 同一币只取一次价
    prices = {}
    for sym in {w["symbol"] for w in lst}:
        try:
            prices[sym], _ = await resolve_price(sym)
        except Exception as e:
            logging.error(f"波动监控取价 {sym} 失败: {e}")
            prices[sym] = None

    changed = False
    for w in lst:
        p = prices.get(w["symbol"])
        if not p:
            continue
        base = w["base"]
        if base <= 0:
            w["base"] = p
            changed = True
            continue
        ch = (p - base) / base * 100
        if abs(ch) >= w["pct"] and now - w.get("last_ts", 0) >= COOLDOWN:
            arrow = "📈 涨" if ch > 0 else "📉 跌"
            try:
                await context.bot.send_message(
                    w["chat_id"],
                    f"{arrow} *{w['symbol']}* {ch:+.2f}%！\n"
                    f"${fmt(base)} → ${fmt(p)}（阈值 ±{w['pct']}%）",
                    parse_mode="Markdown")
            except Exception as e:
                logging.error(f"波动监控推送失败 {w['chat_id']}: {e}")
            w["base"] = p          # 以新价为基准继续盯
            w["last_ts"] = now
            changed = True
    if changed:
        save_data()
