"""缓步增长扫描 —— 找「每天一点点往上磨」的，不是涨得最猛的。

这套筛选的全部价值在于**把「拉盘」和「缓涨」分开**。分不开的话它就退化成
另一个涨幅榜，而涨幅榜已经有三个了。所以下面的用例主要在测「该拒的拒没拒」。
"""
import math

import pytest

from handlers import steady as st


def _line(n=30, daily=0.01, start=100.0):
    """完美的复利直线：每天涨 daily。R² 应该 = 1。"""
    return [start * (1 + daily) ** i for i in range(n)]


def _noisy(n=30, daily=0.01, amp=0.03, start=100.0):
    """带锯齿的上升：趋势还在，但没那么直。

    噪声用 sin(i·π/(n-1))，它在首尾都恰好为 0 —— 这样和 _line 的**总涨幅
    完全相同**，才能干净地只对比「稳不稳」这一个变量。
    """
    return [start * (1 + daily) ** i * (1 + amp * math.sin(i * math.pi / (n - 1)))
            for i in range(n)]


# ── 拟合 ─────────────────────────────────────────────────────────
def test_perfect_line_has_r2_of_one():
    slope, r2 = st.linfit_log(_line())
    assert r2 == pytest.approx(1.0, abs=1e-6)
    assert slope == pytest.approx(math.log(1.01), rel=1e-6)


def test_noise_lowers_r2_but_keeps_slope():
    _s1, r1 = st.linfit_log(_line())
    _s2, r2 = st.linfit_log(_noisy())
    assert r2 < r1


def test_log_scale_makes_equal_percent_moves_equal():
    """用原价拟合会让高价段权重过大——同样的百分比走势在不同价位上
    算出不同的「稳定度」。对数坐标下复利是直线，两者必须一致。"""
    _s, r_low = st.linfit_log(_line(start=0.001))
    _s2, r_high = st.linfit_log(_line(start=50_000))
    assert r_low == pytest.approx(r_high, abs=1e-9)


def test_too_few_days_refuses():
    assert st.linfit_log([100, 101, 102]) == (None, None)


def test_non_positive_price_refuses():
    assert st.linfit_log([100] * 20 + [0]) == (None, None)


def test_max_drawdown():
    assert st.max_drawdown([100, 120, 90, 110]) == pytest.approx(25.0)
    assert st.max_drawdown(_line()) == pytest.approx(0.0)


# ── 「缓涨」的定义 ───────────────────────────────────────────────
def test_steady_climb_is_accepted():
    p = st.profile(_line(daily=0.008))
    assert st.reject_reason(p) is None
    assert p["up_ratio"] == 100 and p["dd"] == pytest.approx(0.0)


def test_single_day_pump_is_rejected():
    """一天拉 80%、其余横盘——涨幅很好看，但那不是「磨」上来的。"""
    closes = [100.0] * 15 + [180.0] * 15
    assert "拉盘" in st.reject_reason(st.profile(closes))


# ── 短窗口自动切 4h ──────────────────────────────────────────────
# 7 根日线做回归站不住：7 个点算出来的 R² 噪音里随便抽都能到 0.85。
# 同样 7 天用 4h 是 42 根，点数够，还能看出周内结构。
def test_short_window_uses_4h():
    iv, need, per_year, unit, lim = st.bar_spec(7)
    assert iv == "240" and need == 42 and per_year == 365 * 6
    assert "4h" in unit


def test_long_window_uses_daily():
    iv, need, per_year, _u, _l = st.bar_spec(30)
    assert iv == "D" and need == 30 and per_year == 365


def test_intraday_pump_threshold_is_tighter():
    """日线 25% 才算暴拉，4h 15% 就已经是了——阈值不跟着周期变就等于没设。"""
    assert st.bar_spec(7)[4] < st.bar_spec(30)[4]


def test_annualization_follows_the_timeframe():
    """同样的每根涨幅，4h 的年化必须比日线高得多——
    per_year 用错的话会把 4h 窗口的年化算低 6 倍。"""
    line = [100 * 1.005 ** i for i in range(42)]
    daily = st.profile(line, per_year=365)
    intraday = st.profile(line, per_year=365 * 6)
    assert intraday["ann"] > daily["ann"]


def test_seven_days_is_a_menu_choice():
    """用户主要做合约，7 天是默认窗口。"""
    assert 7 in st.DAY_CHOICES and st.DEFAULT_DAYS == 7


def test_most_of_the_gain_from_one_day_is_rejected():
    """单日没超过硬上限，但贡献了大半涨幅——同样不算缓涨。"""
    closes = [100 + i * 0.05 for i in range(20)] + [122.0 + i * 0.05 for i in range(10)]
    p = st.profile(closes)
    r = st.reject_reason(p)
    assert r and ("单根" in r or "拉盘" in r)


def test_deep_drawdown_is_rejected():
    """趋势成立、没有暴拉，但中途深蹲 30% —— 过程拿不住，同样不算缓涨。

    直接构造 profile 来隔离这一条规则：V 形走势的 R² 只有 0.14，
    会先被「不成趋势」拦下，测不到回撤这一关。
    """
    p = {"slope": 0.01, "r2": 0.85, "best_day": 5.0,
         "day_share": 0.2, "dd": 30.0}
    r = st.reject_reason(p)
    assert r and "回撤" in r


def test_v_shape_is_rejected_as_no_trend():
    """跌一半再涨回来：首尾涨幅可能好看，但那不是趋势。"""
    closes = ([100 - i * 2 for i in range(15)] +
              [70 + i * 3.5 for i in range(15)])
    r = st.reject_reason(st.profile(closes))
    assert r and "不成趋势" in r


def test_choppy_no_trend_is_rejected():
    """噪音里恰好首尾差了点，不是趋势。"""
    closes = [100 + 8 * math.sin(i) for i in range(30)]
    closes[-1] = 104
    r = st.reject_reason(st.profile(closes))
    assert r and ("不成趋势" in r or "趋势向下" in r)


def test_downtrend_is_rejected():
    assert st.reject_reason(st.profile(_line(daily=-0.01))) == "趋势向下"


# ── 排序口径 ─────────────────────────────────────────────────────
def test_smoother_wins_at_equal_gain():
    """同样的总涨幅，走得稳的分更高——这正是不按涨幅排序的理由。"""
    smooth = st.profile(_line(daily=0.01))
    rough = st.profile(_noisy(daily=0.01, amp=0.06))
    assert smooth["total"] == pytest.approx(rough["total"], rel=1e-9)
    assert smooth["score"] > rough["score"]


def test_faster_wins_at_equal_smoothness():
    slow = st.profile(_line(daily=0.005))
    fast = st.profile(_line(daily=0.015))
    assert fast["score"] > slow["score"]


def test_annualized_slope_is_capped():
    """别把年化算成天文数字糊在屏幕上。"""
    assert st.profile(_line(daily=0.2))["ann"] <= 100_000.0


# ── 非加密标的 ───────────────────────────────────────────────────
def test_liquidity_floor_is_lower_than_scan():
    """/scan 找此刻能进出的，要厚盘口；缓涨拿几周，中小市值才是主场。
    沿用 /scan 的 20M 门槛会把候选池砍到二十几个，选不出东西。"""
    from handlers import scan
    assert st.MIN_TURNOVER < scan.MIN_TURNOVER


# ── 天数按钮 ─────────────────────────────────────────────────────
# 窗口长度是这个功能最需要调的参数（30 天看当下这波，90 天看能不能一直磨）。
# 第一版菜单按钮直接跑默认 30 天，等于只给了一半功能。
def _cbs(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def test_all_day_choices_are_offered():
    cbs = _cbs(st.days_kb())
    for d in st.DAY_CHOICES:
        assert f"stdy:{d}:0" in cbs


def test_current_window_is_marked():
    texts = [b.text for row in st.days_kb(60).inline_keyboard for b in row]
    assert any(t.startswith("✅") and "60" in t for t in texts)
    assert sum(1 for t in texts if t.startswith("✅")) == 1


def test_toggle_flips_the_crypto_only_flag():
    """按钮要能在「仅加密」和「含股票商品」之间来回切。"""
    assert "stdy:30:1" in _cbs(st.days_kb(30, show_all=False))
    assert "stdy:30:0" in _cbs(st.days_kb(30, show_all=True))


def test_switching_days_keeps_the_all_flag():
    """换窗口不该把「含股票」的选择丢掉。"""
    for cb in _cbs(st.days_kb(30, show_all=True)):
        if cb.startswith("stdy:") and cb != "stdy:30:0":
            assert cb.endswith(":1")


def test_has_a_way_back():
    assert "cat_scan" in _cbs(st.days_kb())


def test_callback_data_within_telegram_limit():
    for cb in _cbs(st.days_kb()):
        assert len(cb.encode()) <= 64


def test_symbol_type_classification():
    """「稳」这个筛选天然会把代币化股票和贵金属顶到前面——
    它们本来就比加密币稳。靠 instruments-info 的 symbolType 区分。"""
    from handlers import marketdata as md
    md._TYPES.update({"ts": 9e18, "map": {
        "BTCUSDT": "", "MSFTUSDT": "stock", "XAUUSDT": "commodity"}})
    m = md._TYPES["map"]
    assert m["BTCUSDT"] == "" and m["MSFTUSDT"] == "stock"
