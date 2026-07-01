import io
import logging
import datetime
import matplotlib
matplotlib.use("Agg")  # 无界面后端，服务器上必须
import matplotlib.pyplot as plt
from telegram import Update
from telegram.ext import ContextTypes
from api import get_market_chart
from config import COIN_IDS

async def chart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("用法：/chart BTC 或 /chart BTC 30\n(币种 天数，默认7天)")
        return
    symbol = context.args[0].upper()
    if symbol not in COIN_IDS:
        await update.message.reply_text(f"不支持的币种：{symbol}")
        return
    try:
        days = int(context.args[1]) if len(context.args) > 1 else 7
    except ValueError:
        days = 7

    await update.message.reply_text(f"📈 正在生成 {symbol} {days}天走势图...")

    try:
        prices = await get_market_chart(symbol, days)
        if not prices:
            await update.message.reply_text("获取历史数据失败")
            return

        # 拆成时间和价格
        times = [datetime.datetime.fromtimestamp(p[0] / 1000) for p in prices]
        values = [p[1] for p in prices]

        # 画图
        plt.figure(figsize=(10, 5))
        # 涨绿跌红（按首尾判断）
        color = "green" if values[-1] >= values[0] else "red"
        plt.plot(times, values, color=color, linewidth=1.5)
        plt.fill_between(times, values, min(values), alpha=0.1, color=color)
        plt.title(f"{symbol} / USD  ({days}d)", fontsize=14)
        plt.grid(True, alpha=0.3)
        plt.xticks(rotation=45)
        plt.tight_layout()

        # 存到内存
        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=80)
        buf.seek(0)
        plt.close()

        # 涨跌幅
        change = (values[-1] - values[0]) / values[0] * 100
        caption = f"📊 {symbol} 近{days}天\n当前: ${values[-1]:,.2f}\n区间涨跌: {change:+.2f}%"

        # 发图片
        await update.message.reply_photo(photo=buf, caption=caption)

    except Exception as e:
        logging.error(f"画图出错: {e}")
        await update.message.reply_text("生成图表失败，请稍后再试")


# 功能8：持仓饼图（私聊用）
from storage import data as _data
from api import get_prices as _get_prices

def _is_group(update):
    return update.effective_chat.type in ("group", "supergroup")

async def portfolio_chart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _is_group(update):
        await update.message.reply_text("🔒 持仓功能涉及隐私，请私聊我使用")
        return
    uid = str(update.effective_user.id)
    holdings = _data["holdings"].get(uid, {})
    if not holdings:
        await update.message.reply_text("你还没有持仓。用 /add BTC 0.5 60000 记录")
        return

    await update.message.reply_text("🥧 正在生成持仓饼图...")

    try:
        prices = await _get_prices(list(holdings.keys()))
        labels = []
        values = []
        for sym, h in holdings.items():
            info = prices.get(sym)
            if not info:
                continue
            value = h["amount"] * info["price"]
            labels.append(f"{sym}\n${value:,.0f}")
            values.append(value)

        if not values:
            await update.message.reply_text("无法获取价格数据")
            return

        plt.figure(figsize=(8, 8))
        colors = plt.cm.Set3.colors
        plt.pie(values, labels=labels, autopct="%1.1f%%", startangle=90, colors=colors)
        plt.title("投资组合分布", fontsize=14)
        plt.axis("equal")

        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=80)
        buf.seek(0)
        plt.close()

        total = sum(values)
        await update.message.reply_photo(
            photo=buf,
            caption=f"💼 持仓总价值: ${total:,.2f}"
        )
    except Exception as e:
        logging.error(f"饼图出错: {e}")
        await update.message.reply_text("生成饼图失败，请稍后再试")


# A：技术分析图（价格+均线+布林带）
from api import get_daily_ohlc_prices
from indicators import sma, bollinger

async def analyze_chart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("用法：/chartanalyze BTC")
        return
    symbol = context.args[0].upper()
    if symbol not in COIN_IDS:
        await update.message.reply_text(f"不支持的币种：{symbol}")
        return

    await update.message.reply_text(f"📈 正在生成 {symbol} 技术分析图...")

    try:
        raw = await get_daily_ohlc_prices(symbol, 35)
        if not raw or len(raw) < 20:
            await update.message.reply_text("数据不足")
            return

        times = [datetime.datetime.fromtimestamp(p[0]/1000) for p in raw]
        prices = [p[1] for p in raw]

        # 算每个点的 MA7、MA30、布林带（滑动）
        ma7_line = []
        ma30_line = []
        boll_up = []
        boll_low = []
        for i in range(len(prices)):
            window = prices[:i+1]
            ma7_line.append(sma(window, 7) if len(window) >= 7 else None)
            ma30_line.append(sma(window, 30) if len(window) >= 30 else None)
            b = bollinger(window, 20) if len(window) >= 20 else None
            boll_up.append(b["upper"] if b else None)
            boll_low.append(b["lower"] if b else None)

        plt.figure(figsize=(12, 6))
        # 价格
        plt.plot(times, prices, color="black", linewidth=1.5, label="价格")
        # 均线
        plt.plot(times, ma7_line, color="orange", linewidth=1, label="MA7")
        plt.plot(times, ma30_line, color="blue", linewidth=1, label="MA30")
        # 布林带通道（填充）
        valid_idx = [i for i in range(len(times)) if boll_up[i] is not None]
        if valid_idx:
            vt = [times[i] for i in valid_idx]
            vu = [boll_up[i] for i in valid_idx]
            vl = [boll_low[i] for i in valid_idx]
            plt.plot(vt, vu, color="gray", linewidth=0.5, linestyle="--", alpha=0.6)
            plt.plot(vt, vl, color="gray", linewidth=0.5, linestyle="--", alpha=0.6)
            plt.fill_between(vt, vu, vl, alpha=0.08, color="gray", label="布林带")

        plt.title(f"{symbol} 技术分析 (35d)", fontsize=14)
        plt.legend(loc="best", fontsize=9)
        plt.grid(True, alpha=0.3)
        plt.xticks(rotation=45)
        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=80)
        buf.seek(0)
        plt.close()

        change = (prices[-1] - prices[0]) / prices[0] * 100
        await update.message.reply_photo(
            photo=buf,
            caption=f"📊 {symbol} 技术分析图\n当前 ${prices[-1]:,.2f} | 35天 {change:+.2f}%\n黑=价格 橙=MA7 蓝=MA30 灰=布林带"
        )
    except Exception as e:
        logging.error(f"技术分析图出错: {e}")
        await update.message.reply_text("生成失败，请稍后再试")
