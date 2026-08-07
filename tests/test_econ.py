"""净盈亏比引擎 —— 「价格距离算出来的 1.8:1，到手可能只有 1.1:1」。

止损距离越窄，成本占比越大；这套测试锁死的正是这个非线性关系，
以及"深度不够会部分成交"这种低流动性小币上最容易吃亏的场景。
"""
import pytest

from handlers import econ


# ── 吃穿盘口 ─────────────────────────────────────────────────────
def _book(*levels):
    return list(levels)


def test_walk_book_single_level_enough():
    avg, filled, exhausted = econ.walk_book(_book((100.0, 10.0)), 500)
    assert avg == pytest.approx(100.0) and filled == 500 and not exhausted


def test_walk_book_eats_multiple_levels():
    """吃穿两档，成交均价必然差于最优价——这就是滑点。"""
    avg, filled, exhausted = econ.walk_book(_book((100.0, 1.0), (101.0, 10.0)), 500)
    assert 100.0 < avg < 101.0 and filled == 500 and not exhausted


def test_walk_book_exhausted_means_partial_fill():
    """整本吃光还不够 = 部分成交，低流动性小币的常态。"""
    avg, filled, exhausted = econ.walk_book(_book((100.0, 1.0), (101.0, 1.0)), 10_000)
    assert exhausted and filled == pytest.approx(201.0)


def test_walk_book_ignores_bad_levels():
    avg, _f, _e = econ.walk_book(_book((0, 5.0), (-1, 5.0), (100.0, 10.0)), 100)
    assert avg == pytest.approx(100.0)


def test_slippage_flags_thin_book():
    s = econ.slippage(_book((100.0, 1.0), (101.0, 1.0)), 50_000, 100.0)
    assert s["partial"] and "部分成交" in s["note"]


def test_slippage_zero_on_deep_book():
    s = econ.slippage(_book((100.0, 10_000.0)), 1_000, 100.0)
    assert s["pct"] == pytest.approx(0.0) and not s["partial"]


def test_slippage_without_book_is_explicit():
    s = econ.slippage([], 1000, 100.0)
    assert s["pct"] is None and "无法估算" in s["note"]


# ── 资金费 ───────────────────────────────────────────────────────
def test_funding_charged_per_started_period():
    """持有 9 小时跨两次结算就要付两期——按周期向上取整，这是最常算漏的。"""
    one = econ.funding_cost(10_000, 0.0001, 8, "long")
    two = econ.funding_cost(10_000, 0.0001, 9, "long")
    assert two == pytest.approx(one * 2)


def test_funding_sign_flips_for_short():
    """费率为正时多头付、空头收。"""
    assert econ.funding_cost(10_000, 0.0001, 8, "long") > 0
    assert econ.funding_cost(10_000, 0.0001, 8, "short") < 0


def test_negative_rate_pays_longs():
    assert econ.funding_cost(10_000, -0.0001, 8, "long") < 0


def test_no_funding_for_intraday_zero_hold():
    assert econ.funding_cost(10_000, 0.001, 0, "long") == 0.0


# ── 净盈亏比 ─────────────────────────────────────────────────────
def _a(**kw):
    base = dict(entry=100.0, stop=99.0, tp=102.0, notional=10_000, side="long")
    base.update(kw)
    return econ.analyze(**base)


def test_gross_rr_is_pure_price_distance():
    a = _a()
    assert a["gross_rr"] == pytest.approx(2.0)


def test_costs_always_reduce_rr():
    a = _a(fee_in=econ.TAKER, fee_out=econ.TAKER)
    assert a["net_rr"] < a["gross_rr"]


def test_tight_stop_is_eaten_much_harder():
    """核心非线性：止损 5% 时成本无所谓，止损 0.3% 时成本就是胜负手。"""
    wide = econ.analyze(100, 95, 110, 10_000, "long")       # 止损 5%
    tight = econ.analyze(100, 99.7, 100.6, 10_000, "long")  # 止损 0.3%
    assert tight["eaten_pct"] > wide["eaten_pct"] * 3


def test_costs_can_flip_a_trade_negative():
    """毛看着能做，净是负的——这种单必须被明确否掉。"""
    a = econ.analyze(100, 99.9, 100.15, 50_000, "long",
                     slip_in_pct=0.3, slip_out_pct=0.3)
    assert a["net_win"] < 0
    assert "不该做" in econ.verdict(a)


def test_verdict_thresholds():
    assert "✅" in econ.verdict(econ.analyze(100, 99, 103, 1000, "long"))     # 净 2.7:1
    assert "⚠️" in econ.verdict(econ.analyze(100, 99, 101.6, 1000, "long"))   # 净 1.34:1


def test_gross_above_one_can_be_net_below_one():
    """止损 1% / 止盈 1.2% 的短线单：毛 1.2:1 看着能做，
    光两次吃单手续费就把净值压到 0.98:1——这类单做一辈子也是慢性亏损。"""
    a = econ.analyze(100, 99, 101.2, 1000, "long")
    assert a["gross_rr"] == pytest.approx(1.2)
    assert a["net_rr"] < 1.0
    assert "❌" in econ.verdict(a)


def test_breakeven_is_total_cost_over_notional():
    a = _a(slip_in_pct=0.1, slip_out_pct=0.1)
    assert a["breakeven_pct"] > 0.2      # 手续费两次 + 滑点两次


def test_funding_included_in_overnight_trade():
    flat = _a(funding_rate=0.0, hold_hours=24)
    paid = _a(funding_rate=0.0005, hold_hours=24)      # 3 期
    assert paid["net_win"] < flat["net_win"]
    assert paid["funding"] == pytest.approx(10_000 * 0.0005 * 3)


def test_short_collects_positive_funding():
    a = _a(side="short", stop=101.0, tp=98.0, funding_rate=0.0005, hold_hours=24)
    assert a["funding"] < 0            # 负成本=收钱
    assert a["net_win"] > a["gross_win"] - a["fee_open"] - a["fee_close"]


def test_invalid_inputs_return_none():
    assert econ.analyze(0, 99, 102, 1000, "long") is None
    assert econ.analyze(100, 100, 102, 1000, "long") is None


def test_render_shows_both_rr():
    txt = econ.render(_a())
    assert "毛盈亏比" in txt and "净盈亏比" in txt and "回本需走" in txt
