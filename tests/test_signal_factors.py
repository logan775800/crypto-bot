"""信号因子：方向指标 DI、背离、量能中枢 POC、早期反转预警。

这几个是从 `D:\\Scripts\\bit` 那个 Go 机器人移植过来的——它的信号引擎比这边
细得多，而其中一条是**真 bug**：

    ADX 只测「趋势有多强」，不带方向。一段猛跌里的逆势反抽同样会把 ADX 顶到
    80 以上，而 MA/MACD/RSI 在反抽的那几根上全都翻多。只看 ADX 不看 DI，
    就会把**最容易被套的那种反抽**评成「买入·强」。

`test_counter_trend_bounce_is_not_a_bull_signal` 就是钉这条的。
"""
import random

import pytest

import indicators as I


# ── 造数据 ────────────────────────────────────────────────────
def _hl(closes, pct=0.01):
    """由收盘价造出高低价（上下各 pct）。"""
    return [c * (1 + pct) for c in closes], [c * (1 - pct) for c in closes]


def _leg(c, n, rate, wiggle=0.004):
    """接一段行情。wiggle 让相邻根有小幅摆动——**全平的一段会让 RSI 变成 100**
    （涨跌都为 0 时 `rsi()` 返回 100，这是既有口径），那会造出一片高原，
    而高原不构成峰，测背离就永远测不到。"""
    for i in range(n):
        c.append(c[-1] * (rate + (wiggle if i % 2 else -wiggle)))
    return c


def _random_ohlc(seed, n=90):
    """随机游走的**真实 OHLC**：影线独立于收盘价。

    别用「high = close×1.015 / low = close×0.985」这种对称固定带——
    那样 (high+low)/2 恒等于 close，「收在中点下方」这个条件在数学上
    永远为假，长影拒绝(L3)一辈子测不到，测试会假绿。
    """
    random.seed(seed)
    opens, highs, lows, closes = [], [], [], []
    px = 100.0
    for _ in range(n):
        o = px
        c = o * (1 + random.uniform(-0.04, 0.045))
        opens.append(o)
        highs.append(max(o, c) * (1 + random.uniform(0, 0.03)))
        lows.append(min(o, c) * (1 - random.uniform(0, 0.03)))
        closes.append(c)
        px = c
    return opens, highs, lows, closes


# ── DMI：adx() 只是它的薄封装，重构不能改变老调用点的结果 ──────────
def test_adx_is_exactly_the_dmi_value():
    """`adx()` 改成调 `dmi()` 之后，值必须一模一样——否则所有老调用点的
    「趋势强度」判断会静默漂移，而没人会发现。"""
    random.seed(3)
    closes = [100.0]
    for _ in range(80):
        closes.append(closes[-1] * (1 + random.uniform(-0.03, 0.035)))
    highs, lows = _hl(closes)
    assert I.adx(highs, lows, closes) == I.dmi(highs, lows, closes)["adx"]


def test_dmi_returns_none_when_data_is_short():
    assert I.dmi([1, 2], [1, 2], [1, 2]) is None
    assert I.adx([1, 2], [1, 2], [1, 2]) is None


def test_clean_uptrend_is_bullish_by_direction():
    closes = _leg([100.0], 66, 1.03)
    highs, lows = _hl(closes)
    d = I.dmi(highs, lows, closes)
    assert d["pdi"] > d["mdi"], "干净上涨里 +DI 必须压过 -DI"


def test_clean_downtrend_is_bearish_by_direction():
    closes = _leg([100.0], 66, 0.97)
    highs, lows = _hl(closes)
    d = I.dmi(highs, lows, closes)
    assert d["mdi"] > d["pdi"]


def test_counter_trend_bounce_is_not_a_bull_signal():
    """这条是移植 DI 的**全部理由**，改判据前先读它。

    形态：连跌 60 根之后急反抽 6 根。ADX 会非常高（跌得太猛），
    而 MA/MACD/RSI 在反抽这几根上全部翻多——只看 ADX 的话，
    结论就是「趋势很强 + 多因子共振」＝买入·强。实际上这是下跌中继。
    """
    closes = [100.0]
    for _ in range(60):
        closes.append(closes[-1] * 0.97)   # 猛跌
    for _ in range(6):
        closes.append(closes[-1] * 1.05)   # 急反抽
    highs, lows = _hl(closes)
    d = I.dmi(highs, lows, closes)

    assert d["adx"] >= 25, "前提：ADX 确实很高，所以光看 ADX 会误判"
    assert d["mdi"] > d["pdi"], "方向仍然是空头——这正是 ADX 看不出来的那一半"


# ── RSI 序列 ──────────────────────────────────────────────────
def test_rsi_series_last_value_matches_the_scalar_rsi():
    """序列版和标量版必须同口径，否则「屏幕上的 RSI」和「判背离用的 RSI」是两个数。"""
    random.seed(11)
    closes = [100.0]
    for _ in range(60):
        closes.append(closes[-1] * (1 + random.uniform(-0.02, 0.02)))
    assert I.rsi_series(closes)[-1] == pytest.approx(I.rsi(closes))


def test_rsi_series_is_aligned_and_padded():
    closes = [100.0 + i for i in range(30)]
    rs = I.rsi_series(closes, 14)
    assert len(rs) == len(closes)
    assert all(v is None for v in rs[:14]), "前 period 根还没有值，必须是 None 不是 0"
    assert rs[14] is not None


def test_rsi_series_too_short_is_all_none():
    assert I.rsi_series([1, 2, 3], 14) == [None] * 3


# ── 摆动点 ────────────────────────────────────────────────────
def test_pivot_needs_confirmation_on_both_sides():
    """最后 k 根**永远**不会被认成峰——峰要靠右侧 k 根确认。
    这不是缺陷，是「峰」这个概念自带的确认成本；不理解这条会以为检测漏了。"""
    vals = [1, 2, 3, 9, 3, 2, 1, 5, 9]      # 末尾那个 9 没有右侧确认
    assert I.pivot_highs(vals, 2) == [3]


def test_pivot_lows_mirror():
    vals = [9, 8, 7, 1, 7, 8, 9]
    assert I.pivot_lows(vals, 2) == [3]


def test_pivot_skips_none_padding():
    """RSI 序列前面是 None，不能被当成「更低」而误判出峰。"""
    vals = [None, None, 5, 9, 5, None, None]
    assert I.pivot_highs(vals, 2) == []


# ── 背离 ──────────────────────────────────────────────────────
def _two_peak_top_divergence():
    """两波冲高：第二波价格更高、但斜率更缓（动能更弱）→ 顶背离。"""
    c = [100.0]
    _leg(c, 22, 1.001)
    _leg(c, 11, 1.055)   # 第一波
    _leg(c, 11, 0.982)
    _leg(c, 13, 1.030)   # 第二波：更高但更缓
    _leg(c, 7, 0.992)    # 回落，让第二个峰能被确认
    return c


def test_top_divergence_is_detected():
    c = _two_peak_top_divergence()
    out = I.rsi_divergence(c)
    assert out["bearish"] is True
    assert "顶背离" in out["text"]

    rs = I.rsi_series(c)
    ph = I.pivot_highs(rs, 2)
    assert rs[ph[-1]] < rs[ph[-2]], "前提：RSI 后峰更低"
    assert c[ph[-1]] > c[ph[-2]], "前提：价格后峰更高"


def test_divergence_only_ever_fires_in_the_extreme_zone():
    """30~70 中间区的「背离」绝大多数是噪音。不卡这条会天天报背离，
    而天天报的东西等于没报——这是这个判据能不能用的关键。

    这里直接验规则本身：跑一批随机行情，凡是报了背离的，
    两个峰/谷**必须**都落在超买(≥70)/超卖(≤30)区。
    这比手工造一个反例结实——造不出反例不等于规则成立。
    """
    fired = 0
    for seed in range(400):
        random.seed(seed)
        c = [100.0]
        for _ in range(90):
            c.append(c[-1] * (1 + random.uniform(-0.045, 0.05)))
        d = I.rsi_divergence(c)
        if not (d["bearish"] or d["bullish"]):
            continue
        fired += 1
        rs = I.rsi_series(c)
        pv = I.pivot_highs(rs, 2) if d["bearish"] else I.pivot_lows(rs, 2)
        p1, p2 = pv[-2], pv[-1]
        if d["bearish"]:
            assert rs[p1] >= 70 and rs[p2] >= 70, f"seed={seed} 中间区也报了顶背离"
        else:
            assert rs[p1] <= 30 and rs[p2] <= 30, f"seed={seed} 中间区也报了底背离"
    assert fired > 0, "400 组随机行情一次背离都没报，判据可能过严到没用"


def test_divergence_needs_enough_history():
    assert I.rsi_divergence([100.0] * 10) == {"bearish": False, "bullish": False, "text": ""}


# ── 量能中枢 POC ──────────────────────────────────────────────
def test_poc_lands_on_the_heaviest_price_band():
    """量堆在哪个价位，POC 就该落在哪儿。"""
    highs = [101.0] * 10 + [151.0] * 3
    lows = [99.0] * 10 + [149.0] * 3
    vols = [1000.0] * 10 + [10.0] * 3     # 量几乎都在 100 附近
    poc = I.volume_poc(highs, lows, vols)
    assert 99 <= poc <= 101


def test_poc_spreads_volume_across_the_bar_range():
    """一根从 100 涨到 110 的 K 线，成交是分布在整段上的，不能只记收盘那个桶。"""
    poc = I.volume_poc([110.0], [100.0], [500.0])
    assert poc is None, "只有一根时不足以谈分布"
    poc = I.volume_poc([110.0, 110.0], [100.0, 100.0], [500.0, 500.0])
    assert 100 <= poc <= 110


def test_poc_without_volume_is_none():
    assert I.volume_poc([2.0, 3.0], [1.0, 2.0], [0.0, 0.0]) is None


def test_poc_flat_price_is_none():
    assert I.volume_poc([5.0, 5.0], [5.0, 5.0], [10.0, 10.0]) is None


# ── 早期反转预警 ──────────────────────────────────────────────
def test_early_reversal_needs_enough_candles():
    assert I.early_reversal([1.0] * 10, [1.0] * 10, [1.0] * 10, [1.0] * 10)["top_risk"] is False


def test_momentum_alone_never_triggers_a_reversal_warning():
    """**这条是这个判据能不能用的关键。**

    打分刻意分两类：结构型（背离/长影拒绝，证据独立）与动能型（MACD 柱衰竭 /
    4h RSI 回落，彼此高度相关）。动能型合并最多算 1 分，而门槛是 2 分且
    结构型必须 ≥1——所以**光靠动能永远凑不够**。

    不这么分的话，「强趋势里的一次普通回调」就能报反转，那是最常见的误报来源。
    这里用一批随机行情做性质检查：凡是报了反转的，原因里必须至少有一条结构型。
    """
    structural = ("背离", "影")
    fired = 0
    for seed in range(60):
        out = I.early_reversal(*_random_ohlc(seed))
        if out["top_risk"] or out["bottom_risk"]:
            fired += 1
            assert out["score"] >= 2
            assert any(any(s in r for s in structural) for r in out["reasons"]), \
                f"seed={seed} 只靠动能就报了反转：{out['reasons']}"
    assert fired > 0, "60 组随机行情一次都没报，判据可能过严到没用"


def test_early_reversal_reports_why():
    """报了就要说清是哪几条共振——只给一个「有反转风险」没法判断可信度。"""
    for seed in range(80):
        out = I.early_reversal(*_random_ohlc(seed))
        if out["top_risk"] or out["bottom_risk"]:
            assert out["reasons"] and all(isinstance(r, str) for r in out["reasons"])
            assert not (out["top_risk"] and out["bottom_risk"]), "不能同时见顶又见底"
            return
    pytest.skip("这批随机行情没触发反转预警")


def test_four_hour_rsi_rollover_is_detected():
    """4h 从超买回落——它比日线领先约一天（日线一天一根，4h 一天六根）。"""
    c4 = [100.0]
    for _ in range(40):
        c4.append(c4[-1] * 1.03)     # 拉到超买
    for _ in range(4):
        c4.append(c4[-1] * 0.97)     # 回落
    assert I._intraday_reversal_dir(c4) == -1


def test_four_hour_needs_enough_bars():
    assert I._intraday_reversal_dir([100.0] * 10) == 0
