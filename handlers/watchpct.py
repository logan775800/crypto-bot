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
FAPI = "https://fapi.binance.com"        # 币安 USDT 本位永续合约
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


MARKET_ALIASES = {
    "合约": "swap", "永续": "swap", "swap": "swap", "perp": "swap",
    "futures": "swap", "future": "swap", "u": "swap", "c": "swap",
    "现货": "spot", "spot": "spot", "s": "spot",
}


def parse_market(tok):
    """把第三参数解析成 'auto'/'spot'/'swap'。"""
    return MARKET_ALIASES.get((tok or "").strip().lower(), "auto")


async def _okx(c, inst, label):
    try:
        r = await c.get(f"{OKX}/api/v5/market/ticker", params={"instId": inst})
        d = r.json()
        if d.get("code") == "0" and d.get("data"):
            return float(d["data"][0]["last"]), label
    except Exception:
        pass
    return None


async def resolve_price(symbol, market="auto"):
    """多所取价，兼容小盘/合约币。market: auto(现货优先) / spot(只现货) / swap(只永续)。
    返回 (price, source) 或 (None, None)。"""
    s = symbol.upper()
    async with httpx.AsyncClient(timeout=8) as c:
        # 各来源探测函数（惰性调用，命中即返回）
        async def okx_spot():
            return await _okx(c, f"{s}-USDT", "OKX")

        async def okx_swap():
            return await _okx(c, f"{s}-USDT-SWAP", "OKX永续")

        async def bn_spot():
            try:
                r = await c.get(f"{BN}/api/v3/ticker/price", params={"symbol": f"{s}USDT"})
                if r.status_code == 200 and "price" in r.json():
                    return float(r.json()["price"]), "Binance"
            except Exception:
                pass
            return None

        async def bn_swap():
            try:
                r = await c.get(f"{FAPI}/fapi/v1/ticker/price", params={"symbol": f"{s}USDT"})
                if r.status_code == 200 and "price" in r.json():
                    return float(r.json()["price"]), "Binance永续"
            except Exception:
                pass
            return None

        async def bybit(cat, label):
            try:
                r = await c.get(f"{BYBIT}/v5/market/tickers",
                                params={"category": cat, "symbol": f"{s}USDT"})
                d = r.json()
                lst = d.get("result", {}).get("list") or []
                if d.get("retCode") == 0 and lst:
                    return float(lst[0]["lastPrice"]), label
            except Exception:
                pass
            return None

        if market == "swap":
            chain = [okx_swap, lambda: bybit("linear", "Bybit永续"), bn_swap]
        elif market == "spot":
            chain = [okx_spot, bn_spot, lambda: bybit("spot", "Bybit")]
        else:  # auto：现货优先，回退永续
            chain = [okx_spot, okx_swap, bn_spot,
                     lambda: bybit("spot", "Bybit"), lambda: bybit("linear", "Bybit永续")]

        for probe in chain:
            res = await probe()
            if res:
                return res
    return None, None


# ---------- 设置逻辑（命令与菜单共用）----------
async def add_watch(chat_id, symbol, pct, set_by, market="auto"):
    """新增/更新一个持续波动监控。market: auto/spot/swap。返回 (成功, Markdown文本)。"""
    symbol = symbol.upper()
    if pct <= 0:
        return False, "百分比要大于 0"
    price, src = await resolve_price(symbol, market)
    if price is None:
        kind = "合约" if market == "swap" else ("现货" if market == "spot" else "")
        return False, (f"没查到 {symbol} 的{kind}价格。"
                       + ("该币可能没有对应永续合约。" if market == "swap" else "")
                       + "用交易所里的交易对基名试试（如 KORU、RAM、DOGE）")

    lst = data.setdefault("watchpct", [])
    mine = [w for w in lst if w["chat_id"] == chat_id]
    existed = any(w["symbol"] == symbol for w in mine)
    if not existed and len(mine) >= MAX_PER_CHAT:
        return False, f"最多同时盯 {MAX_PER_CHAT} 个币，先 /unwatchpct 取消几个"
    lst[:] = [w for w in lst if not (w["chat_id"] == chat_id and w["symbol"] == symbol)]
    lst.append({
        "chat_id": chat_id, "symbol": symbol, "pct": pct, "market": market,
        "base": price, "src": src, "last_ts": 0, "set_by": set_by,
    })
    save_data()
    verb = "已更新" if existed else "已开启"
    mkt_tag = "（合约）" if market == "swap" else ("（现货）" if market == "spot" else "")
    return True, (
        f"👁 {verb}持续波动监控：*{symbol}*{mkt_tag} 每涨跌超 *±{pct}%* 提醒\n"
        f"当前基准 ${fmt(price)}（{src}）\n"
        f"报警后自动以新价为基准继续盯（{COOLDOWN//60}分钟冷却）。")


# ---------- 命令 ----------
async def watchpct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "用法：/watchpct 币 百分比 [合约]\n"
            "例：/watchpct DOGE 5      （现货优先，报后以新价继续盯）\n"
            "例：/watchpct BTC 3 合约  （强制盯永续合约价）\n"
            "支持小盘/合约币（如 KORU、RAM）。取消：/unwatchpct 币")
        return
    symbol = args[0].upper()
    try:
        pct = float(args[1])
    except ValueError:
        await update.message.reply_text("百分比要是数字，例：/watchpct DOGE 5")
        return
    market = parse_market(args[2]) if len(args) > 2 else "auto"
    ok, msg = await add_watch(update.effective_chat.id, symbol, pct,
                              update.effective_user.first_name, market)
    tail = f"\n查看 /watchpcts　取消 /unwatchpct {symbol}" if ok else ""
    await update.message.reply_text(msg + tail, parse_mode="Markdown")


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
    # 同一(币,市场)只取一次价
    prices = {}
    for key in {(w["symbol"], w.get("market", "auto")) for w in lst}:
        try:
            prices[key], _ = await resolve_price(key[0], key[1])
        except Exception as e:
            logging.error(f"波动监控取价 {key} 失败: {e}")
            prices[key] = None

    changed = False
    for w in lst:
        p = prices.get((w["symbol"], w.get("market", "auto")))
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
            mkt_tag = "（合约）" if w.get("market") == "swap" else ("（现货）" if w.get("market") == "spot" else "")
            try:
                await context.bot.send_message(
                    w["chat_id"],
                    f"{arrow} *{w['symbol']}*{mkt_tag} {ch:+.2f}%！\n"
                    f"${fmt(base)} → ${fmt(p)}（阈值 ±{w['pct']}%，{w.get('src','')}）",
                    parse_mode="Markdown")
            except Exception as e:
                logging.error(f"波动监控推送失败 {w['chat_id']}: {e}")
            w["base"] = p          # 以新价为基准继续盯
            w["last_ts"] = now
            changed = True
    if changed:
        save_data()
