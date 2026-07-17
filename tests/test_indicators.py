"""marketdata 指标计算：这些数字直接决定 AI 给的止损距离/结构判断，算错就是给错方案。"""
from handlers import marketdata as md


def test_ema_matches_manual():
    vals = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert md.ema(vals, 20) is None                 # 数据不足返回 None
    e = md.ema(vals, 5)
    # 前5根SMA=3，随后按 k=2/6 递推
    k = 2 / 6
    exp = 3.0
    for v in vals[5:]:
        exp = v * k + exp * (1 - k)
    assert abs(e - exp) < 1e-9


def test_atr_wilder():
    # 每根 H-L 恒为 2，且无跳空 → ATR 必然收敛到 2
    n = 30
    highs = [11] * n
    lows = [9] * n
    closes = [10] * n
    a = md.atr(highs, lows, closes, 14)
    assert abs(a - 2) < 1e-9


def test_rsi_bounds_and_extremes():
    up = list(range(1, 40))                          # 单边涨 → RSI=100
    assert md.rsi(up, 14) == 100.0
    down = list(range(40, 1, -1))                    # 单边跌 → RSI≈0
    assert md.rsi(down, 14) < 1
    assert md.rsi([1, 2], 14) is None                # 数据不足


def _zigzag(points, span=7):
    """把一串交替的转折价插值成K线序列。span=7 保证每个摆动点左右各有≥3根确认
    （pivots 的 k=3 要求），否则识别不出摆动点。"""
    highs, lows = [], []
    for i in range(len(points) - 1):
        a, b = points[i], points[i + 1]
        for j in range(span):
            v = a + (b - a) * j / span
            highs.append(v + 0.5)
            lows.append(v - 0.5)
    highs.append(points[-1] + 0.5)
    lows.append(points[-1] - 0.5)
    return highs, lows


def test_structure_detects_uptrend_hh_hl():
    # 峰递增(20→26→32)、谷递增(14→19) → HH+HL
    highs, lows = _zigzag([10, 20, 14, 26, 19, 32, 25])
    tag, h3, l3 = md.structure(highs, lows)
    assert "上升" in tag, (tag, h3, l3)


def test_structure_detects_downtrend_lh_ll():
    # 峰递减、谷递减 → LH+LL
    highs, lows = _zigzag([32, 25, 30, 19, 24, 13, 18])
    tag, h3, l3 = md.structure(highs, lows)
    assert "下降" in tag, (tag, h3, l3)


def test_norm_symbol():
    assert md.norm("btc") == "BTCUSDT"
    assert md.norm("BTC") == "BTCUSDT"
    assert md.norm("BTCUSDT") == "BTCUSDT"
    assert md.norm("btc-usdt".replace("-", "")) == "BTCUSDT"


def test_f_precision_by_magnitude():
    assert md.f(63123.456) == "63,123"               # 大数不要小数
    assert md.f(1.2345) == "1.23"
    assert "0.0012" in md.f(0.0012345)               # 小币保留有效位
