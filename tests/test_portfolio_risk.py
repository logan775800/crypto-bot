"""组合风险聚合 + 减仓推演。

核心立场：在加密永续上把同向仓位的相关性当 0 是最贵的乐观——
BTC 一破位，五个山寨多单是一起走的，「每笔只冒 0.5%」加起来就是 2.5%。
另一条：没设止损的仓，风险不是 0，是到爆仓为止；记 0 等于把最危险的仓藏起来。
"""
import pytest

from handlers import sizing as sz


def _pos(symbol="BANKUSDT", side="Buy", entry=100.0, value=1000.0,
         sl=None, liq=None, upnl=0.0):
    return {"symbol": symbol, "side": side, "avgPrice": str(entry),
            "positionValue": str(value), "unrealisedPnl": str(upnl),
            "stopLoss": ("" if sl is None else str(sl)),
            "liqPrice": ("" if liq is None else str(liq))}


# ── 单仓风险 ─────────────────────────────────────────────────────
def test_risk_from_stop_loss():
    r = sz.portfolio_risk([_pos(entry=100, value=1000, sl=95)], 10_000)
    assert r["risk_long"] == pytest.approx(50.0)      # 1000 × 5%
    assert not r["naked"]


def test_naked_position_uses_liq_price():
    """没止损的仓，风险按到爆仓为止算——不是 0。"""
    r = sz.portfolio_risk([_pos(entry=100, value=1000, liq=80)], 10_000)
    assert r["risk_long"] == pytest.approx(200.0)
    assert r["naked"] == ["BANKUSDT"]


def test_naked_without_liq_counts_whole_position():
    """连爆仓价都没有(全仓)，只能按整笔计——绝不能记 0。"""
    r = sz.portfolio_risk([_pos(entry=100, value=1000)], 10_000)
    assert r["risk_long"] == pytest.approx(1000.0)


# ── 同向聚合 ─────────────────────────────────────────────────────
def test_same_side_risks_add_up():
    """五个「只冒 0.5%」的山寨多单 = 一次 2.5% 的回撤，这才是真实敞口。"""
    poss = [_pos(symbol=f"A{i}USDT", entry=100, value=1000, sl=95) for i in range(5)]
    r = sz.portfolio_risk(poss, 10_000)
    assert r["worst_pct"] == pytest.approx(2.5)


def test_opposite_sides_do_not_offset():
    """多空不该互相抵消——它们不会同时止损，最坏情况取较大的那边。"""
    r = sz.portfolio_risk([
        _pos(symbol="AUSDT", side="Buy", entry=100, value=2000, sl=95),
        _pos(symbol="BUSDT", side="Sell", entry=100, value=1000, sl=105),
    ], 10_000)
    assert r["risk_long"] == pytest.approx(100.0)
    assert r["risk_short"] == pytest.approx(50.0)
    assert r["worst"] == pytest.approx(100.0)         # 取大的，不是相减


def test_new_plan_adds_to_its_own_side():
    base = [_pos(entry=100, value=1000, sl=95)]
    r = sz.portfolio_risk(base, 10_000, new_plan={"side": "long", "risk_usdt": 50})
    assert r["risk_long"] == pytest.approx(100.0)


def test_new_plan_on_other_side_does_not_inflate_worst():
    base = [_pos(entry=100, value=1000, sl=95)]
    r = sz.portfolio_risk(base, 10_000, new_plan={"side": "short", "risk_usdt": 20})
    assert r["worst"] == pytest.approx(50.0)


def test_zero_equity_is_handled():
    assert sz.portfolio_risk([_pos()], 0)["worst"] == 0.0


def test_garbage_positions_skipped():
    bad = {"symbol": "X", "side": "Buy", "avgPrice": "abc", "positionValue": "x"}
    r = sz.portfolio_risk([bad, _pos(entry=100, value=1000, sl=95)], 10_000)
    assert len(r["positions"]) == 1


# ── 减仓推演 ─────────────────────────────────────────────────────
def test_reduce_halves_risk_and_value():
    rows = sz.reduce_scenarios(_pos(entry=100, value=1000, sl=95, upnl=80), 10_000)
    half = [r for r in rows if r["pct"] == 50][0]
    assert half["left_value"] == pytest.approx(500)
    assert half["left_risk"] == pytest.approx(25)      # 原 50 的一半
    assert half["realized"] == pytest.approx(40)       # 浮盈按比例落袋


def test_full_close_zeroes_everything():
    rows = sz.reduce_scenarios(_pos(entry=100, value=1000, sl=95, upnl=80), 10_000)
    full = [r for r in rows if r["pct"] == 100][0]
    assert full["left_value"] == 0 and full["left_risk"] == 0
    assert full["realized"] == pytest.approx(80)


def test_reduce_uses_liq_when_no_stop():
    rows = sz.reduce_scenarios(_pos(entry=100, value=1000, liq=80), 10_000)
    half = [r for r in rows if r["pct"] == 50][0]
    assert half["left_risk"] == pytest.approx(100)     # (1000×20%)/2


def test_reduce_reports_risk_as_pct_of_equity():
    rows = sz.reduce_scenarios(_pos(entry=100, value=1000, sl=95), 10_000)
    assert rows[0]["left_risk_pct"] == pytest.approx(0.375)   # 减25%后 37.5U / 1万


def test_reduce_on_garbage_returns_empty():
    assert sz.reduce_scenarios({"avgPrice": "x"}, 10_000) == []
