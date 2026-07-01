import logging
from telegram import Update
from telegram.ext import ContextTypes
from api import get_daily_prices, get_daily_prices_okx
from config import COIN_IDS
from indicators import analyze as do_analyze, macd, bollinger, support_resistance

async def analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("用法：/analyze BTC")
        return
    symbol = context.args[0].upper()

    await update.message.reply_text(f"🔍 正在分析 {symbol}...")

    try:
        prices = None
        if symbol in COIN_IDS:
            prices = await get_daily_prices(symbol, 35)
        if not prices or len(prices) < 15:
            prices = await get_daily_prices_okx(symbol, 35)
        if not prices or len(prices) < 15:
            await update.message.reply_text("历史数据不足，无法分析")
            return

        r = do_analyze(prices)
        macd_line, macd_signal = macd(prices)
        boll = bollinger(prices)
        sr = support_resistance(prices)

        lines = [f"📊 {symbol} 技术分析\n"]
        lines.append(f"当前价: ${r['price']:,.2f}")

        # 信号统计（简单的多空计分）
        bull = 0
        bear = 0

        lines.append("\n【RSI】")
        if r.get("rsi") is not None:
            lines.append(f"RSI(14): {r['rsi']:.1f} - {r['rsi_signal']}")
            if r["rsi"] <= 30: bull += 1
            elif r["rsi"] >= 70: bear += 1

        lines.append("\n【均线】")
        if r.get("ma7"): lines.append(f"MA7: ${r['ma7']:,.2f}")
        if r.get("ma30"): lines.append(f"MA30: ${r['ma30']:,.2f}")
        if r.get("ma_signal"):
            lines.append(r['ma_signal'])
            if "多头" in r['ma_signal']: bull += 1
            else: bear += 1

        lines.append("\n【MACD】")
        if macd_signal:
            lines.append(f"MACD: {macd_signal}")
            if "多头" in macd_signal: bull += 1
            else: bear += 1

        lines.append("\n【布林带】")
        if boll:
            lines.append(f"上轨 ${boll['upper']:,.2f}")
            lines.append(f"中轨 ${boll['mid']:,.2f}")
            lines.append(f"下轨 ${boll['lower']:,.2f}")
            lines.append(f"当前: {boll['pos']}")
            if "下轨" in boll['pos']: bull += 1
            elif "上轨" in boll['pos']: bear += 1

        lines.append("\n【支撑/阻力】")
        if sr:
            lines.append(f"阻力位: ${sr['resistance']:,.2f}")
            lines.append(f"支撑位: ${sr['support']:,.2f}")

        # 综合信号
        lines.append("\n━━━━━━━━")
        if bull > bear:
            lines.append(f"📈 综合偏多 (多{bull}:空{bear})")
        elif bear > bull:
            lines.append(f"📉 综合偏空 (多{bull}:空{bear})")
        else:
            lines.append(f"➡️ 多空均衡 (多{bull}:空{bear})")

        lines.append("\n⚠️ 技术指标仅供参考，不构成投资建议")

        await update.message.reply_text("\n".join(lines))

    except Exception as e:
        logging.error(f"分析出错: {e}")
        await update.message.reply_text("分析失败，请稍后再试")


# B：多周期分析 /multi BTC
from api import get_prices_by_period
from indicators import rsi, sma

async def multi_period(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("用法：/multi BTC")
        return
    symbol = context.args[0].upper()
    if symbol not in COIN_IDS:
        await update.message.reply_text(f"不支持的币种：{symbol}")
        return

    await update.message.reply_text(f"🔍 多周期分析 {symbol}...")

    # 三个周期：短期(1天=小时级)、中期(7天)、长期(90天=日级)
    periods = [("短期", 1), ("中期", 7), ("长期", 90)]
    lines = [f"📊 {symbol} 多周期分析\n"]

    try:
        bull = bear = 0
        for name, days in periods:
            prices = await get_prices_by_period(symbol, days)
            if not prices or len(prices) < 15:
                lines.append(f"【{name}】数据不足")
                continue
            r = rsi(prices, 14)
            ma_short = sma(prices, 7)
            ma_long = sma(prices, min(30, len(prices)-1))
            cur = prices[-1]

            # 判断
            trend = "📈多" if (ma_short and ma_long and ma_short > ma_long) else "📉空"
            if ma_short and ma_long:
                if ma_short > ma_long: bull += 1
                else: bear += 1

            rsi_str = f"{r:.0f}" if r else "—"
            rsi_tag = ""
            if r:
                if r >= 70: rsi_tag = "超买"
                elif r <= 30: rsi_tag = "超卖"
            lines.append(f"【{name}】{trend}  RSI:{rsi_str} {rsi_tag}")

        lines.append("\n━━━━━━━━")
        if bull > bear:
            lines.append(f"多数周期偏多 📈 ({bull}:{bear})")
        elif bear > bull:
            lines.append(f"多数周期偏空 📉 ({bull}:{bear})")
        else:
            lines.append(f"周期信号分歧 ➡️ ({bull}:{bear})")
        lines.append("\n⚠️ 仅供参考，不构成投资建议")

        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        logging.error(f"多周期分析出错: {e}")
        await update.message.reply_text("分析失败")


# C：KDJ + 成交量 /indicators BTC
from api import get_ohlc, get_volumes
from indicators import kdj, volume_analysis

async def indicators_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("用法：/indicators BTC")
        return
    symbol = context.args[0].upper()
    if symbol not in COIN_IDS:
        await update.message.reply_text(f"不支持的币种：{symbol}")
        return

    await update.message.reply_text(f"🔍 计算 {symbol} KDJ和成交量...")

    try:
        ohlc = await get_ohlc(symbol, 30)
        vols = await get_volumes(symbol, 14)

        lines = [f"📊 {symbol} KDJ & 成交量\n"]

        if ohlc and len(ohlc) >= 9:
            highs = [c[2] for c in ohlc]
            lows = [c[3] for c in ohlc]
            closes = [c[4] for c in ohlc]
            k = kdj(highs, lows, closes)
            if k:
                lines.append("【KDJ】")
                lines.append(f"K:{k['k']:.1f} D:{k['d']:.1f} J:{k['j']:.1f}")
                lines.append(f"{k['signal']}")
        else:
            lines.append("【KDJ】数据不足")

        if vols and len(vols) >= 7:
            v = volume_analysis(vols)
            if v:
                lines.append("\n【成交量】")
                lines.append(f"近期/7日均: {v['ratio']:.2f}倍")
                lines.append(f"{v['signal']}")

        lines.append("\n⚠️ 仅供参考，不构成投资建议")
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        logging.error(f"指标计算出错: {e}")
        await update.message.reply_text("计算失败，请稍后再试")


async def build_analysis_text(symbol):
    """返回分析文本（供按钮调用）"""
    from indicators import analyze as _an, macd as _macd, bollinger as _boll, support_resistance as _sr
    prices = await get_daily_prices(symbol, 35)
    if not prices or len(prices) < 15:
        return "数据不足"
    r = _an(prices)
    ml, ms = _macd(prices)
    b = _boll(prices)
    sr = _sr(prices)
    bull = bear = 0
    lines = [f"📊 *{symbol} 技术分析*\n当前: ${r['price']:,.2f}\n"]
    if r.get("rsi") is not None:
        lines.append(f"RSI: {r['rsi']:.1f} - {r['rsi_signal']}")
        if r["rsi"] <= 30: bull += 1
        elif r["rsi"] >= 70: bear += 1
    if r.get("ma_signal"):
        lines.append(r['ma_signal'])
        bull += 1 if "多头" in r['ma_signal'] else 0
        bear += 1 if "空头" in r['ma_signal'] else 0
    if ms:
        lines.append(f"MACD: {ms}")
        bull += 1 if "多头" in ms else 0
        bear += 1 if "空头" in ms else 0
    if b:
        lines.append(f"布林带: {b['pos']}")
    if sr:
        lines.append(f"阻力 ${sr['resistance']:,.0f} | 支撑 ${sr['support']:,.0f}")
    lines.append("━━━━━━")
    if bull > bear: lines.append(f"📈 综合偏多 ({bull}:{bear})")
    elif bear > bull: lines.append(f"📉 综合偏空 ({bull}:{bear})")
    else: lines.append(f"➡️ 均衡 ({bull}:{bear})")
    lines.append("⚠️ 不构成投资建议")
    return "\n".join(lines)
