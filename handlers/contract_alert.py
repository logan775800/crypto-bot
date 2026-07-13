"""全交易所合约涨跌幅分级告警。

两条触发路径共用本模块的判档去重 + 推送逻辑：
  • WebSocket 实时（handlers/contract_ws.py，OKX/Bybit）：价格穿过阈值秒级触发。
  • REST 轮询（本文件 scan_contracts，覆盖三家，含 Binance）：定时兜底 / 安全网。
两者写同一份 data["contract_tiers"] 分档记录，所以不会重复告警。

当 |涨跌幅| 突破台阶（20/30/40%…到 400%）时向订阅群推送，每条标注交易所来源，
同一个币在多个所同时命中会分别成行、各标来源。

订阅：/watchcontract 订阅，/unwatchcontract 取消。
"""
import time
import logging
import asyncio
import httpx
from telegram import Update
from telegram.ext import ContextTypes
from storage import data, save_data
from handlers.util import escape_md

OKX_BASE = "https://www.okx.com"
FAPI = "https://fapi.binance.com"          # 币安 USDT 本位合约
BYBIT_BASE = "https://api.bybit.com"

# 告警台阶：20% 起，每 10% 一档，封顶 400%
TIERS = list(range(20, 401, 10))
# 迟滞带（百分点）：涨跌幅须回落到 (最低档-迟滞) 以下才重新武装，
# 杜绝币在 20% 边界上下抖动被反复当成"首次穿越"而刷屏
HYSTERESIS = 3
# 最小 24h 成交额（USDT），滤掉僵尸/微盘合约的噪音；按需调整
MIN_TURNOVER = 1_000_000
TIER_RESET = 86400                          # 记录 24h 后过期，允许重新计档
LEV_SUFFIX = ("UP", "DOWN", "BULL", "BEAR")  # 币安杠杆代币，排除
MAX_LINES = 40                              # 单条消息最多多少行，超出分条发


def get_tier(change_abs):
    """返回 |涨跌幅| 命中的最高台阶；不足 20% 返回 0，超 400% 封顶 400。"""
    if change_abs < TIERS[0]:
        return 0
    tier = TIERS[0]
    for t in TIERS:
        if change_abs >= t:
            tier = t
        else:
            break
    return tier


def eval_tier_cross(ex_name, sym, change, now=None):
    """判断某(交易所,币)的当前涨跌幅是否升到了更高台阶。

    命中返回要告警的台阶(int)并更新 data["contract_tiers"]；否则返回 None。
    WS 实时与 REST 轮询共用此函数 → 同一套去重，绝不重复。
    """
    if now is None:
        now = time.time()
    data.setdefault("contract_tiers", {})
    tiers = data["contract_tiers"]
    change_abs = abs(change)
    direction = "up" if change > 0 else "down"
    key = sym                    # 只按币去重：同一个币在多所同时异动＝一个事件，只报一次
    rec = tiers.get(key)

    # 记录已反向 或 已过期(24h) → 作废，视为无记录
    if rec and (rec["dir"] != direction or now - rec["ts"] > TIER_RESET):
        tiers.pop(key, None)
        rec = None

    # 明显回落到迟滞带以下(< 最低档-迟滞) → 解除武装，清记录，之后重新穿越才再报
    if change_abs < TIERS[0] - HYSTERESIS:
        if key in tiers:
            tiers.pop(key, None)
        return None

    # 处于迟滞带或未达最低档(如 17~20%) → 不报；有记录则续命时间戳，别过期
    if change_abs < TIERS[0]:
        if rec:
            rec["ts"] = now
        return None

    tier = get_tier(change_abs)
    prev = rec["tier"] if rec else 0
    if tier > prev:                       # 仅升到更高台阶才报（同档抖动不再重复）
        tiers[key] = {"tier": tier, "dir": direction, "ts": now}
        return tier

    if rec:                               # 同档/回落但仍在高位：续命，不报
        rec["ts"] = now
    return None


async def push_to_subscribers(bot, alerts):
    """把一批告警(dict: ex/sym/change/price/tier/direction)推给所有订阅群，每条标来源。"""
    subs = data.get("contract_watch", [])
    if not subs or not alerts:
        return
    # 同一(币,方向)去重（跨交易所也算重复），保留最高档，只留首个交易所来源
    dedup = {}
    for a in alerts:
        k = (a["sym"], a["direction"])
        if k not in dedup or a["tier"] > dedup[k]["tier"]:
            dedup[k] = a
    alerts = sorted(dedup.values(), key=lambda a: (-a["tier"], a["ex"], a["sym"]))
    body = []
    for a in alerts:
        emoji = "🚀" if a["direction"] == "up" else "💥"
        arrow = "涨破" if a["direction"] == "up" else "跌破"
        body.append(
            f"{emoji} *{a['ex']}* {escape_md(a['sym'])} {arrow} {a['tier']}%！"
            f"现 {a['change']:+.2f}% (${a['price']:,.4g})"
        )
    chunks = [body[i:i + MAX_LINES] for i in range(0, len(body), MAX_LINES)]
    for chat_id in subs:
        for idx, chunk in enumerate(chunks):
            head = "🚨 *合约异动告警*（全交易所）\n" if idx == 0 else "🚨 *合约异动告警*（续）\n"
            text = head + "\n".join(chunk)
            if idx == len(chunks) - 1:
                text += "\n\n⚠️ 合约杠杆风险高，异动剧烈，不构成投资建议"
            try:
                await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
            except Exception as e:
                logging.error(f"合约告警推送失败 {chat_id}: {e}")


# ---------- 各交易所合约行情抓取（REST，统一返回 [{sym, change, price, turnover}]）----------
async def _okx_swap(client):
    r = await client.get(f"{OKX_BASE}/api/v5/market/tickers", params={"instType": "SWAP"})
    r.raise_for_status()
    d = r.json()
    if d.get("code") != "0":
        return []
    out = []
    for t in d.get("data", []):
        iid = t.get("instId", "")
        if not iid.endswith("-USDT-SWAP"):
            continue
        try:
            last = float(t["last"]); op = float(t["open24h"])
            if op <= 0:
                continue
            change = (last - op) / op * 100
            # OKX SWAP 的 volCcy24h 以基础币计价，× 现价 ≈ USD 成交额
            turnover = float(t.get("volCcy24h", 0) or 0) * last
            if turnover < MIN_TURNOVER:
                continue
            out.append({"sym": iid[:-len("-USDT-SWAP")], "change": change,
                        "price": last, "turnover": turnover})
        except (ValueError, KeyError):
            continue
    return out


async def _binance_swap(client):
    r = await client.get(f"{FAPI}/fapi/v1/ticker/24hr")
    r.raise_for_status()
    out = []
    for t in r.json():
        s = t.get("symbol", "")
        if not s.endswith("USDT"):          # 排除交割合约(带日期)/USDC 等
            continue
        base = s[:-4]
        if any(base.endswith(x) for x in LEV_SUFFIX):
            continue
        try:
            last = float(t["lastPrice"]); ch = float(t["priceChangePercent"])
            turnover = float(t.get("quoteVolume", 0) or 0)   # 已是 USDT
            if turnover < MIN_TURNOVER:
                continue
            out.append({"sym": base, "change": ch, "price": last, "turnover": turnover})
        except (ValueError, KeyError):
            continue
    return out


async def _bybit_swap(client):
    r = await client.get(f"{BYBIT_BASE}/v5/market/tickers", params={"category": "linear"})
    r.raise_for_status()
    d = r.json()
    if d.get("retCode") != 0:
        return []
    out = []
    for t in d.get("result", {}).get("list", []):
        s = t.get("symbol", "")
        if not s.endswith("USDT"):          # 排除 USDC 永续/日期交割
            continue
        base = s[:-4]
        try:
            last = float(t["lastPrice"]); ch = float(t["price24hPcnt"]) * 100
            turnover = float(t.get("turnover24h", 0) or 0)   # 已是 USDT
            if turnover < MIN_TURNOVER:
                continue
            out.append({"sym": base, "change": ch, "price": last, "turnover": turnover})
        except (ValueError, KeyError):
            continue
    return out


EXCHANGES = [("OKX", _okx_swap), ("币安", _binance_swap), ("Bybit", _bybit_swap)]


# ---------- 订阅命令 ----------
async def watch_contract(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    data.setdefault("contract_watch", [])
    if chat_id in data["contract_watch"]:
        await update.message.reply_text("本群已订阅合约异动告警 ✅")
        return
    data["contract_watch"].append(chat_id)
    save_data()
    await update.message.reply_text(
        "✅ 已订阅【全交易所合约异动告警】\n\n"
        "• 覆盖 OKX / 币安 / Bybit 永续合约\n"
        "• |涨跌幅| 突破 20% / 30% / … / 400% 分级告警\n"
        "• 每条标注交易所来源，多所同时命中都会发\n"
        "• OKX/Bybit 秒级实时(WebSocket)，币安约1分钟兜底\n"
        "• 同币同方向升档才再报\n\n"
        "取消订阅：/unwatchcontract"
    )


async def unwatch_contract(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    data.setdefault("contract_watch", [])
    if chat_id in data["contract_watch"]:
        data["contract_watch"].remove(chat_id)
        save_data()
        await update.message.reply_text("已取消合约异动告警")
    else:
        await update.message.reply_text("本群还没订阅合约异动告警")


# ---------- 后台扫描（REST 轮询，安全网 + 币安主路）----------
async def scan_contracts(context: ContextTypes.DEFAULT_TYPE):
    if not data.get("contract_watch"):
        return
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            results = await asyncio.gather(
                *[fn(client) for _, fn in EXCHANGES], return_exceptions=True
            )
    except Exception as e:
        logging.error(f"合约扫描取数出错: {e}")
        return

    now = time.time()
    alerts = []
    for (ex_name, _), res in zip(EXCHANGES, results):
        if isinstance(res, Exception):
            logging.warning(f"合约扫描 {ex_name} 失败: {res}")
            continue
        for m in res:
            tier = eval_tier_cross(ex_name, m["sym"], m["change"], now)
            if tier:
                alerts.append({"ex": ex_name, "sym": m["sym"], "change": m["change"],
                               "price": m["price"], "tier": tier,
                               "direction": "up" if m["change"] > 0 else "down"})

    # 清理过期记录，避免无限增长
    tiers = data.get("contract_tiers", {})
    data["contract_tiers"] = {k: v for k, v in tiers.items() if now - v["ts"] < TIER_RESET * 2}
    save_data()

    await push_to_subscribers(context.bot, alerts)
