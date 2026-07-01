import time
import logging
import datetime
import httpx
from telegram import Update
from telegram.ext import ContextTypes
from storage import data, save_data

OKX_BASE = "https://www.okx.com"

TIERS = [20, 30, 40, 50, 60, 70, 80, 90, 100]
MIN_VOLUME = 500000
TIER_RESET = 86400

async def _okx_get(path, params=None):
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{OKX_BASE}{path}", params=params or {})
        resp.raise_for_status()
        return resp.json()

async def watch_market(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in data["market_watch"]:
        await update.message.reply_text("已订阅市场异动告警 ✅")
        return
    data["market_watch"].append(chat_id)
    save_data()
    await update.message.reply_text(
        "✅ 已订阅 OKX 市场异动告警！\n\n"
        "• 涨/跌幅突破阈值分级告警(20/30/40%...)\n"
        "• 新币上线自动通知\n"
        "• 每5分钟扫描\n\n"
        "个性化：\n"
        "/setalert 15 - 设你的阈值\n"
        "/follow BTC ETH - 只关注特定币\n"
        "/quiet 23:00 8:00 - 设免打扰\n"
        "取消订阅 /unwatchmarket"
    )

async def unwatch_market(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in data["market_watch"]:
        data["market_watch"].remove(chat_id)
        save_data()
        await update.message.reply_text("已取消市场异动告警")
    else:
        await update.message.reply_text("你还没订阅")

def get_tier(change_abs, threshold):
    """根据个人阈值生成台阶。基础阈值起，每+10一档"""
    if change_abs < threshold:
        return 0
    # 从threshold开始，按10递增找最高突破档
    tier = threshold
    t = threshold
    while t <= change_abs:
        tier = t
        t += 10
    return tier

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

async def scan_market(context: ContextTypes.DEFAULT_TYPE):
    if not data["market_watch"]:
        return
    try:
        d = await _okx_get("/api/v5/market/tickers", {"instType": "SPOT"})
        if d["code"] != "0":
            return

        now = time.time()
        data.setdefault("coin_tiers", {})
        data.setdefault("user_prefs", {})

        # 收集所有币的当前涨跌幅
        coin_changes = {}  # sym -> {change, price}
        current_coins = []
        for t in d["data"]:
            if not t["instId"].endswith("-USDT"):
                continue
            sym = t["instId"].replace("-USDT", "")
            current_coins.append(sym)
            try:
                last = float(t["last"])
                op = float(t["open24h"])
                vol = float(t["volCcy24h"])
                if op <= 0 or vol < MIN_VOLUME:
                    continue
                change = (last - op) / op * 100
                coin_changes[sym] = {"change": change, "price": last, "vol": vol}
            except (ValueError, KeyError):
                continue

        # 放量检测：对比上一轮成交量
        data.setdefault("last_volumes", {})
        last_vols = data["last_volumes"]
        volume_surges = []  # 放量的币
        for sym, info in coin_changes.items():
            prev_vol = last_vols.get(sym)
            if prev_vol and prev_vol > 0:
                vol_ratio = info["vol"] / prev_vol
                # 成交量突增3倍以上 + 成交量足够大，算放量
                if vol_ratio >= 3 and info["vol"] >= 2000000:
                    volume_surges.append({"sym": sym, "ratio": vol_ratio,
                                          "change": info["change"], "price": info["price"]})
        # 更新成交量记录
        data["last_volumes"] = {s: i["vol"] for s, i in coin_changes.items()}

        # 检测新币
        new_coins = []
        if data["known_coins"]:
            known = set(data["known_coins"])
            new_coins = [s for s in current_coins if s not in known]
        data["known_coins"] = current_coins

        # 针对每个订阅者，用他的偏好判断
        for chat_id in data["market_watch"]:
            pref = data["user_prefs"].get(str(chat_id),
                                          {"follows": [], "threshold": 20, "quiet": None})
            # 静音检查
            if is_quiet(pref):
                continue
            threshold = pref.get("threshold", 20)
            follows = pref.get("follows", [])

            alerts = []
            for sym, info in coin_changes.items():
                # 如果设了关注列表，只看关注的
                if follows and sym not in follows:
                    continue
                change_abs = abs(info["change"])
                direction = "up" if info["change"] > 0 else "down"
                current_tier = get_tier(change_abs, threshold)
                if current_tier == 0:
                    continue
                # 台阶记录按 chat_id+sym 区分（每人独立）
                tkey = f"{chat_id}_{sym}"
                record = data["coin_tiers"].get(tkey)
                prev_tier = 0
                if record:
                    if record["dir"] != direction or now - record["ts"] > TIER_RESET:
                        prev_tier = 0
                    else:
                        prev_tier = record["tier"]
                if current_tier > prev_tier:
                    alerts.append({"sym": sym, "change": info["change"],
                                   "price": info["price"], "tier": current_tier, "direction": direction})
                    data["coin_tiers"][tkey] = {"tier": current_tier, "dir": direction, "ts": now}

            # 推送这个订阅者的告警
            if alerts:
                alerts.sort(key=lambda x: x["tier"], reverse=True)
                lines = ["🚨 *市场异动告警*\n"]
                for a in alerts:
                    emoji = "🚀" if a["direction"] == "up" else "💥"
                    arrow = "涨破" if a["direction"] == "up" else "跌破"
                    lines.append(f"{emoji} {a['sym']} {arrow} {a['tier']:g}%！现 {a['change']:+.2f}% (${a['price']:,.4g})")
                lines.append("\n⚠️ 异动剧烈，风险高！不构成投资建议")
                try:
                    await context.bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode="Markdown")
                except Exception as e:
                    logging.error(f"异动推送失败 {chat_id}: {e}")

            # 新币告警（不受关注列表限制，但受静音限制）
            if new_coins:
                text = "🆕 *OKX 新币上线*\n\n" + "\n".join(f"• {s}/USDT" for s in new_coins[:10])
                text += "\n\n⚠️ 新币风险极高！不构成投资建议"
                try:
                    await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
                except Exception as e:
                    logging.error(f"新币推送失败 {chat_id}: {e}")

            # 放量告警（按关注过滤）
            if volume_surges:
                vol_filtered = [v for v in volume_surges if not follows or v["sym"] in follows]
                if vol_filtered:
                    vol_filtered.sort(key=lambda x: x["ratio"], reverse=True)
                    vlines = ["📊 *放量异动*（成交量突增）\n"]
                    for v in vol_filtered[:5]:
                        vlines.append(f"🔊 {v['sym']}: 量增{v['ratio']:.1f}倍 价{v['change']:+.2f}% (${v['price']:,.4g})")
                    vlines.append("\n(放量常预示大资金进出，早于价格变化)\n⚠️ 不构成投资建议")
                    try:
                        await context.bot.send_message(chat_id=chat_id, text="\n".join(vlines), parse_mode="Markdown")
                    except Exception as e:
                        logging.error(f"放量推送失败 {chat_id}: {e}")

        # 清理过期台阶记录
        data["coin_tiers"] = {k: v for k, v in data["coin_tiers"].items() if now - v["ts"] < TIER_RESET}
        save_data()

    except Exception as e:
        logging.error(f"市场扫描出错: {e}")
