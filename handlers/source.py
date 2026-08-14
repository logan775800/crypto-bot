"""统一取价层：一个币 + 一个数据源 → 一个价格。

以前每个功能自己挑源，挑法还都不一样：价格预警走 CoinGecko（还卡主流币白名单），
波动监控自己维护一条 OKX→币安→Bybit 的链，虚拟合约走 Bybit。用户看到的是
「同一个币在不同功能里价格不一样」，而且没法指定用哪家。这里把取价收成一处。

两层选择，互相独立：
  • 交易所 exchange: auto / okx / binance / bybit / gate
  • 市场 market:     auto / spot(现货) / swap(永续)
「auto」= 按优先级挨个试，谁先有用谁。

⚠️ 标签字符串（"Bybit永续" 这种）是**存量数据的一部分**：波动监控把它写进
data.json 的 src 字段，contract_ws 的 _WS_SRC 也按它匹配 WebSocket 实时 tick。
改标签会让所有历史监控对不上号，所以只能加不能改。
"""
import asyncio
import logging

import httpx

log = logging.getLogger(__name__)

AUTO = "auto"
SPOT, SWAP = "spot", "swap"

EX_CN = {"okx": "OKX", "binance": "币安", "bybit": "Bybit", "gate": "Gate"}
MARKET_CN = {SPOT: "现货", SWAP: "永续", AUTO: "自动"}

# 标签必须与 watchpct 存量 src / contract_ws._WS_SRC 完全一致，见模块注释
LABEL = {
    ("okx", SPOT): "OKX", ("okx", SWAP): "OKX永续",
    ("binance", SPOT): "Binance", ("binance", SWAP): "Binance永续",
    ("bybit", SPOT): "Bybit", ("bybit", SWAP): "Bybit永续",
    ("gate", SPOT): "Gate", ("gate", SWAP): "Gate永续",
    ("coingecko", SPOT): "CoinGecko",
}
FROM_LABEL = {v: k for k, v in LABEL.items()}

# 自动顺序：沿用波动监控原有的 OKX→币安→Bybit 优先级（改顺序会让存量监控换源、
# 基准跳变）。Gate 放最后——小币最全，但深度差、价格更容易偏。
AUTO_EX = ("okx", "binance", "bybit", "gate")
# 自动市场：先现货后永续。做合约的人想盯永续时会显式选，而现货价更适合当"公允价"。
AUTO_MARKET = (SPOT, SWAP)

OKX_BASE = "https://www.okx.com"
BN_SPOT = "https://api.binance.com"
BN_FAPI = "https://fapi.binance.com"
BYBIT_BASE = "https://api.bybit.com"
GATE_BASE = "https://api.gateio.ws/api/v4"

TIMEOUT = 8
MAX_PARALLEL = 8        # 批量取价时的并发上限，别把交易所打出限频

# 全局复用一个客户端：每次 new AsyncClient 光建 SSL 上下文就要 0.7 秒左右
# （实测），而预警任务 60 秒一轮、每轮多个源多个币，这笔开销比请求本身还大。
# 复用还能吃到连接池，握手只做一次。
_client_box = {"c": None, "loop": None, "ssl": None}


def client():
    """复用的 httpx 客户端。

    两级复用，分别对应两种成本（都是实测出来的）：
      • **SSL 上下文**：建一次 0.77 秒（要加载 CA 证书），进程级共用；
      • **连接池**：几乎免费，但绑在创建它的事件循环上，循环换了必须重建，
        否则会拿着已关闭循环里的连接报 Event loop is closed。
    以前每次取价 new 一个默认客户端 = 每次都付那 0.77 秒，比请求本身还贵。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    c = _client_box["c"]
    if c is None or c.is_closed or _client_box["loop"] is not loop:
        if _client_box["ssl"] is None:
            _client_box["ssl"] = httpx.create_ssl_context()
        c = httpx.AsyncClient(timeout=TIMEOUT, verify=_client_box["ssl"])
        _client_box.update(c=c, loop=loop)
    return c


def norm(symbol):
    """规范化币名：用户可能粘贴完整交易对 BTCUSDT / BTC-USDT / BTC_USDT。"""
    s = (symbol or "").upper().strip().replace("/", "-").replace("_", "-").split("-")[0]
    if len(s) > 4 and s.endswith("USDT"):
        s = s[:-4]
    return s


def label_of(ex, market):
    return LABEL.get((ex, market), "")


def split_label(label):
    """"Bybit永续" → ("bybit", "swap")；认不出返回 (auto, auto)。"""
    return FROM_LABEL.get(label or "", (AUTO, AUTO))


def describe(ex, market):
    """给用户看的一句话，如「Bybit 永续」「自动（谁有用谁）」。"""
    if ex == AUTO and market == AUTO:
        return "自动（哪家有用哪家）"
    if ex == AUTO:
        return f"自动 · {MARKET_CN.get(market, market)}"
    if market == AUTO:
        return f"{EX_CN.get(ex, ex)} · 自动"
    return f"{EX_CN.get(ex, ex)} {MARKET_CN.get(market, market)}"


# ---------- 各所取价（只要最新成交价这一个数） ----------
async def _okx(c, sym, market):
    inst = f"{sym}-USDT-SWAP" if market == SWAP else f"{sym}-USDT"
    r = await c.get(f"{OKX_BASE}/api/v5/market/ticker", params={"instId": inst})
    d = r.json()
    if d.get("code") == "0" and d.get("data"):
        return float(d["data"][0]["last"])
    return None


async def _binance(c, sym, market):
    base, path = ((BN_FAPI, "/fapi/v1/ticker/price") if market == SWAP
                  else (BN_SPOT, "/api/v3/ticker/price"))
    r = await c.get(f"{base}{path}", params={"symbol": f"{sym}USDT"})
    if r.status_code == 200:
        d = r.json()
        if "price" in d:
            return float(d["price"])
    return None


async def _bybit(c, sym, market):
    r = await c.get(f"{BYBIT_BASE}/v5/market/tickers",
                    params={"category": "linear" if market == SWAP else "spot",
                            "symbol": f"{sym}USDT"})
    d = r.json()
    lst = d.get("result", {}).get("list") or []
    if d.get("retCode") == 0 and lst:
        return float(lst[0]["lastPrice"])
    return None


async def _gate(c, sym, market):
    if market == SWAP:
        r = await c.get(f"{GATE_BASE}/futures/usdt/tickers",
                        params={"contract": f"{sym}_USDT"})
        d = r.json()
        return float(d[0]["last"]) if d else None
    r = await c.get(f"{GATE_BASE}/spot/tickers",
                    params={"currency_pair": f"{sym}_USDT"})
    d = r.json()
    return float(d[0]["last"]) if d else None


async def _coingecko(c, sym, market):
    """兜底源，不在选择列表里露出（它不是交易所，也没有永续）。

    留着是因为少数币只有 CoinGecko 有映射而各所交易对名字对不上，
    去掉它等于让这批币的预警从「能用」变成「查不到」。"""
    if market == SWAP:
        return None
    from api import get_price as _cg
    r = await _cg(sym)
    return float(r["price"]) if r and r.get("price") else None


_FETCH = {"okx": _okx, "binance": _binance, "bybit": _bybit, "gate": _gate,
          "coingecko": _coingecko}


async def _one(c, sym, ex, market):
    """单点取价，任何异常都当"这家没有"，让上层继续试下一家。"""
    try:
        p = await _FETCH[ex](c, sym, market)
        return p if p and p > 0 else None
    except Exception:
        return None


def candidates(ex=AUTO, market=AUTO):
    """把 (交易所, 市场) 展开成要挨个试的 (ex, market) 列表。"""
    exs = AUTO_EX if ex == AUTO else (ex,)
    mkts = AUTO_MARKET if market == AUTO else (market,)
    # 先把一家的两个市场试完再换下一家：跨所跳来跳去更容易拿到偏离的价
    out = [(e, m) for e in exs for m in mkts]
    if ex == AUTO and market != SWAP:
        out.append(("coingecko", SPOT))     # 四家都没有时的最后一手
    return out


async def price(symbol, ex=AUTO, market=AUTO):
    """取价。返回 (价格, 标签) —— 标签形如 "Bybit永续"；都拿不到返回 (None, None)。"""
    sym = norm(symbol)
    c = client()
    for e, m in candidates(ex, market):
        p = await _one(c, sym, e, m)
        if p:
            return p, label_of(e, m)
    return None, None


async def price_at(symbol, label):
    """从指定标签的源取价（盯盘轮询用：必须和基准同一个源，否则涨跌算歪）。"""
    ex, market = split_label(label)
    if ex == AUTO:
        p, _ = await price(symbol)
        return p
    return await _one(client(), norm(symbol), ex, market)


async def prices_at(symbols, label):
    """一批币在同一个源上的价格 → {原始币名: 价格}。取不到的键不出现。

    用逐个单点查而不是拉全量 tickers：预警通常只有几个到几十个币，
    单点查每次 1KB 上下，全量表一次就是几百 KB，60 秒一轮划不来。
    """
    ex, market = split_label(label)
    syms = list(dict.fromkeys(symbols))
    out = {}
    sem = asyncio.Semaphore(MAX_PARALLEL)
    c = client()

    async def work(raw):
        async with sem:
            if ex == AUTO:
                for e, m in candidates(AUTO, market):
                    p = await _one(c, norm(raw), e, m)
                    if p:
                        return raw, p
                return raw, None
            return raw, await _one(c, norm(raw), ex, market)

    for raw, p in await asyncio.gather(*(work(s) for s in syms)):
        if p:
            out[raw] = p
    return out


# ---------- 用户偏好：全局默认 + 单条覆盖 ----------
def get_pref(chat_id):
    """这个会话的默认数据源 → (ex, market)。没设过就是全自动。"""
    from handlers.prefs import get_pref as _pref
    p = _pref(chat_id)
    return p.get("src_ex", AUTO), p.get("src_market", AUTO)


def set_pref(chat_id, ex, market):
    from handlers.prefs import get_pref as _pref
    from storage import save_data
    p = _pref(chat_id)
    p["src_ex"], p["src_market"] = ex, market
    save_data()


async def price_for(chat_id, symbol, override=None):
    """按「单条覆盖 > 会话默认 > 自动」的顺序取价。返回 (价格, 标签)。

    override 是存在预警/监控条目里的标签，比默认优先——这就是「默认 + 单条覆盖」
    这套选择方式的全部含义，别在调用方各写一遍。
    """
    if override:
        ex, market = split_label(override)
    else:
        ex, market = get_pref(chat_id)
    return await price(symbol, ex, market)


# ---------- 选择面板（默认设置 / 单条预警 / 单个监控 共用） ----------
# target 决定"选完了改谁"：def=会话默认，al:<i>=第 i 条预警，wp:<币>=某个波动监控
def _sel(context, target, ex=None, market=None):
    """选择过程暂存在 user_data，点「用这个」才落盘——避免点一半就改了正在跑的预警。"""
    box = context.user_data.setdefault("src_sel", {})
    cur = box.get(target)
    if cur is None:
        cur = box[target] = {"ex": AUTO, "market": AUTO}
    if ex is not None:
        cur["ex"] = ex
    if market is not None:
        cur["market"] = market
    return cur["ex"], cur["market"]


def source_kb(target, ex=AUTO, market=AUTO, back=None):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    def tick(on, text):
        return ("✅ " if on else "") + text

    rows = [
        [InlineKeyboardButton(tick(ex == AUTO, "自动"), callback_data=f"src:ex:{target}:auto"),
         InlineKeyboardButton(tick(ex == "okx", "OKX"), callback_data=f"src:ex:{target}:okx"),
         InlineKeyboardButton(tick(ex == "binance", "币安"), callback_data=f"src:ex:{target}:binance")],
        [InlineKeyboardButton(tick(ex == "bybit", "Bybit"), callback_data=f"src:ex:{target}:bybit"),
         InlineKeyboardButton(tick(ex == "gate", "Gate"), callback_data=f"src:ex:{target}:gate")],
        [InlineKeyboardButton(tick(market == AUTO, "现货/永续自动"), callback_data=f"src:mk:{target}:auto"),
         InlineKeyboardButton(tick(market == SPOT, "现货"), callback_data=f"src:mk:{target}:spot"),
         InlineKeyboardButton(tick(market == SWAP, "永续"), callback_data=f"src:mk:{target}:swap")],
        [InlineKeyboardButton("✅ 用这个", callback_data=f"src:ok:{target}")],
    ]
    if back:
        rows.append([InlineKeyboardButton("⬅️ 返回", callback_data=back)])
    return InlineKeyboardMarkup(rows)


_TITLE = {
    "def": "📡 *默认数据源*\n\n跟着它走的：价格预警、波动监控、持仓估值。\n"
           "「自动」= 哪家有这个币就用哪家（顺序 OKX → 币安 → Bybit → Gate）。\n"
           "小币常常只有 Gate 或 Bybit 有，指定成没有该币的所会取不到价。\n\n"
           "不跟它走的（有意为之）：实盘交易只能是 Bybit（钱在那）；"
           "虚拟合约固定用 Bybit 永续（它模拟的就是永续，换源会让爆仓价算歪）；"
           "扫描/图表/回测这些要整段 K 线的仍是 Bybit。",
}


def panel_text(target, ex, market):
    head = _TITLE.get(target)
    if head:
        return f"{head}\n\n当前选择：*{describe(ex, market)}*"
    return (f"📡 *这一条用哪家的价*\n\n只改这一条，不动你的默认设置。\n\n"
            f"当前选择：*{describe(ex, market)}*")


async def on_button(query, context):
    """处理 src:* 回调。由 menu.button_handler 转发。"""
    from handlers.util import safe_edit
    bits = query.data.split(":")
    if len(bits) < 3:
        await query.answer("参数看不懂")
        return
    action, target = bits[1], bits[2]
    arg = bits[3] if len(bits) > 3 else None
    chat_id = query.message.chat.id if query.message else 0

    if action == "panel":
        ex, market = (get_pref(chat_id) if target == "def" else (AUTO, AUTO))
        ex, market = _sel(context, target, ex, market)
    elif action == "ex":
        ex, market = _sel(context, target, ex=arg)
    elif action == "mk":
        ex, market = _sel(context, target, market=arg)
    elif action == "ok":
        ex, market = _sel(context, target)
        await _apply(query, context, target, ex, market)
        return
    else:
        await query.answer("不认识的操作")
        return

    await safe_edit(query, panel_text(target, ex, market),
                    reply_markup=source_kb(target, ex, market), parse_mode="Markdown")


async def _apply(query, context, target, ex, market):
    from handlers.util import safe_edit
    chat_id = query.message.chat.id if query.message else 0
    label = label_of(ex, market)        # 自动时为空串，表示不锁源

    if target == "def":
        set_pref(chat_id, ex, market)
        await query.answer("已保存")
        await safe_edit(query, f"✅ 默认数据源已设为 *{describe(ex, market)}*\n\n"
                               f"之后设的预警、监控、持仓估值都用它。\n"
                               f"单条想用别家，在那条的卡片上点「换数据源」。",
                        parse_mode="Markdown")
        return

    if target.startswith("wp"):
        from handlers import watchpct
        sym = target.split("|", 1)[1] if "|" in target else ""
        ok, msg = await watchpct.repoint(chat_id, sym, label)
        await query.answer("已切换" if ok else "没找到")
        await safe_edit(query, msg, parse_mode="Markdown")
        return

    if target.startswith("al"):
        from handlers import alert as alert_mod
        idx = target.split("|", 1)[1] if "|" in target else ""
        ok, msg = await alert_mod.repoint(chat_id, idx, label)
        await query.answer("已切换" if ok else "没找到")
        await safe_edit(query, msg, parse_mode="Markdown")
        return

    await query.answer("不认识的目标")


def change_btn(target):
    """挂在预警/监控确认卡下面的「换数据源」按钮。"""
    from telegram import InlineKeyboardButton
    return InlineKeyboardButton("📡 换数据源", callback_data=f"src:panel:{target}")


# ---------- 命令 ----------
async def source_cmd(update, context):
    """/source —— 看/改这个会话的默认数据源。"""
    from handlers.util import safe_reply
    chat_id = update.effective_chat.id
    ex, market = get_pref(chat_id)
    _sel(context, "def", ex, market)
    await safe_reply(update.message, panel_text("def", ex, market),
                     reply_markup=source_kb("def", ex, market), parse_mode="Markdown")

