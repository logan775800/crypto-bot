"""巨鲸地址追踪：关注指定地址，它一有链上动作(ETH 转账 / ERC20 代币转账)就通知。
数据源：Etherscan V2 API（免费 key）。目前追踪以太坊主网(chainid=1)。
"""
import re
import logging
import asyncio
import httpx
from telegram import Update
from telegram.ext import ContextTypes
from config import ETHERSCAN_API_KEY
from storage import data, save_data
from api import get_price

DEFAULT_MIN_USD = 10000       # 默认只推 ≥ $1万 的转账，过滤小额噪音
STABLES = {"USDT", "USDC", "DAI", "TUSD", "FDUSD", "USDE", "USDS", "BUSD", "PYUSD"}

def _usd(sym, amt, eth_price):
    """估算美元价值：稳定币=面值，ETH=按现价，其它无法定价返回 None。"""
    if sym in STABLES:
        return amt
    if sym == "ETH" and eth_price:
        return amt * eth_price
    return None

ES_BASE = "https://api.etherscan.io/v2/api"
CHAIN_ID = 1
MAX_ADDR_PER_CHAT = 10        # 每个会话最多关注地址数
MAX_ALERTS_PER_ADDR = 5       # 每次轮询每个地址最多推几条，防刷屏
ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def _short(a):
    return a[:6] + "..." + a[-4:] if a and a.startswith("0x") and len(a) > 12 else (a or "?")


async def _es_get(client, params):
    p = {"chainid": CHAIN_ID, "apikey": ETHERSCAN_API_KEY, **params}
    r = await client.get(ES_BASE, params=p)
    r.raise_for_status()
    return r.json()


async def _current_block(client):
    d = await _es_get(client, {"module": "proxy", "action": "eth_blockNumber"})
    return int(d["result"], 16)


async def _new_events(client, addr, last_block):
    """取 addr 在 last_block 之后的新事件(ETH+代币)，返回 (events, max_block)。"""
    events = []
    max_block = last_block
    start = last_block + 1
    for action in ("txlist", "tokentx"):
        try:
            d = await _es_get(client, {
                "module": "account", "action": action, "address": addr,
                "startblock": start, "endblock": 99999999,
                "page": 1, "offset": 30, "sort": "asc",
            })
        except Exception as e:
            logging.error(f"etherscan {action} 出错 {addr}: {e}")
            continue
        if str(d.get("status")) != "1" or not isinstance(d.get("result"), list):
            continue
        for tx in d["result"]:
            try:
                blk = int(tx["blockNumber"])
            except (KeyError, ValueError):
                continue
            if blk <= last_block:
                continue
            max_block = max(max_block, blk)
            frm = (tx.get("from") or "").lower()
            to = (tx.get("to") or "").lower()
            direction = "转出" if frm == addr else ("转入" if to == addr else "相关")
            other = to if direction == "转出" else frm
            if action == "txlist":
                try:
                    amt = int(tx["value"]) / 1e18
                except (KeyError, ValueError):
                    amt = 0
                if amt <= 0:
                    continue   # 跳过 0-ETH 的合约调用
                events.append({"blk": blk, "hash": tx.get("hash", ""),
                               "sym": "ETH", "amt": amt, "dir": direction, "other": other})
            else:  # tokentx
                try:
                    dec = int(tx.get("tokenDecimal") or 18)
                    amt = int(tx["value"]) / (10 ** dec)
                except (KeyError, ValueError):
                    amt = 0
                events.append({"blk": blk, "hash": tx.get("hash", ""),
                               "sym": tx.get("tokenSymbol") or "TOKEN", "amt": amt,
                               "dir": direction, "other": other})
        await asyncio.sleep(0.25)  # 尊重免费额度(5/s)
    events.sort(key=lambda x: x["blk"])
    return events, max_block


# ---------- 登记(命令和按钮共用) ----------
async def add_tracked_addr(chat_id, addr, label=None):
    """登记一个追踪地址，返回 (是否成功, 提示文本)。"""
    if not ETHERSCAN_API_KEY:
        return False, "未配置 Etherscan API key（管理员需在 .env 设 ETHERSCAN_API_KEY）"
    addr = addr.lower()
    if not ADDR_RE.match(addr):
        return False, "地址格式不对，应为 0x 开头的 42 位地址"
    cid = str(chat_id)
    data.setdefault("whale_addr", {}).setdefault(cid, {})
    if addr not in data["whale_addr"][cid] and len(data["whale_addr"][cid]) >= MAX_ADDR_PER_CHAT:
        return False, f"最多关注 {MAX_ADDR_PER_CHAT} 个地址，先删一个"
    label = (label or _short(addr))[:30]
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            cur = await _current_block(client)
    except Exception as e:
        logging.error(f"获取当前区块失败: {e}")
        cur = 0
    data["whale_addr"][cid][addr] = {"label": label, "last": cur}
    save_data()
    return True, f"✅ 已关注 {label}，之后它一有动作(ETH/代币转账)就通知你"


# ---------- 命令 ----------
async def track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("用法：/track 0x地址 [备注]\n(该地址在以太坊主网有转账/代币转账时通知你)")
        return
    label = " ".join(context.args[1:]) if len(context.args) > 1 else None
    ok, msg = await add_tracked_addr(update.effective_chat.id, context.args[0], label)
    await update.message.reply_text(msg)


async def untrack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("用法：/untrack 0x地址")
        return
    addr = context.args[0].lower()
    chat_id = str(update.effective_chat.id)
    d = data.get("whale_addr", {}).get(chat_id, {})
    if addr in d:
        del d[addr]
        save_data()
        await update.message.reply_text("已取消关注")
    else:
        await update.message.reply_text("没关注这个地址")


async def tracked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    d = data.get("whale_addr", {}).get(chat_id, {})
    if not d:
        await update.message.reply_text("还没关注任何地址。用 /track 0x地址 [备注] 添加")
        return
    lines = ["🐋 你关注的地址：\n"]
    for addr, cfg in d.items():
        lines.append(f"• {cfg.get('label') or _short(addr)}\n  `{addr}`")
    lines.append("\n取消：/untrack 0x地址")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ---------- 后台轮询 ----------
async def check_tracked(context: ContextTypes.DEFAULT_TYPE):
    watch = data.get("whale_addr", {})
    if not watch or not ETHERSCAN_API_KEY:
        return
    # 取一次 ETH 现价用于折算美元
    try:
        r = await get_price("ETH")
        eth_price = r["price"] if r else None
    except Exception:
        eth_price = None
    mins = data.get("whale_min", {})
    changed = False
    async with httpx.AsyncClient(timeout=15) as client:
        for chat_id, addrs in list(watch.items()):
            min_usd = mins.get(str(chat_id), DEFAULT_MIN_USD)
            for addr, cfg in list(addrs.items()):
                last = cfg.get("last", 0)
                try:
                    events, max_blk = await _new_events(client, addr, last)
                except Exception as e:
                    logging.error(f"追踪地址出错 {addr}: {e}")
                    continue
                if max_blk > last:
                    cfg["last"] = max_blk
                    changed = True
                # 过滤：min_usd=0 全推；否则只推能折算美元且 ≥ 阈值的(稳定币/ETH)
                kept = []
                for e in events:
                    u = _usd(e["sym"], e["amt"], eth_price)
                    if min_usd <= 0 or (u is not None and u >= min_usd):
                        e["usd"] = u
                        kept.append(e)
                if not kept:
                    continue
                label = cfg.get("label") or _short(addr)
                shown = kept[-MAX_ALERTS_PER_ADDR:]
                lines = [f"🐋 *关注地址异动*：{label}\n"]
                for e in shown:
                    arrow = "➡️" if e["dir"] == "转出" else "⬅️"
                    usd = f" (~${e['usd']:,.0f})" if e.get("usd") else ""
                    lines.append(f"{arrow} {e['dir']} {e['amt']:,.4g} {e['sym']}{usd}  对手 {_short(e['other'])}")
                if len(kept) > len(shown):
                    lines.append(f"...另有 {len(kept)-len(shown)} 笔达标")
                last_hash = shown[-1].get("hash")
                if last_hash:
                    lines.append(f"\n🔗 https://etherscan.io/tx/{last_hash}")
                try:
                    await context.bot.send_message(chat_id=int(chat_id), text="\n".join(lines),
                                                   parse_mode="Markdown", disable_web_page_preview=True)
                except Exception as e:
                    logging.error(f"地址异动推送失败 {chat_id}: {e}")
    if changed:
        save_data()
