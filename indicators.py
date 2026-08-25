"""技术指标计算（纯Python实现）"""

def sma(prices, period):
    """简单移动平均"""
    if len(prices) < period:
        return None
    return sum(prices[-period:]) / period

def rsi(prices, period=14):
    """相对强弱指数 RSI —— Wilder 平滑法（与 TradingView/交易所口径一致）。

    首个均值取前 period 根的简单平均，其后每根用 Wilder 递推平滑；
    传入的 K 线越多，收敛越准（本项目查价时喂 ~120 根日/时线）。"""
    if len(prices) < period + 1:
        return None
    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]
    # 初始平均涨/跌：前 period 根的简单平均
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    # Wilder 递推平滑其余各根
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def analyze(prices):
    """综合分析，返回各指标和信号"""
    result = {}

    # 当前价
    cur = prices[-1]
    result["price"] = cur

    # RSI
    r = rsi(prices, 14)
    result["rsi"] = r
    if r is not None:
        if r >= 70:
            result["rsi_signal"] = "超买 ⚠️ (可能回调)"
        elif r <= 30:
            result["rsi_signal"] = "超卖 💡 (可能反弹)"
        else:
            result["rsi_signal"] = "中性"

    # 均线口径全局统一成 MA3/13/23（handlers/annotchart.MA_PERIODS）——
    # 这里以前是 MA7/MA30，于是同一个币：图上按 MA3/13/23 画、/scan 按 MA3/13/23 判，
    # 而 /analyze 和 /ai 按 MA7/MA30 说话，三者能给出不同的"多头/空头"。
    # 函数内导入：annotchart 会拉起 telegram/marketdata 那条链，模块级导入太重。
    from handlers.annotchart import MA_PERIODS, _ma_series, ma_align
    p1, p2, p3 = MA_PERIODS
    result["ma_periods"] = MA_PERIODS

    def _last(n):
        ser = _ma_series(prices, n)
        return ser[-1] if ser and ser[-1] is not None else None

    m1, m2, m3 = _last(p1), _last(p2), _last(p3)
    result["ma_fast"], result["ma_mid"], result["ma_slow"] = m1, m2, m3

    # 排列判定复用 ma_align：光比大小不够，三根粘一起是缠绕不是顺势（见 annotchart）
    align = ma_align(prices)
    if align > 0:
        result["ma_signal"] = f"MA{p1}>MA{p2}>MA{p3} 多头排列 📈"
    elif align < 0:
        result["ma_signal"] = f"MA{p1}<MA{p2}<MA{p3} 空头排列 📉"
    elif m1 and m3:
        result["ma_signal"] = "均线缠绕，方向未定 ⏸"

    # 价格相对生命线（MA23）——短均线贴着价格走，用它判"站上/跌破"没有意义
    if m3:
        result["price_signal"] = f"价格在 MA{p3} 之{'上' if cur > m3 else '下'}"

    return result


def ema(prices, period):
    """指数移动平均"""
    if len(prices) < period:
        return None
    k = 2 / (period + 1)
    ema_val = sum(prices[:period]) / period  # 初始用SMA
    for price in prices[period:]:
        ema_val = price * k + ema_val * (1 - k)
    return ema_val

def macd(prices):
    """MACD：返回 (macd线, 信号判断)"""
    if len(prices) < 26:
        return None, None
    ema12 = ema(prices, 12)
    ema26 = ema(prices, 26)
    if ema12 is None or ema26 is None:
        return None, None
    macd_line = ema12 - ema26
    signal = "多头 📈 (DIF>0)" if macd_line > 0 else "空头 📉 (DIF<0)"
    return macd_line, signal

def bollinger(prices, period=20):
    """布林带：返回 (上轨, 中轨, 下轨, 位置判断)"""
    if len(prices) < period:
        return None
    recent = prices[-period:]
    mid = sum(recent) / period
    variance = sum((p - mid) ** 2 for p in recent) / period
    std = variance ** 0.5
    upper = mid + 2 * std
    lower = mid - 2 * std
    cur = prices[-1]
    if cur >= upper:
        pos = "触及上轨 ⚠️ (偏高)"
    elif cur <= lower:
        pos = "触及下轨 💡 (偏低)"
    else:
        pos = "区间内"
    return {"upper": upper, "mid": mid, "lower": lower, "pos": pos}

def support_resistance(prices):
    """简单支撑阻力：用近期最高最低"""
    if len(prices) < 10:
        return None
    recent = prices[-30:] if len(prices) >= 30 else prices
    return {"resistance": max(recent), "support": min(recent)}


def kdj(highs, lows, closes, period=9):
    """KDJ指标：返回 (K, D, J, 信号)"""
    if len(closes) < period:
        return None
    # 计算最近period的RSV
    rsv_list = []
    for i in range(period-1, len(closes)):
        window_high = max(highs[i-period+1:i+1])
        window_low = min(lows[i-period+1:i+1])
        if window_high == window_low:
            rsv = 50
        else:
            rsv = (closes[i] - window_low) / (window_high - window_low) * 100
        rsv_list.append(rsv)
    # K、D平滑
    k = 50
    d = 50
    for rsv in rsv_list:
        k = 2/3 * k + 1/3 * rsv
        d = 2/3 * d + 1/3 * k
    j = 3 * k - 2 * d
    # 信号
    if k > 80 or j > 100:
        signal = "超买 ⚠️"
    elif k < 20 or j < 0:
        signal = "超卖 💡"
    elif k > d:
        signal = "金叉偏多 📈"
    else:
        signal = "死叉偏空 📉"
    return {"k": k, "d": d, "j": j, "signal": signal}

def volume_analysis(volumes):
    """成交量分析：最近vs平均"""
    if len(volumes) < 7:
        return None
    recent = volumes[-1]
    avg = sum(volumes[-7:]) / 7
    ratio = recent / avg if avg else 1
    if ratio >= 1.5:
        signal = "放量 📊 (活跃)"
    elif ratio <= 0.6:
        signal = "缩量 💤 (清淡)"
    else:
        signal = "量能正常"
    return {"recent": recent, "avg": avg, "ratio": ratio, "signal": signal}


def _ema_series(prices, period):
    """EMA 序列（与 prices 等长），用于 MACD。"""
    k = 2 / (period + 1)
    ema_val = prices[0]
    out = [ema_val]
    for p in prices[1:]:
        ema_val = p * k + ema_val * (1 - k)
        out.append(ema_val)
    return out


def macd_hist(prices):
    """完整 MACD：返回 {dif, dea, hist, hist_prev}。数据不足返回 None。
    hist>0 = 红柱(多头动能)，hist<0 = 绿柱(空头动能)；hist 绝对值较上一根变大=走强。"""
    if len(prices) < 35:
        return None
    ema12 = _ema_series(prices, 12)
    ema26 = _ema_series(prices, 26)
    dif = [ema12[i] - ema26[i] for i in range(len(prices))]
    dea = _ema_series(dif, 9)
    hist = dif[-1] - dea[-1]
    hist_prev = dif[-2] - dea[-2]
    return {"dif": dif[-1], "dea": dea[-1], "hist": hist, "hist_prev": hist_prev}


def dmi(highs, lows, closes, period=14):
    """ADX + 方向指标 DI，返回 {adx, pdi, mdi}；数据不足返回 None。

    **为什么要单独把 +DI/-DI 拿出来**：ADX 只测「趋势有多强」，不带方向。
    一段强下跌里的逆势反抽同样会把 ADX 顶得很高，只看 ADX 就会把
    **最容易被套的那种反抽**评成「买入·强」。方向要靠 DI 判：
    pdi > mdi 才是多头占优。

    `adx()` 是本函数的薄封装（只取 adx 一个值），两者口径必然一致。
    """
    n = len(closes)
    if n < period * 2 + 1 or len(highs) != n or len(lows) != n:
        return None
    trs, plus_dm, minus_dm = [], [], []
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
        trs.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))

    def _wilder(vals):
        # Wilder 平滑：首值取前 period 之和，其后逐根衰减累加
        s = [sum(vals[:period])]
        for v in vals[period:]:
            s.append(s[-1] - s[-1] / period + v)
        return s

    atr = _wilder(trs)
    pdm = _wilder(plus_dm)
    mdm = _wilder(minus_dm)
    dxs = []
    last_pdi = last_mdi = None
    for i in range(len(atr)):
        if atr[i] == 0:
            continue
        pdi = 100 * pdm[i] / atr[i]
        mdi = 100 * mdm[i] / atr[i]
        last_pdi, last_mdi = pdi, mdi
        denom = pdi + mdi
        dxs.append(100 * abs(pdi - mdi) / denom if denom else 0.0)
    if not dxs or last_pdi is None:
        return None
    if len(dxs) < period:
        adx_val = sum(dxs) / len(dxs)
    else:
        adx_val = sum(dxs[:period]) / period
        for dx in dxs[period:]:
            adx_val = (adx_val * (period - 1) + dx) / period
    return {"adx": adx_val, "pdi": last_pdi, "mdi": last_mdi}


def adx(highs, lows, closes, period=14):
    """平均趋向指数 ADX：衡量趋势强度（不分方向）。
    <20 无趋势/震荡，20-25 趋势萌芽，>25 趋势明确，>40 趋势强。数据不足返回 None。

    只要强度不要方向时用它；要判方向请用 `dmi()`（同一套计算，多返回 +DI/-DI）。"""
    d = dmi(highs, lows, closes, period)
    return d["adx"] if d else None


# ── 序列版指标：背离/衰竭这类要看「历史形态」的判据必须拿到整条序列 ──────────

def rsi_series(prices, period=14):
    """RSI 序列，与 prices 等长，前 period 根为 None（还没有值）。

    口径和 `rsi()` 完全一致（Wilder 平滑），只是把每一步都留下来——
    背离要比较「最近两个 RSI 峰」，只有末值是做不到的。"""
    n = len(prices)
    out = [None] * n
    if n < period + 1:
        return out
    deltas = [prices[i] - prices[i - 1] for i in range(1, n)]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def _val(g, l):
        if l == 0:
            return 100.0
        return 100 - (100 / (1 + g / l))

    out[period] = _val(avg_gain, avg_loss)
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i + 1] = _val(avg_gain, avg_loss)
    return out


def macd_hist_series(prices):
    """MACD 柱序列，与 prices 等长。数据不足返回全 None 的等长列表。

    ⚠️ 前 ~35 根是 EMA 暖机区，柱值失真——判背离时要跳过（见 `early_reversal`）。"""
    n = len(prices)
    if n < 35:
        return [None] * n
    ema12 = _ema_series(prices, 12)
    ema26 = _ema_series(prices, 26)
    dif = [ema12[i] - ema26[i] for i in range(n)]
    dea = _ema_series(dif, 9)
    return [dif[i] - dea[i] for i in range(n)]


def pivot_highs(values, k=2):
    """摆动高点的下标：左右各 k 根都严格更低才算一个峰。

    需要右侧 k 根来确认，所以**最后 k 根永远不会被认定为峰**——
    这不是缺陷，是「峰」这个概念本身要求的确认成本。"""
    idx = []
    for i in range(k, len(values) - k):
        if values[i] is None:
            continue
        win = values[i - k:i + k + 1]
        if any(v is None for v in win):
            continue
        if all(values[j] < values[i] for j in range(i - k, i + k + 1) if j != i):
            idx.append(i)
    return idx


def pivot_lows(values, k=2):
    """摆动低点的下标：左右各 k 根都严格更高才算一个谷。"""
    idx = []
    for i in range(k, len(values) - k):
        if values[i] is None:
            continue
        win = values[i - k:i + k + 1]
        if any(v is None for v in win):
            continue
        if all(values[j] > values[i] for j in range(i - k, i + k + 1) if j != i):
            idx.append(i)
    return idx


def rsi_divergence(closes, period=14):
    """价格与 RSI 的背离（只认极端区）。返回 {bearish, bullish, text}，无背离时全为空。

      顶背离：RSI 最近两个峰「后一个更低」而价格「后一个更高」→ 上涨动能衰竭
      底背离：RSI 最近两个谷「后一个更高」而价格「后一个更低」→ 下跌动能衰竭

    **两个峰必须都在超买区(≥70)、两个谷都在超卖区(≤30)**。
    RSI 在 30~70 中间区的「背离」绝大多数是噪音——不卡这一条会天天报背离，
    而天天报的东西等于没报。另加间距/近期/幅度约束继续压误报。
    """
    K, MIN_GAP, MAX_GAP, RECENT, MARGIN = 2, 4, 60, 25, 2.0
    n = len(closes)
    if n < 2 * K + MIN_GAP + 16:
        return {"bearish": False, "bullish": False, "text": ""}
    rs = rsi_series(closes, period)
    last_confirmable = n - 1 - K   # 峰谷要右侧 K 根确认，最新可确认的下标在这

    ph = pivot_highs(rs, K)
    if len(ph) >= 2:
        p1, p2 = ph[-2], ph[-1]
        gap = p2 - p1
        if (p2 >= last_confirmable - RECENT and MIN_GAP <= gap <= MAX_GAP
                and rs[p1] >= 70 and rs[p2] >= 70
                and rs[p2] < rs[p1] - MARGIN and closes[p2] > closes[p1]):
            return {"bearish": True, "bullish": False,
                    "text": "顶背离：超买区 RSI 高点走低但价格更高，上涨动能衰竭，警惕见顶回落"}

    pl = pivot_lows(rs, K)
    if len(pl) >= 2:
        p1, p2 = pl[-2], pl[-1]
        gap = p2 - p1
        if (p2 >= last_confirmable - RECENT and MIN_GAP <= gap <= MAX_GAP
                and rs[p1] <= 30 and rs[p2] <= 30
                and rs[p2] > rs[p1] + MARGIN and closes[p2] < closes[p1]):
            return {"bearish": False, "bullish": True,
                    "text": "底背离：超卖区 RSI 低点抬高但价格更低，下跌动能衰竭，关注止跌反弹"}

    return {"bearish": False, "bullish": False, "text": ""}


def volume_poc(highs, lows, volumes, bins=50):
    """量能中枢 POC：成交量最集中的那个价位。数据不足或全无成交量返回 None。

    做法是把价格区间切成 bins 个桶，每根 K 线的成交量**均摊到它覆盖的所有桶**
    （不是只记收盘价那个桶——一根从 100 涨到 110 的 K 线，这段成交是分布在整段上的）。
    POC 是多空换手最密集的位置，既是磁吸位也是最容易反复纠缠的地方。"""
    n = len(highs)
    if n < 2 or len(lows) != n or len(volumes) != n:
        return None
    lo_p, hi_p = min(lows), max(highs)
    if hi_p <= lo_p:
        return None
    width = (hi_p - lo_p) / bins
    buckets = [0.0] * bins
    seen = False
    for i in range(n):
        if volumes[i] <= 0:
            continue
        seen = True
        lo = max(0, min(bins - 1, int((lows[i] - lo_p) / width)))
        hi = max(0, min(bins - 1, int((highs[i] - lo_p) / width)))
        if hi < lo:
            lo, hi = hi, lo
        per = volumes[i] / (hi - lo + 1)
        for b in range(lo, hi + 1):
            buckets[b] += per
    if not seen:
        return None
    best = max(range(bins), key=lambda b: buckets[b])
    return lo_p + (best + 0.5) * width


# 早期反转预警的常量（含义见 early_reversal 的 docstring）
_REV_MIN_CANDLES = 40
_REV_CONFLUENCE = 2
_REV_RECENT_PIVOT = 20
_REV_MAX_GAP = 60
_REV_WARMUP = 34      # MACD 的 EMA 暖机区，之前的柱值失真，不能拿来判背离


def _intraday_reversal_dir(closes_4h):
    """4h RSI 是不是正在从超买回落 / 从超卖回升。-1=见顶 1=见底 0=没有。

    它比日线**领先约一天**——日线要等收盘，4h 一天有六根。"""
    if len(closes_4h) < 30:
        return 0
    rs = rsi_series(closes_4h, 14)
    m = len(rs)
    if m < 5 or rs[-1] is None or rs[-2] is None:
        return 0
    cur = rs[-1]
    window = [v for v in rs[-5:] if v is not None]
    peak, trough = max(window), min(window)
    if peak >= 70 and cur < peak - 5 and cur < rs[-2]:
        return -1
    if trough <= 30 and cur > trough + 5 and cur > rs[-2]:
        return 1
    return 0


def early_reversal(opens, highs, lows, closes, closes_4h=None):
    """早期反转预警：在均线掉头**之前**就嗅出见顶/见底。

    四个领先信号：
      L1 动能衰竭 —— MACD 柱在高位连降 / 低位连升（动能先于价格转向）
      L2 动能背离 —— 价格创新高但 MACD 柱峰走低（不限 RSI 极端区，比 `rsi_divergence` 更早更宽）
      L3 K线拒绝 —— 高位长上影 / 低位长下影（单根就反映多空在此被拒，最领先）
      L4 4h 领先 —— 更小周期 RSI 从超买/超卖回头

    **打分刻意分两类**：结构型(L2/L3)各 1 分；动能型(L1/L4)彼此相关，合并最多算 1 分。
    要求 结构型≥1 且 总分≥2。不这么分的话，「强趋势里的一次普通回调」光靠动能项
    就能凑够门槛——那是最常见的误报来源。

    返回 {top_risk, bottom_risk, score, reasons}。
    """
    empty = {"top_risk": False, "bottom_risk": False, "score": 0, "reasons": []}
    n = len(closes)
    if n < _REV_MIN_CANDLES or len(highs) != n or len(lows) != n or len(opens) != n:
        return empty

    hist = macd_hist_series(closes)
    rs = rsi_series(closes, 14)

    l1_top = l1_bot = l2_top = l2_bot = l3_top = l3_bot = False

    # L1 动能衰竭：柱连降/连升，且价格已在偏强/偏弱区（60/40 比 70/30 宽，才配得上"早"）
    if hist[-1] is not None and hist[-3] is not None and rs[-1] is not None:
        h1, h2, h3 = hist[-1], hist[-2], hist[-3]
        l1_top = h1 > 0 and h1 < h2 < h3 and rs[-1] >= 60
        l1_bot = h1 < 0 and h1 > h2 > h3 and rs[-1] <= 40

    # L2 动能背离：价格摆动点创新高/新低，而对应的 MACD 柱明显走弱
    def _div_ok(p1, p2):
        gap = p2 - p1
        return (p1 >= _REV_WARMUP and p2 >= n - 1 - _REV_RECENT_PIVOT
                and 4 <= gap <= _REV_MAX_GAP)

    if hist[-1] is not None:
        ph = pivot_highs(highs, 3)
        if len(ph) >= 2:
            p1, p2 = ph[-2], ph[-1]
            # 柱峰要明显走低(<85%)才算，否则 epsilon 级的擦边也会被当成背离
            l2_top = (_div_ok(p1, p2) and highs[p2] > highs[p1]
                      and hist[p1] is not None and hist[p2] is not None
                      and hist[p1] > 0 and hist[p2] < hist[p1] * 0.85)
        pl = pivot_lows(lows, 3)
        if len(pl) >= 2:
            p1, p2 = pl[-2], pl[-1]
            l2_bot = (_div_ok(p1, p2) and lows[p2] < lows[p1]
                      and hist[p1] is not None and hist[p2] is not None
                      and hist[p1] < 0 and hist[p2] > hist[p1] * 0.85)

    # L3 K 线拒绝：影线占整根一半以上，且收在中点错误的一侧，且发生在近期高/低位
    rng = highs[-1] - lows[-1]
    if rng > 0:
        upper = highs[-1] - max(opens[-1], closes[-1])
        lower = min(opens[-1], closes[-1]) - lows[-1]
        mid = (highs[-1] + lows[-1]) / 2
        recent_hi = max(highs[max(0, n - 10):])
        recent_lo = min(lows[max(0, n - 10):])
        l3_top = upper >= 0.5 * rng and closes[-1] < mid and highs[-1] >= recent_hi * 0.995
        l3_bot = lower >= 0.5 * rng and closes[-1] > mid and lows[-1] <= recent_lo * 1.005

    # L4 4h 领先
    d4 = _intraday_reversal_dir(closes_4h or [])
    l4_top, l4_bot = d4 == -1, d4 == 1

    def _reasons(pairs):
        return [t for hit, t in pairs if hit]

    top_reasons = _reasons([(l1_top, "红柱连降·动能衰竭"), (l2_top, "顶背离·价高动能低"),
                            (l3_top, "高位长上影·冲高被拒"), (l4_top, "4h RSI 从超买回落")])
    bot_reasons = _reasons([(l1_bot, "绿柱收窄·动能衰竭"), (l2_bot, "底背离·价低动能高"),
                            (l3_bot, "低位长下影·杀跌被接"), (l4_bot, "4h RSI 从超卖回升")])

    top_struct = int(l2_top) + int(l3_top)
    bot_struct = int(l2_bot) + int(l3_bot)
    top_score = top_struct + int(l1_top or l4_top)
    bot_score = bot_struct + int(l1_bot or l4_bot)

    if top_struct >= 1 and top_score >= _REV_CONFLUENCE and top_score >= bot_score:
        return {"top_risk": True, "bottom_risk": False,
                "score": top_score, "reasons": top_reasons}
    if bot_struct >= 1 and bot_score >= _REV_CONFLUENCE:
        return {"top_risk": False, "bottom_risk": True,
                "score": bot_score, "reasons": bot_reasons}
    return empty
