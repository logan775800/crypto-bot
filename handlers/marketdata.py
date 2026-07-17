"""Bybit V5 公开行情数据 + 服务端指标计算：给 AI 做多周期量化分析用。

全部走公开接口（无需密钥）。设计原则：**不把几百根原始K线丢给模型**——那样既烧
token 又让模型自己算得不准。这里在服务端算好 EMA/ATR/RSI/结构/VWAP/量能，
只把「结论 + 少量近期K线」以紧凑文本返回，模型据此做结构判断与执行方案。

覆盖：K线多周期、OI 历史、资金费历史+预测+基差、订单簿失衡、逐笔主动买卖、
市场联动(BTC/ETH)、清算(复用OKX源)。
"""
import logging
import httpx

BYBIT = "https://api.bybit.com"
CAT = "linear"          # USDT 永续

log = logging.getLogger(__name__)

# 用户/模型可能用的写法 → Bybit interval
INTERVALS = {
    "1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30",
    "1h": "60", "60": "60", "2h": "120", "4h": "240", "240": "240",
    "6h": "360", "12h": "720", "1d": "D", "d": "D", "1w": "W",
}
# 各周期默认取多少根（5m/15m 要够长做结构，日线不用太多）
DEFAULT_LIMIT = {"1": 400, "3": 400, "5": 400, "15": 300, "30": 300,
                 "60": 250, "120": 200, "240": 200, "360": 200, "720": 200,
                 "D": 200, "W": 100}
OI_INTERVALS = {"5m": "5min", "15m": "15min", "30m": "30min",
                "1h": "1h", "4h": "4h", "1d": "1d"}


def norm(sym):
    """BTC / btc / BTCUSDT → BTCUSDT"""
    s = (sym or "").upper().strip().replace("-", "").replace("/", "")
    return s if s.endswith("USDT") else s + "USDT"


async def _get(path, params):
    async with httpx.AsyncClient(timeout=12) as c:
        r = await c.get(BYBIT + path, params=params)
        r.raise_for_status()
        d = r.json()
    if d.get("retCode") != 0:
        raise RuntimeError(f"Bybit: {d.get('retMsg')}")
    return d.get("result") or {}


def f(x, digits=None):
    """自适应精度显示。"""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return str(x)
    if digits is not None:
        return f"{x:,.{digits}f}"
    a = abs(x)
    if a >= 1000:
        return f"{x:,.0f}"
    if a >= 1:
        return f"{x:,.2f}"
    if a >= 0.01:
        return f"{x:.4f}"
    return f"{x:.8f}".rstrip("0").rstrip(".")


# ── 指标 ────────────────────────────────────────────────────────────
def ema(vals, n):
    if len(vals) < n:
        return None
    k = 2 / (n + 1)
    e = sum(vals[:n]) / n
    for v in vals[n:]:
        e = v * k + e * (1 - k)
    return e


def atr(highs, lows, closes, n=14):
    if len(closes) < n + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    a = sum(trs[:n]) / n                      # Wilder 平滑
    for t in trs[n:]:
        a = (a * (n - 1) + t) / n
    return a


def rsi(closes, n=14):
    if len(closes) < n + 1:
        return None
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0); losses += max(-d, 0)
    ag, al = gains / n, losses / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    if al == 0:
        return 100.0
    rs = ag / al
    return 100 - 100 / (1 + rs)


def pivots(highs, lows, k=3):
    """简易摆动点：左右各 k 根都不高/不低于它 → 摆动高/低。返回(高点list, 低点list)，新→旧。"""
    hs, ls = [], []
    for i in range(k, len(highs) - k):
        if all(highs[i] >= highs[i - j] for j in range(1, k + 1)) and \
           all(highs[i] >= highs[i + j] for j in range(1, k + 1)):
            hs.append((i, highs[i]))
        if all(lows[i] <= lows[i - j] for j in range(1, k + 1)) and \
           all(lows[i] <= lows[i + j] for j in range(1, k + 1)):
            ls.append((i, lows[i]))
    return hs[::-1], ls[::-1]


def structure(highs, lows):
    """用最近3个摆动高/低判断结构：上升(HH/HL) / 下降(LH/LL) / 震荡。"""
    hs, ls = pivots(highs, lows)
    h3 = [round(v, 8) for _, v in hs[:3]]
    l3 = [round(v, 8) for _, v in ls[:3]]
    tag = "震荡/不明确"
    if len(h3) >= 2 and len(l3) >= 2:
        hh = h3[0] > h3[1]
        hl = l3[0] > l3[1]
        lh = h3[0] < h3[1]
        ll = l3[0] < l3[1]
        if hh and hl:
            tag = "上升结构(HH+HL)"
        elif lh and ll:
            tag = "下降结构(LH+LL)"
        elif hh and ll:
            tag = "扩张/震荡放大"
        elif lh and hl:
            tag = "收敛/三角"
    return tag, h3, l3


# ── 1) K线 + 量能 + 指标 ─────────────────────────────────────────────
async def klines_analysis(symbol, interval="15m", limit=None):
    sym = norm(symbol)
    iv = INTERVALS.get(str(interval).lower(), str(interval))
    lim = int(limit or DEFAULT_LIMIT.get(iv, 300))
    lim = max(60, min(lim, 1000))
    r = await _get("/v5/market/kline",
                   {"category": CAT, "symbol": sym, "interval": iv, "limit": lim})
    rows = r.get("list") or []
    if not rows:
        return f"{sym} {interval}: 无K线数据（币种或周期不对？）"
    rows = rows[::-1]      # Bybit 返回新→旧，反成 旧→新
    o = [float(x[1]) for x in rows]
    h = [float(x[2]) for x in rows]
    lo = [float(x[3]) for x in rows]
    c = [float(x[4]) for x in rows]
    v = [float(x[5]) for x in rows]
    tn = [float(x[6]) for x in rows]        # 成交额(USDT)

    last = c[-1]
    e20, e50, e200 = ema(c, 20), ema(c, 50), ema(c, 200)
    a14 = atr(h, lo, c, 14)
    r14 = rsi(c, 14)
    tag, h3, l3 = structure(h, lo)

    # 均线排列
    arr = "数据不足"
    if e20 and e50 and e200:
        if last > e20 > e50 > e200:
            arr = "多头排列(价>20>50>200)"
        elif last < e20 < e50 < e200:
            arr = "空头排列(价<20<50<200)"
        else:
            arr = f"缠绕/过渡(价{'>' if last>e20 else '<'}EMA20)"
    # EMA20 斜率（最近10根变化率）
    slope = ""
    if e20 and len(c) > 30:
        e20_prev = ema(c[:-10], 20)
        if e20_prev:
            slope = f"，EMA20近10根斜率 {(e20-e20_prev)/e20_prev*100:+.2f}%"
    # 量能：最近5根均量 vs 前20根均量
    vol_txt = ""
    if len(v) >= 25:
        recent = sum(v[-5:]) / 5
        base = sum(v[-25:-5]) / 20
        if base > 0:
            ratio = recent / base
            vol_txt = f"{ratio:.2f}x（{'放量' if ratio>1.3 else '缩量' if ratio<0.7 else '平量'}）"
    # 区间VWAP
    tp_v = sum(((h[i] + lo[i] + c[i]) / 3) * v[i] for i in range(len(c)))
    vsum = sum(v)
    vwap = tp_v / vsum if vsum else None
    hi, lowv = max(h), min(lo)
    # 近50根前高前低
    n50 = min(50, len(c))
    ph, pl = max(h[-n50:]), min(lo[-n50:])

    lines = [
        f"【{sym} {interval}】共{len(c)}根",
        f"现价 {f(last)}｜区间 {f(lowv)}~{f(hi)}｜近{n50}根前高/前低 {f(ph)}/{f(pl)}",
        f"EMA20 {f(e20)}／EMA50 {f(e50)}／EMA200 {f(e200)} → {arr}{slope}",
        f"ATR14 {f(a14)}" + (f"（{a14/last*100:.2f}%，1.5×ATR≈{f(a14*1.5)}，可作止损距离参考）" if a14 and last else ""),
        f"RSI14 {r14:.1f}" if r14 is not None else "RSI14 -",
        f"结构 {tag}｜近摆动高 {'/'.join(f(x) for x in h3) or '-'}｜近摆动低 {'/'.join(f(x) for x in l3) or '-'}",
        f"量能 最近5根/前20根均量 {vol_txt}" if vol_txt else "",
        f"区间VWAP {f(vwap)}（价在VWAP{'上' if vwap and last>vwap else '下'}）" if vwap else "",
        "近8根 O/H/L/C/量：",
    ]
    for i in range(max(0, len(c) - 8), len(c)):
        lines.append(f"  {f(o[i])}/{f(h[i])}/{f(lo[i])}/{f(c[i])}/{v[i]:,.0f}")
    return "\n".join(x for x in lines if x)


# ── 2) OI 历史（配合价格变化判断谁在推动）───────────────────────────
async def oi_analysis(symbol, interval="15m"):
    sym = norm(symbol)
    oiv = OI_INTERVALS.get(str(interval).lower(), "15min")
    r = await _get("/v5/market/open-interest",
                   {"category": CAT, "symbol": sym, "intervalTime": oiv, "limit": 50})
    rows = (r.get("list") or [])[::-1]     # 新→旧 反成 旧→新
    if len(rows) < 5:
        return f"{sym}: 无足够 OI 历史"
    ois = [float(x["openInterest"]) for x in rows]
    # 同周期价格
    iv = INTERVALS.get(str(interval).lower(), "15")
    k = await _get("/v5/market/kline",
                   {"category": CAT, "symbol": sym, "interval": iv, "limit": len(rows)})
    kr = (k.get("list") or [])[::-1]
    closes = [float(x[4]) for x in kr] if kr else []

    def pct(a, b):
        return (b - a) / a * 100 if a else 0

    out = [f"【{sym} OI历史 {interval}】最近{len(ois)}期，当前OI {ois[-1]:,.0f}"]
    for span, label in ((3, "近3期"), (12, "近12期"), (len(ois) - 1, "全窗")):
        if len(ois) > span and span > 0:
            d_oi = pct(ois[-1 - span], ois[-1])
            d_px = pct(closes[-1 - span], closes[-1]) if len(closes) > span else None
            if d_px is None:
                out.append(f"{label}: OI {d_oi:+.2f}%")
                continue
            # 价 vs OI 四象限解读
            if d_px > 0 and d_oi > 0:
                judge = "价涨+OI涨 → 新多进场，趋势延续概率高，但防多头拥挤"
            elif d_px > 0 and d_oi < 0:
                judge = "价涨+OI跌 → 空头回补推动，追多谨慎（缺新增买盘）"
            elif d_px < 0 and d_oi > 0:
                judge = "价跌+OI涨 → 新空堆积，可能延续下跌，也可能酝酿空挤"
            else:
                judge = "价跌+OI跌 → 多头平仓/清算，情绪释放，防反弹"
            out.append(f"{label}: 价 {d_px:+.2f}% ／ OI {d_oi:+.2f}% → {judge}")
    return "\n".join(out)


# ── 3) 资金费率历史 + 预测 + 基差 ────────────────────────────────────
async def funding_analysis(symbol):
    sym = norm(symbol)
    t = await _get("/v5/market/tickers", {"category": CAT, "symbol": sym})
    tk = (t.get("list") or [{}])[0]
    cur = float(tk.get("fundingRate") or 0) * 100
    mark = float(tk.get("markPrice") or 0)
    idx = float(tk.get("indexPrice") or 0)
    basis = (mark - idx) / idx * 100 if idx else 0
    h = await _get("/v5/market/funding/history",
                   {"category": CAT, "symbol": sym, "limit": 60})
    rows = (h.get("list") or [])[::-1]
    rates = [float(x["fundingRate"]) * 100 for x in rows]
    out = [f"【{sym} 资金费率】当前(下一期预测) {cur:+.4f}%／期",
           f"标记价 {f(mark)}｜指数价 {f(idx)}｜基差 {basis:+.3f}%"
           + ("（永续溢价，多头付费）" if basis > 0 else "（永续折价，空头付费）")]
    if rates:
        n = len(rates)
        avg = sum(rates) / n
        mx, mn = max(rates), min(rates)
        pos = sum(1 for r in rates if r > 0)
        recent = rates[-3:]
        out.append(f"历史{n}期(约{n*8}h): 均值 {avg:+.4f}%｜区间 {mn:+.4f}%~{mx:+.4f}%｜正费率占比 {pos/n*100:.0f}%")
        out.append(f"最近3期: {'、'.join(f'{r:+.4f}%' for r in recent)}")
        # 极端判断
        if cur >= mx * 0.9 and cur > 0:
            out.append("⚠️ 当前费率接近该窗口历史高位 → 多头拥挤，挤多风险上升")
        elif cur <= mn * 0.9 and cur < 0:
            out.append("⚠️ 当前费率接近历史低位(负值) → 空头拥挤，轧空风险上升")
        else:
            out.append("当前费率处于该窗口常态区间，非极端")
    return "\n".join(out)


# ── 4) 订单簿失衡 + 挂单墙 ───────────────────────────────────────────
async def orderbook_analysis(symbol, depth=200):
    sym = norm(symbol)
    d = max(1, min(int(depth or 200), 200))
    r = await _get("/v5/market/orderbook", {"category": CAT, "symbol": sym, "limit": d})
    bids = [(float(p), float(s)) for p, s in (r.get("b") or [])]
    asks = [(float(p), float(s)) for p, s in (r.get("a") or [])]
    if not bids or not asks:
        return f"{sym}: 无订单簿数据"
    best_b, best_a = bids[0][0], asks[0][0]
    mid = (best_b + best_a) / 2
    spread = (best_a - best_b) / mid * 100
    bv = sum(s for _, s in bids)
    av = sum(s for _, s in asks)
    imb = (bv - av) / (bv + av) * 100 if (bv + av) else 0
    # 挂单墙：单档量 > 该侧均量5倍
    def walls(side, n=3):
        if not side:
            return []
        avg = sum(s for _, s in side) / len(side)
        w = [(p, s) for p, s in side if s > avg * 5]
        w.sort(key=lambda x: -x[1])
        return w[:n]
    wb, wa = walls(bids), walls(asks)
    out = [f"【{sym} 订单簿 前{len(bids)}档】",
           f"买一 {f(best_b)}｜卖一 {f(best_a)}｜中价 {f(mid)}｜价差 {spread:.4f}%",
           f"买盘总量 {bv:,.0f}｜卖盘总量 {av:,.0f}｜失衡 {imb:+.1f}% "
           + ("（买盘占优，承接强）" if imb > 15 else "（卖压占优）" if imb < -15 else "（大致均衡）")]
    if wb:
        out.append("买墙: " + "、".join(f"{f(p)}×{s:,.0f}" for p, s in wb))
    if wa:
        out.append("卖墙: " + "、".join(f"{f(p)}×{s:,.0f}" for p, s in wa))
    if not wb and not wa:
        out.append("无明显挂单墙（无单档>5倍均量）")
    return "\n".join(out)


# ── 5) 逐笔成交：主动买卖盘 + 大单 ───────────────────────────────────
async def trades_analysis(symbol, limit=500):
    sym = norm(symbol)
    n = max(50, min(int(limit or 500), 1000))
    r = await _get("/v5/market/recent-trade", {"category": CAT, "symbol": sym, "limit": n})
    rows = r.get("list") or []
    if not rows:
        return f"{sym}: 无逐笔成交"
    buy_v = sum(float(x["size"]) for x in rows if x.get("side") == "Buy")
    sell_v = sum(float(x["size"]) for x in rows if x.get("side") == "Sell")
    tot = buy_v + sell_v
    delta = (buy_v - sell_v) / tot * 100 if tot else 0
    sizes = [float(x["size"]) for x in rows]
    avg = sum(sizes) / len(sizes)
    big = [x for x in rows if float(x["size"]) > avg * 10]
    big_b = sum(float(x["size"]) for x in big if x.get("side") == "Buy")
    big_s = sum(float(x["size"]) for x in big if x.get("side") == "Sell")
    out = [f"【{sym} 逐笔成交 最近{len(rows)}笔】",
           f"主动买 {buy_v:,.0f}｜主动卖 {sell_v:,.0f}｜净delta {delta:+.1f}% "
           + ("（买方主动占优）" if delta > 10 else "（卖方主动占优）" if delta < -10 else "（多空拉锯）"),
           f"大单(>10×均笔) {len(big)} 笔：买 {big_b:,.0f} ／ 卖 {big_s:,.0f} "
           + ("→ 大单偏买" if big_b > big_s * 1.2 else "→ 大单偏卖" if big_s > big_b * 1.2 else "→ 大单均衡")]
    if rows:
        out.append(f"最新成交价 {f(rows[0]['price'])}")
    return "\n".join(out)


# ── 6) 市场联动：BTC/ETH 多周期 + 情绪 ───────────────────────────────
async def market_context():
    out = ["【市场联动 BTC/ETH】"]
    for sym in ("BTCUSDT", "ETHUSDT"):
        parts = []
        for iv in ("15m", "1h", "4h"):
            try:
                r = await _get("/v5/market/kline",
                               {"category": CAT, "symbol": sym, "interval": INTERVALS[iv], "limit": 60})
                rows = (r.get("list") or [])[::-1]
                c = [float(x[4]) for x in rows]
                if len(c) < 21:
                    continue
                e20 = ema(c, 20)
                chg = (c[-1] - c[-21]) / c[-21] * 100
                parts.append(f"{iv} {chg:+.2f}%/{'价上EMA20' if c[-1] > e20 else '价下EMA20'}")
            except Exception:
                continue
        try:
            t = await _get("/v5/market/tickers", {"category": CAT, "symbol": sym})
            tk = (t.get("list") or [{}])[0]
            px = f(tk.get("lastPrice"))
            fr = float(tk.get("fundingRate") or 0) * 100
            parts.append(f"费率{fr:+.4f}%")
        except Exception:
            px = "?"
        out.append(f"{sym[:-4]} {px}：" + "｜".join(parts))
    try:
        from api import get_fear_greed
        fg = await get_fear_greed()
        out.append(f"恐惧贪婪指数 {fg['value']}/100（{fg['classification']}）")
    except Exception:
        pass
    out.append("提示：山寨多头计划若遇 BTC 在 15m/1h 快速破位，应降仓或取消。")
    return "\n".join(out)


# ── 7) 清算数据（Bybit 无公开清算REST，复用 OKX 源）──────────────────
async def liquidation_analysis(symbol):
    try:
        from handlers.okx import build_liq_text
        return await build_liq_text(norm(symbol).replace("USDT", "")) + "\n(来源: OKX 聚合，仅作挤压空间参考，不可单独作为开仓依据)"
    except Exception as e:
        return f"清算数据获取失败：{str(e)[:80]}"
