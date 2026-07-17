"""标注图表 /achart —— 把 AI 说的那些位置**画在图上**，而不是让你对着一串数字脑补。

和 detail.py 的蜡烛图（日线 MA7/25/99，查币名时自动发）的分工：
那张是「这币最近什么样」；这张是「这单怎么打」——任意周期 + 结构位 + 止损距离都标出来。

画什么：
  EMA20/50/200 三条线（趋势与排列）
  近端摆动高/低（结构失效位——止损该放的地方，不是拍脑袋）
  近50根前高/前低（流动性密集区，止盈参考）
  区间 VWAP
  1.5×ATR 止损带（从现价算，多空各一条）

数据走 Bybit 永续公开接口，指标复用 marketdata 的实现（同一套算法，图和 AI 说的不会打架）。
"""
import io
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from handlers.util import safe_reply
from handlers import marketdata as md

log = logging.getLogger(__name__)

DEFAULT_IV = "1h"
PLOT_BARS = 120        # 画最近 120 根；均线用全量算好再截，保证 EMA200 有值


async def _klines(symbol, interval, limit=400):
    r = await md._get("/v5/market/kline", {
        "category": "linear", "symbol": md.norm(symbol),
        "interval": md.INTERVALS.get(interval, "60"), "limit": limit})
    rows = (r.get("list") or [])[::-1]      # Bybit 返回新→旧，反成旧→新
    return rows


def _ema_series(closes, n):
    """逐根 EMA 序列（marketdata.ema 只给最后一个值，画线要整条）。
    前 n-1 根没有值 → None，mplfinance 会自动断开不画。"""
    if len(closes) < n:
        return [None] * len(closes)
    k = 2 / (n + 1)
    out = [None] * (n - 1)
    e = sum(closes[:n]) / n
    out.append(e)
    for v in closes[n:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def levels(rows, plot_bars=PLOT_BARS):
    """算出要标在图上的关键位。纯函数，方便测。

    plot_bars = 图上实际画出来的根数。ATR/RSI/EMA/摆动点要用**全量**算才准，
    但 VWAP 是「这段区间的平均成本」——必须跟可见区间同口径，否则算出来的线
    落在画布外，caption 却还说「图上的线：VWAP」，等于骗人。"""
    h = [float(x[2]) for x in rows]
    lo = [float(x[3]) for x in rows]
    c = [float(x[4]) for x in rows]
    v = [float(x[5]) for x in rows]
    last = c[-1]
    a14 = md.atr(h, lo, c, 14)
    tag, h3, l3 = md.structure(h, lo)
    n50 = min(50, len(c))
    out = {
        "last": last,
        "atr": a14,
        "structure": tag,
        "swing_high": h3[0] if h3 else None,
        "swing_low": l3[0] if l3 else None,
        "prior_high": max(h[-n50:]),
        "prior_low": min(lo[-n50:]),
        "rsi": md.rsi(c, 14),
        "ema20": md.ema(c, 20), "ema50": md.ema(c, 50), "ema200": md.ema(c, 200),
    }
    # VWAP 只按可见窗口算（见 docstring）
    w = min(plot_bars, len(c))
    tpv = sum(((h[i] + lo[i] + c[i]) / 3) * v[i] for i in range(len(c) - w, len(c)))
    vs = sum(v[-w:])
    out["vwap"] = tpv / vs if vs else None
    # 1.5×ATR 止损带：多单放下方、空单放上方
    if a14:
        out["stop_long"] = last - 1.5 * a14
        out["stop_short"] = last + 1.5 * a14
    # 可见窗口的高低——画图时用它和各标注线一起定 y 轴，保证标注的线真的画得出来
    out["view_high"] = max(h[-w:])
    out["view_low"] = min(lo[-w:])
    return out


def _n(v):
    """md.f(None) 会返回字符串 "None" 并直接印给用户——缺值一律显示破折号。"""
    return md.f(v) if v is not None else "—"


# marketdata.structure 的中文标签 → 图标题用的 ASCII 版（镜像里没有 CJK 字体）
_STRUCT_ASCII = {
    "上升结构(HH+HL)": "Uptrend HH+HL",
    "下降结构(LH+LL)": "Downtrend LH+LL",
    "扩张/震荡放大": "Expanding range",
    "收敛/三角": "Contracting / triangle",
    "震荡/不明确": "Range / unclear",
}


def _ascii_structure(lv):
    return _STRUCT_ASCII.get(lv.get("structure"), "")


def caption(symbol, interval, lv):
    sym = md.norm(symbol).replace("USDT", "")
    arr = "数据不足"
    e20, e50, e200 = lv.get("ema20"), lv.get("ema50"), lv.get("ema200")
    last = lv["last"]
    if e20 and e50 and e200:
        if last > e20 > e50 > e200:
            arr = "多头排列 📈"
        elif last < e20 < e50 < e200:
            arr = "空头排列 📉"
        else:
            arr = f"缠绕（价{'上' if last > e20 else '下'}EMA20）"
    lines = [
        f"📐 *{sym} {interval}* 现价 {md.f(last)}",
        f"结构 {lv['structure']}｜均线 {arr}"
        + (f"｜RSI {lv['rsi']:.0f}" if lv.get("rsi") is not None else ""),
        "",
        "*图上的线*",
    ]
    # K线不够长时 EMA50/200 算不出来，图上也不会画——那就别在说明里列它
    emas = [(f"🟡EMA20 {md.f(e20)}", e20), (f"🔵EMA50 {md.f(e50)}", e50),
            (f"🟣EMA200 {md.f(e200)}", e200)]
    shown = [t for t, v in emas if v is not None]
    lines.append("　".join(shown) if shown else "均线数据不足")
    if lv.get("swing_high") or lv.get("swing_low"):
        lines.append(f"⬛ 摆动高/低 {_n(lv.get('swing_high'))} / {_n(lv.get('swing_low'))}"
                     f"　← 结构失效位，止损放这后面")
    lines.append(f"🔴 前高 {md.f(lv['prior_high'])}　🟢 前低 {md.f(lv['prior_low'])}"
                 f"　← 流动性密集，止盈别放它后面")
    if lv.get("vwap"):
        lines.append(f"⚪ VWAP {md.f(lv['vwap'])}（价在其{'上' if last > lv['vwap'] else '下'}）")
    if lv.get("atr"):
        lines.append("")
        lines.append(f"🟠 *1.5×ATR 止损距离* {md.f(1.5 * lv['atr'])}（{1.5*lv['atr']/last*100:.2f}%）")
        lines.append(f"　做多止损参考 {md.f(lv['stop_long'])}｜做空止损参考 {md.f(lv['stop_short'])}")
        lines.append(f"　仓位 = 权益×0.5~1% ÷ 止损距离")
    lines.append("\n⚠️ 画的是客观位置，不构成投资建议")
    return "\n".join(lines)


async def build_chart(symbol, interval=DEFAULT_IV):
    """返回 (buf, caption_text)；数据/绘图失败返回 None，调用方给友好提示。"""
    rows = await _klines(symbol, interval)
    if len(rows) < 60:
        return None
    try:
        import datetime
        import pandas as pd
        import mplfinance as mpf
    except Exception as e:
        log.error(f"[achart] 绘图库缺失: {e}")
        return None

    lv = levels(rows)
    closes = [float(x[4]) for x in rows]
    e20 = _ema_series(closes, 20)
    e50 = _ema_series(closes, 50)
    e200 = _ema_series(closes, 200)

    idx = [datetime.datetime.utcfromtimestamp(int(x[0]) / 1000) for x in rows]
    df = pd.DataFrame(
        {"Open": [float(x[1]) for x in rows], "High": [float(x[2]) for x in rows],
         "Low": [float(x[3]) for x in rows], "Close": closes,
         "Volume": [float(x[5]) for x in rows],
         "E20": e20, "E50": e50, "E200": e200},
        index=pd.DatetimeIndex(idx))
    # 均线整条算完再截尾：否则最后 120 根里 EMA200 全是空的
    df = df.tail(PLOT_BARS)

    sym = md.norm(symbol).replace("USDT", "")
    mc = mpf.make_marketcolors(up="#26a69a", down="#ef5350", edge="inherit",
                               wick="inherit", volume="in")
    style = mpf.make_mpf_style(base_mpf_style="charles", marketcolors=mc,
                               gridstyle=":", facecolor="white")
    aps = []
    for col, color in (("E20", "#f5b800"), ("E50", "#2962ff"), ("E200", "#8e44ad")):
        if df[col].notna().any():
            aps.append(mpf.make_addplot(df[col], color=color, width=1.1))

    hl, hc, hs = [], [], []

    def _line(val, color, style_):
        if val is None:
            return
        hl.append(val); hc.append(color); hs.append(style_)

    _line(lv.get("swing_high"), "#333333", "--")
    _line(lv.get("swing_low"), "#333333", "--")
    _line(lv["prior_high"], "#ef5350", "-")
    _line(lv["prior_low"], "#26a69a", "-")
    _line(lv.get("vwap"), "#888888", ":")
    _line(lv.get("stop_long"), "#ff9800", "-.")
    _line(lv.get("stop_short"), "#ff9800", "-.")

    kw = {}
    if hl:
        kw["hlines"] = dict(hlines=hl, colors=hc, linestyle=hs, linewidths=0.9)
        # mplfinance 不会为 hlines 自动撑开 y 轴——不显式给 ylim，
        # 落在价格区间外的线(如 1.5×ATR 止损带)会被裁掉，caption 却还说它在图上。
        top = max([lv["view_high"]] + hl)
        bot = min([lv["view_low"]] + hl)
        pad = (top - bot) * 0.04 or top * 0.01
        kw["ylim"] = (bot - pad, top + pad)
    buf = io.BytesIO()
    try:
        # 标题必须**纯 ASCII**：镜像 python:3.11-slim 没有 CJK 字体，
        # 中文在图里会渲染成豆腐块。结构/中文说明一律放 caption（Telegram 文本正常显示）。
        mpf.plot(df, type="candle", volume=True, style=style, addplot=aps,
                 title=f"[{sym}] {interval}  {md.f(lv['last'])}  {_ascii_structure(lv)}",
                 figsize=(11, 6.5), tight_layout=True,
                 savefig=dict(fname=buf, dpi=90, format="png"), **kw)
    except Exception as e:
        log.error(f"[achart] {sym} 绘图失败: {e}")
        return None
    buf.seek(0)
    return buf, caption(symbol, interval, lv)


# ── 命令 ───────────────────────────────────────────────────────────
IVS = ("5m", "15m", "30m", "1h", "4h", "1d")


def _kb(symbol, interval):
    row = [InlineKeyboardButton(("•" if i == interval else "") + i,
                                callback_data=f"ac:{symbol}:{i}") for i in IVS]
    return InlineKeyboardMarkup([
        row[:3], row[3:],
        [InlineKeyboardButton("🤖 AI 解读这张图", callback_data=f"acai:{symbol}:{interval}")],
    ])


async def achart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/achart BTC 1h —— 带结构标注的图。"""
    args = context.args or []
    if not args:
        await safe_reply(update.message,
            "📐 *标注图表*——把结构位/止损距离画在图上\n\n"
            "`/achart BTC`　默认 1h\n"
            "`/achart SOL 15m`　周期：5m/15m/30m/1h/4h/1d\n\n"
            "图上会标：EMA20/50/200、摆动高低点（结构失效位=止损该放的地方）、"
            "前高前低（流动性区=止盈参考）、VWAP、1.5×ATR 止损带。",
            parse_mode="Markdown")
        return
    symbol = args[0].upper().replace("USDT", "")
    interval = args[1].lower() if len(args) > 1 else DEFAULT_IV
    if interval not in IVS:
        await safe_reply(update.message, f"周期只支持 {'/'.join(IVS)}")
        return
    await _send(update.message, symbol, interval)


async def _send(message, symbol, interval):
    try:
        r = await build_chart(symbol, interval)
    except Exception as e:
        log.error(f"achart 出错 {symbol}: {e}")
        await safe_reply(message, f"生成失败：{str(e)[:100]}")
        return
    if not r:
        await safe_reply(message, f"❌ 拿不到 {symbol} 的 {interval} K线（Bybit 有这个永续吗？）")
        return
    buf, cap = r
    try:
        await message.reply_photo(photo=buf, caption=cap, parse_mode="Markdown",
                                  reply_markup=_kb(symbol, interval))
    except Exception as e:
        # caption 里有动态数字，Markdown 渲染失败时降级，别把图也丢了
        log.warning(f"achart 发图 Markdown 失败，降级: {e}")
        buf.seek(0)
        await message.reply_photo(photo=buf, caption=cap.replace("*", ""),
                                  reply_markup=_kb(symbol, interval))


# ── 按钮回调 ───────────────────────────────────────────────────────
async def from_btn(query, context, symbol, interval):
    await query.answer(f"生成 {symbol} {interval}…")
    await _send(query.message, symbol, interval)


async def ai_from_btn(query, context, symbol, interval):
    """让 AI 就着同一份数据解读这张图（图和文字用的是同一套指标，不会打架）。"""
    from config import AI_API_KEY, AI_BASE_URL
    if not AI_API_KEY or not AI_BASE_URL:
        await query.answer("AI 未配置", show_alert=True)
        return
    await query.answer("AI 解读中…")
    try:
        text = await md.klines_analysis(symbol, interval)
        ctx = await md.market_context()
        from handlers.ai import ask_ai_messages
        reply = await ask_ai_messages(
            [{"role": "user", "content":
              f"这是 {symbol} {interval} 的量化数据和大盘环境，用户正看着对应的标注图：\n\n"
              f"{text}\n\n{ctx}\n\n"
              f"请就着图讲：现在是什么结构、关键位在哪、如果要做这个方向进场/止损/止盈"
              f"分别放哪（用数据里的具体价位，不要编）、这单的风险温度。"}],
            system=("你是加密永续合约的执行型分析助手。用户是做杠杆的活跃交易者。"
                    "只用给你的数据里的价位，绝不编造。简体中文，具体、带数字、300字内，"
                    "用风险温度/仓位管理的口吻而非涨跌预测，末尾一句「不构成投资建议」。"))
    except Exception as e:
        log.error(f"achart AI 解读出错: {e}")
        await query.answer("AI 解读失败", show_alert=True)
        return
    from handlers.chat import _send as _ai_send
    await _ai_send(query.message, f"🤖 *{symbol} {interval} 解读*\n\n{reply}")
