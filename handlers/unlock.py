import logging
import time
import datetime
import httpx
from telegram import Update
from telegram.ext import ContextTypes

BASE = "https://defillama-datasets.llama.fi"

# 常见币符号 → DefiLlama 项目名映射（主流币）
SYMBOL_MAP = {
    "ARB": "arbitrum", "OP": "optimism", "APT": "aptos", "SUI": "sui",
    "TIA": "celestia", "SEI": "sei", "STRK": "starknet", "JUP": "jupiter",
    "JTO": "jito", "PYTH": "pyth-network", "W": "wormhole", "ENA": "ethena",
    "ALT": "altlayer", "MANTA": "manta", "DYM": "dymension", "PIXEL": "pixels",
    "PORTAL": "portal", "SAGA": "saga", "OMNI": "omni-network", "ZK": "zksync",
    "ZRO": "layerzero", "BLAST": "blast", "EIGEN": "eigenlayer", "ETHFI": "ether-fi",
    "REZ": "renzo", "SAFE": "safe", "USUAL": "usual", "MOVE": "movement",
    "ME": "magic-eden", "GRASS": "grass", "SCR": "scroll", "DBR": "debridge",
    "LDO": "lido", "UNI": "uniswap", "DYDX": "dydx", "GMX": "gmx",
    "AAVE": "aave", "MKR": "maker", "CRV": "curve-dao-token", "SNX": "synthetix",
}

async def _get(path):
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{BASE}{path}")
        resp.raise_for_status()
        return resp.json()

def resolve_project(symbol):
    """符号转项目名"""
    s = symbol.upper()
    if s in SYMBOL_MAP:
        return SYMBOL_MAP[s]
    # 兜底：直接用小写符号试
    return symbol.lower()

async def get_unlock_events(project):
    """获取某项目的未来解锁事件"""
    try:
        d = await _get(f"/emissions/{project}")
    except Exception:
        return None, None, None
    meta = d.get("metadata", {})
    events = meta.get("events", [])
    name = d.get("name", project)
    # 算总供应量（所有事件解锁量之和的近似，用于算占比）
    total = 0
    for e in events:
        toks = e.get("noOfTokens", [])
        total += sum(toks) if toks else 0
    now = time.time()
    future = [e for e in events if e.get("timestamp", 0) > now]
    future.sort(key=lambda x: x["timestamp"])
    return name, future, total

# /unlock ARB
async def unlock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "用法：/unlock ARB\n查代币解锁安排\n"
            "支持主流币符号(ARB/OP/SUI等)或项目全名"
        )
        return
    symbol = context.args[0].upper()
    await update.message.reply_text(f"🔓 查询 {symbol} 解锁安排...")
    project = resolve_project(symbol)
    name, future, total = await get_unlock_events(project)

    if name is None:
        await update.message.reply_text(
            f"没找到 {symbol} 的解锁数据\n"
            f"(可能该币无锁仓，或项目名不同，试试全名如 /unlock arbitrum)"
        )
        return
    if not future:
        await update.message.reply_text(f"🔓 {name}\n\n未来暂无解锁事件\n(可能已全部解锁完毕)")
        return

    now = time.time()
    lines = [f"🔓 *{name} 解锁安排*\n"]
    for e in future[:6]:
        ts = e["timestamp"]
        dt = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        days = int((ts - now) / 86400)
        toks = e.get("noOfTokens", [])
        amount = sum(toks) if toks else 0
        pct = (amount / total * 100) if total else 0
        category = e.get("category", "?")
        utype = "悬崖" if e.get("unlockType") == "cliff" else "线性"
        lines.append(
            f"📅 {dt} ({days}天后)\n"
            f"  {amount:,.0f} 枚 (约{pct:.2f}%) | {category} | {utype}"
        )
    lines.append("\n⚠️ 大额解锁=新增可流通供应，常带来抛压风险")
    lines.append("不构成投资建议")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

# /unlocks - 近期大额解锁排行
async def unlocks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔓 查询近期大额解锁排行(扫描主流币)...")
    now = time.time()
    week_later = now + 30 * 86400  # 看未来30天

    results = []
    # 扫描映射表里的主流币
    import asyncio
    async def check(symbol, project):
        try:
            name, future, total = await get_unlock_events(project)
            if not future or not total:
                return None
            # 找30天内的解锁
            for e in future:
                if e["timestamp"] <= week_later:
                    toks = e.get("noOfTokens", [])
                    amount = sum(toks) if toks else 0
                    pct = (amount / total * 100) if total else 0
                    if pct >= 0.5:  # 占比0.5%以上才算大额
                        return {"symbol": symbol, "name": name, "ts": e["timestamp"],
                                "amount": amount, "pct": pct, "category": e.get("category", "?")}
            return None
        except Exception:
            return None

    # 并发查（限制数量避免太多请求）
    syms = list(SYMBOL_MAP.items())[:20]
    tasks = [check(s, p) for s, p in syms]
    res = await asyncio.gather(*tasks)
    results = [r for r in res if r]

    if not results:
        await update.message.reply_text("未来30天主流币暂无大额解锁(或数据获取失败)")
        return

    # 按解锁时间排序
    results.sort(key=lambda x: x["ts"])
    lines = ["🔓 *未来30天大额解锁* (主流币)\n"]
    for r in results[:12]:
        dt = datetime.datetime.fromtimestamp(r["ts"]).strftime("%m-%d")
        days = int((r["ts"] - now) / 86400)
        lines.append(f"📅 {dt}({days}天后) *{r['symbol']}* 解锁{r['pct']:.1f}% ({r['category']})")
    lines.append("\n⚠️ 大额解锁常带抛压，提前关注\n不构成投资建议")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ===== 解锁自动提醒 =====
from storage import data as _udata, save_data as _usave

async def sub_unlock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    _udata.setdefault("unlock_subs", [])
    if chat_id in _udata["unlock_subs"]:
        await update.message.reply_text("已订阅解锁提醒 ✅")
        return
    _udata["unlock_subs"].append(chat_id)
    _usave()
    await update.message.reply_text(
        "✅ 已订阅代币解锁提醒！\n\n"
        "• 主流币未来7天内有大额解锁(占比≥1%)自动提醒\n"
        "• 每天检查一次\n"
        "• 提前预警抛压风险\n\n"
        "取消用 /unsubunlock"
    )

async def unsub_unlock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    _udata.setdefault("unlock_subs", [])
    if chat_id in _udata["unlock_subs"]:
        _udata["unlock_subs"].remove(chat_id)
        _usave()
        await update.message.reply_text("已取消解锁提醒")
    else:
        await update.message.reply_text("你还没订阅")

# 定时检查解锁（job调用，每天）
async def check_unlocks(context: ContextTypes.DEFAULT_TYPE):
    _udata.setdefault("unlock_subs", [])
    if not _udata["unlock_subs"]:
        return
    _udata.setdefault("alerted_unlocks", [])

    import asyncio
    now = time.time()
    week_later = now + 7 * 86400  # 未来7天

    async def check(symbol, project):
        try:
            name, future, total = await get_unlock_events(project)
            if not future or not total:
                return None
            for e in future:
                if now < e["timestamp"] <= week_later:
                    toks = e.get("noOfTokens", [])
                    amount = sum(toks) if toks else 0
                    pct = (amount / total * 100) if total else 0
                    if pct >= 1.0:  # 占比1%以上才提醒
                        # 去重key：币+解锁时间
                        akey = f"{symbol}_{int(e['timestamp'])}"
                        if akey not in _udata["alerted_unlocks"]:
                            return {"symbol": symbol, "ts": e["timestamp"],
                                    "pct": pct, "category": e.get("category", "?"), "akey": akey}
            return None
        except Exception:
            return None

    syms = list(SYMBOL_MAP.items())
    tasks = [check(s, p) for s, p in syms]
    res = await asyncio.gather(*tasks)
    alerts = [r for r in res if r]

    if not alerts:
        return

    alerts.sort(key=lambda x: x["ts"])
    lines = ["🔓 *解锁提醒*（未来7天大额解锁）\n"]
    for a in alerts:
        dt = datetime.datetime.fromtimestamp(a["ts"]).strftime("%m-%d")
        days = int((a["ts"] - now) / 86400)
        lines.append(f"⚠️ {dt}({days}天后) *{a['symbol']}* 解锁{a['pct']:.1f}% ({a['category']})")
        _udata["alerted_unlocks"].append(a["akey"])
    lines.append("\n大额解锁常带抛压，持有者注意\n⚠️ 不构成投资建议")
    text = "\n".join(lines)

    for chat_id in _udata["unlock_subs"]:
        try:
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
        except Exception as e:
            logging.error(f"解锁提醒推送失败 {chat_id}: {e}")

    # 清理过期的已提醒记录（保留最近200条）
    _udata["alerted_unlocks"] = _udata["alerted_unlocks"][-200:]
    _usave()
