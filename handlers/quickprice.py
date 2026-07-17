import logging
import asyncio
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from api import get_price, get_price_okx
from config import COIN_IDS
from handlers.util import escape_md, safe_reply


def fmt_price(p):
    """智能价格格式：大数字加逗号，小数字保留有效位"""
    if p >= 1:
        return f"{p:,.2f}"       # 1以上：58,940.00
    elif p >= 0.01:
        return f"{p:.4f}"        # 0.01-1：0.0378
    elif p >= 0.0001:
        return f"{p:.6f}"        # 很小：0.000123
    else:
        return f"{p:.8f}"        # 极小：0.00000012

def is_group(update):
    return update.effective_chat.type in ("group", "supergroup")

async def _okx_ticker(inst):
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get("https://www.okx.com/api/v5/market/ticker", params={"instId": inst})
            d = r.json()
            if d.get("code") == "0" and d.get("data"):
                t = d["data"][0]
                last = float(t["last"]); op = float(t["open24h"])
                ch = (last - op) / op * 100 if op > 0 else 0
                return {"price": last, "change": ch}
    except Exception:
        pass
    return None

async def _okx_funding(inst):
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get("https://www.okx.com/api/v5/public/funding-rate", params={"instId": inst})
            d = r.json()
            if d.get("code") == "0" and d.get("data"):
                return float(d["data"][0]["fundingRate"]) * 100
    except Exception:
        pass
    return None

async def price_hint(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """底部'💰 查价'快捷键：提示直接发币名。"""
    await update.message.reply_text("💰 直接发送币名即可查价，例如：BTC、eth、pepe")


async def quick_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    if text.startswith("/"):
        return

    # AI 问答会话：私聊点了菜单「💬 AI 助手」进入会话后，每条消息都走 AI（连续对话）
    # 只在私聊生效；群里用 @机器人 / 回复机器人。退出发 /menu 或点退出按钮。
    if context.user_data.get("ai_session") and not is_group(update):
        from handlers.chat import handle_ask_text
        await handle_ask_text(update, context, text)
        return

    # 地址追踪：用户点了"添加地址"，现在发来的是以太坊地址
    if context.user_data.get("await_track_addr"):
        import re as _re
        cand = text.strip()
        if not _re.match(r"^0x[0-9a-fA-F]{40}$", cand):
            await update.message.reply_text("请发送 0x 开头的 42 位以太坊地址（取消发 /menu）")
            return
        context.user_data.pop("await_track_addr", None)
        from handlers.whale_track import add_tracked_addr
        ok, msg = await add_tracked_addr(update.effective_chat.id, cand)
        await update.message.reply_text(msg)
        return

    # 引导式实盘开仓：点按钮选完 币/方向/杠杆 后，发来的是「保证金 价格 [tp= sl=]」
    ro = context.user_data.get("await_ropen")
    if ro:
        parts = text.replace(",", " ").replace("，", " ").split()
        if len(parts) < 2:
            await update.message.reply_text("请发「保证金 价格」，例如 `1000 62000`（取消发 /menu）",
                                            parse_mode="Markdown")
            return
        try:
            margin = float(parts[0]); price = float(parts[1])
        except ValueError:
            await update.message.reply_text("保证金和价格要是数字，例如 `1000 62000`",
                                            parse_mode="Markdown")
            return
        from handlers.rtrade import _parse_kv, prepare_open
        tp, sl = _parse_kv(parts[2:])
        context.user_data.pop("await_ropen", None)
        await prepare_open(update.message, context, ro["symbol"], ro["side"],
                           margin, ro["lev"], price, tp, sl)
        return

    # 引导式实盘开仓：点了「其他币」，发来的是币名
    if context.user_data.get("await_ropen_coin"):
        cand = text.strip()
        if " " in cand or len(cand) > 12:
            await update.message.reply_text("请发单个币名，例如 ARB（取消发 /menu）")
            return
        context.user_data.pop("await_ropen_coin", None)
        from handlers.rtrade import guided_after_coin
        await guided_after_coin(update.message, context, cand.upper())
        return

    # 引导式改止损：点了持仓的「改止损」，发来的是新止损价
    rsl = context.user_data.get("await_rsl")
    if rsl:
        try:
            price = float(text.replace(",", "").replace("$", "").replace("，", ""))
        except ValueError:
            await update.message.reply_text("请发数字止损价，例如 60000（取消发 /menu）")
            return
        context.user_data.pop("await_rsl", None)
        from handlers.rtrade import apply_sl
        await apply_sl(update.message, rsl["symbol"], price)
        return

    # 引导式：用户点了"持续波动监控"，现在发来的是「币 百分比」
    if context.user_data.get("await_watchpct"):
        parts = text.split()
        if len(parts) < 2:
            await update.message.reply_text("请发「币 百分比」，例如 `DOGE 5`（取消发 /menu）",
                                            parse_mode="Markdown")
            return
        try:
            pct = float(parts[1])
        except ValueError:
            await update.message.reply_text("百分比要是数字，例如 `DOGE 5`（取消发 /menu）",
                                            parse_mode="Markdown")
            return
        from handlers.watchpct import add_watch, parse_market
        market = parse_market(parts[2]) if len(parts) > 2 else "auto"
        ok, msg = await add_watch(update.effective_chat.id, parts[0].upper(), pct,
                                  update.effective_user.first_name, market)
        if ok:
            context.user_data.pop("await_watchpct", None)
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    # 引导式预警：用户点了"查其他币"，现在发来的是"要设预警的币名"
    if context.user_data.get("await_alert_coin"):
        cand = text.upper()
        if not cand or " " in cand or len(cand) > 12:
            await update.message.reply_text("请发送单个币名，例如 pepe（取消发 /menu）")
            return
        if cand not in COIN_IDS:
            await update.message.reply_text(
                f"暂不支持给 {cand} 设预警（仅支持市值较前的币）。换一个，或发 /menu 取消")
            return
        context.user_data.pop("await_alert_coin", None)
        from handlers.menu import alert_direction_kb
        await update.message.reply_text(
            f"🔔 *{cand} 价格预警*\n选择提醒方式：",
            reply_markup=alert_direction_kb(cand), parse_mode="Markdown")
        return

    # 引导式预警：用户刚点了"选币→选方向"，现在发来的是触发价格
    pending = context.user_data.get("await_alert")
    if pending:
        try:
            target = float(text.replace(",", "").replace("$", "").replace("，", ""))
        except ValueError:
            await update.message.reply_text("请发送数字价格，例如 65000（取消发 /menu）")
            return
        from storage import data as _ad, save_data as _as
        _ad["alerts"].append({
            "type": "fixed", "chat_id": update.effective_chat.id,
            "symbol": pending["symbol"], "target": target,
            "direction": pending["direction"],
            "set_by": update.effective_user.first_name,
        })
        _as()
        context.user_data.pop("await_alert", None)
        arrow = "涨破" if pending["direction"] == "above" else "跌破"
        await update.message.reply_text(
            f"✅ 预警已设置：{pending['symbol']} {arrow} ${target:,.2f}\n到价会自动提醒你。"
        )
        return

    if " " in text or len(text) > 12 or len(text) < 1:
        return
    # 群里只把"像币代码"的消息(纯ASCII字母/数字)当查询；
    # 中文、带标点、普通聊天不触发，避免刷屏
    if is_group(update) and not (text.isascii() and text.isalnum()):
        return
    symbol = text.upper()

    try:
        spot_cg = await get_price(symbol)
        spot_okx = await get_price_okx(symbol) if spot_cg is None else None
        # CoinGecko、OKX 都没有 → 回退币安 → 再回退 Bybit
        spot_bn = None
        spot_by = None
        if spot_cg is None and spot_okx is None:
            from handlers.binance import get_price_binance
            spot_bn = await get_price_binance(symbol)
            if spot_bn is None:
                from handlers.bybit import get_price_bybit
                spot_by = await get_price_bybit(symbol)

        swap_tk = await _okx_ticker(f"{symbol}-USDT-SWAP")
        swap_fr = await _okx_funding(f"{symbol}-USDT-SWAP") if swap_tk else None
        swap_src = "OKX"
        if not swap_tk:  # OKX 无该永续 → 回退币安
            from handlers.binance import get_swap_ticker_binance, get_funding_binance
            swap_tk = await get_swap_ticker_binance(symbol)
            if swap_tk:
                swap_src = "币安"
                swap_fr = await get_funding_binance(symbol)
        if not swap_tk:  # 币安也没有 → 回退 Bybit
            from handlers.bybit import get_swap_ticker_bybit, get_funding_bybit
            swap_tk = await get_swap_ticker_bybit(symbol)
            if swap_tk:
                swap_src = "Bybit"
                swap_fr = await get_funding_bybit(symbol)

        if spot_cg is None and spot_okx is None and spot_bn is None and spot_by is None and not swap_tk:
            # 群里对太短(<3)的不提示，避免把 ok/hi 之类当查询刷屏；
            # 单字母(如 T/B)无论群聊私聊，查不到就静默，避免把随手打的字母当查询刷屏
            if (is_group(update) and len(text) < 3) or len(text) < 2:
                return
            await update.message.reply_text(f"没查到 {symbol}，检查下币名（或试 /price {symbol}）")
            return

        spot = spot_cg or spot_okx or spot_bn or spot_by
        if spot_cg:
            spot_src = "CoinGecko"
        elif spot_okx:
            spot_src = "OKX"
        elif spot_bn:
            spot_src = "Binance"
        elif spot_by:
            spot_src = "Bybit"
        else:
            spot_src = None
        # 资金费率/合约来源统一成英文所名，跟卡片风格一致（币安→Binance）
        swap_src_disp = {"币安": "Binance"}.get(swap_src, swap_src)

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🤖 AI解读", callback_data=f"doai:{symbol}"),
            InlineKeyboardButton("📈 深度分析", callback_data=f"doanalyze:{symbol}"),
        ]])
        # 完整信息：信息卡 + 蜡烛图/研判 两条消息（永久全自动推送）
        from handlers.detail import send_full_detail
        await send_full_detail(update.message, symbol, spot, spot_src,
                               swap_tk, swap_fr, swap_src_disp, reply_markup=kb)
    except Exception as e:
        logging.error(f"快捷查价出错: {e}")
