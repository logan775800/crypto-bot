"""标注图表 /achart —— 把 AI 说的那些位置**画在图上**，而不是让你对着一串数字脑补。

和 detail.py 的蜡烛图（日线 MA7/25/99，查币名时自动发）的分工：
那张是「这币最近什么样」；这张是「这单怎么打」——任意周期 + 结构位 + 止损距离都标出来。

画什么：
  MA3/MA13/MA23 三条线（顺势与否看排列，和破位扫描同一套）
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
PLOT_BARS = 120        # 画最近 120 根；均线用全量算好再截，保证长周期均线有值


async def _klines(symbol, interval, limit=400, source=None):
    """取 K 线（旧→新）。source 是数据源标签，不传就是 Bybit 永续。

    返回的行沿用 [时间戳, 开, 高, 低, 收, 量, 额] 这个顺序——统一层出来就是它，
    画图和指标都按位取值，别改顺序。
    """
    from handlers import klines as kl
    ex, market = ("bybit", "swap") if not source else kl.src_mod.split_label(source)
    if ex == kl.src_mod.AUTO:
        ex, market = "bybit", "swap"
    rows, meta = await kl.fetch(symbol, interval, limit, ex, market)
    return rows, meta


# 均线周期：MA3 / MA13 / MA23。
# 短中长三根，3 贴着价格走、13 是中期、23 是这套打法的生命线——
# 三根同向才叫"顺势"，缠在一起就是箱体，这也是破位扫描的判定依据（handlers/breakout.py），
# 图上画的和信号用的必须是同一套，否则看到的和报的对不上。
MA_PERIODS = (3, 13, 23)
MA_COLORS = ("#f5b800", "#2962ff", "#8e44ad")


def _ma_series(closes, n):
    """逐根简单移动平均。前 n-1 根没有值 → None，mplfinance 会自动断开不画。"""
    if len(closes) < n:
        return [None] * len(closes)
    out = [None] * (n - 1)
    run = sum(closes[:n])
    out.append(run / n)
    for i in range(n, len(closes)):
        run += closes[i] - closes[i - n]
        out.append(run / n)
    return out


# 三根均线之间至少要拉开这么多（占价格%）才算"排好队"。
# 光看大小关系不够：完美震荡的序列也能排出单调顺序，但三根粘在 0.1% 以内，
# 那就是缠绕，不是顺势——这种时候的"突破"最容易立刻被打回来。
MIN_MA_SPREAD_PCT = 0.05


def ma_align(closes, periods=MA_PERIODS):
    """三根均线是否顺势 → 1(多头排列) / -1(空头排列) / 0(缠绕)。

    顺势 = 排列 + 拉开距离：MA3>MA13>MA23 且首尾间距够大才算多头，反之空头。
    """
    vals = []
    for n in periods:
        ser = _ma_series(closes, n)
        if not ser or ser[-1] is None:
            return 0
        vals.append(ser[-1])
    ref = closes[-1] or vals[0]
    if not ref:
        return 0
    if abs(vals[0] - vals[-1]) / abs(ref) * 100 < MIN_MA_SPREAD_PCT:
        return 0                     # 粘在一起 = 还没选边
    if vals[0] > vals[1] > vals[2]:
        return 1
    if vals[0] < vals[1] < vals[2]:
        return -1
    return 0


# ── 中文字体 ──────────────────────────────────────────────────────
# 镜像里装了 fonts-noto-cjk（见 Dockerfile）。但**不能假设一定装上了**：
# 本地跑、旧镜像、别人拿去部署都可能没有。没字体时中文会渲染成一排豆腐块，
# 那比退回显示英文/地址还糟。所以探测一次，探到才用中文。
_CJK = {"checked": False, "name": None}
_CJK_NAMES = ("Noto Sans CJK SC", "Noto Sans CJK JP", "Noto Sans CJK",
              "Source Han Sans SC", "WenQuanYi Zen Hei",
              "Microsoft YaHei", "SimHei", "PingFang SC")
_CJK_FILE_HINTS = ("notosanscjk", "notoserifcjk", "sourcehansans", "wqy", "msyh")


def cjk_font():
    """可用的中文字体名；没有返回 None（调用方退回 ASCII）。"""
    if _CJK["checked"]:
        return _CJK["name"]
    _CJK["checked"] = True
    try:
        import matplotlib.font_manager as fm
        have = {f.name for f in fm.fontManager.ttflist}
        for name in _CJK_NAMES:
            if name in have:
                _CJK["name"] = name
                break
        else:
            # 各发行版 Noto 的 family 名不一致，再按文件名兜一层
            for f in fm.fontManager.ttflist:
                if any(k in (f.fname or "").lower() for k in _CJK_FILE_HINTS):
                    _CJK["name"] = f.name
                    break
    except Exception as e:
        log.debug(f"探测中文字体失败: {e}")
    log.info(f"图表中文字体：{_CJK['name'] or '无（标题退回 ASCII）'}")
    return _CJK["name"]


def apply_cjk(style_kwargs=None):
    """给 mplfinance 的 style 补上中文字体设置。没字体就原样返回。"""
    font = cjk_font()
    out = dict(style_kwargs or {})
    if font:
        rc = dict(out.get("rc") or {})
        rc["font.family"] = font
        rc["axes.unicode_minus"] = False      # 中文字体下负号会变成方块
        out["rc"] = rc
    return out


def levels(rows, plot_bars=PLOT_BARS):
    """算出要标在图上的关键位。纯函数，方便测。

    plot_bars = 图上实际画出来的根数。ATR/RSI/均线/摆动点要用**全量**算才准，
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
        # 均线换成 MA3/13/23（和破位扫描同一套）——图上画的和说明里写的必须一致
        "ma": {n: (_ma_series(c, n)[-1] if len(c) >= n else None)
               for n in MA_PERIODS},
        "ma_align": ma_align(c),
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
    """结构标签。有中文字体就直接用中文（更好读），没有才用 ASCII 对照表。"""
    tag = lv.get("structure") or ""
    if cjk_font():
        return tag
    return _STRUCT_ASCII.get(tag, "")


def caption(symbol, interval, lv):
    sym = md.norm(symbol).replace("USDT", "")
    last = lv["last"]
    mas = lv.get("ma") or {}
    align = lv.get("ma_align", 0)
    arr = {1: "多头排列 📈（MA3>MA13>MA23）",
           -1: "空头排列 📉（MA3<MA13<MA23）"}.get(
        align, "缠绕——三根粘在一起，还在箱体里")
    if not any(v is not None for v in mas.values()):
        arr = "数据不足"
    lines = [
        f"📐 *{sym} {interval}* 现价 {md.f(last)}",
        f"结构 {lv['structure']}｜均线 {arr}"
        + (f"｜RSI {lv['rsi']:.0f}" if lv.get("rsi") is not None else ""),
        "",
        "*图上的线*",
    ]
    # K线不够长时长周期均线算不出来，图上也不会画——那就别在说明里列它
    icons = ("🟡", "🔵", "🟣")
    shown = [f"{ic}MA{n} {md.f(mas.get(n))}"
             for ic, n in zip(icons, MA_PERIODS) if mas.get(n) is not None]
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


async def build_chart(symbol, interval=DEFAULT_IV, source=None):
    """返回 (buf, caption_text)；数据/绘图失败返回 None，调用方给友好提示。"""
    rows, meta = await _klines(symbol, interval, source=source)
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
    ma = {n: _ma_series(closes, n) for n in MA_PERIODS}

    idx = [datetime.datetime.utcfromtimestamp(int(x[0]) / 1000) for x in rows]
    df = pd.DataFrame(
        {"Open": [float(x[1]) for x in rows], "High": [float(x[2]) for x in rows],
         "Low": [float(x[3]) for x in rows], "Close": closes,
         "Volume": [float(x[5]) for x in rows],
         **{f"MA{n}": ma[n] for n in MA_PERIODS}},
        index=pd.DatetimeIndex(idx))
    # 均线整条算完再截尾：否则最后 120 根里长周期均线全是空的
    df = df.tail(PLOT_BARS)

    sym = md.norm(symbol).replace("USDT", "")
    mc = mpf.make_marketcolors(up="#26a69a", down="#ef5350", edge="inherit",
                               wick="inherit", volume="in")
    style = mpf.make_mpf_style(**apply_cjk(
        dict(base_mpf_style="charles", marketcolors=mc,
             gridstyle=":", facecolor="white")))
    aps = []
    for col, color in zip([f"MA{n}" for n in MA_PERIODS], MA_COLORS):
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
    cap = caption(symbol, interval, lv)
    # 图上没地方写来源（标题只能放 ASCII），但看图的人必须知道这是哪家的 K 线
    if meta.get("label"):
        cap += f"\n📡 数据源 {meta['label']}"
        if meta.get("capped"):
            cap += f"（该所单次上限 {meta['got']} 根）"
    return buf, cap


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
            "图上会标：MA3/13/23、摆动高低点（结构失效位=止损该放的地方）、"
            "前高前低（流动性区=止盈参考）、VWAP、1.5×ATR 止损带。",
            parse_mode="Markdown")
        return
    symbol = args[0].upper().replace("USDT", "")
    interval = args[1].lower() if len(args) > 1 else DEFAULT_IV
    if interval not in IVS:
        await safe_reply(update.message, f"周期只支持 {'/'.join(IVS)}")
        return
    await _send(update.message, symbol, interval,
                source=_pref_label(update.effective_chat.id))


def _pref_label(chat_id):
    """这个会话选的数据源标签；没选过返回 None（= Bybit 永续）。"""
    from handlers.source import pref_label
    return pref_label(chat_id)


async def _send(message, symbol, interval, source=None):
    try:
        r = await build_chart(symbol, interval, source=source)
    except Exception as e:
        log.error(f"achart 出错 {symbol}: {e}")
        await safe_reply(message, f"生成失败：{str(e)[:100]}")
        return
    if not r:
        where = source or "Bybit永续"
        await safe_reply(message, f"❌ 拿不到 {symbol} 的 {interval} K线"
                                  f"（{where} 上有这个交易对吗？/source 可换数据源）")
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
    chat_id = query.message.chat_id if query.message else 0
    await _send(query.message, symbol, interval, source=_pref_label(chat_id))


async def ai_from_btn(query, context, symbol, interval):
    """让 AI 就着同一份数据解读这张图（图和文字用的是同一套指标，不会打架）。"""
    from config import AI_API_KEY, AI_BASE_URL
    if not AI_API_KEY or not AI_BASE_URL:
        await query.answer("AI 未配置", show_alert=True)
        return
    await query.answer("AI 解读中…")
    try:
        # 和图用同一个源，否则图上是 Gate 的结构、AI 讲的是 Bybit 的，两边对不上
        text = await md.klines_analysis(
            symbol, interval,
            source=_pref_label(query.message.chat_id if query.message else 0))
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
