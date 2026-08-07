"""数据新鲜度 + 合约规格约束。

前者回答「这份数据现在还能不能拿来下单」，后者回答「这个仓位交易所收不收」。
两个都是「算得对但用不了」类错误的兜底，纯函数，不联网。
"""
import pytest

from handlers import datameta as dm
from handlers import sizing as sz


# ── 新鲜度 ───────────────────────────────────────────────────────
def _rep(delay_s=None, srcs=None):
    r = dm.Report("BANK")
    base = 1_700_000_000_000
    r.server_ms = base
    if delay_s is not None:
        r.local_ms = base + int(delay_s * 1000)
    if srcs:
        r.src_ms = srcs
    return r


def test_delay_measured_against_local_clock():
    assert _rep(delay_s=3).delay_s == pytest.approx(3.0)


def test_negative_delay_clamped_to_zero():
    """交易所时钟略快于本机时不该显示负延迟。"""
    r = _rep()
    r.local_ms = r.server_ms - 5000
    assert r.delay_s == 0.0


def test_fresh_data_allows_immediate_orders():
    assert _rep(delay_s=3).realtime_ok


def test_stale_data_blocks_immediate_orders():
    """超过阈值只能给观察方案——永续在 30 秒里能走完一个止损距离。"""
    r = _rep(delay_s=dm.FRESH_WARN + 5)
    assert not r.realtime_ok
    txt = r.for_ai()
    assert "只能" in txt and "观察方案" in txt


def test_unknown_delay_does_not_block():
    """拿不到本机时间时不该无谓降级（probe 一定会填，未知只在手工构造时出现）。"""
    assert _rep().realtime_ok


def test_source_skew_detected():
    """不同源的时间差太大 = 它们不是同一时刻的快照，跨源对比要降置信度。"""
    base = 1_700_000_000_000
    r = _rep(delay_s=2, srcs={"Bybit-K线15m": base, "Bybit-盘口": base - 20_000})
    assert r.skew_s == pytest.approx(20.0)
    assert "不是同一时刻" in r.freshness_line()
    assert "降低置信度" in r.for_ai()


def test_small_skew_is_silent():
    base = 1_700_000_000_000
    r = _rep(delay_s=2, srcs={"a": base, "b": base - 2000})
    assert "不是同一时刻" not in r.freshness_line()


def test_spike_forbids_using_it_as_a_level():
    """插针价位没有真实成交承接，止损挂在那儿等于白送。"""
    r = _rep(delay_s=2)
    r.spike = "最近5小时内出现插针：单根5m振幅 12.0%（常态 0.30%）"
    assert not r.healthy
    txt = r.for_ai()
    assert "插针" in txt and "不要" in txt


# ── 合约规格 ─────────────────────────────────────────────────────
def _plan(entry=0.081, stop=0.0828, equity=10000, risk=0.5):
    return sz.plan_size(equity, entry, stop, risk)


def test_qty_rounded_down_to_step():
    """向下取整到步长——宁可少开，也不能因为凑整超了计划风险。"""
    s = _plan()
    out = sz.apply_spec(s, {"qty_step": 100, "min_qty": 1, "max_lev": 50, "multiplier": 1})
    assert out["qty"] % 100 == 0
    assert out["qty"] <= out["qty_raw"]


def test_multiplier_converts_coins_to_contracts():
    """1000PEPE：1 张 = 1000 枚，不折算会多开三个数量级。"""
    s = _plan(entry=0.0100, stop=0.0095)
    out = sz.apply_spec(s, {"qty_step": 0, "min_qty": 0, "max_lev": 25, "multiplier": 1000})
    assert out["qty"] == pytest.approx(s["qty"] / 1000)
    assert any("面值" in n for n in out["spec_notes"])


def test_below_min_order_qty_is_flagged():
    """算出来下不进去，必须明说，而不是给个下不了的数字。"""
    s = _plan(equity=50)
    out = sz.apply_spec(s, {"qty_step": 0, "min_qty": 10 ** 9, "max_lev": 25, "multiplier": 1})
    assert out.get("too_small")
    assert any("最小下单量" in n for n in out["spec_notes"])


def test_leverage_above_contract_cap_is_dropped():
    s = _plan()
    out = sz.apply_spec(s, {"qty_step": 0, "min_qty": 0, "max_lev": 5, "multiplier": 1})
    assert max(out["margins"]) <= 5
    assert any("最大杠杆" in n for n in out["spec_notes"])


def test_apply_spec_without_spec_is_a_noop():
    s = _plan()
    assert sz.apply_spec(s, None) is s


# ── 爆仓距离 ─────────────────────────────────────────────────────
def test_liq_distance_long_below_entry():
    liq, pct = sz.liq_distance(100, 10, "long")
    assert liq < 100 and pct == pytest.approx(9.5, abs=0.1)


def test_liq_distance_short_above_entry():
    liq, pct = sz.liq_distance(100, 10, "short")
    assert liq > 100


def test_high_leverage_puts_liq_inside_stop():
    """20x 下爆仓距离约 4.5%，止损放 6% 的话先爆仓再止损——必须能被看出来。"""
    _liq, pct = sz.liq_distance(100, 20, "long")
    assert pct < 6.0


def test_build_text_warns_when_liq_closer_than_stop():
    s = sz.plan_size(10000, 100, 94, 0.5)      # 止损距离 6%
    txt = sz.build_text(s)
    assert "比止损还近" in txt or "插针即爆" in txt
