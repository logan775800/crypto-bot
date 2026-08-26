"""清算地图 /liqmap —— 估算各个价位上堆了多少待强平的仓位，画成图。

## ⚠️ 先说清楚这是估算，不是交易所数据

CoinGlass 那张「清算地图」也不是原始数据——**没有任何交易所公布
「某个价位挂着多少待爆仓的仓位」**，那是每个账户的私有信息。
探针实测（`tools\\probe_liqmap.py`）：
  · 币安 `allForceOrders`（历史强平单）已经 404，公开拿不到了
  · CoinGlass 的 heatmap 接口不在免费档
  · 能白拿的只有 `openInterestHist`（持仓量历史）和 K 线

所以这张图和 CoinGlass 一样是**推算**出来的，口径必须写在脸上，
否则一张看起来很权威的图会被当成事实去下单。

## 推算模型

对每根 K 线：这根期间**新增**的持仓量（ΔOI，金额口径），就当成是在这根的
典型价 P 上建的仓。永续里每一份持仓同时是一个多头和一个空头
（多空持仓量恒等），所以同一份 ΔOI 会同时产生：

    多头爆仓位 = P × (1 − 1/杠杆)      （在 P 下方）
    空头爆仓位 = P × (1 + 1/杠杆)      （在 P 上方）

按几个杠杆档位分配权重，落进价格桶里累加，就是那些柱子。
最后只保留**还没被扫过**的一侧：现价下方的多头爆仓位、上方的空头爆仓位——
已经越过的价位，那些仓早就被平掉了，留着是假的。

**ΔOI 为负（减仓）不计**：那是平仓，不产生新的爆仓位。

杠杆权重是假设，不是实测（交易所不公布每个人开了几倍）。
默认按散户偏高杠杆给，具体数字印在 ℹ️ 卡片上——**假设必须可见**，
否则读图的人不知道柱子高矮有一半是我拍的。

真实爆仓价还要算维持保证金率（`leverageBracket` 要 API key，401），
这里没算，所以估出来的位置比真实爆仓价**略远一点**。
"""
import io
import logging
import time

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

import matplotlib
matplotlib.use("Agg")          # 无界面后端，服务器上必须（和 chart.py 同一条规矩）
import matplotlib.pyplot as plt

from handlers.util import safe_reply, safe_edit
from handlers import busy, guided

log = logging.getLogger(__name__)

BN = "https://fapi.binance.com"
BYBIT = "https://api.bybit.com"

# 杠杆档位和假设权重。和 CoinGlass 那张图用同一批档位，方便对照。
# 权重是**假设**：交易所不公布谁开了几倍。散户在小市值币上普遍偏高杠杆，
# 所以往高倍数偏。改这里等于改整张图的形状，改了要同步改 ℹ️ 卡片上的说明。
LEVS = ((5, 0.12, "#2E9BE6"), (10, 0.20, "#22C3B6"),
        (25, 0.30, "#F5A524"), (50, 0.38, "#F0642B"))

WINDOWS = {                    # 标签 -> (openInterestHist 的 period, 取几根)
    "1日": ("15m", 96),
    "7日": ("1h", 168),
    "30日": ("4h", 180),
    # 90/180 日只有 Bybit 给得出（见 LONG_WINDOWS）。
    # **一年做不到**：Bybit 单次硬卡 200 根，`1d` 就是 199 天上限，
    # 而它没有比 1d 更粗的粒度；币安那边 openInterestHist 只保留 30 天，
    # 换 period、把 limit 提到 500 都一样。别再试了。
    "90日": ("1d", 90),
    "180日": ("1d", 180),
}
# 这些窗口只有 Bybit 有数据，且颗粒粗到一根一天——结论要打折看，见 _long_caveat()
LONG_WINDOWS = {"90日", "180日"}
DEFAULT_WIN = "7日"
BUCKETS = 60                   # 价格分多少个桶
SPAN = 0.30                    # 只画现价 ±30% 的范围，再远的位置没有参考意义
CACHE_TTL = 300

_cache = {}                    # (sym, win) -> {"ts", "data"}


# ── 取数 ────────────────────────────────────────────────────
# Bybit 的持仓量周期名和币安不一样，各写各的
# period -> (Bybit 持仓量接口的 intervalTime, K线接口的 interval)
# 两个接口的粒度写法不一样（一个 "1d"、一个 "D"），映射错了不会报错，
# 只会拿到另一个周期的 K 线去对齐 OI，图安静地画歪。
BYBIT_IV = {"15m": ("15min", "15"), "1h": ("1h", "60"), "4h": ("4h", "240"),
            "1d": ("1d", "D")}


async def _fetch_binance(c, inst, period, limit):
    oi = await c.get(f"{BN}/futures/data/openInterestHist",
                     params={"symbol": inst, "period": period, "limit": limit})
    if oi.status_code != 200:
        return None
    oi_rows = oi.json()
    if not oi_rows:
        return None                      # 币安没这个币，交给 Bybit
    kl = await c.get(f"{BN}/fapi/v1/klines",
                     params={"symbol": inst, "interval": period, "limit": limit})
    kl_rows = kl.json()
    tk = await c.get(f"{BN}/fapi/v1/ticker/price", params={"symbol": inst})
    j = tk.json()
    # ⚠️ 这里以前直接 j["price"]。币安对不存在的币回 400 + {"code":-1121,...}，
    # 于是抛出一个光秃秃的 KeyError('price')，用户看到「画不出来：'price'」
    # ——**报错必须说人话**，不能把内部异常原样甩出去
    if not isinstance(j, dict) or "price" not in j:
        return None
    if not isinstance(kl_rows, list) or len(kl_rows) < 3:
        return None
    return oi_rows, kl_rows, float(j["price"]), "币安"


async def _fetch_bybit(c, inst, period, limit):
    """Bybit 兜底。告警是全交易所的，只认币安等于一半的币点了没图。

    Bybit 的持仓量是**币的个数**，不像币安直接给金额——要自己乘当时的价格
    换成名义金额，否则和币安那条路口径不一致，图的量级会差几个数量级。
    """
    iv = BYBIT_IV.get(period)
    if not iv:
        return None
    oi_iv, kl_iv = iv
    r = await c.get(f"{BYBIT}/v5/market/open-interest", params={
        "category": "linear", "symbol": inst,
        "intervalTime": oi_iv, "limit": min(limit, 200)})
    d = r.json()
    rows = (d.get("result") or {}).get("list") or []
    if d.get("retCode") != 0 or not rows:
        return None
    k = await c.get(f"{BYBIT}/v5/market/kline", params={
        "category": "linear", "symbol": inst,
        "interval": kl_iv, "limit": min(limit, 200)})
    kd = k.json()
    kl_rows = (kd.get("result") or {}).get("list") or []
    if kd.get("retCode") != 0 or len(kl_rows) < 3:
        return None
    closes = {}
    for row in kl_rows:
        try:
            closes[int(row[0])] = float(row[4])
        except (TypeError, ValueError, IndexError):
            continue
    t = await c.get(f"{BYBIT}/v5/market/tickers",
                    params={"category": "linear", "symbol": inst})
    tl = (t.json().get("result") or {}).get("list") or []
    if not tl:
        return None
    last = float(tl[0]["lastPrice"])
    # 归一成币安那套结构，build_map 一行都不用改。
    # 币安是旧→新，Bybit 是新→旧，这里翻过来——不翻的话 ΔOI 的正负全反，
    # "加仓"会被当成"减仓"，整张图直接空掉
    out = []
    for row in reversed(rows):
        try:
            ts = int(row["timestamp"])
            oi_coins = float(row["openInterest"])
        except (KeyError, TypeError, ValueError):
            continue
        px = closes.get(ts) or last
        out.append({"sumOpenInterestValue": str(oi_coins * px), "timestamp": ts})
    if len(out) < 3:
        return None
    return out, kl_rows, last, "Bybit"


async def _fetch(symbol, win):
    """先币安后 Bybit。两家都没有才报错，且要说清是哪一步没有。

    ⚠️ 长窗口（90/180 日）**跳过币安直接走 Bybit**：币安的 openInterestHist
    只保留 30 天，拿它取 90 天会安静地只回 30 天的数据，画出来的图看着正常、
    实际窗口对不上标题——那比画不出来更糟。
    """
    period, limit = WINDOWS[win]
    inst = symbol.upper()
    if not inst.endswith("USDT"):
        inst += "USDT"
    async with httpx.AsyncClient(timeout=20) as c:
        got = None
        if win not in LONG_WINDOWS:
            try:
                got = await _fetch_binance(c, inst, period, limit)
            except Exception as e:
                log.debug(f"清算地图取币安失败 {inst}: {e}")
        if got is None:
            try:
                got = await _fetch_bybit(c, inst, period, limit)
            except Exception as e:
                log.debug(f"清算地图取 Bybit 失败 {inst}: {e}")
    if got is None:
        raise RuntimeError(
            f"{symbol} 画不出来：币安和 Bybit 的永续上都取不到它的持仓量历史。\n"
            f"（清算地图靠持仓量推算，只有这两家提供。"
            f"OKX/Gate 独有的币、或刚上市不久的币会取不到）")
    oi_rows, kl_rows, last, src = got
    return oi_rows, kl_rows, last, inst


# ── 推算 ────────────────────────────────────────────────────
def build_map(oi_rows, kl_rows, last):
    """→ {"lo","hi","edges","longs","shorts","added","bucket"}。

    longs/shorts: 每个杠杆档位一条 list，长度 = BUCKETS，值是估算的名义金额。
    """
    # K 线按时间对齐 OI（两个接口的 period 一样，条数可能差一两根）
    kmap = {int(k[0]): k for k in kl_rows}
    lo, hi = last * (1 - SPAN), last * (1 + SPAN)
    width = (hi - lo) / BUCKETS
    longs = {L: [0.0] * BUCKETS for L, _w, _c in LEVS}
    shorts = {L: [0.0] * BUCKETS for L, _w, _c in LEVS}
    added_total = 0.0

    def put(book, price, amount):
        if price < lo or price >= hi:
            return          # 越出画布范围的位置不画，也不算进强度
        book[int((price - lo) / width)] += amount

    prev = None
    for row in oi_rows:
        try:
            val = float(row["sumOpenInterestValue"])
            ts = int(row["timestamp"])
        except (KeyError, TypeError, ValueError):
            continue
        if prev is not None:
            delta = val - prev
            k = kmap.get(ts)
            if delta > 0 and k:
                # 典型价：这根期间新开的仓大致建在这个价位上
                try:
                    p = (float(k[2]) + float(k[3]) + float(k[4])) / 3
                except (TypeError, ValueError, IndexError):
                    p = None
                if p and p > 0:
                    added_total += delta
                    for L, w, _c in LEVS:
                        amt = delta * w
                        # 永续里每份持仓同时是一多一空，所以两侧都要放
                        put(longs[L], p * (1 - 1 / L), amt)
                        put(shorts[L], p * (1 + 1 / L), amt)
        prev = val

    # 已经被价格越过的一侧要抹掉：那些仓早就被强平或平掉了，留着是假的
    cur_b = int((last - lo) / width)
    for L, _w, _c in LEVS:
        for i in range(BUCKETS):
            if i >= cur_b:
                longs[L][i] = 0.0      # 多头爆仓位只可能在现价下方
            if i <= cur_b:
                shorts[L][i] = 0.0
    edges = [lo + width * i for i in range(BUCKETS)]
    return {"lo": lo, "hi": hi, "edges": edges, "width": width,
            "longs": longs, "shorts": shorts, "added": added_total,
            "cur_bucket": cur_b}


def zones(m, side, top=3):
    """密度最高的几个价位区间。图是给人看的，这个是给人用的。"""
    book = m["longs"] if side == "long" else m["shorts"]
    tot = [sum(book[L][i] for L, _w, _c in LEVS) for i in range(BUCKETS)]
    idx = sorted(range(BUCKETS), key=lambda i: -tot[i])[:top]
    out = []
    for i in idx:
        if tot[i] <= 0:
            continue
        out.append({"lo": m["edges"][i], "hi": m["edges"][i] + m["width"],
                    "amount": tot[i]})
    return sorted(out, key=lambda z: -z["amount"])


def _money(x):
    if x >= 1e8:
        return f"{x / 1e8:.2f}亿"
    if x >= 1e4:
        return f"{x / 1e4:.0f}万"
    return f"{x:.0f}"


def _px(x):
    return f"{x:,.6g}"


# ── 画图 ────────────────────────────────────────────────────
def render(m, sym, win, last):
    # 中文字体：镜像里装了 fonts-noto-cjk，但本地/旧镜像可能没有。
    # 探测复用 annotchart 那套——没字体时中文会渲染成一排豆腐块，
    # 那比退回英文难看得多，所以探到才用中文。
    from handlers.annotchart import cjk_font
    font = cjk_font()
    if font:
        plt.rcParams["font.family"] = font
        plt.rcParams["axes.unicode_minus"] = False
    T = (lambda cn, en: cn) if font else (lambda cn, en: en)

    fig, ax = plt.subplots(figsize=(10, 5.6), dpi=110)
    fig.patch.set_facecolor("#FFFFFF")
    ax.set_facecolor("#FFFFFF")
    w = m["width"] * 0.86

    # 柱子：按杠杆档位堆叠。多头一侧和空头一侧本来就不重叠（被现价分开）
    bottom = [0.0] * BUCKETS
    for L, _wt, color in LEVS:
        vals = [m["longs"][L][i] + m["shorts"][L][i] for i in range(BUCKETS)]
        ax.bar([e + m["width"] / 2 for e in m["edges"]], vals, width=w,
               bottom=bottom, color=color, label=T(f"{L}x 杠杆", f"{L}x"),
               linewidth=0)
        bottom = [bottom[i] + vals[i] for i in range(BUCKETS)]

    # 累计强度：从现价往两边累加——"价格走到这儿，一共会扫掉多少"
    ax2 = ax.twinx()
    cb = m["cur_bucket"]
    tot = bottom
    cum_l, s = [None] * BUCKETS, 0.0
    for i in range(min(cb, BUCKETS - 1), -1, -1):
        s += tot[i]
        cum_l[i] = s
    cum_s, s = [None] * BUCKETS, 0.0
    for i in range(cb, BUCKETS):
        s += tot[i]
        cum_s[i] = s
    xs = [e + m["width"] / 2 for e in m["edges"]]
    lx = [x for x, v in zip(xs, cum_l) if v is not None]
    ly = [v for v in cum_l if v is not None]
    sx = [x for x, v in zip(xs, cum_s) if v is not None]
    sy = [v for v in cum_s if v is not None]
    if lx:
        ax2.plot(lx, ly, color="#E8455F", linewidth=2.2, zorder=5)
        ax2.fill_between(lx, ly, color="#E8455F", alpha=.08)
    if sx:
        ax2.plot(sx, sy, color="#12B39A", linewidth=2.2, zorder=5)
        ax2.fill_between(sx, sy, color="#12B39A", alpha=.08)

    ax.axvline(last, color="#E01E37", linestyle="--", linewidth=2, zorder=6)
    ax.annotate(T(f"现价 {_px(last)}", f"last {_px(last)}"),
                xy=(last, ax.get_ylim()[1]),
                xytext=(0, 6), textcoords="offset points",
                ha="center", color="#E01E37", fontsize=10, fontweight="bold")

    ax.set_title(T(f"{sym}/USDT 清算地图（估算）· 币安永续 · 近{win}",
                   f"{sym}/USDT liquidation map (estimated) - Binance perp"),
                 fontsize=12, fontweight="bold", pad=22)
    # 轴上别出现 1e7 这种科学计数法——他要一眼看出是多少钱
    from matplotlib.ticker import FuncFormatter

    def _axis(v, _pos):
        if font:
            if v >= 1e8:
                return f"{v / 1e8:g}亿"
            return f"{v / 1e4:g}万" if v >= 1e4 else f"{v:g}"
        if v >= 1e6:
            return f"{v / 1e6:g}M"
        return f"{v / 1e3:g}K" if v >= 1e3 else f"{v:g}"
    ax.yaxis.set_major_formatter(FuncFormatter(_axis))
    ax2.yaxis.set_major_formatter(FuncFormatter(_axis))
    ax.set_xlim(m["lo"], m["hi"])
    ax.tick_params(labelsize=9)
    ax2.tick_params(labelsize=8, colors="#7A8794")
    ax.grid(axis="y", color="#EDF0F3", linewidth=.8)
    ax.set_axisbelow(True)
    for sp in ("top", "right", "left"):
        ax.spines[sp].set_visible(False)
        ax2.spines[sp].set_visible(False)
    ax.spines["bottom"].set_color("#D8DEE4")
    ax.legend(loc="upper center", bbox_to_anchor=(.5, -.09), ncol=4,
              frameon=False, fontsize=9)
    # 左下角标死"估算"，图会被单独转发出去，脱离文字说明也不能让人误读
    fig.text(.01, .012, T("模型估算，非交易所数据", "model estimate, not exchange data"),
             fontsize=7.5, color="#9AA5B1")
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf


# ── 文字 ────────────────────────────────────────────────────
def caption(m, sym, win, last):
    up = zones(m, "short")
    dn = zones(m, "long")
    lines = [f"💣 *{sym} 清算地图*（估算）· 币安永续 · 近{win}",
             f"现价 {_px(last)}"]
    if dn:
        lines.append("")
        lines.append("🔻 *下方多头爆仓密集区*（跌下去会连环平多）")
        for z in dn:
            mid = (z["lo"] + z["hi"]) / 2
            lines.append(f"　{_px(z['lo'])}–{_px(z['hi'])}　约 {_money(z['amount'])} U"
                         f"　距现价 {(mid / last - 1) * 100:+.1f}%")
    if up:
        lines.append("")
        lines.append("🔺 *上方空头爆仓密集区*（涨上去会连环平空）")
        for z in up:
            mid = (z["lo"] + z["hi"]) / 2
            lines.append(f"　{_px(z['lo'])}–{_px(z['hi'])}　约 {_money(z['amount'])} U"
                         f"　距现价 {(mid / last - 1) * 100:+.1f}%")
    if not up and not dn:
        lines.append("")
        lines.append("这个窗口里持仓量几乎没增长，估不出密集区。换个更长的窗口试试。")
    lines.append("")
    lines.append("密集区常被当成磁吸位：价格容易被推过去扫一轮再走。")
    lines.append("止损别正好压在柱子上——那正是最容易被插针带走的位置。")
    lines.append("⚠️ 模型估算不是交易所数据，口径点 ℹ️")
    return "\n".join(lines)


def detail(m, sym, win):
    tiers = "、".join(f"{L}x {w * 100:.0f}%" for L, w, _c in LEVS)
    return "\n".join([
        f"ℹ️ *清算地图 · 这张图是怎么来的*", "━━━━━━━━━━━━━━",
        "*先说最重要的一句*",
        "没有任何交易所公布「某个价位挂着多少待爆仓的仓位」——那是每个账户的",
        "私有信息。CoinGlass 那张图也是推算的。**这是模型估算，不是数据。**",
        "",
        "*怎么算的*",
        "1. 拿币安的持仓量历史，看每根 K 线期间持仓量**新增**了多少（减仓不算，",
        "　 那是平仓，不产生新的爆仓位）",
        "2. 把这些新增的仓当成建在那根 K 线的典型价上",
        "3. 永续里每份持仓同时是一多一空，所以同一份仓在两侧都留下爆仓位：",
        "　 多头爆仓价 = 建仓价 × (1 − 1/杠杆)，空头 = 建仓价 × (1 + 1/杠杆)",
        "4. 按杠杆档位分配权重后落进价格桶累加，就是那些柱子",
        "5. 现价**已经越过**的一侧抹掉——那些仓早被平了，留着是假的",
        "",
        "*哪些是假设（这决定了柱子高矮）*",
        f"· 杠杆分布：{tiers}。交易所不公布谁开了几倍，这是拍的",
        "· 建仓价用 K 线典型价，实际是一根里分散成交的",
        "· **没算维持保证金率**（要 API key，公开拿不到），",
        "　 所以估出来的位置比真实爆仓价**略远一点**",
        "",
        "*怎么用*",
        "密集区是**磁吸位**：价格容易被推过去扫一轮再走。",
        "止损别正好压在柱子上——那正是最容易被插针带走的位置。",
        "它说的是「这里有人会被平」，不是「价格会到这里」，别当信号用。",
        "",
        f"扫描窗口：近{win}（{WINDOWS[win][1]} 根 {WINDOWS[win][0]}）",
        f"画的范围：现价 ±{SPAN * 100:.0f}%，分 {BUCKETS} 个价格桶",
        *_long_caveat(win),
        "",
        "⚠️ 估算值，不构成投资建议",
    ])


def _long_caveat(win):
    """90/180 日窗口额外要说的两条。**不说的话这两个窗口会被当成更准的图看**，
    而实际上恰恰相反——窗口越长，这张图越是虚构。"""
    if win not in LONG_WINDOWS:
        return []
    return [
        "",
        f"*近{win}这个窗口要打折看*",
        "· 只有 Bybit 有这么长的持仓量历史，所以这张图**是 Bybit 的**，不是币安",
        "· 颗粒是**一根一天**：整天新增的仓都算在当天典型价上，"
        "比短窗口粗得多",
        "· **最要紧的一条**：这个模型假设那些仓到现在还没平。"
        "永续里几个月不动的仓极少，所以越老的柱子越可能已经不存在了——"
        "长窗口适合看「历史上哪些价位堆过量」，不适合当成当下的爆仓分布",
    ]


# ── 入口 ────────────────────────────────────────────────────
async def _get(sym, win, force=False):
    k = (sym.upper(), win)
    c = _cache.get(k)
    if not force and c and time.time() - c["ts"] < CACHE_TTL:
        return c["data"]
    oi, kl, last, inst = await _fetch(sym, win)
    m = build_map(oi, kl, last)
    data = (m, last, inst)
    _cache[k] = {"ts": time.time(), "data": data}
    return data


def kb(sym, win):
    # 五个窗口一行会挤成一排小方块，点不准。切成两行：短窗口一行、长窗口一行，
    # 顺带把「这两个是另一类」这件事用排版说出来。
    def _b(w):
        return InlineKeyboardButton(f"{'✅' if w == win else ''}{w}",
                                    callback_data=f"lq:w:{sym}:{w}")
    short = [_b(w) for w in WINDOWS if w not in LONG_WINDOWS]
    long_ = [_b(w) for w in WINDOWS if w in LONG_WINDOWS]
    return InlineKeyboardMarkup([
        short,
        long_,
        [InlineKeyboardButton("ℹ️ 口径", callback_data=f"lq:i:{sym}:{win}"),
         InlineKeyboardButton("🔄 重算", callback_data=f"lq:r:{sym}:{win}"),
         InlineKeyboardButton("🪙 换币", callback_data="lq:pick:-:-")],
    ])


async def _send(message, sym, win, force=False):
    m, last, inst = await _get(sym, win, force)
    buf = render(m, sym, win, last)
    cap = caption(m, sym, win, last)
    try:
        await message.reply_photo(photo=buf, caption=cap, parse_mode="Markdown",
                                  reply_markup=kb(sym, win))
    except Exception as e:
        log.warning(f"清算地图发图失败，降级纯文本: {e}")
        buf.seek(0)
        await message.reply_photo(photo=buf, caption=cap.replace("*", ""),
                                  reply_markup=kb(sym, win))


async def liqmap_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/liqmap TRUMP [1日|7日|30日|90日|180日] —— 清算地图（估算各价位待强平仓位）。"""
    args = context.args or []
    if not args:
        await safe_reply(update.message,
            "💣 *清算地图*（估算）\n\n"
            "`/liqmap TRUMP`　　默认近 7 日\n"
            "`/liqmap BTC 30日`　换窗口（1日 / 7日 / 30日）\n\n"
            "画的是**各个价位上估计堆了多少待强平的仓位**。\n"
            "没有交易所公布这个数（那是私有信息），CoinGlass 那张也是推算的——"
            "所以这是模型估算，口径在图下面的 ℹ️ 里写清楚了。\n\n"
            "菜单：📈 分析与图表 → 💣 清算地图", parse_mode="Markdown")
        return
    sym = args[0].upper()
    win = next((a for a in args[1:] if a in WINDOWS), DEFAULT_WIN)
    uid = update.effective_user.id
    async with busy.guard(uid, "liqmap") as ok:
        if not ok:
            await safe_reply(update.message, busy.busy_text(uid, "liqmap", "清算地图"))
            return
        await safe_reply(update.message, f"💣 算 {sym} 近{win}的清算地图…")
        try:
            await _send(update.message, sym, win)
        except RuntimeError as e:
            await safe_reply(update.message, f"❌ {e}")
        except Exception as e:
            log.error(f"清算地图出错 {sym}: {e}")
            await safe_reply(update.message, f"画不出来：{str(e)[:100]}")


async def from_btn(query, context, action, sym, win):
    if action == "pick":
        from handlers.menu import coin_grid
        await safe_edit(query, "💣 *清算地图*　点一个币（或查其他币）",
                        reply_markup=coin_grid("lqcoin", "cat_analysis"),
                        parse_mode="Markdown")
        return
    if action == "i":
        try:
            m, _last, _inst = await _get(sym, win)
        except Exception as e:
            await query.answer(f"取数失败：{str(e)[:60]}", show_alert=True)
            return
        await query.answer()
        await safe_reply(query.message, detail(m, sym, win), parse_mode="Markdown")
        return
    uid = query.from_user.id
    async with busy.guard(uid, "liqmap") as ok:
        if not ok:
            await query.answer(f"上一张还在算（已 {busy.elapsed(uid, 'liqmap')} 秒）",
                               show_alert=True)
            return
        await query.answer("重新算…" if action == "r" else f"切到近{win}…")
        try:
            # 图片消息改不了图，只能发新的一条
            await _send(query.message, sym, win, force=(action == "r"))
        except RuntimeError as e:
            await safe_reply(query.message, f"❌ {e}")
        except Exception as e:
            log.error(f"清算地图按钮出错 {sym}: {e}")
            await safe_reply(query.message, f"画不出来：{str(e)[:100]}")


async def on_coin(message, context, sym):
    """点了「查其他币」之后发来的币名。"""
    uid = message.from_user.id
    async with busy.guard(uid, "liqmap") as ok:
        if not ok:
            await safe_reply(message, busy.busy_text(uid, "liqmap", "清算地图"))
            return
        try:
            await _send(message, sym.upper(), DEFAULT_WIN)
        except RuntimeError as e:
            await safe_reply(message, f"❌ {e}")
        except Exception as e:
            log.error(f"清算地图出错 {sym}: {e}")
            await safe_reply(message, f"画不出来：{str(e)[:100]}")
