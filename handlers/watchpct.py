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

COOLDOWN = 60           # 同一币两次提醒最短间隔秒，防急涨急跌时刷屏（配合重设基准）
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


def norm_symbol(sym):
    """规范化币名：用户可能粘贴完整交易对(TUSDT/BTCUSDT)，去掉结尾 USDT 取基名。"""
    s = (sym or "").upper().strip()
    if s.endswith("USDT") and len(s) > 4:   # TUSDT→T, BTCUSDT→BTC；'USDT' 本身不动
        s = s[:-4]
    return s


async def _fetch(c, symbol, source):
    """从指定来源取一次价，返回 (price, source) 或 None。"""
    s = symbol.upper()
    try:
        if source in ("OKX", "OKX永续"):
            inst = f"{s}-USDT-SWAP" if source == "OKX永续" else f"{s}-USDT"
            r = await c.get(f"{OKX}/api/v5/market/ticker", params={"instId": inst})
            d = r.json()
            if d.get("code") == "0" and d.get("data"):
                return float(d["data"][0]["last"]), source
        elif source == "Binance":
            r = await c.get(f"{BN}/api/v3/ticker/price", params={"symbol": f"{s}USDT"})
            if r.status_code == 200 and "price" in r.json():
                return float(r.json()["price"]), source
        elif source == "Binance永续":
            r = await c.get(f"{FAPI}/fapi/v1/ticker/price", params={"symbol": f"{s}USDT"})
            if r.status_code == 200 and "price" in r.json():
                return float(r.json()["price"]), source
        elif source in ("Bybit", "Bybit永续"):
            cat = "linear" if source == "Bybit永续" else "spot"
            r = await c.get(f"{BYBIT}/v5/market/tickers",
                            params={"category": cat, "symbol": f"{s}USDT"})
            d = r.json()
            lst = d.get("result", {}).get("list") or []
            if d.get("retCode") == 0 and lst:
                return float(lst[0]["lastPrice"]), source
    except Exception:
        pass
    return None


# 各模式下的取价优先级（第一个取到就用）
CHAINS = {
    "swap": ["OKX永续", "Bybit永续", "Binance永续"],
    "spot": ["OKX", "Binance", "Bybit"],
    "auto": ["OKX", "OKX永续", "Binance", "Bybit", "Bybit永续"],
}


async def resolve_price(symbol, market="auto"):
    """多所取价，兼容小盘/合约币。market: auto/spot/swap。返回 (price, source) 或 (None, None)。"""
    async with httpx.AsyncClient(timeout=8) as c:
        for src in CHAINS.get(market, CHAINS["auto"]):
            res = await _fetch(c, symbol, src)
            if res:
                return res
    return None, None


async def fetch_pinned(symbol, source):
    """从固定来源取价（波动监控轮询用，保证与基准同一交易所）。返回 price 或 None。"""
    async with httpx.AsyncClient(timeout=8) as c:
        res = await _fetch(c, symbol, source)
    return res[0] if res else None


def on_tick(source, sym, price):
    """WebSocket 实时 tick 命中检查（同步，供 contract_ws 秒级调用）。
    source 形如 'OKX永续'/'Bybit永续'，只匹配来源相同的监控。
    命中即就地更新基准/冷却，返回 [(chat_id, text), ...] 交由调用方即时推送。"""
    lst = data.get("watchpct")
    if not lst:
        return []
    now = time.time()
    out = []
    for w in lst:
        if w.get("symbol") != sym or w.get("src") != source:
            continue
        base = w.get("base", 0)
        if base <= 0:
            w["base"] = price
            continue
        ch = (price - base) / base * 100
        if abs(ch) >= w["pct"] and now - w.get("last_ts", 0) >= COOLDOWN:
            arrow = "📈 涨" if ch > 0 else "📉 跌"
            mkt_tag = "（合约）" if w.get("market") == "swap" else ("（现货）" if w.get("market") == "spot" else "")
            out.append((w["chat_id"],
                        f"{arrow} *{sym}*{mkt_tag} {ch:+.2f}%！\n"
                        f"${fmt(base)} → ${fmt(price)}（阈值 ±{w['pct']}%，{source} 实时）"))
            w["base"] = price
            w["last_ts"] = now
    return out


# ---------- 设置逻辑（命令与菜单共用）----------
async def add_watch(chat_id, symbol, pct, set_by, market="auto"):
    """新增/更新一个持续波动监控。market: auto/spot/swap。返回 (成功, Markdown文本)。"""
    symbol = norm_symbol(symbol)
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
    # OKX/Bybit 永续走 WebSocket 秒级实时；其余走约1分钟轮询
    realtime = "⚡ 秒级实时(WebSocket)" if src in ("OKX永续", "Bybit永续") else "约1分钟轮询"
    return True, (
        f"👁 {verb}持续波动监控：*{symbol}*{mkt_tag} 每涨跌超 *±{pct}%* 提醒\n"
        f"当前基准 ${fmt(price)}（{src}）\n"
        f"触发方式：{realtime}，报警后自动以新价为基准继续盯（{COOLDOWN//60}分钟冷却）。")


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
    symbol = norm_symbol(args[0])
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
    if arg != "ALL":
        arg = norm_symbol(arg)
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
    # 按(币,固定来源)取价：锁定与基准同一交易所，避免跨所比价算歪
    prices = {}
    for key in {(w["symbol"], w.get("src")) for w in lst}:
        sym, src = key
        try:
            if src:
                prices[key] = await fetch_pinned(sym, src)
            else:                       # 老数据没记来源 → 退回按模式解析
                prices[key], _ = await resolve_price(sym, "auto")
        except Exception as e:
            logging.error(f"波动监控取价 {key} 失败: {e}")
            prices[key] = None

    changed = False
    for w in lst:
        p = prices.get((w["symbol"], w.get("src")))
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
