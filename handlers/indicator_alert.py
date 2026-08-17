"""技术指标告警：订阅某币后，日线 RSI 进入超买/超卖、或 MA3/MA23 金叉/死叉时主动推送。
复用 api 的日线序列（CoinGecko，OKX 回退）+ indicators 的 rsi/sma。
基于"状态切换"触发，只在状态发生变化时推送一次，避免刷屏。
"""
import logging
from telegram import Update
from telegram.ext import ContextTypes
from api import get_daily_prices, get_daily_prices_okx
from config import COIN_IDS
from indicators import rsi, sma
from storage import data, save_data


def _rsi_state(r):
    if r is None:
        return None
    if r >= 70:
        return "overbought"
    if r <= 30:
        return "oversold"
    return "neutral"


def _ma_state(ma_fast, ma_slow):
    """短均线在生命线之上/之下 → bull/bear。周期由 annotchart.MA_PERIODS 定，
    参数名别再写死成 ma7/ma30——口径换过一次，写死的名字就是下一次误判的起点。"""
    if not ma_fast or not ma_slow:
        return None
    return "bull" if ma_fast > ma_slow else "bear"


# /rsialert BTC —— 开关订阅（同一 chat + 币 已存在则取消）
async def rsi_alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "📈 *技术指标告警*\n\n"
            "用法：`/rsialert BTC`\n"
            "订阅后，该币日线出现以下情况会主动提醒你：\n"
            "• RSI 进入超买(≥70)/超卖(≤30)\n"
            "• MA3/MA23 金叉(转多)/死叉(转空)\n\n"
            "再发一次同样命令即可取消；`/rsialerts` 查看已订阅。",
            parse_mode="Markdown")
        return
    symbol = context.args[0].upper()
    if symbol not in COIN_IDS:
        await update.message.reply_text(f"不支持的币种：{symbol}")
        return
    chat_id = update.effective_chat.id
    subs = data.setdefault("ti_alerts", [])
    # 已订阅 → 取消
    for s in subs:
        if s["chat_id"] == chat_id and s["symbol"] == symbol:
            subs.remove(s)
            save_data()
            await update.message.reply_text(f"已取消 {symbol} 的技术指标告警")
            return
    subs.append({
        "chat_id": chat_id, "symbol": symbol,
        "rsi_state": None, "ma_state": None,   # 首次评估只记录状态、不推送
    })
    save_data()
    await update.message.reply_text(
        f"✅ 已订阅 *{symbol}* 技术指标告警\n"
        f"RSI 超买/超卖、均线金叉/死叉时会提醒你(每15分钟检查)。\n"
        f"⚠️ 指标仅供参考，不构成投资建议",
        parse_mode="Markdown")


async def rsi_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    mine = [s for s in data.get("ti_alerts", []) if s["chat_id"] == chat_id]
    if not mine:
        await update.message.reply_text("还没有技术指标告警。用 /rsialert BTC 订阅")
        return
    lines = ["📈 我的技术指标告警："]
    for s in mine:
        lines.append(f"• {s['symbol']} (RSI超买超卖 + 均线金叉死叉)")
    lines.append("\n再发 /rsialert 币名 可取消对应订阅")
    await update.message.reply_text("\n".join(lines))


async def _prices_for(symbol):
    prices = None
    if symbol in COIN_IDS:
        prices = await get_daily_prices(symbol, 35)
    if not prices or len(prices) < 31:
        prices = await get_daily_prices_okx(symbol, 35)
    return prices


# 后台检查（job，每15分钟）
async def check_ti_alerts(context: ContextTypes.DEFAULT_TYPE):
    subs = data.get("ti_alerts", [])
    if not subs:
        return
    # 按币聚合，避免同一币重复取价
    symbols = sorted({s["symbol"] for s in subs})
    computed = {}   # symbol -> (rsi_state, ma_state, rsi_val)
    for sym in symbols:
        try:
            prices = await _prices_for(sym)
            if not prices or len(prices) < 31:
                continue
            r = rsi(prices, 14)
            # 均线口径全局统一成 MA3/13/23（handlers/annotchart.MA_PERIODS）——
            # 金叉/死叉看短均线 MA3 与生命线 MA23（原来是 MA7/MA30）
            from handlers.annotchart import MA_PERIODS, _ma_series
            def _last(n):
                ser = _ma_series(prices, n)
                return ser[-1] if ser and ser[-1] is not None else None
            computed[sym] = (_rsi_state(r),
                             _ma_state(_last(MA_PERIODS[0]), _last(MA_PERIODS[-1])), r)
        except Exception as e:
            logging.error(f"指标告警取价/计算出错 {sym}: {e}")

    changed = False
    for s in subs:
        c = computed.get(s["symbol"])
        if not c:
            continue
        new_rsi, new_ma, rsi_val = c
        msgs = []

        # RSI 状态切换（只在进入超买/超卖时提醒）
        prev_rsi = s.get("rsi_state")
        if new_rsi and new_rsi != prev_rsi and prev_rsi is not None:
            if new_rsi == "overbought":
                msgs.append(f"RSI {rsi_val:.0f} 进入超买区(≥70) ⚠️ 可能回调")
            elif new_rsi == "oversold":
                msgs.append(f"RSI {rsi_val:.0f} 进入超卖区(≤30) 💡 可能反弹")
        if new_rsi != prev_rsi:
            s["rsi_state"] = new_rsi
            changed = True

        # 均线金叉/死叉。
        # 键名从 ma_state 换成 ma_state_ma3：口径变了，旧状态是按 MA7/MA30 记的，
        # 沿用它会在换版后凭空推一次"金叉"。新键首轮取到 None → 只记录不推送。
        from handlers.annotchart import MA_PERIODS as _MP
        prev_ma = s.get("ma_state_ma3")
        if new_ma and new_ma != prev_ma and prev_ma is not None:
            if new_ma == "bull":
                msgs.append(f"MA{_MP[0]} 上穿 MA{_MP[-1]} 金叉 📈 转多头")
            else:
                msgs.append(f"MA{_MP[0]} 下穿 MA{_MP[-1]} 死叉 📉 转空头")
        if new_ma != prev_ma:
            s["ma_state_ma3"] = new_ma
            changed = True

        if msgs:
            text = f"📈 {s['symbol']} 技术指标告警\n" + "\n".join(f"• {m}" for m in msgs) + \
                   "\n⚠️ 仅供参考，不构成投资建议"
            try:
                await context.bot.send_message(chat_id=s["chat_id"], text=text)
            except Exception as e:
                logging.error(f"指标告警推送失败 {s['chat_id']}: {e}")

    if changed:
        save_data()
