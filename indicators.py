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


def adx(highs, lows, closes, period=14):
    """平均趋向指数 ADX：衡量趋势强度（不分方向）。
    <20 无趋势/震荡，20-25 趋势萌芽，>25 趋势明确，>40 趋势强。数据不足返回 None。"""
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
    for i in range(len(atr)):
        if atr[i] == 0:
            continue
        pdi = 100 * pdm[i] / atr[i]
        mdi = 100 * mdm[i] / atr[i]
        denom = pdi + mdi
        dxs.append(100 * abs(pdi - mdi) / denom if denom else 0.0)
    if len(dxs) < period:
        return sum(dxs) / len(dxs) if dxs else None
    adx_val = sum(dxs[:period]) / period
    for dx in dxs[period:]:
        adx_val = (adx_val * (period - 1) + dx) / period
    return adx_val
