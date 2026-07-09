"""全交易所合约涨跌幅分级告警。

并发拉取 OKX / 币安 / Bybit 三家的 **永续合约** 24h 行情，计算涨跌幅，
当 |涨跌幅| 突破台阶（20/30/40%…一直到 400%）时向订阅群推送告警。
每条告警都标注交易所来源；同一个币在多个所同时命中，会分别成行、各标来源。

订阅模型沿用市场异动告警：在目标群里 /watchcontract 订阅，/unwatchcontract 取消。
分级去重是「市场属性」（按 交易所+币 记录，全局共享），所有订阅群收到一致的告警。
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
# 最小 24h 成交额（USDT），滤掉僵尸/微盘合约的噪音；按需调整
MIN_TURNOVER = 1_000_000
TIER_RESET = 86400                          # 记录 24h 后过期，允许重新计档
LEV_SUFFIX = ("UP", "DOWN", "BULL", "BEAR")  # 币安杠杆代币，排除
MAX_LINES = 40                              # 单条消息最多多少行，超出分条发


# ---------- 各交易所合约行情抓取（统一返回 [{sym, change, price, turnover}]）----------
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
        "• 每 5 分钟扫描，同币同方向升档才再报\n\n"
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


# ---------- 后台扫描（job）----------
async def scan_contracts(context: ContextTypes.DEFAULT_TYPE):
    subs = data.get("contract_watch", [])
    if not subs:
        return

    # 并发拉三家；任一家失败不影响其它
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            results = await asyncio.gather(
                *[fn(client) for _, fn in EXCHANGES], return_exceptions=True
            )
    except Exception as e:
        logging.error(f"合约扫描取数出错: {e}")
        return

    now = time.time()
    data.setdefault("contract_tiers", {})
    tiers = data["contract_tiers"]

    alerts = []
    for (ex_name, _), res in zip(EXCHANGES, results):
        if isinstance(res, Exception):
            logging.warning(f"合约扫描 {ex_name} 失败: {res}")
            continue
        for m in res:
            sym = m["sym"]
            change = m["change"]
            change_abs = abs(change)
            direction = "up" if change > 0 else "down"
            key = f"{ex_name}_{sym}"

            # 跌回阈值以下：清记录，下次重新穿越可再报
            if change_abs < TIERS[0]:
                tiers.pop(key, None)
                continue

            rec = tiers.get(key)
            prev = 0
            if rec and rec["dir"] == direction and now - rec["ts"] <= TIER_RESET:
                prev = rec["tier"]

            tier = get_tier(change_abs)
            if tier > prev:
                alerts.append({"ex": ex_name, "sym": sym, "change": change,
                               "price": m["price"], "tier": tier, "direction": direction})
                tiers[key] = {"tier": tier, "dir": direction, "ts": now}

    # 清理过期记录，避免无限增长
    data["contract_tiers"] = {k: v for k, v in tiers.items() if now - v["ts"] < TIER_RESET * 2}
    save_data()

    if not alerts:
        return

    # 高档在前；同档按交易所、币名排序
    alerts.sort(key=lambda a: (-a["tier"], a["ex"], a["sym"]))
    body = []
    for a in alerts:
        emoji = "🚀" if a["direction"] == "up" else "💥"
        arrow = "涨破" if a["direction"] == "up" else "跌破"
        body.append(
            f"{emoji} *{a['ex']}* {escape_md(a['sym'])} {arrow} {a['tier']}%！"
            f"现 {a['change']:+.2f}% (${a['price']:,.4g})"
        )

    # 分条（Telegram 单条长度有限）
    chunks = [body[i:i + MAX_LINES] for i in range(0, len(body), MAX_LINES)]
    for chat_id in subs:
        for idx, chunk in enumerate(chunks):
            head = "🚨 *合约异动告警*（全交易所）\n" if idx == 0 else "🚨 *合约异动告警*（续）\n"
            text = head + "\n".join(chunk)
            if idx == len(chunks) - 1:
                text += "\n\n⚠️ 合约杠杆风险高，异动剧烈，不构成投资建议"
            try:
                await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
            except Exception as e:
                logging.error(f"合约告警推送失败 {chat_id}: {e}")
