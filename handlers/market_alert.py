"""市场异动告警：全交易所「新币上线」+「放量异动」。

（涨跌幅分级告警已迁移到 handlers/contract_alert.py 的全交易所合约告警，本模块不再做价格分级。）

覆盖 OKX / 币安 / Bybit 三家现货：
  • 新币上线：每轮 diff 各所 USDT 现货交易对，出现的新符号即通知，标注来源。
  • 放量异动：对比上一轮 24h 成交额，突增≥3倍且量足够大即通知，标注来源。
订阅：/watchmarket 订阅，/unwatchmarket 取消。支持 /follow 只看关注币、/quiet 免打扰。
"""
import re
import logging
import datetime
import asyncio
import httpx
from telegram import Update
from telegram.ext import ContextTypes
from storage import data, save_data

OKX_BASE = "https://www.okx.com"
BINANCE_SPOT = "https://api.binance.com"
BYBIT_BASE = "https://api.bybit.com"

SURGE_RATIO = 3            # 成交额突增倍数阈值
SURGE_MIN_VOL = 2_000_000  # 放量币的最小 24h 成交额（USDT）
LEV_SUFFIX = ("UP", "DOWN", "BULL", "BEAR")   # 币安杠杆代币
_BYBIT_LEV_RE = re.compile(r"\d+[LS]$")         # Bybit 杠杆代币 BTC3L/ETH3S


async def watch_market(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in data["market_watch"]:
        await update.message.reply_text("已订阅市场异动告警 ✅")
        return
    data["market_watch"].append(chat_id)
    save_data()
    await update.message.reply_text(
        "✅ 已订阅市场异动告警！（OKX / 币安 / Bybit）\n\n"
        "• 🆕 新币上线自动通知（标注交易所）\n"
        "• 📊 放量异动（成交量突增，标注交易所）\n"
        "• 每5分钟扫描\n\n"
        "个性化：\n"
        "/follow BTC ETH - 只看关注的币\n"
        "/quiet 23:00 8:00 - 设免打扰\n"
        "取消订阅 /unwatchmarket\n\n"
        "💡 合约涨跌幅分级告警请用 /watchcontract"
    )


async def unwatch_market(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in data["market_watch"]:
        data["market_watch"].remove(chat_id)
        save_data()
        await update.message.reply_text("已取消市场异动告警")
    else:
        await update.message.reply_text("你还没订阅")


def is_quiet(pref):
    """判断当前是否在用户的静音时段"""
    quiet = pref.get("quiet")
    if not quiet:
        return False
    try:
        now = datetime.datetime.now().strftime("%H:%M")
        start, end = quiet[0], quiet[1]
        if start <= end:
            return start <= now < end
        else:  # 跨午夜，如 23:00-8:00
            return now >= start or now < end
    except Exception:
        return False


# ---------- 各交易所现货行情抓取（统一返回 [{sym, change, price, vol}]，vol 为 USDT 成交额）----------
async def _okx_spot(client):
    r = await client.get(f"{OKX_BASE}/api/v5/market/tickers", params={"instType": "SPOT"})
    r.raise_for_status()
    d = r.json()
    if d.get("code") != "0":
        return []
    out = []
    for t in d.get("data", []):
        iid = t.get("instId", "")
        if not iid.endswith("-USDT"):
            continue
        try:
            last = float(t["last"]); op = float(t["open24h"])
            change = (last - op) / op * 100 if op > 0 else 0
            vol = float(t.get("volCcy24h", 0) or 0)   # 现货 volCcy24h 以计价币(USDT)计
            out.append({"sym": iid[:-len("-USDT")], "change": change, "price": last, "vol": vol})
        except (ValueError, KeyError):
            continue
    return out


async def _binance_spot(client):
    r = await client.get(f"{BINANCE_SPOT}/api/v3/ticker/24hr")
    r.raise_for_status()
    out = []
    for t in r.json():
        s = t.get("symbol", "")
        if not s.endswith("USDT"):
            continue
        base = s[:-4]
        if any(base.endswith(x) for x in LEV_SUFFIX):
            continue
        try:
            last = float(t["lastPrice"]); ch = float(t["priceChangePercent"])
            vol = float(t.get("quoteVolume", 0) or 0)
            out.append({"sym": base, "change": ch, "price": last, "vol": vol})
        except (ValueError, KeyError):
            continue
    return out


async def _bybit_spot(client):
    r = await client.get(f"{BYBIT_BASE}/v5/market/tickers", params={"category": "spot"})
    r.raise_for_status()
    d = r.json()
    if d.get("retCode") != 0:
        return []
    out = []
    for t in d.get("result", {}).get("list", []):
        s = t.get("symbol", "")
        if not s.endswith("USDT"):
            continue
        base = s[:-4]
        if _BYBIT_LEV_RE.search(base):
            continue
        try:
            last = float(t["lastPrice"]); ch = float(t["price24hPcnt"]) * 100
            vol = float(t.get("turnover24h", 0) or 0)
            out.append({"sym": base, "change": ch, "price": last, "vol": vol})
        except (ValueError, KeyError):
            continue
    return out


EXCHANGES = [("OKX", _okx_spot), ("币安", _binance_spot), ("Bybit", _bybit_spot)]


async def scan_market(context: ContextTypes.DEFAULT_TYPE):
    if not data["market_watch"]:
        return
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            results = await asyncio.gather(
                *[fn(client) for _, fn in EXCHANGES], return_exceptions=True
            )
    except Exception as e:
        logging.error(f"市场扫描取数出错: {e}")
        return

    data.setdefault("user_prefs", {})
    data.setdefault("known_coins_ex", {})    # {交易所: [已知币]}
    data.setdefault("last_volumes_ex", {})   # {交易所: {币: 成交额}}
    known_ex = data["known_coins_ex"]
    lastvol_ex = data["last_volumes_ex"]

    new_coins = []     # [{ex, sym}]
    volume_surges = [] # [{ex, sym, ratio, change, price}]

    for (ex_name, _), res in zip(EXCHANGES, results):
        if isinstance(res, Exception):
            logging.warning(f"市场扫描 {ex_name} 失败: {res}")
            continue

        cur_syms = [m["sym"] for m in res]
        vol_map = {m["sym"]: m["vol"] for m in res}

        # 新币检测：与上轮该所已知币集合 diff（首轮只建基线，不告警）
        prev_known = known_ex.get(ex_name)
        if prev_known:
            known_set = set(prev_known)
            for m in res:
                if m["sym"] not in known_set:
                    new_coins.append({"ex": ex_name, "sym": m["sym"]})
        known_ex[ex_name] = cur_syms

        # 放量检测：对比上轮该所成交额（首轮只建基线，不告警）
        prev_vol = lastvol_ex.get(ex_name, {})
        for m in res:
            pv = prev_vol.get(m["sym"])
            if pv and pv > 0 and m["vol"] >= SURGE_MIN_VOL:
                ratio = m["vol"] / pv
                if ratio >= SURGE_RATIO:
                    volume_surges.append({"ex": ex_name, "sym": m["sym"], "ratio": ratio,
                                          "change": m["change"], "price": m["price"]})
        lastvol_ex[ex_name] = vol_map

    save_data()

    if not new_coins and not volume_surges:
        return

    volume_surges.sort(key=lambda v: v["ratio"], reverse=True)

    for chat_id in data["market_watch"]:
        pref = data["user_prefs"].get(str(chat_id), {"follows": [], "quiet": None})
        if is_quiet(pref):
            continue
        follows = pref.get("follows", [])

        # 新币告警（不受关注列表限制）
        if new_coins:
            lines = ["🆕 *新币上线*\n"]
            for n in new_coins[:15]:
                lines.append(f"• [{n['ex']}] {n['sym']}/USDT")
            lines.append("\n⚠️ 新币风险极高！不构成投资建议")
            try:
                await context.bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode="Markdown")
            except Exception as e:
                logging.error(f"新币推送失败 {chat_id}: {e}")

        # 放量告警（按关注过滤）
        vs = [v for v in volume_surges if not follows or v["sym"] in follows]
        if vs:
            vlines = ["📊 *放量异动*（成交量突增）\n"]
            for v in vs[:10]:
                vlines.append(f"🔊 [{v['ex']}] {v['sym']}: 量增{v['ratio']:.1f}倍 "
                              f"价{v['change']:+.2f}% (${v['price']:,.4g})")
            vlines.append("\n(放量常预示大资金进出，早于价格变化)\n⚠️ 不构成投资建议")
            try:
                await context.bot.send_message(chat_id=chat_id, text="\n".join(vlines), parse_mode="Markdown")
            except Exception as e:
                logging.error(f"放量推送失败 {chat_id}: {e}")
