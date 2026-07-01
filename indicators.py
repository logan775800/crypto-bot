"""技术指标计算（纯Python实现）"""

def sma(prices, period):
    """简单移动平均"""
    if len(prices) < period:
        return None
    return sum(prices[-period:]) / period

def rsi(prices, period=14):
    """相对强弱指数 RSI"""
    if len(prices) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(1, len(prices)):
        change = prices[i] - prices[i-1]
        if change >= 0:
            gains.append(change)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(-change)
    # 取最近 period 个
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
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

    # 均线
    ma7 = sma(prices, 7)
    ma30 = sma(prices, 30)
    result["ma7"] = ma7
    result["ma30"] = ma30

    # 均线趋势判断
    if ma7 and ma30:
        if ma7 > ma30:
            result["ma_signal"] = "短期均线在长期之上 📈 (偏多头)"
        else:
            result["ma_signal"] = "短期均线在长期之下 📉 (偏空头)"

    # 价格相对均线
    if ma7:
        if cur > ma7:
            result["price_signal"] = "价格在7日均线之上"
        else:
            result["price_signal"] = "价格在7日均线之下"

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
