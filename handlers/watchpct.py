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
from telegram import Update
from telegram.ext import ContextTypes
from storage import data, save_data

# 各所的取价端点已收进 handlers.source，这里不再自己维护一份

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


EXCHANGE_ALIASES = {
    "okx": "okx", "欧易": "okx", "ok": "okx",
    "binance": "binance", "币安": "binance", "bn": "binance", "bnb": "binance",
    "bybit": "bybit", "by": "bybit", "b": "bybit",
    "gate": "gate", "gateio": "gate", "芝麻": "gate", "芝麻开门": "gate",
    "auto": "auto", "自动": "auto",
}


def parse_exchange(tok):
    """把交易所参数解析成 okx/binance/bybit/gate/auto；认不出返回 None。"""
    return EXCHANGE_ALIASES.get((tok or "").strip().lower())


def parse_tokens(tokens):
    """把「合约」「gate」这类尾巴解析成 (market, exchange)，不分先后。

    命令 /watchpct 和菜单引导流程共用——分成两份写，改一处必漏另一处
    （引导流程就漏过一次：命令支持选交易所了，点按钮进来的那条还不支持）。
    """
    market, exchange = "auto", None
    for tok in tokens:
        m = parse_market(tok)
        if m != "auto":
            market = m
            continue
        e = parse_exchange(tok)
        if e:
            exchange = e
    return market, exchange


def norm_symbol(sym):
    """规范化币名：用户可能粘贴完整交易对(TUSDT/BTCUSDT)，去掉结尾 USDT 取基名。"""
    s = (sym or "").upper().strip()
    if s.endswith("USDT") and len(s) > 4:   # TUSDT→T, BTCUSDT→BTC；'USDT' 本身不动
        s = s[:-4]
    return s


async def resolve_price(symbol, market="auto", exchange="auto"):
    """取价，返回 (price, source) 或 (None, None)。

    取价逻辑已收进 handlers.source 统一层（原来这里自己维护一条 OKX→币安→Bybit
    的链，和价格预警、虚拟合约各走各的，同一个币在不同功能里价格能对不上）。
    exchange 显式指定时只查那一家——查不到就如实说没有，不偷偷换一家，
    否则用户以为在盯 Gate 的价、实际盯的是 Bybit 的。
    """
    from handlers import source as src_mod
    return await src_mod.price(symbol, exchange, market)


async def fetch_pinned(symbol, source):
    """从固定来源取价（轮询用，保证与基准同一交易所）。返回 price 或 None。"""
    from handlers import source as src_mod
    return await src_mod.price_at(symbol, source)


async def repoint(chat_id, symbol, label):
    """把某个监控换到指定数据源，并按新源重设基准。

    基准必须一起换：换源那一刻价格是阶跃的（现货↔永续的基差尤其明显），
    沿用旧基准等于把这个阶跃当成行情，立刻误报一次。
    """
    symbol = norm_symbol(symbol)
    mine = [w for w in data.get("watchpct", [])
            if w["chat_id"] == chat_id and w["symbol"] == symbol]
    if not mine:
        return False, f"没找到 {symbol} 的波动监控（可能已经取消了）"
    from handlers import source as src_mod
    price = await src_mod.price_at(symbol, label) if label else None
    if label and price is None:
        return False, (f"⚠️ *{label}* 上查不到 {symbol}，没有切换。\n"
                       f"小币常常只有某几家有，换一家试试。")
    if not label:                       # 选了「自动」
        price, label = await src_mod.price(symbol)
        if price is None:
            return False, f"⚠️ 各家都查不到 {symbol}，没有切换。"
    for w in mine:
        w["src"], w["base"], w["last_ts"] = label, price, 0
    save_data()
    realtime = "⚡ 秒级实时(WebSocket)" if label in ("OKX永续", "Bybit永续") else "约1分钟轮询"
    return True, (f"✅ *{symbol}* 的监控已改用 *{label}*\n"
                  f"新基准 ${fmt(price)}　触发方式：{realtime}")


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
async def add_watch(chat_id, symbol, pct, set_by, market="auto", exchange=None):
    """新增/更新一个持续波动监控。market: auto/spot/swap。返回 (成功, Markdown文本)。

    exchange 不传时用会话默认数据源（/source 设的），传了就只认这一家。
    """
    from handlers import source as src_mod
    from handlers import onchain as oc
    if pct <= 0:
        return False, "百分比要大于 0"

    # 链上代币：标的是合约地址，不能走 norm_symbol（那会 .upper() 把 Solana
    # 的 base58 地址改坏，也会把结尾像 USDT 的地址截断）
    if oc.is_address(symbol):
        addr = symbol.strip()
        price, t = await oc.price_of(addr)
        if price is None:
            return False, ("链上查不到这个地址的交易对，监控没建立。\n"
                           "确认地址没抄错，或者这个币还没有 DEX 池子。")
        return _store(chat_id, addr, pct, set_by, price,
                      src_mod.onchain_label(t["chain_key"]), "onchain",
                      name=t["symbol"], extra={"chain": t["chain_key"],
                                               "liq": t["liq"]})

    symbol = norm_symbol(symbol)
    if exchange is None:
        exchange, pref_market = src_mod.get_pref(chat_id)
        if market == "auto":            # 命令里没写「合约/现货」才用默认里的
            market = pref_market
    price, src = await resolve_price(symbol, market, exchange)
    if price is None:
        kind = "合约" if market == "swap" else ("现货" if market == "spot" else "")
        where = f"{src_mod.EX_CN.get(exchange, exchange)}上" if exchange != "auto" else ""
        return False, (f"没查到 {where}{symbol} 的{kind}价格。"
                       + ("该币可能没有对应永续合约。" if market == "swap" else "")
                       + ("换一家试试（小币常常只有 Gate 或 Bybit 有），或"
                          if exchange != "auto" else "")
                       + "用交易所里的交易对基名试试（如 KORU、RAM、DOGE）")

    return _store(chat_id, symbol, pct, set_by, price, src, market)


def disp(w):
    """一条监控给用户看的名字。链上的标的是 42 位合约地址，直接显示没法读。"""
    name = w.get("name")
    sym = w.get("symbol", "")
    if name:
        return f"{name}（{sym[:6]}…{sym[-4:]}）" if len(sym) > 14 else name
    return sym


def _store(chat_id, symbol, pct, set_by, price, src, market, name=None, extra=None):
    """落盘 + 生成回执。交易所和链上共用，免得两边各写一份存储格式。"""
    lst = data.setdefault("watchpct", [])
    mine = [w for w in lst if w["chat_id"] == chat_id]
    existed = any(w["symbol"] == symbol for w in mine)
    if not existed and len(mine) >= MAX_PER_CHAT:
        return False, f"最多同时盯 {MAX_PER_CHAT} 个币，先 /unwatchpct 取消几个"
    lst[:] = [w for w in lst if not (w["chat_id"] == chat_id and w["symbol"] == symbol)]
    item = {
        "chat_id": chat_id, "symbol": symbol, "pct": pct, "market": market,
        "base": price, "src": src, "last_ts": 0, "set_by": set_by,
    }
    if name:
        item["name"] = name
    if extra:
        item.update(extra)
    lst.append(item)
    save_data()

    verb = "已更新" if existed else "已开启"
    onchain = market == "onchain"
    mkt_tag = "" if onchain else (
        "（合约）" if market == "swap" else ("（现货）" if market == "spot" else ""))
    # OKX/Bybit 永续走 WebSocket 秒级实时；其余走约1分钟轮询
    realtime = "⚡ 秒级实时(WebSocket)" if src in ("OKX永续", "Bybit永续") else "约1分钟轮询"
    lines = [f"👁 {verb}持续波动监控：*{disp(item)}*{mkt_tag} 每涨跌超 *±{pct}%* 提醒",
             f"当前基准 ${fmt(price)}（{src}）",
             f"触发方式：{realtime}，报警后自动以新价为基准继续盯（{COOLDOWN//60}分钟冷却）。"]
    if onchain:
        liq = (extra or {}).get("liq") or 0
        lines.append(f"池子 ${liq:,.0f}"
                     + ("　⛔ 太浅，单笔大单就能打出假信号" if liq < 50_000 else ""))
        lines.append("⚠️ 链上波动本来就比交易所大得多，阈值别设太小，否则会被刷屏")
    return True, "\n".join(lines)


def src_kb(symbol):
    """监控确认卡下面的「换数据源」按钮（只改这一个币）。"""
    from telegram import InlineKeyboardMarkup
    from handlers import source as src_mod
    return InlineKeyboardMarkup([[src_mod.change_btn(f"wp|{norm_symbol(symbol)}")]])


# ---------- 命令 ----------
async def watchpct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "用法：/watchpct 币 百分比 [合约] [交易所]\n"
            "例：/watchpct DOGE 5        （用你的默认数据源，/source 可改）\n"
            "例：/watchpct BTC 3 合约    （强制盯永续合约价）\n"
            "例：/watchpct AKE 2 合约 gate（只盯 Gate 的永续）\n"
            "交易所可写 okx / 币安 / bybit / gate。\n"
            "支持小盘/合约币（如 KORU、RAM）。取消：/unwatchpct 币")
        return
    symbol = norm_symbol(args[0])
    try:
        pct = float(args[1])
    except ValueError:
        await update.message.reply_text("百分比要是数字，例：/watchpct DOGE 5")
        return
    # 第三、四个参数不分先后：「合约 gate」和「gate 合约」都认
    market, exchange = parse_tokens(args[2:4])
    ok, msg = await add_watch(update.effective_chat.id, symbol, pct,
                              update.effective_user.first_name, market, exchange)
    tail = f"\n查看 /watchpcts　取消 /unwatchpct {symbol}" if ok else ""
    await update.message.reply_text(msg + tail, parse_mode="Markdown",
                                    reply_markup=src_kb(symbol) if ok else None)


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
        lines.append(f"• {disp(w)}  ±{w['pct']}%  基准 ${fmt(w['base'])}（{w.get('src','?')}）")
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
                    f"{arrow} *{disp(w)}*{mkt_tag} {ch:+.2f}%！\n"
                    f"${fmt(base)} → ${fmt(p)}（阈值 ±{w['pct']}%，{w.get('src','')}）",
                    parse_mode="Markdown")
            except Exception as e:
                logging.error(f"波动监控推送失败 {w['chat_id']}: {e}")
            w["base"] = p          # 以新价为基准继续盯
            w["last_ts"] = now
            changed = True
    if changed:
        save_data()
