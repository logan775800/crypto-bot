"""数据新鲜度。

回答「这份数据现在还能不能拿来下单」——「算得对但用不了」类错误的兜底，纯函数，不联网。
（合约规格/爆仓距离那几组测的是 /sizing，v1.70.0 随实盘一起删了。）
"""
import pytest

from handlers import datameta as dm


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
